"""MuJoCo preview of the AprilTag lift task: viewer window + what the wrist and environment webcams see.

    python sim2real/view_tag_env.py                          # scene only (arm holds the look pose)
    python sim2real/view_tag_env.py runs/tag_.../actor_best.pt   # run a trained policy

Camera window: wrist webcam (left) and environment webcam across the table (right), 1280x720 each, with the
REAL OpenCV AprilTag detector run on them (green box = detected) and the policy's tag input.
Keys in the webcam window: n = new placement, space = pause, q = quit.
"""
import sys
import time

import cv2
import mujoco
import mujoco.viewer
import numpy as np
import torch

from tag_env import TAG, TagLiftEnv


def main():
    ck = torch.load(sys.argv[1], map_location="cpu") if len(sys.argv) > 1 else None
    env = TagLiftEnv("lego_tree", seed=int(time.time()), start=ck["start"] if ck else None)
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
    o, t, paused, info = env.reset(), 0, False, {"success": False}
    with mujoco.viewer.launch_passive(m, d) as viewer:
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
            for cam in (env.cam_id, env.env_cam):
                cam_r.update_scene(d, camera=cam)
                view = cv2.cvtColor(cam_r.render(), cv2.COLOR_RGB2BGR)
                corners, ids, _ = det.detectMarkers(cv2.cvtColor(view, cv2.COLOR_BGR2GRAY))
                if ids is not None:
                    cv2.aruco.drawDetectedMarkers(view, corners, ids, (0, 255, 0))
                found.append(ids.ravel().tolist() if ids is not None else [])
                views.append(view)
            for view, label in zip(views, ("WRIST camera", "ENVIRONMENT camera")):
                cv2.putText(view, label, (14, view.shape[0] - 24), cv2.FONT_HERSHEY_SIMPLEX, 1.2, (255, 255, 0), 3,
                            cv2.LINE_AA)
            img = np.hstack(views)
            s = o["state"]
            lines = [f"OpenCV detector: wrist sees {found[0] or 'no tag'}, environment sees {found[1] or 'no tag'}",
                     f"policy input: tag at front {-s[13] * 100:5.1f} cm, side {s[12] * 100:+5.1f} cm, "
                     f"height {s[14] * 100:4.1f} cm" if s[-2] else "policy input: tag not seen yet",
                     f"visible now: {bool(s[-3])}   step {t}/{env.max_steps}"
                     + ("   SUCCESS" if info["success"] else "") + ("   (no policy: holding pose)" if not actor else "")]
            for i, line in enumerate(lines):
                cv2.putText(img, line, (14, 36 + 34 * i), cv2.FONT_HERSHEY_SIMPLEX, 0.9, (0, 255, 255), 2, cv2.LINE_AA)
            cv2.imshow("webcams (simulated) - n new placement, space pause, q quit", cv2.resize(img, (1600, 450)))
            key = cv2.waitKey(1) & 0xFF
            if key == ord("q"):
                break
            if key == ord(" "):
                paused = not paused
            if key == ord("n"):
                o, t, info = env.reset(), 0, {"success": False}
            time.sleep(max(0, env.control_dt - (time.time() - t0)))
    cv2.destroyAllWindows()


if __name__ == "__main__":
    main()
