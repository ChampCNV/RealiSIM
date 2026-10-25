"""Live tracking of several objects at once (each by its own AprilTags) [and a person's hand]: the sim follows.

    python robot/track.py lego_tree celsius_can                 # these objects
    python robot/track.py --all                                  # every object whose tag IDs are set
    python robot/track.py lego_tree celsius_can --hand my_hand   # + the hand (MediaPipe)
    python robot/track.py celsius_can --video clip.mp4           # a recording instead of the camera

The environment camera (calibrated with photo2grasp.py calibrate) finds every tag in each frame; each tag ID
belongs to one object (objects.json "tags"), so every object is placed where its tags are seen: position and
which way it is turned (upright, tilted, lying, upside down), never below the table. An object not seen for
UNSEEN_S seconds is taken off the table in the sim. The arm (optional, torque off) is copied too.
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
from hand_sim import SimHand, add_hand, load_hand  # noqa: E402
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

    def __init__(self, names, hand=None, skip_clashes=False, env_camera=None):
        self.tags = tagged_objects(names, skip_clashes)
        self.names = names = list(self.tags)
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
        if hand:
            add_hand(scene, hand)
        if env_camera and "pos" in env_camera:  # where the calibrated camera stands (drawn in the viewer)
            q = np.zeros(4)
            mujoco.mju_mat2Quat(q, (np.array(env_camera["R"]) @ np.diag([1, -1, -1])).ravel())  # OpenCV -> MuJoCo
            scene.worldbody.add_camera(name="envcam", pos=env_camera["pos"], quat=q, fovy=env_camera["fovy_used"])
        self.model = scene.compile()
        self.model.opt.gravity[:] = 0  # moved by code only
        m = self.model
        self.data = mujoco.MjData(m)
        self.qadr = np.array([m.jnt_qposadr[m.joint(j).id] for j in ARM_JOINTS])
        self.data.qpos[self.qadr] = (m.jnt_range[:6, 0] + m.jnt_range[:6, 1]) / 2  # zero pose
        self.obj_qadr = {n: m.jnt_qposadr[m.body(n).jntadr[0]] for n in names}
        self.geoms = {n: [g for g in range(m.ngeom) if m.geom_bodyid[g] == m.body(n).id and m.geom_contype[g]]
                      for n in names}
        self.hand = SimHand(m, hand) if hand else None
        if self.hand:
            self.hand.park(self.data)
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

    def __init__(self, scene, cfg, width, height):
        ec = cfg["env_camera"]
        if "pos" not in ec:
            raise SystemExit("environment camera not calibrated yet: python photo2grasp.py calibrate <object>")
        self.scene, self.pos, self.R = scene, np.array(ec["pos"]), np.array(ec["R"])
        self.K = intrinsics(ec["fovy_used"], width, height)
        s = cfg.get("tag_mm", TAG["size"] * 1000) / 2000
        self.pts = np.array([[-s, s, 0], [s, s, 0], [s, -s, 0], [-s, -s, 0]], np.float32)  # OpenCV corner order
        self.detector = cv2.aruco.ArucoDetector(cv2.aruco.getPredefinedDictionary(TAG["dictionary"]),
                                                cv2.aruco.DetectorParameters())
        self.roll = {n: tag_roll(n) for n in scene.names}
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
                save_tag_roll(n, side, 90 * round(measured_roll(seen[side][0]) / 90))
                self.roll[n] = tag_roll(n)
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
            save_tag_roll(n, side, measured_roll(R_tag))
            print(f"saved: {n}'s {side} tag is glued turned {measured_roll(R_tag):.0f} deg")
            self.roll[n] = tag_roll(n)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("objects", nargs="*", help="object names (assets/<name>)")
    ap.add_argument("--all", action="store_true", help="every object whose tag IDs are set (clashing ones left out)")
    ap.add_argument("--hand", help="also track this person's hand (hands/<name>)")
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
    hand = load_hand(args.hand) if args.hand else None
    scene = Scene(names, hand, skip_clashes=args.all, env_camera=cfg["env_camera"])
    print("tracking:", ", ".join(f"{n} (tags {scene.tags[n][0]}/{scene.tags[n][1]})" for n in names))

    arm = mapping = None
    try:  # optional: the sim arm copies the real one (torque off)
        from arm import SOArm
        from sim_map import SimMap
        arm = SOArm(cfg["port"], ROOT / cfg["calibration"], cfg["baudrate"])
        mapping = SimMap(ROOT / cfg["sim_mapping"], arm.calibration)
        arm.connect()
        arm.torque(False)
    except Exception as e:
        print(f"arm not connected ({e}): the sim arm stays at zero")
        arm = None
    ec = cfg["env_camera"]
    if args.video:
        cam = VideoFile(args.video)
    elif args.depth:
        cam = RealSense(ec["width"], ec["height"])
    else:
        cam = Camera(camera_indices(cfg)[1] if args.camera is None else args.camera, ec["width"], ec["height"],
                     ec.get("fixed_exposure"))
    h, w = cam.read().shape[:2]
    track = Tracker(scene, cfg, w, h)
    watch = HandWatcher(hand, ec["pos"], ec["R"], track.K, getattr(cam, "depth_scale", 1.0)) if hand else None
    viewer = mujoco.viewer.launch_passive(scene.model, scene.data)
    viewer.opt.flags[mujoco.mjtVisFlag.mjVIS_CAMERA] = 1  # draw the environment camera
    try:
        while viewer.is_running():
            rgb = cam.read()
            frame = cv2.cvtColor(rgb, cv2.COLOR_RGB2BGR)
            lines = list(track(frame, time.time()).values())
            if watch is not None:
                seen = watch(rgb, cam.depth() if hasattr(cam, "depth") else None)
                lines.append("hand: not seen")
                if seen is not None:
                    watch.draw(frame, seen)
                    points = scene.hand.fit(seen["points"])
                    scene.hand.set(scene.data, points)
                    f, sd, ht = ruler(scene.hand.palm_top(points)[0])
                    lines[-1] = (f"hand: palm front {f * 100:5.1f} cm  side {sd * 100:+5.1f} cm  "
                                 f"height {ht * 100:4.1f} cm")
            if arm is not None:
                scene.data.qpos[scene.qadr] = mapping.to_sim(arm.read_degrees())
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


if __name__ == "__main__":
    main()
