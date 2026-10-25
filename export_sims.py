"""Save the simulations as files: one self-contained MuJoCo file (.mjb: arm, object, tags, cameras, meshes,
textures) per sim, opening in its start pose.

    python export_sims.py                  # sims/tree_pick.mjb, sims/can_pick.mjb, sims/two_arms.mjb
    python -m mujoco.viewer --mjcf sims/tree_pick.mjb

A file is the scene only: training builds it fresh every episode (random placement, lighting, cameras) and a
trained model drives it with sim2real/view_tag_env.py <model>. sims/sims.json says what each file holds.
"""
import json
import sys
from pathlib import Path

import mujoco
import numpy as np
import torch

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT / "sim2real"))
sys.path.insert(0, str(ROOT / "robot"))
from tag_env import DEFAULT_AREA, TagLiftEnv, from_ruler  # noqa: E402

OUT = ROOT / "sims"
PICK_SIMS = {"tree_pick": "runs/tag_envcam_2250k/actor_last.pt",
             "can_pick": "runs/tag_celsius_can_celsius_can_backup_2000k/actor_last.pt"}


def save(model, qpos, name, about):
    model.qpos0[:] = qpos  # opens (and resets) in this pose
    path = OUT / f"{name}.mjb"
    mujoco.mj_saveModel(model, str(path), None)
    print(f"{path.relative_to(ROOT)}: {about} ({path.stat().st_size / 1e6:.1f} MB)")
    return dict(file=path.name, about=about)


def pick_sim(name, checkpoint):
    """The SO-100 scene a pick-up model was trained in: its object in the middle of its area, arm at its start."""
    ck = torch.load(ROOT / checkpoint, map_location="cpu", weights_only=False)
    env = TagLiftEnv(ck.get("object", "lego_tree"), domain_randomization=False, start=ck["start"],
                     area=ck.get("area"))
    env.spawn_half, env.face_yaw = np.zeros(2), 0.0  # object centred, facing the robot
    env.reset()
    return save(env.model, env.data.qpos.copy(), name,
                f"SO-100 + {env.object_name}, as trained for {checkpoint} (watch it: sim2real/view_tag_env.py "
                f"{checkpoint})")


def two_arm_sim(object_name="lego_tree"):
    """The registered arms at their measured spots, the object, the calibrated camera (robot/arms.py)."""
    from arms import arms_scene_spec, load
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
    data = mujoco.MjData(model)
    arms = ", ".join(f"{n} ({cfg['arms'][n]['model']})" for n in cfg["arms"])
    return save(model, data.qpos.copy(), "two_arms",
                f"{arms} at their measured spots (reference: {ref}), {object_name}, the calibrated camera")


def main():
    OUT.mkdir(exist_ok=True)
    index = {}
    for name, checkpoint in PICK_SIMS.items():
        if (ROOT / checkpoint).exists():
            index[name] = pick_sim(name, checkpoint)
        else:
            print(f"{checkpoint} not found: {name} skipped")
    two = two_arm_sim()
    if two:
        index["two_arms"] = two
    (OUT / "sims.json").write_text(json.dumps(index, indent=2))
    print("open one: python -m mujoco.viewer --mjcf sims/<name>.mjb")


if __name__ == "__main__":
    main()
