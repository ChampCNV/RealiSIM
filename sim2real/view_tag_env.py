"""MuJoCo preview of the AprilTag lift task: viewer window + what the wrist and environment webcams see.

    python sim2real/view_tag_env.py                          # scene only (arm holds the look pose)
    python sim2real/view_tag_env.py runs/tag_.../actor_best.pt   # run a trained policy
    python sim2real/view_tag_env.py runs/tag_.../actor_last.pt --nominal   # cameras exactly at their planned
                                                                          # spots (to place the real ones)

Camera window: wrist webcam (left) and environment webcam across the table (right), 1280x720 each, with the
REAL OpenCV AprilTag detector run on them (green box = detected) and the policy's tag input.
Keys in the webcam window: n = new placement, space = pause, q = quit.
"""
import argparse
import time

import cv2
import mujoco
import mujoco.viewer
import numpy as np
import torch

from lift_env import start_pose
from tag_env import ENV_CAMERA, TAG, TagLiftEnv, ruler


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("checkpoint", nargs="?")
    ap.add_argument("--nominal", action="store_true", help="no randomization: cameras at their planned spots")
    ap.add_argument("--object", default="lego_tree", help="object to show when no checkpoint is given")
    ap.add_argument("--zero", action="store_true",
                    help="arm at its zero pose, object at the middle of the placement area facing the robot")
    ap.add_argument("--real-camera", action="store_true",
                    help="environment camera where the real one is calibrated (robot/config.json), shown alone")
    args = ap.parse_args()
    ck = torch.load(args.checkpoint, map_location="cpu", weights_only=False) if args.checkpoint else None
    env = TagLiftEnv(ck.get("object", "lego_tree") if ck else args.object, seed=int(time.time()),
                     start=ck["start"] if ck else None, area=ck.get("area") if ck else None,
                     domain_randomization=not args.nominal)
    if args.zero:  # the real robot's resting start: middle of every joint's range; object centred, facing it
        env.start = list(start_pose(env.model, "zero"))
        env.spawn_half, env.face_yaw = np.zeros(2), 0.0
    if args.nominal:
        e = ENV_CAMERA
        cf, cs, ch = ruler(e["pos"])
        tf, _, th = ruler(e["target"])
        print(f"environment camera: {cf * 100:.0f} cm in front of the rotation axis, "
              f"{cs * 100:+.0f} cm sideways, {ch * 100:.0f} cm above the table, aimed at "
              f"{tf * 100:.1f} cm in front, {th * 100:.0f} cm high "
              f"(training covers +-{e['pos_noise'][0] * 100:.0f} cm sideways, +-{e['pos_noise'][1] * 100:.0f} cm "
              f"front/back, +-{e['pos_noise'][2] * 100:.0f} cm height)")
    actor = None
    if ck:
        from train_tag import Actor
        actor = Actor(ck["state_dim"], ck["action_dim"])
        actor.load_state_dict(ck["actor"])
        actor.eval()
    m, d = env.model, env.data
    m.vis.global_.offwidth, m.vis.global_.offheight = env.cam_w, env.cam_h
    cam_r = mujoco.Renderer(m, env.cam_h, env.cam_w)
    det = cv2.aruco.ArucoDetector(cv2.aruco.getPredefinedDictionary(TAG["dictionary"]),
                                  cv2.aruco.DetectorParameters())
    def place_real_camera():
        """Put the sim environment camera where calibrate_env_camera.py found the real one."""
        import json
        from pathlib import Path
        ec = json.loads((Path(__file__).resolve().parent.parent / "robot" / "config.json").read_text())["env_camera"]
        R = np.array(ec["R"]) @ np.diag([1, -1, -1])  # OpenCV camera axes -> MuJoCo camera axes
        q = np.zeros(4)
        mujoco.mju_mat2Quat(q, R.ravel())
        m.cam_pos[env.env_cam], m.cam_quat[env.env_cam], m.cam_fovy[env.env_cam] = ec["pos"], q, ec["fovy_used"]

    def reset():
        o = env.reset()
        if args.real_camera:
            place_real_camera()
        return o

    o, t, paused, info = reset(), 0, False, {"success": False}
    with mujoco.viewer.launch_passive(m, d) as viewer:
        viewer.opt.flags[mujoco.mjtVisFlag.mjVIS_CAMERA] = 1  # draw where the cameras are
        while viewer.is_running():
            t0 = time.time()
            if not paused and t < env.max_steps:
                if actor is not None:
                    with torch.no_grad():
                        a = actor.act(torch.as_tensor(o["state"][None]))[0].numpy()
                else:
                    a = np.zeros(env.action_dim)  # hold the look pose
                o, _, _, _, info = env.step(a)
                t += 1
            viewer.sync()
            views, found = [], []
            for cam in ((env.env_cam,) if args.real_camera else (env.cam_id, env.env_cam)):
                cam_r.update_scene(d, camera=cam)
                view = cv2.cvtColor(cam_r.render(), cv2.COLOR_RGB2BGR)
                corners, ids, _ = det.detectMarkers(cv2.cvtColor(view, cv2.COLOR_BGR2GRAY))
                if ids is not None:
                    cv2.aruco.drawDetectedMarkers(view, corners, ids, (0, 255, 0))
                found.append(ids.ravel().tolist() if ids is not None else [])
                views.append(view)
            for view, label in zip(views, ("ENVIRONMENT camera",) if args.real_camera else
                                   ("WRIST camera", "ENVIRONMENT camera")):
                cv2.putText(view, label, (14, view.shape[0] - 24), cv2.FONT_HERSHEY_SIMPLEX, 1.2, (255, 255, 0), 3,
                            cv2.LINE_AA)
            img = np.hstack(views)
            s = o["state"]
            f, sd, h = ruler(s[12:15])
            lines = [f"OpenCV detector sees tags {found[-1] or 'none'}" if args.real_camera else
                     f"OpenCV detector: wrist sees {found[0] or 'no tag'}, environment sees {found[1] or 'no tag'}",
                     f"policy input: tag at front {f * 100:5.1f} cm, side {sd * 100:+5.1f} cm, "
                     f"height {h * 100:4.1f} cm" if s[-2] else "policy input: tag not seen yet",
                     f"visible now: {bool(s[-3])}   step {t}/{env.max_steps}"
                     + ("   SUCCESS" if info["success"] else "") + ("   (no policy: holding pose)" if not actor else "")]
            for i, line in enumerate(lines):
                cv2.putText(img, line, (14, 36 + 34 * i), cv2.FONT_HERSHEY_SIMPLEX, 0.9, (0, 255, 255), 2, cv2.LINE_AA)
            size = (960, 540) if args.real_camera else (1600, 450)
            cv2.imshow("webcams (simulated) - n new placement, space pause, q quit", cv2.resize(img, size))
            key = cv2.waitKey(1) & 0xFF
            if key == ord("q"):
                break
            if key == ord(" "):
                paused = not paused
            if key == ord("n"):
                o, t, info = reset(), 0, {"success": False}
            time.sleep(max(0, env.control_dt - (time.time() - t0)))
    cv2.destroyAllWindows()


if __name__ == "__main__":
    main()
