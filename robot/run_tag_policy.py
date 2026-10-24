"""Run the AprilTag lift policy on the real SO-100 (wrist webcam + optional environment camera).

    python robot/run_tag_policy.py runs/tag_.../actor_last.pt --dry-run     # torque OFF: watch the numbers
    python robot/run_tag_policy.py runs/tag_.../actor_last.pt --speed 0.3   # real run, slow
    python robot/run_tag_policy.py runs/tag_.../actor_last.pt --env-only    # environment camera only

Every 0.1 s, exactly as in training (tag_env.py):
  camera frames -> AprilTag detector -> solvePnP (printed tag size + lens) -> tag position in the robot frame
  (wrist camera: from the arm's joint angles; environment camera: from its calibration, tag ID 1 on the back
  turned into where tag ID 0 is) -> tag_state() -> policy -> small joint-target change.
Between attempts: ENTER starts, "c" re-calibrates the environment camera (touch mode, the arm goes limp),
"q" quits. While running it compares the two cameras when both see the object and warns if they disagree
(the camera was probably moved: press "c" before the next attempt).
Safety: slow eased moves, --speed scales every action, per-step limit, the gripper tip is kept above the table
and within reach, the wrist camera is kept away from the arm (every eased move follows a route checked in the
model: from zero first a little forward and up; policy targets that bring it too close are held, as in
training), Ctrl+C folds the arm to SAFE_REST (camera turned away) and turns torque off. Every attempt is logged
to runs/real_logs/.
"""
import argparse
import collections
import csv
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
from tag_env import (MIN_CAMERA_CLEARANCE, OLD_POLICY_CLEARANCE, SAFE_REST, TAG, TagLiftEnv,  # noqa: E402
                     back_to_front, intrinsics, object_tags, pose_clearance, ruler, safe_route, tag_state, vfov)
from train_tag import Actor  # noqa: E402

from arm import SOArm  # noqa: E402
from calibrate_env_camera import CONFIG, touch_calibrate  # noqa: E402
from camera import Camera, list_cameras  # noqa: E402
from sim_map import SimMap  # noqa: E402
from sim_mirror import Mirror  # noqa: E402

DRIFT_WARN = 0.025  # m: wrist and environment camera disagree by more than this -> camera probably moved
MIN_TIP_HEIGHT, MAX_REACH = 0.005, 0.40  # m: gripper tip above the table, horizontally within reach of the axis


class TagTracker:
    """Real-robot version of the simulated detection: same maths, same memory, same policy input."""

    def __init__(self, ck, cfg, wrist_fovy):
        self.ck = ck
        self.env = TagLiftEnv(ck.get("object", "lego_tree"), domain_randomization=False, start=ck["start"],
                              area=ck.get("area"))  # model for kinematics, same object + placement area
        self.env.calib_pos, self.env.calib_rot = np.zeros(3), np.eye(3)
        self.env.min_camera_clearance = ck.get("min_camera_clearance", OLD_POLICY_CLEARANCE)  # as in its training
        self.K = intrinsics(wrist_fovy, ck["webcam"]["width"], ck["webcam"]["height"])
        self.depth = self.env.tag_depth  # this object's thickness between its tags (from its asset)
        self.front_id, self.back_id = object_tags(ck.get("object", "lego_tree"))  # IDs glued on this object
        self.tips = [self.env.model.geom(g).id for g in ("fixed_jaw_pad_1", "moving_jaw_pad_1")]
        self.detector = cv2.aruco.ArucoDetector(cv2.aruco.getPredefinedDictionary(TAG["dictionary"]),
                                                cv2.aruco.DetectorParameters())
        self.load_calibration(cfg)
        self.reset()

    def load_calibration(self, cfg):
        """Printed tag size + environment camera pose (after calibrate_env_camera / pressing "c")."""
        self.obj = self.env.tag_obj_pts * (cfg.get("tag_mm", TAG["size"] * 1000) / (TAG["size"] * 1000))
        ec = cfg.get("env_camera") or {}
        self.env_cal = "pos" in ec
        if self.env_cal:
            self.K_env = intrinsics(ec["fovy_used"], ec["width"], ec["height"])
            self.env_pos, self.env_R = np.array(ec["pos"]), np.array(ec["R"])

    def reset(self):
        self.env.tag_est, self.env.seen, self.env.age = np.zeros(5), False, 0

    def find(self, frame_bgr, tag_id, K):
        """(corners, rvec, tvec) of tag_id in this frame, or None."""
        corners, ids, _ = self.detector.detectMarkers(cv2.cvtColor(frame_bgr, cv2.COLOR_BGR2GRAY))
        if ids is None or tag_id not in ids.ravel():
            return None
        c = corners[list(ids.ravel()).index(tag_id)][0]
        ok, rvec, tvec = cv2.solvePnP(self.obj, c, K, None, flags=cv2.SOLVEPNP_IPPE_SQUARE)
        return (c, rvec, tvec) if ok else None

    def update(self, q_sim, wrist_bgr, env_bgr=None):
        """Joint angles (sim radians) + frames -> dict(visible, source, wrist/env corners, drift). The wrist
        camera's reading wins when both see a tag, as in training; drift = how far the two readings differ."""
        e = self.env
        e.data.qpos[e.qadr] = q_sim
        mujoco.mj_kinematics(e.model, e.data)
        wrist_det = env_det = wc = ec = None
        found = self.find(wrist_bgr, self.front_id, self.K) \
            if wrist_bgr is not None and self.front_id is not None else None
        if found:
            wc, rvec, tvec = found
            npos, nR = e.nominal_camera()
            wrist_det = (npos + nR @ tvec.ravel(), nR @ cv2.Rodrigues(rvec)[0][:, 2])
        if env_bgr is not None and self.env_cal and self.back_id is not None:
            found = self.find(env_bgr, self.back_id, self.K_env)
            if found:
                ec, rvec, tvec = found
                back = (self.env_pos + self.env_R @ tvec.ravel(), self.env_R @ cv2.Rodrigues(rvec)[0][:, 2])
                env_det = back_to_front(*back, self.depth)
        detection = wrist_det or env_det
        drift = np.linalg.norm(wrist_det[0] - env_det[0]) if wrist_det and env_det else None
        return dict(visible=e.update_tag(detection), source="wrist" if wrist_det else "env" if env_det else "-",
                    wrist_corners=wc, env_corners=ec, drift=drift)

    def state(self, q_sim, target, visible):
        e = self.env
        return tag_state(q_sim, target, e.tag_est, visible, e.seen, e.age)

    def safe(self, target):
        """Is the gripper tip for these joint targets above the table and within reach, and the wrist camera
        clear of the arm?"""
        d = self.env.data
        keep = d.qpos.copy()
        d.qpos[self.env.qadr] = target
        mujoco.mj_kinematics(self.env.model, d)
        tip = d.geom_xpos[self.tips].mean(0)
        d.qpos[:] = keep
        front, side, height = ruler(tip)
        return height > MIN_TIP_HEIGHT and np.hypot(front, side) < MAX_REACH and self.env.target_ok(target)


def main():
    p = argparse.ArgumentParser()
    p.add_argument("checkpoint")
    p.add_argument("--dry-run", action="store_true", help="torque off: move the arm by hand, watch the numbers")
    p.add_argument("--speed", type=float, default=0.3, help="scale on every action (1.0 = as in simulation)")
    p.add_argument("--max-step-deg", type=float, default=5.0, help="max change of any joint target per step")
    p.add_argument("--episodes", type=int, default=20)
    p.add_argument("--env-only", action="store_true", help="use only the environment camera")
    p.add_argument("--move-speed", type=float, default=20.0, help="max joint speed for start/return moves, deg/s")
    args = p.parse_args()

    cfg = json.loads(CONFIG.read_text())
    ck = torch.load(args.checkpoint, map_location="cpu", weights_only=False)
    actor = Actor(ck["state_dim"], ck["action_dim"])
    actor.load_state_dict(ck["actor"])
    actor.eval()
    action_scale = np.array(ck["action_scale"])
    object_name = ck.get("object", "lego_tree")

    arm = SOArm(cfg["port"], ROOT / cfg["calibration"], cfg["baudrate"])
    mapping = SimMap(ROOT / cfg["sim_mapping"], arm.calibration)
    if arm.calibration is None or not mapping.ready:
        sys.exit("The arm is not calibrated yet: python photo2grasp.py robot")
    cc, web = cfg["wrist_camera"], ck["webcam"]
    wrist_fovy = cc.get("vertical_fov_deg") or vfov(web["hfov"], web["width"], web["height"])
    index = cc["index"]
    found = list_cameras()
    if index not in found and len(found) == 1:
        print(f"warning: camera {index} not found, using the only camera available ({found[0]})")
        index = found[0]
    cam = Camera(index, web["width"], web["height"], cc.get("fixed_exposure"))
    ec = cfg.get("env_camera")
    env_cam = Camera(ec["index"], ec["width"], ec["height"], ec.get("fixed_exposure")) \
        if ec and "env_camera" in ck else None
    tracker = TagTracker(ck, cfg, wrist_fovy)
    if env_cam and not tracker.env_cal:
        print("environment camera not calibrated yet: press 'c' before the first attempt")
    if args.env_only and not env_cam:
        sys.exit("--env-only needs the environment camera (robot/config.json env_camera)")

    def read_wrist():
        frame = cv2.cvtColor(cam.read(), cv2.COLOR_RGB2BGR)
        return None if args.env_only else frame, frame  # (what the tracker gets, what the window shows)

    def read_env():
        return cv2.cvtColor(env_cam.read(), cv2.COLOR_RGB2BGR) if env_cam else None

    model = tracker.env.model
    lo, hi = model.actuator_ctrlrange[:, 0], model.actuator_ctrlrange[:, 1]
    start, rest_pose = np.array(ck["start"]), np.array(SAFE_REST)
    clearance = pose_clearance(model, tracker.env.check_data, start)
    if clearance < MIN_CAMERA_CLEARANCE:
        print(f"warning: this model's look pose leaves the wrist camera only {clearance * 100:.1f} cm from the arm "
              f"(new trainings keep >= {MIN_CAMERA_CLEARANCE * 100:.0f} cm); watch the camera")
    max_step = np.radians(args.max_step_deg)
    a = tracker.env.area
    log_dir = ROOT / "runs" / "real_logs" / time.strftime("%Y%m%d_%H%M%S")
    log_dir.mkdir(parents=True, exist_ok=True)

    arm.connect()
    arm.configure(**cfg.get("pid", {}))
    arm.torque(not args.dry_run)
    mover = Mirror(arm, mapping, args.move_speed)

    def torque_on_here():
        """Torque on without a jump: the servos' goal becomes where the arm is now (they move to their old goal
        otherwise)."""
        now = arm.read_degrees()
        arm.write_degrees(now)
        arm.torque(True)
        mover.current = mapping.to_sim(now)

    def move(goal, min_seconds=3.0, optional=False):
        """Eased move to `goal` along a route that keeps the wrist camera clear of the arm (checked in the model).
        No such route: optional moves are skipped (returns False), others ask for a hand move first."""
        while True:
            route = safe_route(model, tracker.env.check_data, mover.current, goal)
            if route is not None:
                break
            if optional:
                return False
            print("no move from here keeps the wrist camera clear of the arm. The arm goes limp: hold it, move it "
                  "by hand towards its zero pose (upright, camera away from the arm), keep holding it")
            arm.torque(False)
            input("then press ENTER (torque comes back on where the arm is): ")
            torque_on_here()
        if len(route) > 1:
            print(f"   camera-safe route: {len(route)} moves")
        for point in route:
            mover.ease_to(point, min_seconds)
        return True

    steps = int(ck["max_steps"] / min(1.0, args.speed))  # slower actions need more steps for the same motion
    drift = collections.deque(maxlen=30)

    def recalibrate():
        if env_cam is None:
            print("no environment camera configured")
            return
        print("RE-CALIBRATING the environment camera: the arm goes limp; touch tag 0 with the gripper tip at "
              "4 spots (SPACE at each)")
        if not args.dry_run:
            mover.current = mapping.to_sim(arm.read_degrees())
            move(start)
        arm.torque(False)
        if touch_calibrate(env_cam, arm, mapping, cfg, object_name):
            tracker.load_calibration(cfg)
            drift.clear()
        cv2.destroyAllWindows()
        if not args.dry_run:
            torque_on_here()
            move(start)

    try:
        if not args.dry_run:
            print("moving slowly to the look pose (webcam sees the placement area)...")
            move(start)
        for ep in range(args.episodes):
            while True:
                key = input(f"\nAttempt {ep + 1}: place the {object_name} {a['front_min'] * 100:.0f}-"
                            f"{a['front_max'] * 100:.0f} cm in front of the rotation axis, +-{a['side'] * 100:.0f} cm "
                            f"sideways, tag ID 0 facing the robot. ENTER = go | c = re-calibrate camera | q = quit: ")
                if key.strip().lower() == "c":
                    recalibrate()
                    continue
                break
            if key.strip().lower() == "q":
                break
            tracker.reset()
            # same scan as in training: hold still and try a few frames, else look left/right/up/down, come back
            for turn, tilt in ck.get("scan_turns", [(0.0, 0.0)]):
                if (args.dry_run or args.env_only) and (turn or tilt):  # torque off / fixed camera: no looking
                    break
                if turn or tilt:
                    goal = start.copy()
                    goal[0] += turn
                    goal[3] += tilt
                    if not move(goal, min_seconds=1.5, optional=True):
                        continue  # this look direction would bring the camera too close to the arm
                for _ in range(ck.get("scan_frames", 3)):
                    tracker.update(mapping.to_sim(arm.read_degrees()), read_wrist()[0], read_env())
                    if tracker.env.seen:
                        break
                if tracker.env.seen:
                    break
            print("tag found by the scan" if tracker.env.seen else "tag NOT found by the scan (policy starts blind)")
            if not args.dry_run and np.abs(mover.current - start).max() > 1e-3:
                move(start, min_seconds=1.5)
            target = mapping.to_sim(arm.read_degrees())
            log = csv.writer(open(log_dir / f"attempt_{ep + 1}.csv", "w", newline=""))
            log.writerow(["t"] + [f"q{i}" for i in range(6)] + [f"target{i}" for i in range(6)]
                         + ["tag_x", "tag_y", "tag_z", "visible", "source", "drift_m", "safety_hold"]
                         + [f"action{i}" for i in range(6)])
            holds, t_start = 0, time.time()
            for _ in range(steps):
                t0 = time.time()
                q = mapping.to_sim(arm.read_degrees())
                wrist_in, frame = read_wrist()
                env_frame = read_env()
                seen = tracker.update(q, wrist_in, env_frame)
                if seen["drift"] is not None:
                    drift.append(seen["drift"])
                state = tracker.state(q, target, seen["visible"])
                with torch.no_grad():
                    action = actor.act(torch.as_tensor(state[None]))[0].numpy()
                step = np.clip(action * action_scale * args.speed, -max_step, max_step)
                new_target = np.clip(target + step, lo, hi)
                hold = not tracker.safe(new_target)
                if hold:
                    holds += 1  # keep the previous (safe) target
                else:
                    target = new_target
                if not args.dry_run:
                    arm.write_degrees(mapping.to_real(target))
                log.writerow([round(t0 - t_start, 3), *np.round(q, 4), *np.round(target, 4), *np.round(state[12:15], 4),
                              int(seen["visible"]), seen["source"],
                              "" if seen["drift"] is None else round(seen["drift"], 4), int(hold), *np.round(action, 3)])
                # live view: detection + what the policy is told
                if seen["wrist_corners"] is not None:
                    cv2.polylines(frame, [seen["wrist_corners"].astype(int)], True, (0, 255, 0), 3)
                f, sd, h = ruler(state[12:15])
                txt = [f"tag: front {f * 100:5.1f} cm  side {sd * 100:+5.1f} cm  height {h * 100:4.1f} cm  ({seen['source']})"
                       if state[-2] else "tag: not seen yet",
                       f"last seen {state[-1]:.1f} s ago   gripper action {action[5]:+.2f}"]
                if len(drift) >= 8 and np.median(drift) > DRIFT_WARN:
                    txt.append(f"CAMERAS DISAGREE by {np.median(drift) * 100:.1f} cm: camera moved? press c next")
                for i, t in enumerate(txt):
                    colour = (0, 0, 255) if t.startswith("CAMERAS") else (0, 255, 255)
                    cv2.putText(frame, t, (14, 40 + 38 * i), cv2.FONT_HERSHEY_SIMPLEX, 1.0, colour, 2, cv2.LINE_AA)
                view = cv2.resize(frame, (960, 540))
                if env_frame is not None:
                    if seen["env_corners"] is not None:
                        cv2.polylines(env_frame, [seen["env_corners"].astype(int)], True, (0, 255, 0), 3)
                    view = np.hstack([view, cv2.resize(env_frame, (960, 540))])
                cv2.imshow("wrist | environment camera - AprilTag policy (Esc ends the attempt)", view)
                if cv2.waitKey(1) == 27:
                    break
                time.sleep(max(0, ck["control_dt"] - (time.time() - t0)))
            if holds:
                print(f"safety: held {holds} moves that would have put the gripper into the table / out of reach / "
                      f"the camera against the arm")
            if len(drift) >= 8 and np.median(drift) > DRIFT_WARN:
                print(f"the two cameras disagree by {np.median(drift) * 100:.1f} cm: press 'c' to re-calibrate")
            if not args.dry_run:
                print("attempt done, returning slowly to the look pose")
                mover.current = target
                move(start)
        print(f"logs: {log_dir}")
    except KeyboardInterrupt:
        print("\nstopped by user")
    finally:
        if not args.dry_run:
            print("folding the arm to rest (camera turned away from the arm), then torque off...")
            mover.current = mapping.to_sim(arm.read_degrees())
            move(rest_pose)
        arm.disconnect(torque_off=True)
        cam.close()
        if env_cam:
            env_cam.close()
        cv2.destroyAllWindows()


if __name__ == "__main__":
    main()
