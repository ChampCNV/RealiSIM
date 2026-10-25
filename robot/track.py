"""Live tracking of several objects at once (each by its own AprilTags) [and a person's hand]: the sim follows.

    python robot/track.py lego_tree celsius_can                 # these objects
    python robot/track.py --all                                  # every object whose tag IDs are set
    python robot/track.py lego_tree celsius_can --hand           # + your hand as a stick pose (MediaPipe)
    python robot/track.py lego_tree celsius_can --hand my_hand   # + the hand made with photo2grasp.py hand
    python robot/track.py celsius_can --video clip.mp4           # a recording instead of the camera

The environment camera (calibrated with photo2grasp.py calibrate) finds every tag in each frame; each tag ID
belongs to one object (objects.json "tags"), so every object is placed where its tags are seen: position and
which way it is turned (upright, tilted, lying, upside down), never below the table. An object not seen for
UNSEEN_S seconds is taken off the table in the sim. The arm (optional, torque off) is copied too.
--hand alone shows the hand as sticks between MediaPipe's 21 points (no hand model needed); hidden flesh around
them (sim2real/hand_sim.py StickHand) keeps an object resting on the hand from sinking into it.
Orientation needs to know how each tag is glued: the first time a tag is seen the object is assumed to stand
upright (glue roll rounded to 90 deg); "u" in the camera window measures it exactly for every object in view
(stand them upright first). q quits. Tracking only: training and the robot runner use one object.
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
from hand_sim import SimHand, StickHand, add_hand, add_stick_hand, load_hand  # noqa: E402
from lift_env import ARM_JOINTS, ARM_SCENE, front_face_y  # noqa: E402
from tag_env import (TAG, intrinsics, measured_roll, object_rotation, object_setup, object_tags,  # noqa: E402
                     orientation, ruler, save_tag_roll, tag_png, tag_roll)

from camera import Camera, RealSense, VideoFile, camera_indices  # noqa: E402
from hand_watch import HandWatcher  # noqa: E402

CONFIG = Path(__file__).parent / "config.json"
UNSEEN_S = 1.0  # s without a tag before an object leaves the sim table
PARK = np.array([1.5, 1.5, 0.0])  # where unseen objects wait (off the table)
WINDOW = "environment camera - tracking (u = objects stand upright now, q quits)"
COLOURS = [(0, 255, 255), (255, 200, 0), (0, 200, 255), (255, 0, 255), (0, 255, 0), (255, 255, 255)]


def tagged_objects(names, skip_clashes=False):
    """{name: (front ID or None, back ID or None)} (objects.json "tags", else the defaults 0/1). Each ID must
    belong to one object: a clash stops, or with skip_clashes leaves that object out (objects with their own
    tags go first, those on the defaults after)."""
    objects = json.loads((ROOT / "objects.json").read_text())
    own = lambda n: bool(objects.get(n, {}).get("tags"))  # noqa: E731
    tags, owner = {}, {}
    for n in sorted(names, key=lambda n: not own(n)) if skip_clashes else names:
        ids = object_tags(n)
        clash = [i for i in ids if i is not None and i in owner]
        if clash:
            msg = f"tag ID {clash[0]} is on both {owner[clash[0]]} and {n}"
            if not skip_clashes:
                raise SystemExit(f"{msg}: give one of them other tags (python photo2grasp.py tags <name>)")
            print(f"skipping {n}: {msg} (python photo2grasp.py tags {n} gives it its own)")
            continue
        tags[n] = ids
        owner.update({i: n for i in ids if i is not None})
    return {n: tags[n] for n in names if n in tags}


class Scene:
    """SO-100 + the objects (with their tags drawn) [+ the hand], every object a free body moved by code."""

    def __init__(self, names, hand=None, skip_clashes=False, env_camera=None, arms=None):
        """arms: None = the SO-100 scene; or (cfg, arm names) = those arms (robot/arms.py) at their measured
        spots, in the reference arm's robot frame (env_camera must then be that arm's calibration)."""
        self.tags = tagged_objects(names, skip_clashes)
        self.names = names = list(self.tags)
        if arms:
            from arms import arms_scene_spec
            scene, _, _ = arms_scene_spec(*arms)
        else:
            scene = mujoco.MjSpec.from_file(str(ARM_SCENE))
            for key in list(scene.keys):  # keyframes would need every object's pose
                scene.delete(key)
        half = TAG["size"] / 2 + TAG["margin"]
        self.local, self.depth = {}, {}  # tag centres in each object's frame; front-to-back distance
        for i, n in enumerate(names):
            obj = mujoco.MjSpec.from_file(str(ROOT / "assets" / n / f"{n}.xml"))
            height = object_setup(n)["tag_height"]
            self.local[n] = {}
            for side, sign, tag_id in (("front", 1, self.tags[n][0]), ("back", -1, self.tags[n][1])):
                y = front_face_y(obj, n, sign) + sign * 0.0004
                self.local[n][side] = np.array([0.0, y, height])
                if tag_id is None:
                    continue
                tex = f"{n}_tag_{side}"
                obj.add_texture(name=tex, type=mujoco.mjtTexture.mjTEXTURE_2D, file=str(tag_png(tag_id, sign < 0)))
                obj.add_material(name=tex).textures[mujoco.mjtTextureRole.mjTEXROLE_RGB] = tex
                obj.body(n).add_geom(name=tex, type=mujoco.mjtGeom.mjGEOM_BOX, size=[half, 0.0003, half],
                                     pos=[0, y, height], material=tex, contype=0, conaffinity=0, group=2, density=0)
            scene.worldbody.add_frame(pos=PARK + [0.25 * i, 0, 0]).attach_body(obj.body(n), "", "")
        if hand == "stick":
            add_stick_hand(scene)
        elif hand:
            add_hand(scene, hand)
        if env_camera and "pos" in env_camera:  # where the calibrated camera stands (drawn in the viewer)
            q = np.zeros(4)
            mujoco.mju_mat2Quat(q, (np.array(env_camera["R"]) @ np.diag([1, -1, -1])).ravel())  # OpenCV -> MuJoCo
            scene.worldbody.add_camera(name="envcam", pos=env_camera["pos"], quat=q, fovy=env_camera["fovy_used"])
        self.model = scene.compile()
        self.model.opt.gravity[:] = 0  # moved by code only
        m = self.model
        self.data = mujoco.MjData(m)
        if not arms:
            self.qadr = np.array([m.jnt_qposadr[m.joint(j).id] for j in ARM_JOINTS])
            self.data.qpos[self.qadr] = (m.jnt_range[:6, 0] + m.jnt_range[:6, 1]) / 2  # zero pose
        self.obj_qadr = {n: m.jnt_qposadr[m.body(n).jntadr[0]] for n in names}
        self.geoms = {n: [g for g in range(m.ngeom) if m.geom_bodyid[g] == m.body(n).id and m.geom_contype[g]]
                      for n in names}
        self.hand = SimHand(m, hand) if isinstance(hand, dict) else None  # a person's hand model
        self.stick = StickHand(m) if hand == "stick" else None  # or the tracked points as they are
        for h in (self.hand, self.stick):
            if h is not None:
                h.park(self.data)
        for i, n in enumerate(names):
            self.place(n, PARK + [0.25 * i, 0, 0], np.eye(3))

    def place(self, name, origin, R):
        """Object at origin with rotation R, lifted if any part would be below the table."""
        m, d, a = self.model, self.data, self.obj_qadr[name]
        q = np.zeros(4)
        mujoco.mju_mat2Quat(q, np.ascontiguousarray(R).ravel())
        d.qpos[a:a + 7] = [*origin, *q]
        mujoco.mj_kinematics(m, d)
        signs = np.array([[x, y, z] for x in (-1, 1) for y in (-1, 1) for z in (-1, 1)])
        lowest = min((d.geom_xpos[g, 2] + d.geom_xmat[g].reshape(3, 3)[2] @ (m.geom_aabb[g, :3, None] + signs.T *
                      m.geom_aabb[g, 3:, None])).min() for g in self.geoms[name])
        d.qpos[a + 2] -= min(lowest, 0.0)


class Tracker:
    """Camera frame -> every object's pose (robot frame) from its tags, same maths as tracking one object."""

    def __init__(self, scene, cfg, width, height, roll=None):
        """roll: how the objects' tags are glued ({name: {"front", "back"}}) to use instead of objects.json; then
        nothing is written to objects.json (a package's own settings)."""
        ec = cfg["env_camera"]
        if "pos" not in ec:
            raise SystemExit("environment camera not calibrated yet: python photo2grasp.py calibrate <object>")
        self.scene, self.pos, self.R = scene, np.array(ec["pos"]), np.array(ec["R"])
        self.K = intrinsics(ec["fovy_used"], width, height)
        s = cfg.get("tag_mm", TAG["size"] * 1000) / 2000
        self.pts = np.array([[-s, s, 0], [s, s, 0], [s, -s, 0], [-s, -s, 0]], np.float32)  # OpenCV corner order
        self.detector = cv2.aruco.ArucoDetector(cv2.aruco.getPredefinedDictionary(TAG["dictionary"]),
                                                cv2.aruco.DetectorParameters())
        self.persist = roll is None
        self.roll = {n: dict(roll[n]) for n in scene.names} if roll else {n: tag_roll(n) for n in scene.names}
        self.last_seen = {n: -1e9 for n in scene.names}
        self.tag_rotations = {}  # this frame: {(name, side): tag rotation}, for "u"

    def __call__(self, frame_bgr, now):
        """Moves the sim objects; returns {name: text line}."""
        corners, ids, _ = self.detector.detectMarkers(cv2.cvtColor(frame_bgr, cv2.COLOR_BGR2GRAY))
        raw = {int(i): c[0] for i, c in zip(ids.ravel(), corners)} if ids is not None else {}
        if ids is not None:
            cv2.aruco.drawDetectedMarkers(frame_bgr, corners, ids, (0, 255, 0))
        self.tag_rotations, lines = {}, {}
        for i, n in enumerate(self.scene.names):
            seen = {}
            for side, tag_id in zip(("back", "front"), self.scene.tags[n][::-1]):  # back first: it faces the camera
                if tag_id in raw:
                    ok, rv, tv = cv2.solvePnP(self.pts, raw[tag_id], self.K, None, flags=cv2.SOLVEPNP_IPPE_SQUARE)
                    if ok:
                        seen[side] = (self.R @ cv2.Rodrigues(rv)[0], self.pos + self.R @ tv.ravel())
                        self.tag_rotations[(n, side)] = seen[side][0]
            for side in [s for s in seen if self.roll[n][s] is None]:  # first sight: assume it stands, glued square
                self.keep_roll(n, side, 90 * round(measured_roll(seen[side][0]) / 90))
                print(f"{n}: orientation set up from its {side} tag, assuming it stands upright now (u measures it)")
            if seen:
                side = next(iter(seen))
                R_tag, tag_pos = seen[side]
                R_obj = object_rotation(R_tag, side == "back", self.roll[n][side])
                self.scene.place(n, tag_pos - R_obj @ self.scene.local[n][side], R_obj)
                self.last_seen[n] = now
                f, sd, _ = ruler(self.scene.data.qpos[self.scene.obj_qadr[n]:self.scene.obj_qadr[n] + 3])
                name, tilt = orientation(R_obj)
                lines[n] = f"{n}: front {f * 100:5.1f} cm  side {sd * 100:+5.1f} cm  {name} ({tilt:.0f} deg)"
            else:
                if now - self.last_seen[n] > UNSEEN_S:
                    self.scene.place(n, PARK + [0.25 * i, 0, 0], np.eye(3))
                lines[n] = f"{n}: not seen"
        return lines

    def set_upright(self):
        """"u": every object in view stands upright now: measure how each visible tag is glued."""
        if not self.tag_rotations:
            print("u: no object tag in view")
        for (n, side), R_tag in self.tag_rotations.items():
            self.keep_roll(n, side, measured_roll(R_tag))
            print(f"{'saved' if self.persist else 'using'}: {n}'s {side} tag is glued turned "
                  f"{measured_roll(R_tag):.0f} deg")

    def keep_roll(self, name, side, deg):
        if self.persist:
            save_tag_roll(name, side, deg)
            self.roll[name] = tag_roll(name)
        else:
            self.roll[name][side] = deg


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("objects", nargs="*", help="object names (assets/<name>)")
    ap.add_argument("--all", action="store_true", help="every object whose tag IDs are set (clashing ones left out)")
    ap.add_argument("--hand", nargs="?", const="stick",
                    help="also track the hand: alone = stick pose from MediaPipe; or a hand made with photo2grasp")
    ap.add_argument("--depth", action="store_true", help="RealSense depth for the hand's distance")
    ap.add_argument("--video", help="a recorded video instead of the environment camera")
    ap.add_argument("--camera", type=int, help="this camera index instead of the environment camera")
    args = ap.parse_args()
    cfg = json.loads(CONFIG.read_text())
    names = args.objects
    if args.all:  # objects whose tag IDs were set (photo2grasp.py tags); the defaults 0/1 would clash
        objects = json.loads((ROOT / "objects.json").read_text())
        names = sorted(n for n, v in objects.items()
                       if v.get("tags") and (ROOT / "assets" / n / f"{n}.xml").exists())
    if not names:
        raise SystemExit("give object names, or --all")
    hand = "stick" if args.hand == "stick" else load_hand(args.hand) if args.hand else None
    from sim_map import arm_model
    two_arm = arm_model() != "so100" and bool(cfg.get("arms"))
    if two_arm:  # the registered (SO-101) arms at their measured spots, in the reference arm's frame
        from arms import arm_poses, plugged_arms
        arm_names = plugged_arms(cfg)
        ref = arm_poses(cfg)[0]
        if ref is None:
            raise SystemExit("calibrate the environment camera with the arm first: "
                             "python photo2grasp.py calibrate <object>")
        cfg["env_camera"] = dict(cfg["env_camera"], **cfg["arms"][ref]["env_camera"])  # seen from the reference arm
        scene = Scene(names, hand, skip_clashes=args.all, env_camera=cfg["env_camera"], arms=(cfg, arm_names))
    else:
        scene = Scene(names, hand, skip_clashes=args.all, env_camera=cfg["env_camera"])
    print("tracking:", ", ".join(f"{n} (tags {scene.tags[n][0]}/{scene.tags[n][1]})" for n in names))

    arm = mapping = None
    arms = []  # the sim arm(s) copy the real one(s), torque off
    if two_arm:
        from arms import connect_arms
        arms = connect_arms(cfg, arm_names, scene.model)
    else:
        try:  # optional
            from arm import SOArm
            from sim_map import SimMap
            arm = SOArm(cfg["port"], ROOT / cfg["calibration"], cfg["baudrate"])
            mapping = SimMap(ROOT / cfg["sim_mapping"], arm.calibration)
            arm.connect()
            arm.torque(False)
        except Exception as e:
            print(f"arm not connected ({e}): the sim arm stays still")
            arm = None
    ec = cfg["env_camera"]
    if args.video:
        cam = VideoFile(args.video)
    elif args.depth:
        cam = RealSense(ec["width"], ec["height"])
    else:
        cam = Camera(camera_indices(cfg, need_wrist=False)[1] if args.camera is None else args.camera, ec["width"], ec["height"],
                     ec.get("fixed_exposure"))
    h, w = cam.read().shape[:2]
    track = Tracker(scene, cfg, w, h)
    watch = HandWatcher(hand if isinstance(hand, dict) else None, ec["pos"], ec["R"], track.K,
                        getattr(cam, "depth_scale", 1.0)) if hand else None
    smooth, hand_seen = None, -1e9  # stick hand: points averaged over frames (MediaPipe jitters a little)
    viewer = mujoco.viewer.launch_passive(scene.model, scene.data)
    viewer.opt.flags[mujoco.mjtVisFlag.mjVIS_CAMERA] = 1  # draw the environment camera
    try:
        while viewer.is_running():
            rgb = cam.read()
            frame = cv2.cvtColor(rgb, cv2.COLOR_RGB2BGR)
            now = time.time()
            hand_line = None
            if watch is not None:  # the hand first: objects resting on it are placed after
                seen = watch(rgb, cam.depth() if hasattr(cam, "depth") else None)
                hand_line = "hand: not seen"
                if seen is not None:
                    watch.draw(frame, seen)
                    if scene.stick is not None:
                        smooth = seen["points"] if smooth is None else 0.5 * seen["points"] + 0.5 * smooth
                        scene.stick.set(scene.data, smooth)
                        palm = smooth[[0, 5, 9, 13, 17]].mean(0)
                    else:
                        points = scene.hand.fit(seen["points"])
                        scene.hand.set(scene.data, points)
                        palm = scene.hand.palm_top(points)[0]
                    hand_seen = now
                    f, sd, ht = ruler(palm)
                    hand_line = (f"hand: palm front {f * 100:5.1f} cm  side {sd * 100:+5.1f} cm  "
                                 f"height {ht * 100:4.1f} cm")
                elif now - hand_seen > UNSEEN_S:
                    smooth = None
                    (scene.stick or scene.hand).park(scene.data)
            lines = list(track(frame, now).values()) + ([hand_line] if hand_line else [])
            if scene.stick is not None and smooth is not None:  # an object resting on the hand: not into it
                for n in scene.names:
                    if scene.data.qpos[scene.obj_qadr[n]] < 1.0:  # on the table, not parked
                        scene.stick.lift_out(scene.data, scene.obj_qadr[n], scene.geoms[n])
            if arm is not None:
                scene.data.qpos[scene.qadr] = mapping.to_sim(arm.read_degrees())
            if arms:
                from arms import copy_arms
                copy_arms(arms, scene.data)
            mujoco.mj_forward(scene.model, scene.data)
            viewer.sync()
            for i, text in enumerate(lines):
                cv2.putText(frame, text, (14, 40 + 40 * i), cv2.FONT_HERSHEY_SIMPLEX, 1.0, COLOURS[i % len(COLOURS)],
                            2, cv2.LINE_AA)
            cv2.imshow(WINDOW, cv2.resize(frame, (960, int(960 * h / w))))
            key = cv2.waitKey(1) & 0xFF
            if key == ord("u"):
                track.set_upright()
            if key == ord("q"):
                break
    finally:
        viewer.close()
        cam.close()
        cv2.destroyAllWindows()
        if arm is not None:
            arm.disconnect(torque_off=True)
        for a in arms:
            a["arm"].disconnect(torque_off=True)


if __name__ == "__main__":
    main()
