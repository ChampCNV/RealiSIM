"""Photo2Grasp: one command for the whole pipeline, step by step.

    python photo2grasp.py asset celsius_can inputs/can_1.mp4 inputs/can_2.mp4   # 1. videos/photos -> sim object
    python photo2grasp.py hand my_hand inputs/hand.mp4  (or --webcam 0)         #    your hand -> sim hand
    python photo2grasp.py add [asset] masking_tape [D:/models/tape.glb]         #    add an object back / import one
    python photo2grasp.py add hand my_hand [hand.json | folder | video]         #    add a hand back / import one
    python photo2grasp.py list                                                  #    objects + hands (+ trained models)
    python photo2grasp.py remove [asset] celsius_can [--models] [--purge]       #    take an object out of the sim
    python photo2grasp.py remove hand my_hand [--purge]                         #    take a hand out
    python photo2grasp.py check celsius_can [lego_tree ...] / check --all      # 2. drop test + viewer
    python photo2grasp.py check celsius_can --hand my_hand                      #    the object on your palm
    python photo2grasp.py tags celsius_can [video]                              #    which tag IDs are on it
    python photo2grasp.py robot                                                 #    arm setup (only if needed)
    python photo2grasp.py calibrate celsius_can                                 # 3. environment camera (touch)
    python photo2grasp.py track celsius_can [--hand my_hand] [--depth]          #    live: real object (+ hand) -> sim
    python photo2grasp.py train celsius_can --area wide [--colab]               # 4. train (here or on Colab)
    python photo2grasp.py train celsius_can --hand my_hand --colab              #    take it from your hand
    python photo2grasp.py import tag_run.zip                                    #    a run trained on Colab
    python photo2grasp.py test runs/<run>/actor_last.pt [--to-hand my_hand]     # 5. sim test + preview
    python photo2grasp.py run runs/<run>/actor_last.pt [--env-only] [--dry-run] # 6. real robot
    python photo2grasp.py run runs/<run>/actor_last.pt --to-hand my_hand        #    then put it on your palm

Each step can also be run on its own (the scripts it calls are listed in README.md). Robot setup is not a step
of its own every time: "run" and "calibrate" check that the arm is set up and only then ask.
"""
import argparse
import json
import shutil
import subprocess
import sys
import zipfile
from pathlib import Path

import numpy as np

from api.config import load_env

ROOT = Path(__file__).resolve().parent
load_env()  # so child scripts (make_asset, the scene API) see keys from the repo .env
PY = sys.executable
BEST_MODEL = ROOT / "runs" / "tag_envcam_2250k"  # starting point for new objects (trained on the Lego tree)


def call(script, *args, cwd=ROOT):
    print(f"\n>>> {Path(script).name} {' '.join(map(str, args))}")
    return subprocess.call([PY, str(ROOT / script), *map(str, args)], cwd=cwd) == 0


def ask(question, default="y"):
    answer = input(f"{question} [{'Y/n' if default == 'y' else 'y/N'}]: ").strip().lower()
    return (answer or default) == "y"


def robot_status():
    """What of the one-time arm setup is done (LeRobot-style: port, motor IDs, joint calibration, gripper)."""
    cfg = json.loads((ROOT / "robot" / "config.json").read_text())
    mapping = ROOT / cfg["sim_mapping"]
    gripper = json.loads(mapping.read_text()).get("gripper", {}) if mapping.exists() else {}
    return dict(port=bool(cfg.get("port")), joints=(ROOT / cfg["calibration"]).exists() and mapping.exists(),
                gripper="offset_deg" in gripper, env_camera="pos" in cfg.get("env_camera", {}))


def robot_setup(force=False):
    s = robot_status()
    if all((s["port"], s["joints"], s["gripper"])) and not force:
        print("arm already set up (port, joint calibration, gripper offset).")
        if not ask("redo any of it?", "n"):
            return True
    if force or not s["port"] or ask("find the arm's USB port again?", "n"):
        call("robot/find_port.py")
    if ask("number the motors (only for a new / rebuilt arm, one motor connected at a time)?", "n"):
        call("robot/setup_motors.py")
    if force or not s["joints"] or ask("calibrate the joints again (limits + directions, sim shown)?", "n"):
        call("robot/calibrate.py")
    if force or not s["gripper"] or ask("line up the gripper (closed in sim = closed for real)?", "n"):
        call("robot/gripper_check.py")
    if ask("check the cameras against the sim?", "y"):
        call("robot/camera_check.py")
    return True


def ensure_robot():
    s = robot_status()
    if not (s["port"] and s["joints"] and s["gripper"]):
        print("the arm is not fully set up yet:", ", ".join(k for k in ("port", "joints", "gripper") if not s[k]))
        if ask("set it up now?"):
            robot_setup()


def trained_models():
    """{object name: [run dirs]} for every run with a policy in runs/."""
    import torch
    found = {}
    for ck in sorted((ROOT / "runs").glob("*/actor_last.pt")):
        try:
            meta = torch.load(ck, map_location="cpu", weights_only=False)
        except Exception:
            continue
        found.setdefault(meta.get("object", "lego_tree"), []).append(ck.parent)
    return found


MESH_TYPES = {".obj", ".stl", ".ply", ".glb", ".gltf", ".off"}


def restore(name):
    """Add back an object made before (or removed), by name: its saved mesh in outputs/<name> + its saved
    size/mass/tags -> rebuilt asset. No video, no TRELLIS."""
    work = ROOT / "outputs" / name
    if (ROOT / "assets" / name / f"{name}.xml").exists():
        print(f"'{name}' is already added (python photo2grasp.py check {name})")
        return
    if not (work / "raw_mesh.obj").exists():
        made = sorted(p.parent.name for p in (ROOT / "outputs").glob("*/raw_mesh.obj")
                      if not (ROOT / "assets" / p.parent.name).exists())
        raise SystemExit(f"no saved mesh for '{name}'. Objects that can be added back by name: {made or 'none'}; "
                         f"otherwise: python photo2grasp.py asset {name} <video>")
    objects = json.loads((ROOT / "objects.json").read_text())
    saved = work / "object.json"  # written by "remove"
    if name not in objects and saved.exists():
        objects[name] = json.loads(saved.read_text())
        (ROOT / "objects.json").write_text(json.dumps(objects, indent=2))
    print(f"adding '{name}' back from outputs/{name} (saved mesh{', size/mass/tags' if name in objects else ''})")
    if call("make_asset.py", name, "--skip-mesh"):
        call("make_scene.py", name, "--no-view")


def add_existing(name, source):
    """Bring in an object that already exists:
    - an asset folder (<x>.xml + properties.json, made by this pipeline): copied to assets/<name>, renamed if needed,
      its size/mass/friction copied into objects.json
    - a 3D model file: skips TRELLIS and runs the rest of the asset step (clean-up, real size, collision shapes,
      MuJoCo file, size and tag questions)"""
    src = Path(source).resolve()
    dest = ROOT / "assets" / name
    if dest.exists():
        raise SystemExit(f"assets/{name} already exists (python photo2grasp.py remove {name} first)")
    if src.is_dir():
        xmls = sorted(src.glob("*.xml"))
        if len(xmls) != 1 or not (src / "properties.json").exists():
            raise SystemExit("an asset folder needs exactly one <name>.xml and a properties.json")
        old = xmls[0].stem
        dest.mkdir(parents=True)
        for f in src.iterdir():
            if f.is_file():
                target = dest / f.name.replace(old, name, 1) if f.name.startswith(old) else dest / f.name
                if f.suffix in (".xml", ".json") and old != name:  # body, mesh and material names carry the object name
                    target.write_text(f.read_text(encoding="utf-8").replace(old, name), encoding="utf-8")
                else:
                    shutil.copy2(f, target)
        props = json.loads((dest / "properties.json").read_text())
        objects = json.loads((ROOT / "objects.json").read_text())
        keep = {k: props[k] for k in ("source", "size_m", "mass_kg", "material", "friction") if k in props}
        objects[name] = {**keep, **{k: v for k, v in objects.get(name, {}).items() if k == "tags"}}
        (ROOT / "objects.json").write_text(json.dumps(objects, indent=2))
        print(f"added assets/{name} (from {src})")
        if "tags" not in objects[name]:
            sys.path.insert(0, str(ROOT / "pipeline"))
            from tag_ids import tag_setup
            tag_setup(name, [], ROOT / "objects.json")
        call("make_scene.py", name, "--no-view")
    elif src.suffix.lower() in MESH_TYPES:
        import trimesh
        work = ROOT / "outputs" / name
        work.mkdir(parents=True, exist_ok=True)
        mesh = trimesh.load(src, force="mesh")
        if src.suffix.lower() in (".glb", ".gltf"):
            shutil.copy2(src, work / "trellis.glb")  # textured: its colours are kept (same path as TRELLIS output)
            mesh.apply_transform(trimesh.transformations.rotation_matrix(np.pi / 2, [1, 0, 0]))  # glTF Y-up -> Z-up
        mesh.export(work / "raw_mesh.obj")
        print(f"model copied to outputs/{name}/raw_mesh.obj; building the asset (no TRELLIS needed)")
        if call("make_asset.py", name, "--skip-mesh"):
            call("make_scene.py", name, "--no-view")
    else:
        raise SystemExit(f"{src} is neither an asset folder nor a 3D model ({', '.join(sorted(MESH_TYPES))})")


HANDS = ROOT / "hands"
REMOVED_HANDS = ROOT / "outputs" / "hands"  # removed hands wait here for "add hand <name>"


def list_hands(models):
    names = sorted(p.parent.name for p in HANDS.glob("*/hand.json")) if HANDS.exists() else []
    if not names:
        print("\nno hands yet: python photo2grasp.py hand <name> <video of your open hand>")
        return
    print("\nhands:")
    for name in names:
        h = json.loads((HANDS / name / "hand.json").read_text())
        runs = ", ".join(r.name for rs in models.values() for r in rs if f"_from_{name}_" in r.name) or "none"
        print(f"{name}\n   {h['side']} hand, {h['length_m'] * 100:.1f} cm long, from {h['frames_used']} frames | "
              f"trained to take objects from it: {runs}")


def remove_hand(name, purge=False):
    src = HANDS / name
    if not src.exists():
        print(f"no hand called '{name}' (python photo2grasp.py list shows the hands)")
        return
    print(f"this deletes the hand '{name}' (hands/{name})" + ("" if purge else f"; it is kept in outputs/hands/{name} "
          f"so 'add hand {name}' brings it back (--purge deletes it for good)"))
    print("   (models trained with it keep working: they carry their own copy of the hand)")
    if input(f"type the name ({name}) to confirm: ").strip() != name:
        print("not deleted")
        return
    if purge:
        shutil.rmtree(src)
    else:
        dest = REMOVED_HANDS / name
        if dest.exists():
            shutil.rmtree(dest)
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(src), str(dest))
    print(f"hand '{name}' deleted")


def add_hand(name, source=None):
    """A removed hand back (by name), a hand.json / hand folder from elsewhere, or a new one from videos/photos."""
    dest = HANDS / name
    if dest.exists():
        raise SystemExit(f"hands/{name} already exists (python photo2grasp.py remove hand {name} first)")
    if source is None:
        saved = REMOVED_HANDS / name
        if not (saved / "hand.json").exists():
            raise SystemExit(f"no removed hand '{name}' (removed hands: "
                             f"{sorted(q.name for q in REMOVED_HANDS.glob('*')) if REMOVED_HANDS.exists() else []}); "
                             f"make one: python photo2grasp.py hand {name} <video>")
        shutil.move(str(saved), str(dest))
        print(f"hand '{name}' added back")
        return
    src = Path(source).resolve()
    hand_file = src / "hand.json" if src.is_dir() else src
    if hand_file.suffix == ".json":
        hand = json.loads(hand_file.read_text())
        if "points" not in hand or len(hand["points"]) != 21:
            raise SystemExit(f"{hand_file} is not a hand made by make_hand.py")
        dest.mkdir(parents=True)
        if src.is_dir():
            for f in src.iterdir():
                if f.is_file():
                    shutil.copy2(f, dest / f.name)
        hand["name"] = name
        (dest / "hand.json").write_text(json.dumps(hand, indent=2))
        print(f"hand '{name}' added from {src}")
    else:  # videos / photos of the hand
        call("make_hand.py", name, src)


def list_objects():
    objects = json.loads((ROOT / "objects.json").read_text())
    models = trained_models()
    names = sorted({p.parent.name for p in (ROOT / "assets").glob("*/properties.json")})
    if not names:
        print("no objects yet: python photo2grasp.py asset <name> <video>")
    for name in names:
        props = json.loads((ROOT / "assets" / name / "properties.json").read_text())
        tags = objects.get(name, {}).get("tags")
        show = lambda i: "none" if i is None else i  # noqa: E731
        tag_text = f"robot side {show(tags['front'])}, camera side {show(tags['back'])}" if tags else             "not set (defaults 0 / 1)"
        size = " x ".join(f"{v * 100:.1f}" for v in props["size_m"])
        runs = ", ".join(r.name for r in models.get(name, [])) or "none"
        print(f"{name}\n   size {size} cm | {props['mass_kg'] * 1000:.0f} g | tags: {tag_text}\n"
              f"   trained models: {runs}")
    list_hands(models)


def remove_object(name, with_models=False, purge=False):
    """Deletes the object from the sim (assets/ + objects.json). Its mesh and properties stay in outputs/<name> so
    "add <name>" can bring it back, unless purge."""
    targets = [ROOT / "assets" / name] + ([ROOT / "outputs" / name] if purge else [])
    objects = json.loads((ROOT / "objects.json").read_text())
    runs = trained_models().get(name, []) if with_models else []
    if not any(t.exists() for t in targets) and name not in objects and not runs:
        print(f"nothing called '{name}' (python photo2grasp.py list shows the objects)")
        return
    print(f"this deletes '{name}':")
    for t in targets + runs:
        if t.exists():
            print(f"   {t.relative_to(ROOT)}")
    if name in objects:
        print(f"   its entry in objects.json (size, weight, tags)")
    if not with_models and trained_models().get(name):
        print("   (its trained models in runs/ are kept; add --models to delete them too)")
    if not purge and (ROOT / "outputs" / name / "raw_mesh.obj").exists():
        print(f"   (its mesh + properties stay in outputs/{name}: 'add {name}' brings it back; --purge deletes them)")
    if name == "lego_tree":
        print("   note: the Lego tree's trained model is the starting point for training new objects")
    if input(f"type the name ({name}) to confirm: ").strip() != name:
        print("not deleted")
        return
    if name in objects and not purge and (ROOT / "outputs" / name).exists():  # kept for "add <name>"
        (ROOT / "outputs" / name / "object.json").write_text(json.dumps(objects[name], indent=2))
    for t in targets + runs:
        if t.exists():
            shutil.rmtree(t)
    if name in objects:
        del objects[name]
        (ROOT / "objects.json").write_text(json.dumps(objects, indent=2))
    print(f"'{name}' deleted")


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="step", required=True)
    a = sub.add_parser("asset", help="videos/photos -> MuJoCo object")
    a.add_argument("name")
    a.add_argument("inputs", nargs="+")
    hd = sub.add_parser("hand", help="videos/photos (or a webcam) of your open hand -> your hand in the sim")
    hd.add_argument("name")
    hd.add_argument("inputs", nargs="*")
    hd.add_argument("--webcam", type=int, help="record from this camera instead")
    hd.add_argument("--length", type=float, help="cm, wrist crease to middle fingertip (asked if not given)")
    hd.add_argument("--side", choices=["left", "right"])
    ad = sub.add_parser("add", help="add an object/hand made before (by name), or import one from a file/folder")
    ad.add_argument("words", nargs="+", metavar="[asset|hand] name [source]",
                    help="asset (default) or hand, the name, optional source: asset folder or model file (.obj .stl "
                         ".ply .glb .gltf); for a hand a hand.json, a hand folder, or videos/photos")
    sub.add_parser("list", help="all objects and hands, with trained models")
    rm = sub.add_parser("remove", help="delete an object or a hand (asks to confirm)")
    rm.add_argument("words", nargs="+", metavar="[asset|hand] name")
    rm.add_argument("--models", action="store_true", help="also delete its trained models in runs/")
    rm.add_argument("--purge", action="store_true",
                    help="also delete its saved mesh / the saved hand (then it cannot be added back)")
    tg = sub.add_parser("tags", help="set which AprilTag IDs are on an object (from a video, or typed)")
    tg.add_argument("name")
    tg.add_argument("inputs", nargs="*", help="optional video/photo showing the tags")
    c = sub.add_parser("check", help="drop test + viewer with the SO-100 (one, several, or --all objects)")
    c.add_argument("names", nargs="*")
    c.add_argument("--all", action="store_true", help="every object in the sim")
    c.add_argument("--hand", help="your hand, palm up, the object standing on it")
    r = sub.add_parser("robot", help="one-time arm setup (asks what to redo)")
    r.add_argument("--all", action="store_true", help="go through every step")
    k = sub.add_parser("calibrate", help="environment camera position (touch the tag with the gripper)")
    k.add_argument("name", nargs="?", default="lego_tree")
    tr = sub.add_parser("track", help="live preview: move the real object (and your hand), the sim follows")
    tr.add_argument("name", nargs="?", default="lego_tree")
    tr.add_argument("--hand", help="also track your hand (hands/<name>) with MediaPipe")
    tr.add_argument("--depth", action="store_true", help="RealSense depth for the hand's distance")
    tr.add_argument("--video", help="a recorded video instead of the environment camera (try without the setup)")
    tr.add_argument("--camera", type=int, help="this camera index instead of the environment camera")
    t = sub.add_parser("train", help="train the pick-up policy in simulation")
    t.add_argument("name")
    t.add_argument("--area", choices=["default", "wide"], default="default")
    t.add_argument("--steps", type=int)
    t.add_argument("--from-scratch", action="store_true", help="do not start from the trained Lego tree model")
    t.add_argument("--colab", action="store_true", help="make the Colab zip instead of training here")
    t.add_argument("--hand", help="train to take the object from this hand (hands/<name>), held out palm up")
    i = sub.add_parser("import", help="unpack a run downloaded from Colab into runs/")
    i.add_argument("zip")
    e = sub.add_parser("test", help="sim test (100 attempts) + preview")
    e.add_argument("checkpoint")
    e.add_argument("--no-view", action="store_true")
    e.add_argument("--to-hand", help="preview: after the pick-up, put the object on this hand (scripted)")
    g = sub.add_parser("run", help="real robot")
    g.add_argument("checkpoint")
    g.add_argument("rest", nargs=argparse.REMAINDER, help="--env-only, --dry-run, --speed 0.3 ...")
    args = ap.parse_args()

    if args.step == "asset":
        ok = call("make_asset.py", args.name, *[str(Path(p).resolve()) for p in args.inputs])
        if ok:
            call("make_scene.py", args.name, "--no-view")
            print(f"next: python photo2grasp.py check {args.name}   (look at it)   or   train {args.name}")
    elif args.step == "hand":
        if not args.inputs and args.webcam is None:
            raise SystemExit("give videos/photos of your open hand, or --webcam <camera index>")
        extra = [*(["--webcam", args.webcam] if args.webcam is not None else []),
                 *(["--length", args.length] if args.length else []), *(["--side", args.side] if args.side else [])]
        if call("make_hand.py", args.name, *[Path(x).resolve() for x in args.inputs], *extra):
            print(f"next: python photo2grasp.py check <object> --hand {args.name}")
    elif args.step == "add":
        kind, rest = (args.words[0], args.words[1:]) if args.words[0] in ("asset", "hand") else ("asset", args.words)
        if not 1 <= len(rest) <= 2:
            raise SystemExit("add [asset|hand] <name> [source]")
        name, source = rest[0], (rest[1] if len(rest) == 2 else None)
        if kind == "hand":
            add_hand(name, source)
        elif source:
            add_existing(name, source)
        else:
            restore(name)
    elif args.step == "list":
        list_objects()
    elif args.step == "remove":
        kind, rest = (args.words[0], args.words[1:]) if args.words[0] in ("asset", "hand") else ("asset", args.words)
        if len(rest) != 1:
            raise SystemExit("remove [asset|hand] <name>")
        if kind == "hand":
            remove_hand(rest[0], args.purge)
        else:
            remove_object(rest[0], args.models, args.purge)
    elif args.step == "tags":
        sys.path.insert(0, str(ROOT / "pipeline"))
        from tag_ids import tag_setup
        tag_setup(args.name, [str(Path(p).resolve()) for p in args.inputs], ROOT / "objects.json")
    elif args.step == "check":
        if args.hand:
            call("make_scene.py", *args.names, "--hand", args.hand)
        else:
            call("make_scene.py", *(["--all"] if args.all or not args.names else args.names))
    elif args.step == "robot":
        robot_setup(force=args.all)
    elif args.step == "calibrate":
        ensure_robot()
        call("robot/calibrate_env_camera.py", "--object", args.name)
    elif args.step == "track":
        extra = [*(["--hand", args.hand] if args.hand else []), *(["--depth"] if args.depth else []),
                 *(["--video", Path(args.video).resolve()] if args.video else []),
                 *(["--camera", args.camera] if args.camera is not None else [])]
        call("robot/calibrate_env_camera.py", "--object", args.name, "--track", *extra)
    elif args.step == "train":
        init = [] if args.from_scratch or not (BEST_MODEL / "train_state.pt").exists() else ["--init", BEST_MODEL]
        extra = (["--steps", args.steps] if args.steps else []) + (["--hand", args.hand] if args.hand else [])
        if args.colab:
            call("colab/build_zip.py", "--object", args.name, "--area", args.area, *init, *extra)
            task = f"{args.name}_from_{args.hand}" if args.hand else args.name
            print(f"next: Colab (T4): open colab/{task}_train.ipynb or paste colab/colab_cell.py, upload "
                  f"colab/{task}_train.zip")
        else:
            call("sim2real/train_tag.py", "--object", args.name, "--area", args.area, *init, *extra,
                 cwd=ROOT / "sim2real")
    elif args.step == "import":
        z = zipfile.ZipFile(args.zip)
        ck_name = next(n for n in z.namelist() if n.endswith("actor_last.pt"))
        meta = __import__("torch").load(z.open(ck_name), map_location="cpu", weights_only=False)
        out = ROOT / "runs" / f"tag_{meta.get('object', 'lego_tree')}_{Path(args.zip).stem}"
        z.extractall(out)
        print(f"unpacked to {out}\nnext: python photo2grasp.py test {out / 'actor_last.pt'}")
    elif args.step == "test":
        call("sim2real/evaluate_tag.py", Path(args.checkpoint).resolve(), cwd=ROOT / "sim2real")
        if not args.no_view:
            call("sim2real/view_tag_env.py", Path(args.checkpoint).resolve(),
                 *(["--to-hand", args.to_hand] if args.to_hand else []), cwd=ROOT / "sim2real")
    elif args.step == "run":
        ensure_robot()
        if not robot_status()["env_camera"]:
            print("environment camera not calibrated: the runner lets you press 'c' to do it first")
        call("robot/run_tag_policy.py", args.checkpoint, *args.rest)


if __name__ == "__main__":
    main()
