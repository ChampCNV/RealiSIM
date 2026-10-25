"""Save each simulation as a complete package: the scene plus everything that defines it.

    python export_sims.py                              # sims/tree_pick/, sims/can_pick/, sims/two_arms/
    python -m mujoco.viewer --mjcf sims/tree_pick/scene.mjb
    python export_sims.py --load sims/tree_pick        # put a package back into this project
    python export_sims.py --load sims/two_arms --calibration   # ... including the robot/camera calibration

A package (sims/<name>/):
  scene.mjb       the scene in one self-contained MuJoCo file (arm(s), object, tags, cameras, meshes, textures),
                  opening in its start pose
  params.json     every parameter: arm model(s) and where they stand, camera calibration, tag IDs / glue roll /
                  height / printed size, the object's size, mass, friction, and a trained model's settings
                  (start pose, placement area, cameras, ...)
  assets/<obj>/   the object asset (meshes, texture, look poses)
  model/          the trained model (actor_last.pt, actor_best.pt, training log), for pick sims
  calibration/    the arms' calibration files, for the two-arm sim
Training builds the scene fresh every episode (random placement, lighting, cameras); a trained model drives it
with sim2real/view_tag_env.py <package>/model/actor_last.pt.
--load copies the asset (if missing), the object's objects.json entry and the model (runs/<package name>) into
this project; --calibration also restores the camera and arm calibration (robot/config.json is backed up first).
"""
import argparse
import json
import shutil
import sys
import time
from pathlib import Path

import mujoco
import numpy as np
import torch

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT / "sim2real"))
sys.path.insert(0, str(ROOT / "robot"))
from tag_env import DEFAULT_AREA, TagLiftEnv, from_ruler  # noqa: E402

OUT = ROOT / "sims"
CONFIG = ROOT / "robot" / "config.json"
PICK_SIMS = {"tree_pick": "runs/tag_envcam_2250k", "can_pick": "runs/tag_celsius_can_celsius_can_backup_2000k"}
MODEL_FILES = ("actor_last.pt", "actor_best.pt", "log.csv")


def jsonable(x):
    if isinstance(x, dict):
        return {k: jsonable(v) for k, v in x.items()}
    if isinstance(x, (list, tuple)):
        return [jsonable(v) for v in x]
    if isinstance(x, np.ndarray):
        return x.tolist()
    if isinstance(x, (np.floating, np.integer)):
        return x.item()
    return x


def object_params(name):
    """Everything about an object: objects.json entry (tags, glue roll, tag height, sources) + its asset's
    properties (size, mass, friction, collision)."""
    objects = json.loads((ROOT / "objects.json").read_text())
    return dict(name=name, objects_json=objects.get(name, {}),
                properties=json.loads((ROOT / "assets" / name / "properties.json").read_text()))


def write_package(name, model, qpos, params, copies):
    """sims/<name>/: scene.mjb (opening in qpos), params.json, and the copied files {dest: source}."""
    out = OUT / name
    if out.exists():
        shutil.rmtree(out)
    out.mkdir(parents=True)
    model.qpos0[:] = qpos
    mujoco.mj_saveModel(model, str(out / "scene.mjb"), None)
    params = dict(name=name, exported=time.strftime("%Y-%m-%d %H:%M"), mujoco=mujoco.__version__, **params)
    (out / "params.json").write_text(json.dumps(jsonable(params), indent=2))
    for dest, src in copies.items():
        (out / dest).parent.mkdir(parents=True, exist_ok=True)
        if src.is_dir():
            shutil.copytree(src, out / dest)
        elif src.exists():
            shutil.copy2(src, out / dest)
    size = sum(f.stat().st_size for f in out.rglob("*") if f.is_file()) / 1e6
    print(f"sims/{name}/: {params['about']} ({size:.0f} MB)")
    return out


def pick_package(name, run):
    """An SO-100 pick-up model with the scene it was trained in (object centred, arm at its start)."""
    ck = torch.load(ROOT / run / "actor_last.pt", map_location="cpu", weights_only=False)
    obj = ck.get("object", "lego_tree")
    env = TagLiftEnv(obj, domain_randomization=False, start=ck["start"], area=ck.get("area"), hand=ck.get("hand"))
    env.spawn_half, env.face_yaw = np.zeros(2), 0.0
    env.reset()
    cfg = json.loads(CONFIG.read_text())
    params = dict(
        about=f"SO-100 + {obj}, trained model {Path(run).name}",
        kind="pick", arm_model=ck.get("arm_model", "so100"),
        object=object_params(obj),
        training={k: v for k, v in ck.items() if k not in ("actor", "args")},
        training_args=ck.get("args"),
        real_setup=dict(note="the SO-100 setup this model ran on (camera seen from the SO-100's base)",
                        env_camera=cfg.get("env_camera_so100", cfg.get("env_camera")), tag_mm=cfg.get("tag_mm"),
                        wrist_camera=cfg.get("wrist_camera")),
        files=dict(scene="scene.mjb", asset=f"assets/{obj}", model="model/actor_last.pt"))
    copies = {f"assets/{obj}": ROOT / "assets" / obj}
    copies.update({f"model/{f}": ROOT / run / f for f in MODEL_FILES})
    return write_package(name, env.model, env.data.qpos.copy(), params, copies)


def two_arm_package(object_name="lego_tree"):
    """The registered arms at their measured spots, the object, the calibrated camera (robot/arms.py)."""
    from arms import arm_poses, arms_scene_spec, load
    cfg = load()
    if not cfg.get("arms"):
        print("no arms registered (robot/arms.py add): two_arms skipped")
        return None
    spec, ref, ec = arms_scene_spec(cfg, list(cfg["arms"]))
    a = DEFAULT_AREA
    obj = mujoco.MjSpec.from_file(str(ROOT / "assets" / object_name / f"{object_name}.xml"))
    spec.worldbody.add_frame(pos=from_ruler((a["front_min"] + a["front_max"]) / 2, 0.0, 0.0)).attach_body(
        obj.body(object_name), "", "")
    if "pos" in ec:
        q = np.zeros(4)
        mujoco.mju_mat2Quat(q, (np.array(ec["R"]) @ np.diag([1, -1, -1])).ravel())  # OpenCV -> MuJoCo axes
        spec.worldbody.add_camera(name="envcam", pos=ec["pos"], quat=q, fovy=ec["fovy_used"])
    model = spec.compile()
    _, poses, report = arm_poses(cfg)
    arms = {}
    copies = {f"assets/{object_name}": ROOT / "assets" / object_name}
    for n, arm in cfg["arms"].items():
        shift, turn = poses.get(n, (np.zeros(3), 0.0))
        arms[n] = dict(model=arm["model"], serial=arm["serial"], measured=n in poses,
                       stands_at_m=shift, turned_deg=float(np.degrees(turn)), env_camera=arm.get("env_camera"),
                       calibration=f"calibration/{Path(arm['calibration']).name}",
                       sim_mapping=f"calibration/{Path(arm['sim_mapping']).name}")
        for key in ("calibration", "sim_mapping"):
            copies[arms[n][key]] = ROOT / arm[key]
    names = ", ".join(f"{n} ({a['model']})" for n, a in cfg["arms"].items())
    params = dict(
        about=f"{names} at their measured spots, {object_name}, the calibrated camera",
        kind="two_arms", reference_arm=ref, arms=arms, placement_report=report,
        object=object_params(object_name),
        camera=dict(env_camera=dict(cfg["env_camera"], **(ec or {})), wrist_camera=cfg.get("wrist_camera"),
                    tag_mm=cfg.get("tag_mm")),
        files=dict(scene="scene.mjb", asset=f"assets/{object_name}", calibration="calibration/"))
    return write_package("two_arms", model, mujoco.MjData(model).qpos.copy(), params, copies)


def load_package(path, with_calibration=False):
    """Put a package back into this project: asset (if missing), objects.json entry, model; optionally the
    calibration (robot/config.json backed up first)."""
    pkg = Path(path)
    params = json.loads((pkg / "params.json").read_text())
    obj = params["object"]["name"]
    if (ROOT / "assets" / obj).exists():
        print(f"assets/{obj} exists already: kept (the package's copy is in {pkg / 'assets' / obj})")
    else:
        shutil.copytree(pkg / "assets" / obj, ROOT / "assets" / obj)
        print(f"added assets/{obj}")
    objects = json.loads((ROOT / "objects.json").read_text())
    objects[obj] = {**objects.get(obj, {}), **params["object"]["objects_json"]}
    (ROOT / "objects.json").write_text(json.dumps(objects, indent=2))
    print(f"objects.json: {obj} (tags {objects[obj].get('tags')}, glue roll {objects[obj].get('tag_roll_deg')})")
    if (pkg / "model").exists():
        run = ROOT / "runs" / pkg.name
        if run.exists():
            print(f"runs/{pkg.name} exists already: kept")
        else:
            shutil.copytree(pkg / "model", run)
            print(f"added the model: runs/{pkg.name}/actor_last.pt")
    if with_calibration:
        backup = CONFIG.with_name(f"config_backup_{time.strftime('%Y%m%d_%H%M%S')}.json")
        shutil.copy2(CONFIG, backup)
        cfg = json.loads(CONFIG.read_text())
        if params["kind"] == "two_arms":
            for n, a in params["arms"].items():
                for key in ("calibration", "sim_mapping"):
                    dest = ROOT / "robot" / "calibration" / Path(a[key]).name
                    shutil.copy2(pkg / a[key], dest)
                cfg.setdefault("arms", {})[n] = dict(serial=a["serial"], model=a["model"],
                                                    calibration=f"robot/calibration/{Path(a['calibration']).name}",
                                                    sim_mapping=f"robot/calibration/{Path(a['sim_mapping']).name}",
                                                    **({"env_camera": a["env_camera"]} if a.get("env_camera") else {}))
            cfg["env_camera"], cfg["tag_mm"] = params["camera"]["env_camera"], params["camera"]["tag_mm"]
        else:
            cfg["env_camera_so100"] = params["real_setup"]["env_camera"]
        CONFIG.write_text(json.dumps(cfg, indent=2))
        print(f"calibration restored (previous config: {backup.relative_to(ROOT)})")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--load", help="a package folder (sims/<name>) to put back into this project")
    ap.add_argument("--calibration", action="store_true", help="with --load: also restore the calibration")
    args = ap.parse_args()
    if args.load:
        return load_package(args.load, args.calibration)
    OUT.mkdir(exist_ok=True)
    for f in OUT.glob("*.mjb"):  # the older single-file exports
        f.unlink()
    index = {}
    for name, run in PICK_SIMS.items():
        if (ROOT / run / "actor_last.pt").exists():
            index[name] = str(pick_package(name, run).relative_to(ROOT))
        else:
            print(f"{run} not found: {name} skipped")
    two = two_arm_package()
    if two:
        index["two_arms"] = str(two.relative_to(ROOT))
    (OUT / "sims.json").write_text(json.dumps(index, indent=2))
    print("open one: python -m mujoco.viewer --mjcf sims/<name>/scene.mjb")


if __name__ == "__main__":
    main()
