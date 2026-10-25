"""SO-100 lift task where the policy sees the object only through an AprilTag on it (no images, no rendering).

What the policy gets (all available on the real robot):
  - joint angles (small noise) + the current joint targets
  - the tag's last detected position in the robot frame + which way it faces, whether it is seen right now,
    whether it was ever seen, and how long ago
The tag detection is simulated the way it happens on the real robot, from geometry only:
  project the tag's corners into the (randomized, "true") webcam -> visible? (in view, big enough, not too
  slanted, not hidden behind the gripper) -> pixel noise / missed frames -> cv2.solvePnP with the NOMINAL lens
  -> into the robot frame with the NOMINAL camera mount (+ small calibration error).
So a slightly wrong lens, mount or calibration on the real robot is already part of training.
A second, fixed environment camera across the table (facing the robot, pose found by
robot/calibrate_env_camera.py) sees a second tag (BACK_TAG) on the back of the object, where the arm never
blocks it. Its detection is converted to where the front tag is (object thickness), so the policy input means
the same whichever camera saw it; the wrist camera's detection is used when both do. The env camera's
placement is randomized widely: the policy only gets positions in the robot frame.
Physics randomization (mass, friction, motor strength, object position) comes from LiftEnv.
"""
import json

import cv2
import mujoco
import numpy as np

from hand_sim import SimHand, add_hand, load_hand
from lift_env import ARM_SCENE, LiftEnv, look_at, front_face_y

TAG = dict(dictionary=cv2.aruco.DICT_APRILTAG_36h11, id=0, size=0.025, margin=0.002,
           centre_height=0.040)  # black square 25 mm, white margin 2 mm; centre 4 cm above the table
BACK_TAG_ID = 1  # same size and height, glued on the back of the object (faces away from the robot)
SETUP_VERSION = "wrist+env-camera-v1"  # bump when what the policy sees changes (resets "best" on resume)
# Simple USB webcam (normal field of view), glued above the jaws looking along them (Fixed_Jaw frame:
# fingertips at y ~ -0.1, jaws close along x). Rough guess from the wrist photo: training randomizes it.
WEBCAM = dict(width=1280, height=720, hfov=65.0, pos=[0.0, -0.02, 0.035], target=[0.0, -0.2, 0.0])
# Fixed environment webcam across the table, facing the robot, looking at the placement area and the back
# tag. Robot frame, metres (y is negative in front of the robot).
ENV_CAMERA = dict(width=1280, height=720, hfov=65.0, pos=[0.0, -0.60, 0.30], target=[0.0, -0.235, 0.03],
                  pos_noise=[0.15, 0.08, 0.10], target_noise=0.04, hfov_noise=8.0)
# Pose where the webcam sees the whole area the tree is placed in (found with find_look_pose.py)
LOOK_POSE = [0.0, -2.606, 2.07, 1.646, 1.802, 1.0]
# Scan routine before every attempt (sim and real robot do the same): hold still at the look pose and try a
# few frames; if the tag is not found, look around: (base "Rotation", "Wrist_Pitch") offsets in rad, tried in
# order (straight, left, right, then tilted up / down), then come back.
SCAN_TURNS = [(0.0, 0.0), (-0.45, 0.0), (0.45, 0.0), (0.0, 0.3), (0.0, -0.3), (-0.45, 0.3), (0.45, 0.3),
              (-0.45, -0.3), (0.45, -0.3)]
SCAN_FRAMES = 3
# The glued wrist webcam as a 2.5 cm ball (hidden, no physics) for distance checks: poses that bring it close to
# the arm crushed the real camera, so ready poses keep it >= MIN_CAMERA_CLEARANCE from the arm.
CAMERA_PROBE_RADIUS = 0.025
MIN_CAMERA_CLEARANCE = 0.06
MIN_MOVE_CLEARANCE = 0.03  # m: the real robot never moves (or lets the policy move) the camera closer than this
# Folded pose for torque-off: the SO-100 "rest" keyframe with the wrist turned so the camera points away from the
# lower arm (the keyframe itself leaves it 1.8 cm away; this one 7.9 cm).
SAFE_REST = [0.0, -3.32, 3.11, 0.38, -1.57, -0.174]
OLD_POLICY_CLEARANCE = 0.015  # m: policies trained before the camera rule reach 1.9 cm while grasping (sim)


# Distances people measure with a ruler start at the base's rotation axis, which sits PAN_AXIS_FRONT in front
# of the robot model's origin (robot frame: x = robot's left as seen from behind it, -y = straight out, z up).
_arm = mujoco.MjModel.from_xml_path(str(ARM_SCENE))
PAN_AXIS_FRONT = float(-_arm.body("Rotation_Pitch").pos[1])  # 0.0452 m


def ruler(p):
    """Robot-frame point -> (front, side, height) in metres as measured from the rotation axis."""
    return -p[1] - PAN_AXIS_FRONT, p[0], p[2]


def from_ruler(front, side, height):
    """(front, side, height) measured from the rotation axis (m) -> robot-frame point."""
    return np.array([side, -(front + PAN_AXIS_FRONT), height])


def vfov(hfov, width, height):
    return float(np.degrees(2 * np.arctan(np.tan(np.radians(hfov) / 2) * height / width)))


def intrinsics(fovy_deg, width, height):
    f = (height / 2) / np.tan(np.radians(fovy_deg) / 2)
    return np.array([[f, 0, width / 2], [0, f, height / 2], [0, 0, 1]])


def tag_png(tag_id=TAG["id"], back=False):
    from lift_env import ROOT
    path = ROOT / "assets" / "apriltag" / f"tag36h11_{tag_id}{'_back' if back else ''}.png"
    if not path.exists():
        px = 8 * 40  # 40 pixels per tag cell
        margin = round(px * TAG["margin"] / TAG["size"])
        tag = cv2.aruco.generateImageMarker(cv2.aruco.getPredefinedDictionary(TAG["dictionary"]), tag_id, px)
        card = cv2.copyMakeBorder(tag, margin, margin, margin, margin, cv2.BORDER_CONSTANT, value=255)
        path.parent.mkdir(parents=True, exist_ok=True)
        # MuJoCo shows the box's +y face mirrored left-right (pre-mirror it); the -y face comes out as is
        cv2.imwrite(str(path), card if back else card[:, ::-1])
    return path


# Placement area, measured from the base's rotation axis to the object: front from/to and +-side (m), and how
# far the object may be turned from facing the robot (deg). DEFAULT_AREA is what the first models trained on.
DEFAULT_AREA = dict(front_min=0.145, front_max=0.235, side=0.08, yaw_deg=30)
WIDE_AREA = dict(front_min=0.12, front_max=0.28, side=0.15, yaw_deg=45)
# Taking the object from a person's open hand (palm up): where the palm centre is (front/side as above), the palm
# surface's height above the table (m), palm tilt (deg), fingers within +-finger_deg of pointing at the robot, and
# a slow wobble of the hand (m) while the robot reaches for it.
HAND_AREA = dict(front_min=0.23, front_max=0.29, side=0.08, yaw_deg=30, height=[0.05, 0.12], tilt_deg=8,
                 finger_deg=25, wobble=0.004)
GRIPPER_OPEN_GAP = 0.086  # m between the jaw pads at the policy's "open" jaw angle (SO-100 model)
MAX_MASS = 0.4  # kg the SO-100 lifts comfortably


def object_setup(name):
    """Size of an asset and where its tags go: tag centre height = objects.json "tag_height_m" if given, else
    half the object's height (kept 2-5 cm so it is reachable and visible)."""
    from lift_env import ROOT
    props = json.loads((ROOT / "assets" / name / "properties.json").read_text())
    objects = json.loads((ROOT / "objects.json").read_text()) if (ROOT / "objects.json").exists() else {}
    length, width, height = props["size_m"]
    tag_h = objects.get(name, {}).get("tag_height_m") or float(np.clip(0.5 * height, 0.02, 0.05))
    return dict(size=(length, width, height), mass=props["mass_kg"], tag_height=tag_h)


def object_tags(name):
    """(front ID, back ID) glued on this object (objects.json -> tags, set when the asset was made); None = no
    tag on that side. Objects without the entry use the defaults 0 (front) and 1 (back)."""
    from lift_env import ROOT
    path = ROOT / "objects.json"
    tags = (json.loads(path.read_text()).get(name, {}) if path.exists() else {}).get("tags")
    return (tags.get("front"), tags.get("back")) if tags else (TAG["id"], BACK_TAG_ID)


def check_object(name):
    """Can the SO-100 gripper hold it? Returns a list of problems (empty = fine)."""
    o = object_setup(name)
    problems = []
    if min(o["size"][:2]) > 0.8 * GRIPPER_OPEN_GAP:
        problems.append(f"narrowest side {min(o['size'][:2]) * 1000:.0f} mm is too wide for the gripper "
                        f"(opens ~{GRIPPER_OPEN_GAP * 1000:.0f} mm)")
    if o["mass"] > MAX_MASS:
        problems.append(f"{o['mass'] * 1000:.0f} g is heavy for the SO-100 (comfortable up to {MAX_MASS * 1000:.0f} g)")
    if o["size"][2] < 0.03:
        problems.append("lower than 3 cm: no room for a 2.5 cm tag")
    return problems


class TagLiftEnv(LiftEnv):
    corner_noise_px = 0.7
    miss_prob = 0.08  # frames where the detector misses a visible tag (blur, glare)
    min_side_px = 18  # 36h11 needs roughly 2+ pixels per cell (8 cells across)
    max_view_angle = np.radians(70)
    calib_pos_noise, calib_rot_noise = 0.003, np.radians(1.5)  # error left after calibrating the mount
    # env camera after calibrate_env_camera.py: calibrating on the workspace itself leaves mostly a small offset
    env_calib_pos_noise, env_calib_rot_noise = 0.004, np.radians(0.3)
    env_fov_error = 0.3  # deg: lens error left after calibrating on the workspace

    def __init__(self, object_name="lego_tree", seed=0, domain_randomization=True, start=None, area=None, hand=None,
                 hand_role="from"):
        """hand: a person's hand (name in hands/, or the hand model itself) in the scene. hand_role "from": the
        object stands on the open palm (task: take it from the hand; area defaults to HAND_AREA); "to": the object
        starts on the table and the hand is held out (offer_hand) once it is picked up, to put it on the palm."""
        self.cam_w, self.cam_h = WEBCAM["width"], WEBCAM["height"]
        self.tag_height = object_setup(object_name)["tag_height"]
        self.hand_model = (load_hand(hand) if isinstance(hand, str) else dict(hand)) if hand else None
        self.hand_name = self.hand_model["name"] if hand else None
        self.hand_role = hand_role if hand else None
        if hand and hand_role == "from" and area is None:
            area = HAND_AREA
        super().__init__(object_name, seed=seed, domain_randomization=domain_randomization, camera="webcam",
                         start=list(start if start is not None else LOOK_POSE))
        self.face_robot = True  # the tag is glued on the side facing the robot
        self.area = dict(area or DEFAULT_AREA)
        a = self.area
        near, far = from_ruler(a["front_min"], 0, 0), from_ruler(a["front_max"], 0, 0)
        self.spawn_center = np.array([0.0, (near[1] + far[1]) / 2])
        self.spawn_half = np.array([a["side"], abs(near[1] - far[1]) / 2])
        self.face_yaw = np.radians(a["yaw_deg"])
        self.qpos_obs_noise = np.radians(1.0)  # servo encoders are precise; the image policy needed more
        m = self.model
        self.tag_geom = m.geom("tag").id
        self.back_tag_geom = m.geom("tag_back").id
        self.fixed_jaw = m.body("Fixed_Jaw").id
        self.min_camera_clearance = MIN_MOVE_CLEARANCE  # joint targets closer than this are held (sim and robot)
        self.check_data = mujoco.MjData(m)  # scratch data for those checks
        self.state_dim = 20
        s = TAG["size"] / 2
        self.tag_obj_pts = np.array([[-s, s, 0], [s, s, 0], [s, -s, 0], [-s, -s, 0]], np.float32)
        self.K_nominal = intrinsics(self.base_fovy, self.cam_w, self.cam_h)
        self.ray_groups = np.array([1, 1, 1, 1, 0, 0], np.uint8)
        self.env_cam = m.camera("envcam").id
        self.env_fovy_nominal = m.cam_fovy[self.env_cam]
        self.hand = SimHand(m, self.hand_model) if self.hand_model else None
        self.hand_points, self.wobble = None, np.zeros(3)

    def _extend(self, scene, obj, name):
        if self.hand_model:
            add_hand(scene, self.hand_model)
        scene.body("Fixed_Jaw").add_geom(name="webcam_probe", type=mujoco.mjtGeom.mjGEOM_SPHERE,
                                         size=[CAMERA_PROBE_RADIUS, 0, 0], pos=WEBCAM["pos"], contype=0,
                                         conaffinity=0, group=4, density=0)
        e = ENV_CAMERA
        scene.worldbody.add_camera(name="envcam", pos=e["pos"], fovy=vfov(e["hfov"], e["width"], e["height"]),
                                   xyaxes=look_at(np.array(e["pos"]), np.array(e["target"])))
        scene.body("Fixed_Jaw").add_camera(
            name="webcam", pos=WEBCAM["pos"], fovy=vfov(WEBCAM["hfov"], WEBCAM["width"], WEBCAM["height"]),
            xyaxes=look_at(np.array(WEBCAM["pos"]), np.array(WEBCAM["target"]), up=(0, 0, 1)))
        half = TAG["size"] / 2 + TAG["margin"]
        front_id, back_id = object_tags(name)  # textures only: the detection itself is simulated geometrically
        front_id, back_id = (TAG["id"] if front_id is None else front_id), (BACK_TAG_ID if back_id is None else back_id)
        for geom, tag_id, side in (("tag", front_id, 1), ("tag_back", back_id, -1)):
            obj.add_texture(name=geom, type=mujoco.mjtTexture.mjTEXTURE_2D, file=str(tag_png(tag_id, side < 0)))
            mat = obj.add_material(name=geom)
            mat.textures[mujoco.mjtTextureRole.mjTEXROLE_RGB] = geom
            obj.body(name).add_geom(name=geom, type=mujoco.mjtGeom.mjGEOM_BOX, size=[half, 0.0003, half],
                                    pos=[0, front_face_y(obj, name, side) + side * 0.0004, self.tag_height],
                                    material=geom, contype=0, conaffinity=0, group=2, density=0)
        self.tag_depth = front_face_y(obj, name) - front_face_y(obj, name, -1) + 0.0008  # front to back tag

    # ── Episode ────────────────────────────────────────────────────────────
    def _place_object(self, xy, yaw):
        if self.hand is None:
            return super()._place_object(xy, yaw)
        r, a = self.rng, self.area
        if self.hand_role == "from":
            heading = np.arctan2(-xy[1], -xy[0]) + r.uniform(-1, 1) * np.radians(a["finger_deg"])
            self.place_on_hand(xy, yaw - np.arctan2(xy[0], -xy[1]), r.uniform(*a["height"]), heading,
                               r.uniform(-1, 1, 2) * np.radians(a["tilt_deg"]))
            direction = r.normal(size=3)
            self.wobble = direction / np.linalg.norm(direction) * a["wobble"]
            self.wobble_phase, self.wobble_hz = r.uniform(0, 2 * np.pi), r.uniform(0.2, 0.5)
        else:  # not held out yet: offer_hand() after the pick-up
            self.hand.park(self.data)
            self.hand_points = self.hand.points
            super()._place_object(xy, yaw)

    def offer_hand(self, tries=50):
        """The person holds out the open hand, palm up, fingers toward the robot, somewhere the arm can put
        the object (low, in front), not touching the arm or the object. Returns the palm's surface centre."""
        r, m, d = self.rng, self.model, self.data
        avoid = set(self.robot_geoms) | set(self.obj_geoms)
        for _ in range(tries):
            centre = from_ruler(r.uniform(0.17, 0.26), r.uniform(-0.15, 0.15), r.uniform(0.04, 0.08))
            heading = np.arctan2(-centre[1], -centre[0]) + r.uniform(-1, 1) * np.radians(20)
            self.hand_points = self.hand.palm_up(centre - [0, 0, self.hand.box_half[2]], heading)
            self.hand.set(d, self.hand_points)
            mujoco.mj_forward(m, d)
            if not any((c.geom1 in avoid and c.geom2 in self.hand.geoms) or (c.geom2 in avoid and c.geom1 in
                       self.hand.geoms) for c in d.contact[:d.ncon]):
                return self.hand.palm_top()[0]
        raise RuntimeError("no free spot for the hand")

    def place_on_hand(self, xy, turn, height, heading, tilt=(0.0, 0.0)):
        """Hand palm up with its palm centre at xy and its surface `height` above the table, fingers along
        `heading`, tilted; the object standing on the palm centre, turned `turn` from facing the robot."""
        h = self.hand
        self.hand_points = h.palm_up([xy[0], xy[1], height - h.box_half[2]], heading, tilt)
        h.set(self.data, self.hand_points)
        top, normal = h.palm_top()
        q, qz = np.zeros(4), np.zeros(4)
        mujoco.mju_quatZ2Vec(q, normal)
        face = np.arctan2(xy[0], -xy[1]) + turn  # facing the robot base, as on the table
        mujoco.mju_mulQuat(qz, q, [np.cos(face / 2), 0, 0, np.sin(face / 2)])
        self.data.qpos[self.obj_qadr:self.obj_qadr + 7] = [*(top + 0.002 * normal), *qz]
        self.base_height = top[2]
        self.wobble = np.zeros(3)

    def step(self, action):
        if self.hand is not None and self.wobble.any():  # the person's hand is never perfectly still
            t = (self.steps + 1) * self.control_dt  # starts from where it was placed: no jump
            w = 2 * np.pi * self.wobble_hz
            self.hand.set(self.data, self.hand_points + self.wobble * (np.sin(w * t + self.wobble_phase) -
                                                                       np.sin(self.wobble_phase)))
        return super().step(action)

    def _evaluate(self):
        info = super()._evaluate()
        info["touching_hand"] = False
        if self.hand is not None:
            hand, robot = set(self.hand.geoms), set(self.robot_geoms)
            for c in self.data.contact[:self.data.ncon]:
                if (c.geom1 in hand and c.geom2 in robot) or (c.geom2 in hand and c.geom1 in robot):
                    info["touching_hand"] = True
                    break
        return info

    def target_ok(self, target):
        return pose_clearance(self.model, self.check_data, target) >= self.min_camera_clearance

    def reset(self):
        r = self.rng
        self.calib_pos = r.normal(0, self.calib_pos_noise, 3) if self.dr else np.zeros(3)
        self.calib_rot = small_rotation(r, self.calib_rot_noise) if self.dr else np.eye(3)
        self._place_env_camera()
        self.tag_est = np.zeros(5)  # x, y, z (robot frame) + cos/sin of the direction the tag faces
        self.seen, self.age = False, 0
        super().reset()
        self.scan()
        return self._obs()

    def _place_env_camera(self):
        """Where the env camera is this episode (randomized) and the small error left after calibrating it."""
        m, r, e, c = self.model, self.rng, ENV_CAMERA, self.env_cam
        pos, target, fovy = np.array(e["pos"], float), np.array(e["target"], float), self.env_fovy_nominal
        if self.dr:
            pos += r.uniform(-1, 1, 3) * e["pos_noise"]
            target += r.uniform(-1, 1, 3) * e["target_noise"]
            fovy = vfov(e["hfov"] + r.uniform(-1, 1) * e["hfov_noise"], e["width"], e["height"])
        xy = look_at(pos, target)
        R = np.stack([xy[:3], xy[3:], np.cross(xy[:3], xy[3:])], 1)
        q = np.zeros(4)
        mujoco.mju_mat2Quat(q, R.ravel())
        m.cam_pos[c], m.cam_quat[c], m.cam_fovy[c] = pos, q, fovy
        self.env_calib_pos = r.normal(0, self.env_calib_pos_noise, 3) if self.dr else np.zeros(3)
        self.env_calib_rot = small_rotation(r, self.env_calib_rot_noise) if self.dr else np.eye(3)
        f_err = r.normal(0, self.env_fov_error) if self.dr else 0.0
        self.K_env = intrinsics(fovy + f_err, e["width"], e["height"])

    def scan(self):
        """Look for the tag before the attempt (the real robot runs the same routine with slow moves)."""
        m, d = self.model, self.data
        q0, v0 = d.qpos.copy(), d.qvel.copy()
        for turn, tilt in SCAN_TURNS:
            d.qpos[self.qadr[0]] = q0[self.qadr[0]] + turn
            d.qpos[self.qadr[3]] = q0[self.qadr[3]] + tilt
            mujoco.mj_forward(m, d)
            for _ in range(SCAN_FRAMES):
                if self.update_tag(self.detect()):
                    break
            if self.seen:
                break
        d.qpos[:], d.qvel[:] = q0, v0
        mujoco.mj_forward(m, d)

    def _reward(self, info):
        # an early-closed gripper can never grasp: small cost for closing it while still far from the object
        r = super()._reward(info)
        jaw = self.data.qpos[self.qadr[5]]
        closed = np.clip((1.0 - jaw) / 1.1, 0, 1)  # 0 = open (1.0 rad), 1 = closed (-0.1 rad)
        return r - 0.3 * closed * (info["tcp_dist"] > 0.04) - 1.0 * info["touching_hand"]

    # ── Simulated tag detection ────────────────────────────────────────────
    def tag_corners_world(self, back=False):
        """Corners of the black square in OpenCV order (top-left, top-right, bottom-right, bottom-left as
        seen by someone facing the tag) and the outward normal."""
        d = self.data
        g = self.back_tag_geom if back else self.tag_geom
        c, R = d.geom_xpos[g], d.geom_xmat[g].reshape(3, 3)
        if back:  # viewer behind the object (-y) sees +x as "right"
            right, up, normal = R[:, 0], R[:, 2], -R[:, 1]
        else:  # viewer in front (+y) sees -x as "right"
            right, up, normal = -R[:, 0], R[:, 2], R[:, 1]
        s = TAG["size"] / 2
        return np.array([c - s * right + s * up, c + s * right + s * up, c + s * right - s * up,
                         c - s * right - s * up]), normal

    def nominal_camera(self):
        """Webcam pose as the real robot computes it: forward kinematics + the NOMINAL mount (+ calibration
        error). Returns position and OpenCV rotation (x right, y down, z forward)."""
        m, d = self.model, self.data
        Rj, pj = d.xmat[self.fixed_jaw].reshape(3, 3), d.xpos[self.fixed_jaw]
        Rc = np.zeros(9)
        mujoco.mju_quat2Mat(Rc, self.base_cam_quat)
        R = self.calib_rot @ Rj @ Rc.reshape(3, 3)
        return pj + Rj @ self.base_cam_pos + self.calib_pos, R @ np.diag([1, -1, -1])

    def detect(self):
        """What the real robot reports this frame: the wrist camera's detection, else the env camera's;
        (position, normal) in the robot frame, or None."""
        return self.detect_wrist() or self.detect_env()

    def detect_wrist(self):
        px = self.tag_pixels(self.cam_id, self.cam_w, self.cam_h)
        if px is None:
            return None
        npos, nR = self.nominal_camera()
        return self.solve(px, self.K_nominal, npos, nR)

    def detect_env(self):
        e, c, d = ENV_CAMERA, self.env_cam, self.data
        px = self.tag_pixels(c, e["width"], e["height"], back=True)
        if px is None:
            return None
        # calibrated pose = true pose + the small error calibration leaves
        R = self.env_calib_rot @ d.cam_xmat[c].reshape(3, 3) @ np.diag([1, -1, -1])
        back = self.solve(px, self.K_env, d.cam_xpos[c] + self.env_calib_pos, R)
        return None if back is None else back_to_front(*back, self.tag_depth)

    def solve(self, px, K, cam_pos, cam_R):
        """Detected corner pixels -> tag position + normal in the robot frame (same maths as the robot)."""
        ok, rvec, tvec = cv2.solvePnP(self.tag_obj_pts, px.astype(np.float32), K, None,
                                      flags=cv2.SOLVEPNP_IPPE_SQUARE)
        if not ok:
            return None
        return cam_pos + cam_R @ tvec.ravel(), cam_R @ cv2.Rodrigues(rvec)[0][:, 2]

    def tag_pixels(self, cam, width, height, back=False):
        """Corner pixels the detector would find in this camera (with noise), or None if not detected."""
        m, d = self.model, self.data
        corners, normal = self.tag_corners_world(back)
        cpos, Rcv = d.cam_xpos[cam], d.cam_xmat[cam].reshape(3, 3) @ np.diag([1, -1, -1])
        to_cam = cpos - corners.mean(0)
        if normal @ to_cam < np.cos(self.max_view_angle) * np.linalg.norm(to_cam):
            return None  # seen from behind or too slanted
        pc = (corners - cpos) @ Rcv  # camera coordinates
        if (pc[:, 2] < 0.02).any():
            return None
        K = intrinsics(m.cam_fovy[cam], width, height)
        px = (pc[:, :2] / pc[:, 2:]) * [K[0, 0], K[1, 1]] + K[:2, 2]
        if (px < 4).any() or (px[:, 0] > width - 4).any() or (px[:, 1] > height - 4).any():
            return None
        if np.linalg.norm(px - np.roll(px, 1, 0), axis=1).min() < self.min_side_px:
            return None
        gid = np.zeros(1, np.int32)
        for p in corners:  # hidden behind the gripper (or anything else)?
            v = p - cpos
            dist = mujoco.mj_ray(m, d, cpos, v, self.ray_groups, 1, -1, gid)
            if 0 <= dist < 1 - 0.002 / np.linalg.norm(v) and m.geom_bodyid[gid[0]] != self.obj_body:
                return None
        if self.dr and self.rng.uniform() < self.miss_prob:
            return None
        if self.dr:
            px = px + self.rng.normal(0, self.corner_noise_px, px.shape)
        return px

    def update_tag(self, detection):
        """Shared with the real robot: keep the last detection (robot frame) and how long ago it was."""
        if detection is None:
            self.age += 1
            return False
        pos, normal = detection
        heading = np.arctan2(normal[1], normal[0])
        self.tag_est = np.array([*pos, np.cos(heading), np.sin(heading)])
        self.seen, self.age = True, 0
        return True

    def _obs(self):
        visible = self.update_tag(self.detect())
        qpos = self.data.qpos[self.qadr]
        noisy = qpos + (self.rng.normal(0, self.qpos_obs_noise, 6) if self.dr else 0)
        state = tag_state(noisy, self.target, self.tag_est, visible, self.seen, self.age)
        obj = self.data.xpos[self.obj_body]
        tcp = self.tcp()
        grasped, _ = self._contacts()
        priv = np.concatenate([qpos, obj, tcp - obj, [float(grasped)]]).astype(np.float32)
        return dict(state=state, priv=priv)


def camera_clearance(model, data):
    """Distance (m) from the wrist webcam (probe ball) to the arm's base, shoulder, upper and lower arm."""
    probe = model.geom("webcam_probe").id
    bodies = {model.body(n).id for n in ("Base", "Rotation_Pitch", "Upper_Arm", "Lower_Arm")}
    fromto = np.zeros(6)
    return min(mujoco.mj_geomDistance(model, data, probe, g, 0.5, fromto) for g in range(model.ngeom)
               if model.geom_bodyid[g] in bodies and model.geom_contype[g])


def pose_clearance(model, data, q):
    """camera_clearance for joint angles q (rad); data's own pose is left unchanged."""
    keep = data.qpos.copy()
    data.qpos[:6] = q
    mujoco.mj_kinematics(model, data)
    c = camera_clearance(model, data)
    data.qpos[:] = keep
    mujoco.mj_kinematics(model, data)
    return c


def path_clearance(model, data, a, b, samples=40):
    """Smallest camera clearance along a straight joint-space move from a to b (what ease_to does)."""
    a, b = np.asarray(a, float), np.asarray(b, float)
    return min(pose_clearance(model, data, a + (b - a) * s) for s in np.linspace(0, 1, samples))


def safe_route(model, data, current, goal, minimum=MIN_MOVE_CLEARANCE):
    """Waypoints from current to goal that keep the wrist camera away from the arm, or None.

    Tried in order: straight; via "a little forward and up" (a quarter of the way from zero to the goal, shoulder
    forward, elbow up), which is how the arm unfolds from zero without folding onto the camera; via zero first;
    each of these after first turning the wrist as in SAFE_REST (camera away from the lower arm).
    If the arm is already closer than `minimum` (e.g. folded by hand), it must not get any closer than it is.
    """
    zero = (model.jnt_range[:6, 0] + model.jnt_range[:6, 1]) / 2
    current, goal = np.asarray(current, float), np.asarray(goal, float)
    minimum = min(minimum, pose_clearance(model, data, current) - 0.002)
    up = zero + 0.25 * (goal - zero)
    up[1] += 0.25
    up[2] -= 0.25
    wrist_away = current.copy()
    wrist_away[3:5] = SAFE_REST[3:5]
    routes = [[goal], [up, goal], [zero, up, goal], [zero, goal]]
    for route in routes + [[wrist_away] + r for r in routes]:
        points = [current] + route
        if all(path_clearance(model, data, p, n) >= minimum for p, n in zip(points[:-1], points[1:])):
            return route
    return None


def back_to_front(pos, normal, depth):
    """Back tag (position, outward normal) -> where the front tag is and the way it faces. A small, distant tag's
    tilt is the least certain part of its pose, and the object stands upright, so the normal is taken level."""
    level = np.array([normal[0], normal[1], 0.0])
    level /= max(np.linalg.norm(level), 1e-9)
    return pos - level * depth, -level


# ── Which way the object is turned (one tag gives its full 3D rotation) ─────────────────────────────────
# Tag axes as OpenCV reports them (tag_obj_pts): x right, y up, z out of the printed face, as seen by someone
# facing the tag. A tag glued turned by roll degrees (counter-clockwise, seen facing it) is told apart from a
# turned object only by knowing that roll: tag_roll() reads it from objects.json (set with "u" in tracking).

def _rot_z(deg):
    c, s = np.cos(np.radians(deg)), np.sin(np.radians(deg))
    return np.array([[c, -s, 0], [s, c, 0], [0, 0, 1.0]])


def tag_roll(name):
    """How the object's tags are glued: {"front": deg or None, "back": deg or None} (objects.json tag_roll_deg;
    None = not measured yet: the orientation is unknown, a turned tag would look like a turned object)."""
    from lift_env import ROOT
    path = ROOT / "objects.json"
    roll = (json.loads(path.read_text()).get(name, {}) if path.exists() else {}).get("tag_roll_deg") or {}
    return {"front": roll.get("front"), "back": roll.get("back")}


def save_tag_roll(name, side, deg):
    """Store how a tag ("front"/"back") is glued, measured while the object stands upright."""
    from lift_env import ROOT
    path = ROOT / "objects.json"
    objects = json.loads(path.read_text())
    objects.setdefault(name, {}).setdefault("tag_roll_deg", {})[side] = round(float(deg), 1)
    path.write_text(json.dumps(objects, indent=2))


def object_rotation(R_tag, back=False, roll_deg=0.0):
    """Object rotation (columns: object x, y, z in the robot frame) from a tag's rotation in the robot frame.
    Front tag on the object's +y face, back tag on its -y face; the object's z is up when it stands."""
    x, y, z = (np.asarray(R_tag) @ _rot_z(-roll_deg)).T  # the tag's axes as if glued straight
    return np.stack([x, -z, y], 1) if back else np.stack([-x, z, y], 1)


def measured_roll(R_tag):
    """The glue roll (deg) of a tag on an object that stands upright now."""
    up = np.asarray(R_tag).T @ [0, 0, 1.0]  # world up in the tag's axes
    return float(np.degrees(np.arctan2(up[0], up[1])))


def orientation(R_obj):
    """(name, tilt deg): upright / tilted / lying (on which face) / upside down."""
    R = np.asarray(R_obj)
    tilt = float(np.degrees(np.arccos(np.clip(R[2, 2], -1, 1))))
    if tilt < 20:
        return "upright", tilt
    if tilt < 60:
        return "tilted", tilt
    if tilt > 150:
        return "upside down", tilt
    # which face is down: the object axis pointing most downward (R[2, i] = how far axis i points up)
    down = {"on its front (front tag face down)": -R[2, 1], "on its back (front tag face up)": R[2, 1],
            "on its side": abs(R[2, 0])}
    return "lying " + max(down, key=down.get), tilt


def small_rotation(rng, sigma):
    """Random rotation about a random axis, angle ~ N(0, sigma)."""
    axis = rng.normal(size=3)
    q, R = np.zeros(4), np.zeros(9)
    mujoco.mju_axisAngle2Quat(q, axis / np.linalg.norm(axis), rng.normal(0, sigma))
    mujoco.mju_quat2Mat(R, q)
    return R.reshape(3, 3)


def tag_state(qpos, target, tag_est, visible, seen, age):
    """Policy input (same function on the real robot): 6 joints, 6 targets, tag x/y/z + facing (cos, sin),
    visible now, ever seen, time since last seen (s, capped at 3)."""
    return np.concatenate([qpos, target, tag_est, [float(visible), float(seen), min(age * 0.1, 3.0)]]
                          ).astype(np.float32)
