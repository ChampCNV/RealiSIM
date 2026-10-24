"""Photo2Grasp: one command for the whole pipeline, step by step.

    python photo2grasp.py asset celsius_can inputs/can_1.mp4 inputs/can_2.mp4   # 1. videos/photos -> sim object
    python photo2grasp.py check celsius_can                                     # 2. drop test + viewer
    python photo2grasp.py tags celsius_can [video]                              #    which tag IDs are on it
    python photo2grasp.py robot                                                 #    arm setup (only if needed)
    python photo2grasp.py calibrate celsius_can                                 # 3. environment camera (touch)
    python photo2grasp.py track celsius_can                                     #    live: real object -> sim
    python photo2grasp.py train celsius_can --area wide [--colab]               # 4. train (here or on Colab)
    python photo2grasp.py import tag_run.zip                                    #    a run trained on Colab
    python photo2grasp.py test runs/<run>/actor_last.pt                         # 5. sim test + preview
    python photo2grasp.py run runs/<run>/actor_last.pt [--env-only] [--dry-run] # 6. real robot

Each step can also be run on its own (the scripts it calls are listed in README.md). Robot setup is not a step
of its own every time: "run" and "calibrate" check that the arm is set up and only then ask.
"""
import argparse
import json
import subprocess
import sys
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent
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


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="step", required=True)
    a = sub.add_parser("asset", help="videos/photos -> MuJoCo object")
    a.add_argument("name")
    a.add_argument("inputs", nargs="+")
    tg = sub.add_parser("tags", help="set which AprilTag IDs are on an object (from a video, or typed)")
    tg.add_argument("name")
    tg.add_argument("inputs", nargs="*", help="optional video/photo showing the tags")
    c = sub.add_parser("check", help="drop test + viewer with the SO-100")
    c.add_argument("name")
    r = sub.add_parser("robot", help="one-time arm setup (asks what to redo)")
    r.add_argument("--all", action="store_true", help="go through every step")
    k = sub.add_parser("calibrate", help="environment camera position (touch the tag with the gripper)")
    k.add_argument("name", nargs="?", default="lego_tree")
    tr = sub.add_parser("track", help="live preview: move the real object, the sim object follows")
    tr.add_argument("name", nargs="?", default="lego_tree")
    t = sub.add_parser("train", help="train the pick-up policy in simulation")
    t.add_argument("name")
    t.add_argument("--area", choices=["default", "wide"], default="default")
    t.add_argument("--steps", type=int)
    t.add_argument("--from-scratch", action="store_true", help="do not start from the trained Lego tree model")
    t.add_argument("--colab", action="store_true", help="make the Colab zip instead of training here")
    i = sub.add_parser("import", help="unpack a run downloaded from Colab into runs/")
    i.add_argument("zip")
    e = sub.add_parser("test", help="sim test (100 attempts) + preview")
    e.add_argument("checkpoint")
    e.add_argument("--no-view", action="store_true")
    g = sub.add_parser("run", help="real robot")
    g.add_argument("checkpoint")
    g.add_argument("rest", nargs=argparse.REMAINDER, help="--env-only, --dry-run, --speed 0.3 ...")
    args = ap.parse_args()

    if args.step == "asset":
        ok = call("make_asset.py", args.name, *[str(Path(p).resolve()) for p in args.inputs])
        if ok:
            call("make_scene.py", args.name, "--no-view")
            print(f"next: python photo2grasp.py check {args.name}   (look at it)   or   train {args.name}")
    elif args.step == "tags":
        sys.path.insert(0, str(ROOT / "pipeline"))
        from tag_ids import tag_setup
        tag_setup(args.name, [str(Path(p).resolve()) for p in args.inputs], ROOT / "objects.json")
    elif args.step == "check":
        call("make_scene.py", args.name)
    elif args.step == "robot":
        robot_setup(force=args.all)
    elif args.step == "calibrate":
        ensure_robot()
        call("robot/calibrate_env_camera.py", "--object", args.name)
    elif args.step == "track":
        call("robot/calibrate_env_camera.py", "--object", args.name, "--track")
    elif args.step == "train":
        init = [] if args.from_scratch or not (BEST_MODEL / "train_state.pt").exists() else ["--init", BEST_MODEL]
        extra = (["--steps", args.steps] if args.steps else [])
        if args.colab:
            call("colab/build_zip.py", "--object", args.name, "--area", args.area, *init, *extra)
            print("next: open colab/colab_cell.py's steps (scratchpad notebook, T4), upload colab/so100_tag_train.zip")
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
            call("sim2real/view_tag_env.py", Path(args.checkpoint).resolve(), cwd=ROOT / "sim2real")
    elif args.step == "run":
        ensure_robot()
        if not robot_status()["env_camera"]:
            print("environment camera not calibrated: the runner lets you press 'c' to do it first")
        call("robot/run_tag_policy.py", args.checkpoint, *args.rest)


if __name__ == "__main__":
    main()
