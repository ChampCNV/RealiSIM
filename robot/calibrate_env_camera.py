"""Find where the environment camera is, from the tag on the back of the object (once per camera placement).

    python robot/calibrate_env_camera.py                     # TOUCH mode (no ruler): the arm measures
    python robot/calibrate_env_camera.py --ruler             # 4 spots measured with a ruler instead
    python robot/calibrate_env_camera.py --object celsius_can

TOUCH mode (torque off): put the object anywhere in the placement area (tag ID 0 facing the robot, object
straight), close the gripper and touch its tip to the CENTRE of tag ID 0, press SPACE, keep still ~2 s. Do this
at 4 spots spread over the area (near, far, left, right). The arm's joint angles give where tag 0 is (forward
kinematics, the same the policy uses) while the environment camera sees tag ID 1 on the back.
From the 4 spots it finds, all automatically:
  - the camera's position and rotation relative to the robot (all 16 tag corners in one solve)
  - the REAL printed tag size (printers often shrink pages): the size at which the camera's own distance
    readings agree with the arm's
  - how the tag was turned when glued on (the corners are taken in picture order)
Saved to robot/config.json (env_camera + tag_mm). Afterwards it shows live where the camera thinks the object
is. A MuJoCo window shows it all: the sim arm copies the real arm, the object sits under the gripper tip
while you touch, and afterwards the camera is drawn at its measured spot with the object where it sees it.
touch_calibrate() is also called by run_tag_policy.py when you press "c" between attempts.

RULER mode: the object at 4 measured spots; "front" = from the base's rotation axis to the face of tag ID 0,
"side" = + to the robot's left as seen from behind it.
"""
import argparse
import json
import sys
from pathlib import Path

import cv2
import mujoco
import mujoco.viewer
import numpy as np

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "sim2real"))
from tag_env import (ENV_CAMERA, TAG, TagLiftEnv, back_to_front, from_ruler, intrinsics, object_tags,  # noqa: E402
                     ruler, vfov)

from arm import SOArm  # noqa: E402
from camera import Camera  # noqa: E402
from sim_map import SimMap  # noqa: E402

CONFIG = Path(__file__).parent / "config.json"
# back tag frame (OpenCV marker axes: x right, y up, z out of the face) seen from behind the object, in the
# robot frame: right = +x, up = +z, out = -y
R_WORLD_BACK_TAG = np.array([[1, 0, 0], [0, 0, 1], [0, -1, 0]], float).T
SPOTS = [(18, 0), (28, 0), (23, -7), (23, 7)]  # ruler mode (front, side) cm: near, far, right, left
WINDOW = "environment camera calibration (q quits)"


def upright(corners):
    """Detected corners re-started at the image top-left (same clockwise order), so it does not matter how the
    tag was turned when glued on: the environment camera stands upright and the object too."""
    return np.roll(corners, -int(np.argmin(corners.sum(1))), axis=0)


def camera_intrinsics(ec):
    fovy = ec.get("vertical_fov_deg") or vfov(ENV_CAMERA["hfov"], ec["width"], ec["height"])
    return intrinsics(fovy, ec["width"], ec["height"]), fovy


def tag_obj_pts(tag_mm=TAG["size"] * 1000):
    s = tag_mm / 2000
    return np.array([[-s, s, 0], [s, s, 0], [s, -s, 0], [-s, -s, 0]], np.float32)  # OpenCV corner order


class BackTagFinder:
    """Finds the object's camera-side tag (its ID from objects.json) and returns its corners in picture order."""

    def __init__(self, tag_id):
        self.tag_id = tag_id
        self.detector = cv2.aruco.ArucoDetector(cv2.aruco.getPredefinedDictionary(TAG["dictionary"]),
                                                cv2.aruco.DetectorParameters())

    def __call__(self, frame):
        corners, ids, _ = self.detector.detectMarkers(cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY))
        if ids is not None:
            cv2.aruco.drawDetectedMarkers(frame, corners, ids, (0, 255, 0))
            if self.tag_id in ids.ravel():
                return upright(corners[list(ids.ravel()).index(self.tag_id)][0])
        return None


class SimView:
    """MuJoCo window next to the calibration: the sim arm copies the real one (torque off), the object is drawn
    where the robot thinks it is (under the gripper tip while touching, then where the camera sees it) and,
    once calibrated, the environment camera at its measured spot."""

    def __init__(self, sim, arm=None, mapping=None):
        self.sim, self.arm, self.mapping = sim, arm, mapping
        self.local = sim.model.geom_pos[sim.tag_geom].copy()  # robot-side tag centre in the object's frame
        self.viewer = mujoco.viewer.launch_passive(sim.model, sim.data)
        self.viewer.opt.flags[mujoco.mjtVisFlag.mjVIS_CAMERA] = 1

    def show(self, face=None, yaw=0.0, camera=None):
        """face: robot-side tag centre (robot frame); yaw: which way the object faces (0 = toward the robot);
        camera: (position, OpenCV rotation) of the environment camera."""
        m, d, sim = self.sim.model, self.sim.data, self.sim
        if self.arm is not None:
            d.qpos[sim.qadr] = self.mapping.to_sim(self.arm.read_degrees())
        if face is not None:
            c, s_ = np.cos(yaw), np.sin(yaw)
            origin = np.asarray(face) - np.array([[c, -s_, 0], [s_, c, 0], [0, 0, 1]]) @ self.local
            d.qpos[sim.obj_qadr:sim.obj_qadr + 7] = [origin[0], origin[1], max(origin[2], 0.0),
                                                    np.cos(yaw / 2), 0, 0, np.sin(yaw / 2)]
        if camera is not None:
            q = np.zeros(4)
            mujoco.mju_mat2Quat(q, (np.asarray(camera[1]) @ np.diag([1, -1, -1])).ravel())  # OpenCV -> MuJoCo axes
            m.cam_pos[sim.env_cam], m.cam_quat[sim.env_cam] = camera[0], q
        mujoco.mj_forward(m, d)
        self.viewer.sync()

    def running(self):
        return self.viewer.is_running()

    def close(self):
        self.viewer.close()


def solve_camera(centres, image_pts, K, tag_mm, fit_size=True):
    """Camera pose (robot frame) from back-tag centres (robot frame) + their averaged image corners, and the
    printed tag size that makes single-tag distance readings agree with the arm's (fit_size)."""
    I = np.concatenate(image_pts).astype(np.float64)
    for _ in range(4 if fit_size else 1):
        W = np.concatenate([c + tag_obj_pts(tag_mm) @ R_WORLD_BACK_TAG.T for c in centres]).astype(np.float64)
        ok, rvec, tvec = cv2.solvePnP(W, I, K, None, flags=cv2.SOLVEPNP_ITERATIVE)
        R = cv2.Rodrigues(rvec)[0].T
        pos = (-R @ tvec).ravel()
        if fit_size:  # true distance (arm) / distance the tag's apparent size implies
            ratios = []
            for c, img in zip(centres, image_pts):
                ok, rv, tv = cv2.solvePnP(tag_obj_pts(tag_mm), img.astype(np.float32), K, None,
                                          flags=cv2.SOLVEPNP_IPPE_SQUARE)
                ratios.append(np.linalg.norm(c - pos) / np.linalg.norm(tv))
            tag_mm *= float(np.median(ratios))
    err = np.linalg.norm(cv2.projectPoints(W, rvec, tvec, K, None)[0].reshape(-1, 2) - I, axis=1).mean()
    err_mm = err / K[1, 1] * np.median(np.linalg.norm(W - pos, axis=1)) * 1000
    return pos, R, tag_mm, err_mm, W, I


def touch_calibrate(cam, arm, mapping, cfg, object_name="lego_tree", spots=4, frames=15, ruler_spots=None,
                    height_cm=None, view=None):
    """Interactive calibration with the camera window; updates and saves cfg (env_camera, tag_mm).
    The arm must be torque-off (touch mode) or None (ruler mode, ruler_spots given). Returns a summary dict,
    or None if the user quit."""
    ec = cfg["env_camera"]
    K, fovy = camera_intrinsics(ec)
    sim = TagLiftEnv(object_name, domain_randomization=False)  # the arm's kinematics + the object's thickness
    depth = sim.tag_depth  # tag ID 0 face to tag ID 1 face
    tips = [sim.model.geom(g).id for g in ("fixed_jaw_pad_1", "moving_jaw_pad_1")]
    front_id, back_id = object_tags(object_name)
    if back_id is None:
        raise SystemExit(f"{object_name} has no tag facing the camera (objects.json -> tags -> back)")
    find = BackTagFinder(back_id)
    touch = f"the centre of tag {front_id}" if front_id is not None else \
        "the robot-side face of the object, opposite the camera's tag, at the same height"

    def gripper_tip():
        """Where the closed gripper's tip is (robot frame), from the real arm's joint angles."""
        sim.data.qpos[sim.qadr] = mapping.to_sim(arm.read_degrees())
        mujoco.mj_kinematics(sim.model, sim.data)
        return sim.data.geom_xpos[tips].mean(0)

    own_view = view is None
    view = view or SimView(sim, arm, mapping)
    plan = ruler_spots or [None] * spots
    hints = ["near (close to the robot)", "far", "to one side", "to the other side"]
    centres, image_pts = [], []
    for n, spot in enumerate(plan):
        if spot:
            print(f"spot {n + 1}/{len(plan)}: tag ID 0 face {spot[0]} cm in front, {spot[1]:+} cm sideways -> SPACE")
        else:
            print(f"spot {n + 1}/{len(plan)}: object {hints[n % 4]}, touch {touch} with the closed gripper tip "
                  "-> SPACE")
        captured, touched = [], []
        while len(captured) < frames:
            frame = cv2.cvtColor(cam.read(), cv2.COLOR_RGB2BGR)
            c = find(frame)
            tip = None if spot else gripper_tip()
            h_ruler = (height_cm or TAG["centre_height"] * 100) / 100
            view.show(face=tip if tip is not None else from_ruler(spot[0] / 100, spot[1] / 100, h_ruler))
            key = cv2.waitKey(1) & 0xFF
            if key == ord("q") or not view.running():
                if own_view:
                    view.close()
                return None
            if c is not None and (captured or key == ord(" ")):
                captured.append(c)
                if tip is not None:
                    touched.append(tip)
            if captured:
                msg = f"capturing {len(captured)}/{frames}, keep still"
            elif spot:
                msg = f"spot {n + 1}: front {spot[0]} cm, side {spot[1]:+} cm -> SPACE"
            else:
                f, sd, h = ruler(tip)
                msg = f"spot {n + 1}: touch tag 0 centre, SPACE | tip: front {f * 100:.1f} side {sd * 100:+.1f} " \
                      f"height {h * 100:.1f} cm"
            cv2.putText(frame, msg, (14, 40), cv2.FONT_HERSHEY_SIMPLEX, 1.0, (0, 255, 255), 2, cv2.LINE_AA)
            if c is None:
                cv2.putText(frame, f"tag ID {back_id} not seen", (14, 80), cv2.FONT_HERSHEY_SIMPLEX, 1.0,
                            (0, 0, 255), 2, cv2.LINE_AA)
            cv2.imshow(WINDOW, cv2.resize(frame, (960, 540)))
        if spot:
            h = (height_cm or TAG["centre_height"] * 100) / 100
            centres.append(from_ruler(spot[0] / 100 + depth, spot[1] / 100, h))  # back tag, robot frame
        else:  # touched the front tag's centre; the back tag is the object's thickness further out (-y)
            front = np.median(touched, 0)
            centres.append(front + np.array([0, -depth, 0]))
            f, sd, h = ruler(front)
            print(f"   touched point measured by the arm: front {f * 100:.1f} cm, side {sd * 100:+.1f} cm, "
                  f"height {h * 100:.1f} cm")
        image_pts.append(np.mean(captured, 0))

    tag_mm = cfg.get("tag_mm", TAG["size"] * 1000)
    pos, R, tag_mm, err_mm, W, I = solve_camera(centres, image_pts, K, tag_mm, fit_size=ruler_spots is None)
    ec.update(pos=pos.round(4).tolist(), R=R.round(5).tolist(), fovy_used=round(fovy, 2),
              calib_points=dict(world=W.round(5).tolist(), image=I.round(2).tolist(), tag_mm=round(tag_mm, 2)))
    cfg["tag_mm"] = round(tag_mm, 2)
    CONFIG.write_text(json.dumps(cfg, indent=2))
    cf, cs, ch = ruler(pos)
    print(f"saved: camera {cf * 100:.1f} cm in front of the rotation axis, {cs * 100:+.1f} cm sideways, "
          f"{ch * 100:.1f} cm high, looking {np.degrees(np.arcsin(-R[2, 2])):.0f} deg down | printed tag "
          f"{tag_mm:.1f} mm | fit error about {err_mm:.0f} mm (under ~8 mm is good; larger = a spot was off)")
    view.show(camera=(pos, R))
    if own_view:
        view.close()
    return dict(pos=pos, R=R, tag_mm=tag_mm, err_mm=err_mm)


def live_check(cam, cfg, object_name, view=None):
    """Show where the calibrated camera thinks the object is (robot-side face, from the rotation axis), in the
    camera window and in the sim (object + camera at the measured spot)."""
    ec = cfg["env_camera"]
    K = intrinsics(ec["fovy_used"], ec["width"], ec["height"])
    pos, R = np.array(ec["pos"]), np.array(ec["R"])
    depth = TagLiftEnv(object_name, domain_randomization=False).tag_depth
    back_id = object_tags(object_name)[1]
    obj, find = tag_obj_pts(cfg.get("tag_mm", TAG["size"] * 1000)), BackTagFinder(back_id)
    print("CHECK: touch tag 0 again or put the object at a measured spot and compare. q quits.")
    while True:
        frame = cv2.cvtColor(cam.read(), cv2.COLOR_RGB2BGR)
        c = find(frame)
        text = f"tag ID {back_id} not seen"
        if c is not None:
            ok, rv, tv = cv2.solvePnP(obj, c, K, None, flags=cv2.SOLVEPNP_IPPE_SQUARE)
            if ok:
                face, normal = back_to_front(pos + R @ tv.ravel(), R @ cv2.Rodrigues(rv)[0][:, 2], depth)
                f, sd, h = ruler(face)
                text = f"object face: front {f * 100:5.1f} cm  side {sd * 100:+5.1f} cm  height {h * 100:4.1f} cm"
                if view is not None:  # object turned so its robot-side face points along `normal`
                    view.show(face=face, yaw=np.arctan2(-normal[0], normal[1]), camera=(pos, R))
        elif view is not None:
            view.show(camera=(pos, R))
        cv2.putText(frame, text, (14, 40), cv2.FONT_HERSHEY_SIMPLEX, 1.0, (0, 255, 255), 2, cv2.LINE_AA)
        cv2.imshow(WINDOW, cv2.resize(frame, (960, 540)))
        if cv2.waitKey(1) & 0xFF == ord("q") or (view is not None and not view.running()):
            break


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--object", default="lego_tree", help="asset name (its thickness between the two tags)")
    ap.add_argument("--ruler", action="store_true", help="4 spots measured with a ruler (no arm)")
    ap.add_argument("--height", type=float, help="ruler mode: tag centre height, cm")
    ap.add_argument("--spots", type=int, default=4, help="touch mode: number of spots")
    ap.add_argument("--frames", type=int, default=15, help="frames averaged per spot")
    ap.add_argument("--track", action="store_true",
                    help="no calibration: track the object live with the saved calibration (sim follows it)")
    args = ap.parse_args()
    cfg = json.loads(CONFIG.read_text())
    ec = cfg["env_camera"]
    arm = mapping = None
    if not args.ruler:  # the arm is read (torque off) so the sim arm copies it
        arm = SOArm(cfg["port"], ROOT / cfg["calibration"], cfg["baudrate"])
        mapping = SimMap(ROOT / cfg["sim_mapping"], arm.calibration)
        arm.connect()
        arm.torque(False)
    cam = Camera(ec["index"], ec["width"], ec["height"], ec.get("fixed_exposure"))
    view = SimView(TagLiftEnv(args.object, domain_randomization=False), arm, mapping)
    try:
        if args.track:
            if "pos" not in ec:
                raise SystemExit("not calibrated yet: run without --track first")
            print("tracking: move the object, the sim follows (q in the camera window quits)")
            live_check(cam, cfg, args.object, view)
            return
        done = touch_calibrate(cam, arm, mapping, cfg, args.object, args.spots, args.frames,
                               SPOTS if args.ruler else None, args.height, view=view)
        if done:
            live_check(cam, cfg, args.object, view)
    finally:
        view.close()
        cam.close()
        cv2.destroyAllWindows()
        if arm is not None:
            arm.disconnect(torque_off=True)


if __name__ == "__main__":
    main()
