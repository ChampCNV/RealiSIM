"""Live check of both real cameras next to the simulation, to place them like in training.

    python robot/camera_check.py            # arm connected: torque OFF, the sim arm copies the real one
    python robot/camera_check.py --no-arm   # cameras only (sim arm stays at the look pose)

Top row: real wrist webcam | simulated wrist webcam. Bottom row: real environment camera | simulated one at
its planned spot (tree at the middle of the placement area). Every view runs the AprilTag detector: green box
+ ID, and a warning if a camera sees the wrong tag (wrist must see ID 0, environment camera ID 1).
Keys: b = blend real and sim environment views (to line the camera up), q = quit.
"""
import argparse
import json
import sys
from pathlib import Path

import cv2
import mujoco
import numpy as np

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "sim2real"))
from tag_env import ENV_CAMERA, TAG, TagLiftEnv, object_tags, ruler  # noqa: E402

from arm import SOArm  # noqa: E402
from camera import Camera, camera_indices  # noqa: E402
from sim_map import SimMap  # noqa: E402

TILE = (640, 360)


def detect(detector, img, expected):
    corners, ids, _ = detector.detectMarkers(cv2.cvtColor(img, cv2.COLOR_BGR2GRAY))
    found = [] if ids is None else ids.ravel().tolist()
    if ids is not None:
        cv2.aruco.drawDetectedMarkers(img, corners, ids, (0, 255, 0))
    if expected in found:
        i = found.index(expected)
        side = cv2.arcLength(corners[i], True) / 4
        return f"tag {expected} OK ({side:.0f} px)", (0, 255, 0)
    if found:
        return f"sees tag {found}, should be {expected}: turn the tree around", (0, 0, 255)
    return f"tag {expected} not seen", (0, 200, 255)


def label(img, lines):
    img = cv2.resize(img, TILE)
    for i, (text, colour) in enumerate(lines):
        cv2.putText(img, text, (10, 30 + 30 * i), cv2.FONT_HERSHEY_SIMPLEX, 0.75, colour, 2, cv2.LINE_AA)
    return img


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--no-arm", action="store_true")
    ap.add_argument("--object", default="lego_tree", help="whose tag IDs to look for")
    args = ap.parse_args()
    cfg = json.loads((Path(__file__).parent / "config.json").read_text())
    wc, ec = cfg["wrist_camera"], cfg["env_camera"]
    wrist_index, env_index = camera_indices(cfg)
    wrist = Camera(wrist_index, wc["width"], wc["height"], wc.get("fixed_exposure"))
    envcam = Camera(env_index, ec["width"], ec["height"], ec.get("fixed_exposure"))

    env = TagLiftEnv(args.object, domain_randomization=False)  # cameras + object at their planned spots
    front_id, back_id = object_tags(args.object)
    env.reset()
    m, d = env.model, env.data
    m.vis.global_.offwidth, m.vis.global_.offheight = 1280, 720
    renderer = mujoco.Renderer(m, 720, 1280)
    detector = cv2.aruco.ArucoDetector(cv2.aruco.getPredefinedDictionary(TAG["dictionary"]),
                                       cv2.aruco.DetectorParameters())
    arm = mapping = None
    if not args.no_arm:
        try:
            arm = SOArm(cfg["port"], ROOT / cfg["calibration"], cfg["baudrate"])
            mapping = SimMap(ROOT / cfg["sim_mapping"], arm.calibration)
            arm.connect()
            arm.torque(False)
        except Exception as err:  # cameras are still worth checking without the arm
            print(f"arm not connected ({err}); the sim arm stays at the look pose")
            arm = None
    e = ENV_CAMERA
    print(f"Place the environment camera {ruler(e['pos'])[0] * 100:.0f} cm in front of the robot's rotation axis, "
          f"{e['pos'][2] * 100:.0f} cm above the table, aimed at the tree spot ({ruler(e['target'])[0] * 100:.1f} cm "
          f"in front). Tree: tag ID {TAG['id']} toward the robot. Keys: b = blend, q = quit.")
    blend = False
    try:
        while True:
            if arm is not None:
                d.qpos[env.qadr] = mapping.to_sim(arm.read_degrees())
            mujoco.mj_forward(m, d)
            views = []
            for real, cam_id, expected, name in ((wrist.read(), env.cam_id, front_id, "WRIST"),
                                                 (envcam.read(), env.env_cam, back_id, "ENVIRONMENT")):
                real = cv2.cvtColor(real, cv2.COLOR_RGB2BGR)
                renderer.update_scene(d, camera=cam_id)
                sim = cv2.cvtColor(renderer.render(), cv2.COLOR_RGB2BGR)
                if blend and name == "ENVIRONMENT":
                    real = cv2.addWeighted(cv2.resize(real, (1280, 720)), 0.5, sim, 0.5, 0)
                status_r = detect(detector, real, expected)
                status_s = detect(detector, sim, expected)
                views.append(np.hstack([
                    label(real, [(f"REAL {name} (brightness {real.mean():.0f})", (255, 255, 0)), status_r]),
                    label(sim, [(f"SIM {name} (target)", (255, 255, 0)), status_s])]))
            cv2.imshow("cameras: real (left) vs sim (right) - b blend, q quit", np.vstack(views))
            key = cv2.waitKey(1) & 0xFF
            if key == ord("q"):
                break
            if key == ord("b"):
                blend = not blend
    finally:
        wrist.close()
        envcam.close()
        cv2.destroyAllWindows()
        if arm is not None:
            arm.disconnect(torque_off=True)


if __name__ == "__main__":
    main()
