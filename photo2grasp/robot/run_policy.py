"""Run a sim-trained policy on the real SO-100 with its wrist camera.

    python robot/run_policy.py runs/<run>/actor_best.pt --dry-run      # camera + policy only, arm stays limp
    python robot/run_policy.py runs/<run>/actor_best.pt --speed 0.3    # real run, slow

Same loop as the simulation: every 0.1 s, wrist image (64x64) + joint angles -> policy -> new joint targets.
Safety: slow start/stop moves, `--speed` scales every action, per-step limit, Ctrl+C folds the arm and
turns torque off.
"""
import argparse
import json
import sys
import time
from pathlib import Path

import cv2
import numpy as np
import torch

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "sim2real"))
from lift_env import LiftEnv, start_pose  # noqa: E402  (task constants only, no simulation is created)
from train_sac import Actor, to_img  # noqa: E402

from arm import SOArm  # noqa: E402
from camera import Camera, list_cameras, policy_view  # noqa: E402
from sim_map import SimMap, load_sim, require_arm_model, sim_pose  # noqa: E402
from sim_mirror import Mirror  # noqa: E402  (slow, speed-limited eased moves)

STATE_DIM = 12  # 6 joint angles + 6 joint targets, as in the simulation


def main():
    p = argparse.ArgumentParser()
    p.add_argument("checkpoint")
    p.add_argument("--dry-run", action="store_true", help="no torque: just show what the policy sees/does")
    p.add_argument("--speed", type=float, default=0.3, help="scale on every action (1.0 = as in simulation)")
    p.add_argument("--max-step-deg", type=float, default=5.0, help="max change of any joint target per step")
    p.add_argument("--episodes", type=int, default=5)
    p.add_argument("--move-speed", type=float, default=20.0, help="max joint speed for start/return moves, deg/s")
    args = p.parse_args()

    cfg = json.loads((Path(__file__).parent / "config.json").read_text())
    ckpt = torch.load(args.checkpoint, map_location="cpu")
    size = ckpt["args"]["image_size"]
    if ckpt["args"].get("camera") != "wrist":
        sys.exit("This checkpoint was trained with a fixed camera; run_policy.py drives the wrist camera only.")
    actor = Actor(STATE_DIM, LiftEnv.action_scale.size, size)
    actor.load_state_dict(ckpt["actor"])
    actor.eval()

    require_arm_model("so100", "this image policy")
    arm = SOArm(cfg["port"], ROOT / cfg["calibration"], cfg["baudrate"])
    mapping = SimMap(ROOT / cfg["sim_mapping"], arm.calibration)
    if arm.calibration is None or not mapping.ready:
        sys.exit("Run robot/calibrate.py (option 1) first.")
    cc = cfg["wrist_camera"]
    index = cc["index"]
    found = list_cameras()
    if index not in found and len(found) == 1:
        print(f"warning: camera {index} not found, using the only camera available ({found[0]})")
        index = found[0]
    cam = Camera(index, cc["width"], cc["height"], cc.get("fixed_exposure"))
    model, _ = load_sim()
    lo, hi = model.actuator_ctrlrange[:, 0], model.actuator_ctrlrange[:, 1]
    start = start_pose(model, ckpt["args"].get("start", "home"))  # same start pose the policy was trained from
    rest_pose = sim_pose(model, "rest")
    max_step = np.radians(args.max_step_deg)

    arm.connect()
    arm.configure(**cfg.get("pid", {}))
    if not args.dry_run:
        arm.torque(True)
    mover = Mirror(arm, mapping, args.move_speed)
    # slower actions need more steps: same total motion as an 8 s simulation attempt
    steps = int(LiftEnv.max_steps / min(1.0, args.speed))
    try:
        if not args.dry_run:
            print(f"moving slowly to the start pose ('{ckpt['args'].get('start', 'home')}')...")
            mover.ease_to(start)
        for ep in range(args.episodes):
            input(f"\nEpisode {ep + 1}: place the object in front of the arm, then press ENTER "
                  f"(up to {steps * LiftEnv.control_dt:.0f} s; Esc in the camera window ends it)")
            target = mapping.to_sim(arm.read_degrees())
            for _ in range(steps):
                t0 = time.time()
                q = mapping.to_sim(arm.read_degrees())
                img = policy_view(cam.read(), size, cc.get("vertical_fov_deg"))
                state = np.concatenate([q, target]).astype(np.float32)
                with torch.no_grad():
                    mean, _ = actor(to_img(img[None], "cpu", False), torch.as_tensor(state[None]))
                action = torch.tanh(mean)[0].numpy()
                step = np.clip(action * LiftEnv.action_scale * args.speed, -max_step, max_step)
                target = np.clip(target + step, lo, hi)
                if not args.dry_run:
                    arm.write_degrees(mapping.to_real(target))
                view = cv2.resize(img, (256, 256), interpolation=cv2.INTER_NEAREST)
                cv2.imshow("what the policy sees (64x64)", cv2.cvtColor(view, cv2.COLOR_RGB2BGR))
                if cv2.waitKey(1) == 27:  # Esc ends the episode
                    break
                time.sleep(max(0, LiftEnv.control_dt - (time.time() - t0)))
            if not args.dry_run:
                print("episode done, returning slowly to the start pose (gripper opens)")
                mover.current = target
                mover.ease_to(start)
    except KeyboardInterrupt:
        print("\nstopped by user")
    finally:
        if not args.dry_run:
            print("folding the arm to rest, then torque off...")
            mover.current = mapping.to_sim(arm.read_degrees())
            mover.ease_to(rest_pose)
        arm.disconnect(torque_off=True)
        cam.close()
        cv2.destroyAllWindows()


if __name__ == "__main__":
    main()
