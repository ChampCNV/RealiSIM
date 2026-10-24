"""Move the real arm to a policy's start (look) pose and back, to check the wrist camera clears the arm.

    python robot/goto_pose.py                                   # Celsius can, wide area look pose
    python robot/goto_pose.py runs/<run>/actor_last.pt           # that model's start pose
    python robot/goto_pose.py --object lego_tree                 # an object's look pose (default area)
    python robot/goto_pose.py --from-zero                        # first to the zero pose, then the start pose

Same moves as run_tag_policy.py: slow eased moves along a route checked in the model (wrist camera >= 3 cm
from the arm all the way). The planned route and its clearances are printed and nothing moves before ENTER.
A MuJoCo window shows every command. At the pose: ENTER folds the arm to SAFE_REST (camera turned away) and
turns torque off. Ctrl+C at any time also folds and turns torque off.
"""
import argparse
import json
import sys
from pathlib import Path

import mujoco
import mujoco.viewer
import numpy as np

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "sim2real"))
from find_look_pose import look_pose  # noqa: E402
from tag_env import (DEFAULT_AREA, SAFE_REST, WIDE_AREA, TagLiftEnv, path_clearance, pose_clearance,  # noqa: E402
                     safe_route)

from arm import SOArm  # noqa: E402
from calibrate_env_camera import CONFIG  # noqa: E402
from sim_map import SimMap  # noqa: E402
from sim_mirror import Mirror  # noqa: E402


def main():
    p = argparse.ArgumentParser()
    p.add_argument("checkpoint", nargs="?", help="a trained model: its start pose (default: --object's look pose)")
    p.add_argument("--object", default="celsius_can")
    p.add_argument("--area", choices=["default", "wide"], default="wide")
    p.add_argument("--move-speed", type=float, default=15.0, help="max joint speed, deg/s")
    p.add_argument("--from-zero", action="store_true", help="go to the zero pose first, wait, then the start pose")
    args = p.parse_args()
    if args.checkpoint:
        import torch
        ck = torch.load(args.checkpoint, map_location="cpu", weights_only=False)
        obj, start = ck.get("object", "lego_tree"), np.array(ck["start"])
    else:
        obj = args.object
        start = np.array(look_pose(obj, WIDE_AREA if args.area == "wide" else DEFAULT_AREA))
    env = TagLiftEnv(obj, domain_randomization=False, start=list(start))
    m, cd = env.model, env.check_data

    cfg = json.loads(CONFIG.read_text())
    arm = SOArm(cfg["port"], ROOT / cfg["calibration"], cfg["baudrate"])
    mapping = SimMap(ROOT / cfg["sim_mapping"], arm.calibration)
    if arm.calibration is None or not mapping.ready:
        sys.exit("The arm is not calibrated yet: python photo2grasp.py robot")
    arm.connect()
    arm.configure(**cfg.get("pid", {}))
    d = env.data
    viewer = mujoco.viewer.launch_passive(m, d)

    def show(q):
        d.qpos[env.qadr] = q
        mujoco.mj_forward(m, d)
        viewer.sync()

    mover = Mirror(arm, mapping, args.move_speed, on_command=show)
    show(mover.current)
    torque = False

    def move(goal, label):
        route = safe_route(m, cd, mover.current, goal)
        if route is None:
            print(f"no camera-safe route to {label} from here: move the arm by hand toward its zero pose (upright)")
            return False
        points = [mover.current] + route
        print(f"route to {label}: {len(route)} move(s); camera clearance along each: "
              + ", ".join(f"{path_clearance(m, cd, a, b) * 100:.1f} cm" for a, b in zip(points[:-1], points[1:])))
        for q in route:
            mover.ease_to(np.asarray(q))
        return True

    try:
        print(f"now: camera {pose_clearance(m, cd, mover.current) * 100:.1f} cm from the arm | {obj} start pose: "
              f"{pose_clearance(m, cd, start) * 100:.1f} cm | rest pose: {pose_clearance(m, cd, SAFE_REST) * 100:.1f} cm")
        if input("ENTER = move to the start pose (watch the camera; Ctrl+C stops and folds) | q = quit: ").strip():
            return
        now = arm.read_degrees()
        arm.write_degrees(now)  # torque on where the arm is (no jump to an old goal)
        arm.torque(True)
        torque = True
        mover.current = mapping.to_sim(now)
        if args.from_zero:
            zero = (m.jnt_range[:6, 0] + m.jnt_range[:6, 1]) / 2  # middle of every joint's range
            if not move(zero, "the zero pose"):
                return
            input("at the zero pose. ENTER = move to the start pose (Ctrl+C stops and folds): ")
        if move(start, "the start pose"):
            input("at the start pose. ENTER = fold to rest and turn torque off: ")
    except KeyboardInterrupt:
        print("\nstopped")
    finally:
        if torque:
            print("folding to rest (camera turned away), then torque off...")
            mover.current = mapping.to_sim(arm.read_degrees())
            move(np.array(SAFE_REST), "rest")
        arm.disconnect(torque_off=True)
        viewer.close()


if __name__ == "__main__":
    main()
