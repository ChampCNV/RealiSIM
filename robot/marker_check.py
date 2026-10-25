"""Live ArUco check: where is the marker (on the Lego tree) relative to the robot base?

    .\\.venv\\Scripts\\python.exe robot\\marker_check.py

Torque stays OFF. The real arm drives the sim (calibrated joints -> sim pose -> wrist camera pose), the wrist
camera finds the marker, and the marker's pose in the robot frame is shown live:
  - camera window: detection + numbers (cm from the robot base; "front" = the direction the arm reaches)
  - sim window: the arm copying the real one, a black plate where the marker was found
Check: put the tree at a spot you measured with a ruler and compare. Keys in the camera window: q quit.
"""
import argparse
import json
import sys
import time
from pathlib import Path

import cv2
import mujoco
import mujoco.viewer
import numpy as np

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "sim2real"))
from lift_env import ARM_SCENE, WRIST_POS, WRIST_TARGET, look_at  # noqa: E402
from arm import SOArm  # noqa: E402
from camera import SIM_WRIST_FOVY, Camera, list_cameras  # noqa: E402
from sim_map import SimMap, require_arm_model  # noqa: E402

MARKER_DICT = cv2.aruco.DICT_ARUCO_ORIGINAL
MARKER_ID = 0
MARKER_SIZE = 0.054  # black square side, metres


def build_sim():
    """SO-100 + the policy's wrist camera + a mocap plate that shows the detected marker."""
    scene = mujoco.MjSpec.from_file(str(ARM_SCENE))
    scene.body("Fixed_Jaw").add_camera(name="wrist", pos=WRIST_POS, fovy=SIM_WRIST_FOVY,
                                       xyaxes=look_at(WRIST_POS, WRIST_TARGET, up=(0, 0, 1)))
    plate = scene.worldbody.add_body(name="marker", mocap=True, pos=[0, 0, -1])
    plate.add_geom(type=mujoco.mjtGeom.mjGEOM_BOX, size=[MARKER_SIZE / 2, MARKER_SIZE / 2, 0.001],
                   rgba=[0.05, 0.05, 0.05, 1], contype=0, conaffinity=0)
    plate.add_geom(type=mujoco.mjtGeom.mjGEOM_SPHERE, size=[0.006], pos=[0, 0, 0.004], rgba=[1, 0.2, 0.2, 1],
                   contype=0, conaffinity=0)  # red dot on the marker's front face
    model = scene.compile()
    return model, mujoco.MjData(model)


def intrinsics(w, h, fovy_deg):
    f = (h / 2) / np.tan(np.radians(fovy_deg) / 2)
    return np.array([[f, 0, w / 2], [0, f, h / 2], [0, 0, 1]])


def camera_pose(model, data):
    """World position and OpenCV-convention rotation (x right, y down, z forward) of the sim wrist camera."""
    cid = model.camera("wrist").id
    R_mj = data.cam_xmat[cid].reshape(3, 3)  # MuJoCo camera: x right, y up, looking along -z
    return data.cam_xpos[cid].copy(), R_mj @ np.diag([1, -1, -1])


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--fovy", type=float, help="real camera vertical field of view (deg); default from config")
    args = ap.parse_args()
    cfg = json.loads((Path(__file__).parent / "config.json").read_text())
    cc = cfg["wrist_camera"]
    fovy = args.fovy or cc.get("vertical_fov_deg") or SIM_WRIST_FOVY
    if not (args.fovy or cc.get("vertical_fov_deg")):
        print(f"NOTE: real camera field of view unknown, assuming {fovy} deg (distances scale with this)")

    require_arm_model("so100", "this script")
    arm = SOArm(cfg["port"], ROOT / cfg["calibration"], cfg["baudrate"])
    if arm.calibration is None:
        raise SystemExit("Calibrate first: python robot\\calibrate.py")
    mapping = SimMap(ROOT / cfg["sim_mapping"], arm.calibration)
    index = cc["index"] if cc["index"] in list_cameras() else (list_cameras() or [None])[0]
    if index is None:
        raise SystemExit("no camera found")
    cam = Camera(index, cc["width"], cc["height"], cc.get("fixed_exposure"))
    detector = cv2.aruco.ArucoDetector(cv2.aruco.getPredefinedDictionary(MARKER_DICT),
                                       cv2.aruco.DetectorParameters())
    s = MARKER_SIZE / 2
    obj_pts = np.array([[-s, s, 0], [s, s, 0], [s, -s, 0], [-s, -s, 0]], np.float32)  # OpenCV corner order

    model, data = build_sim()
    arm.connect()
    arm.torque(False)
    last_print = 0.0
    try:
        with mujoco.viewer.launch_passive(model, data) as viewer:
            while viewer.is_running():
                data.qpos[:6] = mapping.to_sim(arm.read_degrees())
                mujoco.mj_forward(model, data)
                frame = cam.read()
                h, w = frame.shape[:2]
                K = intrinsics(w, h, fovy)
                bgr = cv2.cvtColor(frame, cv2.COLOR_RGB2BGR)
                corners, ids, _ = detector.detectMarkers(cv2.cvtColor(frame, cv2.COLOR_RGB2GRAY))
                lines = ["marker not found"]
                if ids is not None and MARKER_ID in ids.ravel():
                    c = corners[list(ids.ravel()).index(MARKER_ID)][0]
                    ok, rvec, tvec = cv2.solvePnP(obj_pts, c, K, None, flags=cv2.SOLVEPNP_IPPE_SQUARE)
                    if ok:
                        cv2.drawFrameAxes(bgr, K, None, rvec, tvec, MARKER_SIZE / 2)
                        cpos, R_cv = camera_pose(model, data)
                        R_m = cv2.Rodrigues(rvec)[0]
                        pos = cpos + R_cv @ tvec.ravel()  # marker centre, robot frame (m)
                        normal = R_cv @ R_m[:, 2]  # out of the marker's printed face
                        R_world = R_cv @ R_m
                        data.mocap_pos[0] = pos
                        mujoco.mju_mat2Quat(data.mocap_quat[0], R_world.ravel())
                        facing = np.degrees(np.arctan2(normal[1], normal[0]))
                        tilt = np.degrees(np.arcsin(np.clip(abs(normal[2]), 0, 1)))
                        lines = [f"front {-pos[1] * 100:5.1f} cm   side {pos[0] * 100:+5.1f} cm",
                                 f"height {pos[2] * 100:4.1f} cm   camera dist {np.linalg.norm(tvec) * 100:4.1f} cm",
                                 f"marker faces {facing:+4.0f} deg  (tilt {tilt:3.0f} deg)"]
                else:
                    data.mocap_pos[0] = [0, 0, -1]
                if time.time() - last_print > 1:  # once a second in the terminal too
                    print(" | ".join(lines), flush=True)
                    last_print = time.time()
                viewer.sync()
                for i, t in enumerate(lines):
                    cv2.putText(bgr, t, (10, 28 + 26 * i), cv2.FONT_HERSHEY_SIMPLEX, 0.7, (0, 255, 255), 2,
                                cv2.LINE_AA)
                cv2.imshow("wrist camera - marker check (q quits)", bgr)
                if cv2.waitKey(1) & 0xFF == ord("q"):
                    break
    finally:
        cam.close()
        cv2.destroyAllWindows()
        arm.disconnect(torque_off=True)


if __name__ == "__main__":
    main()
