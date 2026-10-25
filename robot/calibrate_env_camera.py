"""Find where the environment camera is, from the tag on the back of the object (once per camera placement).

    python robot/calibrate_env_camera.py                     # TOUCH mode (no ruler): the arm measures
    python robot/calibrate_env_camera.py --ruler             # 4 spots measured with a ruler instead
    python robot/calibrate_env_camera.py --object celsius_can
    python robot/calibrate_env_camera.py --object celsius_can --track --hand my_hand   # live: object + hand
    python robot/calibrate_env_camera.py --track --hand my_hand --video hand.mp4       # no setup: a video

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

TRACK mode (--track): no calibration, the saved one is used to show live where the object is (its camera-side
tag) and, with --hand, where the person's hand is (MediaPipe, hand_watch.py; --depth uses the RealSense depth).
It also shows which way the object is turned (upright, tilted, lying on its back/front/side, upside down)
and turns the sim object to match: stand the object upright and press "u" once, which measures how each visible
tag is glued (objects.json tag_roll_deg; a turned tag would otherwise look like a turned object).
The arm is optional there; --video / --camera replace the environment camera (without a calibration the camera is
assumed at its planned spot, tag_env.ENV_CAMERA).

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
from tag_env import (ENV_CAMERA, TAG, TagLiftEnv, back_to_front, from_ruler, intrinsics, measured_roll,  # noqa: E402
                     object_rotation, object_tags, orientation, ruler, save_tag_roll, tag_roll, vfov)

from arm import SOArm  # noqa: E402
from camera import Camera, RealSense, VideoFile, camera_indices  # noqa: E402
from hand_watch import HandWatcher, env_camera_pose  # noqa: E402
from sim_map import MENAGERIE, ROBOT_XML, SIM_JOINTS, TIP_GEOMS, SimMap, arm_model, arm_placement  # noqa: E402

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
        self.raw = {}  # every tag in the last frame: {id: corners as detected} (their order carries the rotation)

    def __call__(self, frame):
        corners, ids, _ = self.detector.detectMarkers(cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY))
        self.raw = {int(i): c[0] for i, c in zip(ids.ravel(), corners)} if ids is not None else {}
        if ids is not None:
            cv2.aruco.drawDetectedMarkers(frame, corners, ids, (0, 255, 0))
            if self.tag_id in ids.ravel():
                return upright(corners[list(ids.ravel()).index(self.tag_id)][0])
        return None


class ArmScene:
    """The active arm's model + the object with its tags + the environment camera, with the names touch
    calibration and SimView use (as TagLiftEnv has them, which is the SO-100 training scene)."""

    def __init__(self, object_name, model_name):
        from lift_env import front_face_y
        from tag_env import BACK_TAG_ID, object_setup, tag_png
        background = (MENAGERIE / "trs_so_arm100" / "scene.xml").read_text()  # the SO-100 scene without its arm
        scene = mujoco.MjSpec.from_string(background.replace('<include file="so_arm100.xml"/>', ""))
        xml, base = ROBOT_XML[model_name]
        arm = mujoco.MjSpec.from_file(str(MENAGERIE / xml))
        for k in ("timestep", "iterations", "ls_iterations", "impratio", "integrator", "cone"):
            setattr(scene.option, k, getattr(arm.option, k))
        pos, quat = arm_placement(model_name)  # same robot frame as the SO-100
        scene.worldbody.add_frame(pos=pos, quat=quat).attach_body(arm.body(base), "", "")
        obj = mujoco.MjSpec.from_file(str(ROOT / "assets" / object_name / f"{object_name}.xml"))
        height, half = object_setup(object_name)["tag_height"], TAG["size"] / 2 + TAG["margin"]
        front_id, back_id = object_tags(object_name)
        for geom, tag_id, side in (("tag", front_id if front_id is not None else TAG["id"], 1),
                                   ("tag_back", back_id if back_id is not None else BACK_TAG_ID, -1)):
            obj.add_texture(name=geom, type=mujoco.mjtTexture.mjTEXTURE_2D, file=str(tag_png(tag_id, side < 0)))
            obj.add_material(name=geom).textures[mujoco.mjtTextureRole.mjTEXROLE_RGB] = geom
            obj.body(object_name).add_geom(name=geom, type=mujoco.mjtGeom.mjGEOM_BOX, size=[half, 0.0003, half],
                                           pos=[0, front_face_y(obj, object_name, side) + side * 0.0004, height],
                                           material=geom, contype=0, conaffinity=0, group=2, density=0)
        self.tag_depth = front_face_y(obj, object_name) - front_face_y(obj, object_name, -1) + 0.0008
        scene.worldbody.add_frame(pos=[0, -0.22, 0]).attach_body(obj.body(object_name), "", "")
        e = ENV_CAMERA
        from lift_env import look_at
        scene.worldbody.add_camera(name="envcam", pos=e["pos"], fovy=vfov(e["hfov"], e["width"], e["height"]),
                                   xyaxes=look_at(np.array(e["pos"]), np.array(e["target"])))
        m = self.model = scene.compile()
        self.data = mujoco.MjData(m)
        self.qadr = np.array([m.jnt_qposadr[m.joint(j).id] for j in SIM_JOINTS[model_name].values()])
        self.obj_body = m.body(object_name).id
        self.obj_qadr = m.jnt_qposadr[m.body_jntadr[self.obj_body]]
        self.obj_geoms = np.where(m.geom_bodyid == self.obj_body)[0]
        self.obj_collision = np.array([g for g in self.obj_geoms if m.geom_contype[g]])
        self.tag_geom, self.back_tag_geom = m.geom("tag").id, m.geom("tag_back").id
        self.env_cam = m.camera("envcam").id
        self.hand = self.hand_model = None
        mujoco.mj_forward(m, self.data)


def calibration_scene(object_name):
    """The scene for touch calibration and its sim window: the SO-100 training scene, or the same for another arm."""
    if arm_model() == "so100":
        return TagLiftEnv(object_name, domain_randomization=False)
    return ArmScene(object_name, arm_model())


class SimView:
    """MuJoCo window next to the calibration: the sim arm copies the real one (torque off), the object is drawn
    where the robot thinks it is (under the gripper tip while touching, then where the camera sees it) and,
    once calibrated, the environment camera at its measured spot."""

    def __init__(self, sim, arm=None, mapping=None):
        self.sim, self.arm, self.mapping = sim, arm, mapping
        self.local = sim.model.geom_pos[sim.tag_geom].copy()  # robot-side tag centre in the object's frame
        if sim.hand is not None:
            sim.hand.park(sim.data)  # until it is seen
        self.viewer = mujoco.viewer.launch_passive(sim.model, sim.data)
        self.viewer.opt.flags[mujoco.mjtVisFlag.mjVIS_CAMERA] = 1

    def show(self, face=None, yaw=0.0, camera=None, hand=None, pose=None):
        """face: robot-side tag centre (robot frame); yaw: which way the object faces (0 = toward the robot);
        camera: (position, OpenCV rotation) of the environment camera; hand: the person's 21 hand points;
        pose: (object origin, object rotation) instead of face/yaw, for an object that does not stand upright."""
        m, d, sim = self.sim.model, self.sim.data, self.sim
        if hand is not None and sim.hand is not None:
            sim.hand.set(d, sim.hand.fit(hand))
        if self.arm is not None:
            d.qpos[sim.qadr] = self.mapping.to_sim(self.arm.read_degrees())
        if pose is not None:
            q = np.zeros(4)
            mujoco.mju_mat2Quat(q, np.ascontiguousarray(pose[1]).ravel())
            d.qpos[sim.obj_qadr:sim.obj_qadr + 7] = [*pose[0], *q]
            mujoco.mj_kinematics(m, d)  # never below the table: the tag reading is ~1 cm off at times
            signs = np.array([[x, y, z] for x in (-1, 1) for y in (-1, 1) for z in (-1, 1)])
            lowest = min((d.geom_xpos[g, 2] + d.geom_xmat[g].reshape(3, 3)[2] @ (m.geom_aabb[g, :3, None] + signs.T *
                          m.geom_aabb[g, 3:, None])).min() for g in sim.obj_collision)
            d.qpos[sim.obj_qadr + 2] -= min(lowest, 0.0)
        elif face is not None:
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
    sim = view.sim if view is not None else calibration_scene(object_name)  # arm kinematics + object thickness
    depth = sim.tag_depth  # tag ID 0 face to tag ID 1 face
    tips = [sim.model.geom(g).id for g in TIP_GEOMS[arm_model()]]
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

    spread = max(np.linalg.norm(a - b) for a in centres for b in centres)
    if spread < 0.04:  # the touched spots must be spread out, or the arm being read did not move
        print(f"NOT saved: the {len(centres)} touched spots are only {spread * 100:.1f} cm apart. Touch with the ACTIVE "
              f"arm ({cfg.get('active_arm', 'the configured one')}: robot/arms.py list) and move the object between "
              f"spots (near, far, left, right).")
        if own_view:
            view.close()
        return None
    tag_mm = cfg.get("tag_mm", TAG["size"] * 1000)
    pos, R, fitted_mm, err_mm, W, I = solve_camera(centres, image_pts, K, tag_mm, fit_size=ruler_spots is None)
    if not 0.7 < fitted_mm / tag_mm < 1.4 or err_mm > 30:  # a real print is never that far off
        print(f"NOT saved: the result makes no sense (printed tag {fitted_mm:.0f} mm, fit error {err_mm:.0f} mm). "
              f"Was the tip exactly on the tag centre at every spot, with the active arm?")
        if own_view:
            view.close()
        return None
    tag_mm = fitted_mm
    ec.update(pos=pos.round(4).tolist(), R=R.round(5).tolist(), fovy_used=round(fovy, 2),
              calib_points=dict(world=W.round(5).tolist(), image=I.round(2).tolist(), tag_mm=round(tag_mm, 2)))
    cfg["tag_mm"] = round(tag_mm, 2)
    if cfg.get("active_arm") in cfg.get("arms", {}):  # measured from this arm's base: kept per arm as well
        cfg["arms"][cfg["active_arm"]]["env_camera"] = {k: ec[k] for k in ("pos", "R", "fovy_used")}
    CONFIG.write_text(json.dumps(cfg, indent=2))
    cf, cs, ch = ruler(pos)
    print(f"saved: camera {cf * 100:.1f} cm in front of the rotation axis, {cs * 100:+.1f} cm sideways, "
          f"{ch * 100:.1f} cm high, looking {np.degrees(np.arcsin(-R[2, 2])):.0f} deg down | printed tag "
          f"{tag_mm:.1f} mm | fit error about {err_mm:.0f} mm (under ~8 mm is good; larger = a spot was off)")
    view.show(camera=(pos, R))
    if own_view:
        view.close()
    return dict(pos=pos, R=R, tag_mm=tag_mm, err_mm=err_mm)


def live_check(cam, cfg, object_name, view=None, watch=None):
    """Show live where the calibrated camera sees the object (robot-side face, from the rotation axis) [and the
    person's hand, with a HandWatcher], in the camera window and in the sim."""
    ec = cfg["env_camera"]
    calibrated = "pos" in ec
    front_id, back_id = object_tags(object_name)
    find = None
    if calibrated and back_id is not None:
        K = intrinsics(ec["fovy_used"], ec["width"], ec["height"])
        sim = view.sim if view is not None else calibration_scene(object_name)
        depth = sim.tag_depth
        local = {"front": sim.model.geom_pos[sim.tag_geom].copy(),  # tag centres in the object's frame
                 "back": sim.model.geom_pos[sim.back_tag_geom].copy()}
        obj, find = tag_obj_pts(cfg.get("tag_mm", TAG["size"] * 1000)), BackTagFinder(back_id)
    elif not calibrated:
        print("environment camera not calibrated: the object is not tracked (python photo2grasp.py calibrate)")
    camera = (np.array(ec["pos"]), np.array(ec["R"])) if calibrated else None
    roll = tag_roll(object_name)
    print("CHECK: touch tag 0 again or put the object at a measured spot and compare. q quits." if find else
          "tracking: q in the camera window quits")
    if find is not None:
        print("orientation: stand the object upright and press u once (measures how its tags are glued)")
    while True:
        rgb = cam.read()
        frame = cv2.cvtColor(rgb, cv2.COLOR_RGB2BGR)
        lines, face, yaw, hand, pose, tags_now = [], None, 0.0, None, None, {}
        if find is not None:
            c = find(frame)
            lines.append(f"tag ID {back_id} not seen")
            if c is not None:
                ok, rv, tv = cv2.solvePnP(obj, c, K, None, flags=cv2.SOLVEPNP_IPPE_SQUARE)
                if ok:
                    pos, R = camera
                    face, normal = back_to_front(pos + R @ tv.ravel(), R @ cv2.Rodrigues(rv)[0][:, 2], depth)
                    yaw = np.arctan2(-normal[0], normal[1])  # object turned so its robot-side face points along it
                    f, sd, h = ruler(face)
                    lines[-1] = (f"object face: front {f * 100:5.1f} cm  side {sd * 100:+5.1f} cm  "
                                 f"height {h * 100:4.1f} cm")
            # orientation: from the tag's corners as detected (their order says how the tag is turned)
            seen_at = {}
            for side, tid in (("back", back_id), ("front", front_id)):
                if tid is not None and tid in find.raw:
                    ok, rv, tv = cv2.solvePnP(obj, find.raw[tid], K, None, flags=cv2.SOLVEPNP_IPPE_SQUARE)
                    if ok:
                        pos, R = camera
                        tags_now[side], seen_at[side] = R @ cv2.Rodrigues(rv)[0], pos + R @ tv.ravel()
            for side in [s for s in tags_now if roll[s] is None]:  # first sight: assume it stands, tag glued square
                save_tag_roll(object_name, side, 90 * round(measured_roll(tags_now[side]) / 90))
                print(f"orientation set up from the {side} tag, assuming the object stands upright now "
                      f"(press u while it stands to measure it exactly)")
                roll = tag_roll(object_name)
            usable = [s for s in tags_now if roll[s] is not None]  # back first: the camera faces it
            if usable:
                side = usable[0]
                R_obj = object_rotation(tags_now[side], side == "back", roll[side])
                name, tilt = orientation(R_obj)
                lines.append(f"orientation: {name} (tilt {tilt:.0f} deg, from the {side} tag)")
                pose = (seen_at[side] - R_obj @ local[side], R_obj)  # the sim object follows every tilt and roll
            elif tags_now:
                lines.append("orientation: not set up yet (stand the object upright, press u)")
        if watch is not None:
            seen = watch(rgb, cam.depth() if hasattr(cam, "depth") else None)
            lines.append("hand not seen")
            if seen is not None:
                watch.draw(frame, seen)
                hand = seen["points"]
                if view is not None and view.sim.hand is not None:
                    top, _ = view.sim.hand.palm_top(view.sim.hand.fit(hand))
                    f, sd, h = ruler(top)
                    lines[-1] = f"palm: front {f * 100:5.1f} cm  side {sd * 100:+5.1f} cm  height {h * 100:4.1f} cm"
        if view is not None:
            view.show(face=face, yaw=yaw, camera=camera, hand=hand, pose=pose)
        for i, text in enumerate(lines):
            cv2.putText(frame, text, (14, 40 + 40 * i), cv2.FONT_HERSHEY_SIMPLEX, 1.0, (0, 255, 255), 2, cv2.LINE_AA)
        cv2.imshow(WINDOW, cv2.resize(frame, (960, int(960 * frame.shape[0] / frame.shape[1]))))
        key = cv2.waitKey(1) & 0xFF
        if key == ord("u"):
            if not tags_now:
                print("u: no tag of this object seen right now")
            for side, R_tag in tags_now.items():  # the object stands upright now: how is each visible tag glued?
                save_tag_roll(object_name, side, measured_roll(R_tag))
                print(f"saved: {object_name}'s {side} tag is glued turned {measured_roll(R_tag):.0f} deg "
                      f"(objects.json tag_roll_deg)")
            roll = tag_roll(object_name)
        if key == ord("q") or (view is not None and not view.running()):
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
    ap.add_argument("--hand", help="track mode: also track this person's hand (hands/<name>)")
    ap.add_argument("--depth", action="store_true", help="track mode: RealSense depth for the hand's distance")
    ap.add_argument("--video", help="track mode: a recorded video instead of the environment camera")
    ap.add_argument("--camera", type=int, help="track mode: this camera index instead of the environment camera")
    args = ap.parse_args()
    cfg = json.loads(CONFIG.read_text())
    ec = cfg["env_camera"]
    arm = mapping = None
    if not args.ruler:  # the arm is read (torque off) so the sim arm copies it
        try:
            arm = SOArm(cfg["port"], ROOT / cfg["calibration"], cfg["baudrate"])
            mapping = SimMap(ROOT / cfg["sim_mapping"], arm.calibration)
            arm.connect()
            arm.torque(False)
        except Exception as e:  # tracking works without the arm (the sim arm stays still)
            if not args.track:
                raise
            print(f"arm not connected ({e}): the sim arm stays still")
            arm = mapping = None
    if args.video:
        cam = VideoFile(args.video)
    elif args.depth:
        cam = RealSense(ec["width"], ec["height"])
    else:
        cam = Camera(camera_indices(cfg, need_wrist=False)[1] if args.camera is None else args.camera, ec["width"], ec["height"],
                     ec.get("fixed_exposure"))
    if arm_model() == "so100":
        sim = TagLiftEnv(args.object, domain_randomization=False, hand=args.hand, hand_role="to")
    else:  # other arms: their own model (no hand model in this scene: use robot/track.py for the hand)
        sim = calibration_scene(args.object)
    view = SimView(sim, arm, mapping)
    try:
        if args.track:
            if "pos" not in ec and not args.hand:
                raise SystemExit("not calibrated yet: run without --track first")
            watch = None
            if args.hand:
                h, w = cam.read().shape[:2]
                pos, R, K, calibrated = env_camera_pose(cfg, w, h)
                if not calibrated:
                    print("environment camera not calibrated: assuming it stands where tag_env.ENV_CAMERA plans it")
                watch = HandWatcher(view.sim.hand_model, pos, R, K, getattr(cam, "depth_scale", 1.0))
            print("tracking: move the object" + (" and your hand" if watch else "") + ", the sim follows "
                  "(q in the camera window quits)")
            live_check(cam, cfg, args.object, view, watch)
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
