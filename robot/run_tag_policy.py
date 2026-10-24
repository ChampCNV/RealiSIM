"""Run the AprilTag lift policy on the real SO-100 with the wrist webcam.

    python robot/run_tag_policy.py runs/tag_.../actor_best.pt --dry-run    # torque OFF: move the arm by hand,
                                                                            # check the tag numbers + policy output
    python robot/run_tag_policy.py runs/tag_.../actor_best.pt --speed 0.3  # real run, slow

Every 0.1 s, exactly as in training (tag_env.py):
  webcam frame -> AprilTag detector (OpenCV) -> solvePnP (tag size + lens) -> camera pose from the calibrated
  joint angles (forward kinematics in the sim model) -> tag position in the robot frame -> tag_state() ->
  policy -> small joint-target change.
Safety: slow eased moves from zero to the start pose, `--speed` scales every action, per-step limit,
Ctrl+C folds the arm and turns torque off.
"""
import argparse
import json
import sys
import time
from pathlib import Path

import cv2
import mujoco
import numpy as np
import torch

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "sim2real"))
from tag_env import TAG, TagLiftEnv, intrinsics, tag_state, vfov  # noqa: E402
from train_tag import Actor  # noqa: E402

from arm import SOArm  # noqa: E402
from camera import Camera, list_cameras  # noqa: E402
from sim_map import SimMap, sim_pose  # noqa: E402
from sim_mirror import Mirror  # noqa: E402


class TagTracker:
    """Real-robot version of the simulated detection: same maths, same memory, same policy input."""

    def __init__(self, ck, fovy):
        self.env = TagLiftEnv("lego_tree", domain_randomization=False, start=ck["start"])  # model for kinematics
        self.env.calib_pos, self.env.calib_rot = np.zeros(3), np.eye(3)
        self.K = intrinsics(fovy, ck["webcam"]["width"], ck["webcam"]["height"])
        self.detector = cv2.aruco.ArucoDetector(cv2.aruco.getPredefinedDictionary(TAG["dictionary"]),
                                                cv2.aruco.DetectorParameters())
        self.reset()

    def reset(self):
        self.env.tag_est, self.env.seen, self.env.age = np.zeros(5), False, 0

    def update(self, q_sim, frame_bgr):
        """Joint angles (sim radians) + webcam frame -> (visible, corners or None)."""
        e = self.env
        e.data.qpos[e.qadr] = q_sim
        mujoco.mj_kinematics(e.model, e.data)
        corners, ids, _ = self.detector.detectMarkers(cv2.cvtColor(frame_bgr, cv2.COLOR_BGR2GRAY))
        detection, c = None, None
        if ids is not None and TAG["id"] in ids.ravel():
            c = corners[list(ids.ravel()).index(TAG["id"])][0]
            ok, rvec, tvec = cv2.solvePnP(e.tag_obj_pts, c, self.K, None, flags=cv2.SOLVEPNP_IPPE_SQUARE)
            if ok:
                npos, nR = e.nominal_camera()
                detection = (npos + nR @ tvec.ravel(), nR @ cv2.Rodrigues(rvec)[0][:, 2])
        return e.update_tag(detection), c

    def state(self, q_sim, target, visible):
        e = self.env
        return tag_state(q_sim, target, e.tag_est, visible, e.seen, e.age)


def main():
    p = argparse.ArgumentParser()
    p.add_argument("checkpoint")
    p.add_argument("--dry-run", action="store_true", help="torque off: move the arm by hand, watch the numbers")
    p.add_argument("--speed", type=float, default=0.3, help="scale on every action (1.0 = as in simulation)")
    p.add_argument("--max-step-deg", type=float, default=5.0, help="max change of any joint target per step")
    p.add_argument("--episodes", type=int, default=5)
    p.add_argument("--move-speed", type=float, default=20.0, help="max joint speed for start/return moves, deg/s")
    args = p.parse_args()

    cfg = json.loads((Path(__file__).parent / "config.json").read_text())
    ck = torch.load(args.checkpoint, map_location="cpu")
    actor = Actor(ck["state_dim"], ck["action_dim"])
    actor.load_state_dict(ck["actor"])
    actor.eval()
    action_scale = np.array(ck["action_scale"])

    arm = SOArm(cfg["port"], ROOT / cfg["calibration"], cfg["baudrate"])
    mapping = SimMap(ROOT / cfg["sim_mapping"], arm.calibration)
    if arm.calibration is None or not mapping.ready:
        sys.exit("Run robot/calibrate.py (option 1) first.")
    cc, web = cfg["wrist_camera"], ck["webcam"]
    fovy = cc.get("vertical_fov_deg") or vfov(web["hfov"], web["width"], web["height"])
    if not cc.get("vertical_fov_deg"):
        print(f"NOTE: webcam field of view not measured; using the trained nominal {fovy:.1f} deg vertical")
    index = cc["index"]
    found = list_cameras()
    if index not in found and len(found) == 1:
        print(f"warning: camera {index} not found, using the only camera available ({found[0]})")
        index = found[0]
    cam = Camera(index, web["width"], web["height"], cc.get("fixed_exposure"))
    tracker = TagTracker(ck, fovy)
    model = tracker.env.model
    lo, hi = model.actuator_ctrlrange[:, 0], model.actuator_ctrlrange[:, 1]
    start, rest_pose = np.array(ck["start"]), sim_pose(model, "rest")
    max_step = np.radians(args.max_step_deg)

    arm.connect()
    arm.configure(**cfg.get("pid", {}))
    arm.torque(not args.dry_run)
    mover = Mirror(arm, mapping, args.move_speed)
    steps = int(ck["max_steps"] / min(1.0, args.speed))  # slower actions need more steps for the same motion
    try:
        if not args.dry_run:
            print("moving slowly to the look pose (webcam sees the placement area)...")
            mover.ease_to(start)
        for ep in range(args.episodes):
            input(f"\nEpisode {ep + 1}: place the tree 19-28 cm in front, tag facing the robot, then ENTER "
                  f"(Esc in the camera window ends the episode)")
            tracker.reset()
            # same scan as in training: hold still and try a few frames, else look left/right/up/down, come back
            for turn, tilt in ck.get("scan_turns", [(0.0, 0.0)]):
                if args.dry_run and (turn or tilt):
                    break  # torque off: the arm cannot look around by itself
                if turn or tilt:
                    goal = start.copy()
                    goal[0] += turn
                    goal[3] += tilt
                    mover.ease_to(goal, min_seconds=1.5)
                for _ in range(ck.get("scan_frames", 3)):
                    tracker.update(mapping.to_sim(arm.read_degrees()), cv2.cvtColor(cam.read(), cv2.COLOR_RGB2BGR))
                    if tracker.env.seen:
                        break
                if tracker.env.seen:
                    break
            print("tag found by the scan" if tracker.env.seen else "tag NOT found by the scan (policy starts blind)")
            if not args.dry_run and np.abs(mover.current - start).max() > 1e-3:
                mover.ease_to(start, min_seconds=1.5)
            target = mapping.to_sim(arm.read_degrees())
            for _ in range(steps):
                t0 = time.time()
                q = mapping.to_sim(arm.read_degrees())
                frame = cv2.cvtColor(cam.read(), cv2.COLOR_RGB2BGR)
                visible, corners = tracker.update(q, frame)
                state = tracker.state(q, target, visible)
                with torch.no_grad():
                    action = actor.act(torch.as_tensor(state[None]))[0].numpy()
                step = np.clip(action * action_scale * args.speed, -max_step, max_step)
                target = np.clip(target + step, lo, hi)
                if not args.dry_run:
                    arm.write_degrees(mapping.to_real(target))
                # live view: detection + what the policy is told
                if corners is not None:
                    cv2.polylines(frame, [corners.astype(int)], True, (0, 255, 0), 3)
                s = state
                txt = [f"tag: front {-s[13] * 100:5.1f} cm  side {s[12] * 100:+5.1f} cm  height {s[14] * 100:4.1f} cm"
                       if s[-2] else "tag: not seen yet",
                       f"visible now: {bool(s[-3])}   last seen {s[-1]:.1f} s ago   gripper action {action[5]:+.2f}"]
                for i, t in enumerate(txt):
                    cv2.putText(frame, t, (14, 40 + 38 * i), cv2.FONT_HERSHEY_SIMPLEX, 1.0, (0, 255, 255), 2,
                                cv2.LINE_AA)
                cv2.imshow("webcam - AprilTag policy (Esc ends episode)", cv2.resize(frame, (960, 540)))
                if cv2.waitKey(1) == 27:
                    break
                time.sleep(max(0, ck["control_dt"] - (time.time() - t0)))
            if not args.dry_run:
                print("episode done, returning slowly to the look pose")
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
