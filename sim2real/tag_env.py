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
import cv2
import mujoco
import numpy as np

from lift_env import LiftEnv, look_at, front_face_y

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


def vfov(hfov, width, height):
    return float(np.degrees(2 * np.arctan(np.tan(np.radians(hfov) / 2) * height / width)))


def intrinsics(fovy_deg, width, height):
    f = (height / 2) / np.tan(np.radians(fovy_deg) / 2)
    return np.array([[f, 0, width / 2], [0, f, height / 2], [0, 0, 1]])


def tag_png(tag_id=TAG["id"]):
    from lift_env import ROOT
    path = ROOT / "assets" / "apriltag" / f"tag36h11_{tag_id}.png"
    if not path.exists():
        px = 8 * 40  # 40 pixels per tag cell
        margin = round(px * TAG["margin"] / TAG["size"])
        tag = cv2.aruco.generateImageMarker(cv2.aruco.getPredefinedDictionary(TAG["dictionary"]), tag_id, px)
        card = cv2.copyMakeBorder(tag, margin, margin, margin, margin, cv2.BORDER_CONSTANT, value=255)
        path.parent.mkdir(parents=True, exist_ok=True)
        cv2.imwrite(str(path), card[:, ::-1])  # MuJoCo shows this box face mirrored left-right: pre-mirror
    return path


class TagLiftEnv(LiftEnv):
    # placement area: 19-28 cm in front of the base, +-8 cm sideways (where the look pose sees the tag)
    spawn_center = np.array([0.0, -0.235])
    spawn_half = np.array([0.08, 0.045])
    corner_noise_px = 0.7
    miss_prob = 0.08  # frames where the detector misses a visible tag (blur, glare)
    min_side_px = 18  # 36h11 needs roughly 2+ pixels per cell (8 cells across)
    max_view_angle = np.radians(70)
    calib_pos_noise, calib_rot_noise = 0.003, np.radians(1.5)  # error left after calibrating the mount
    # env camera after calibrate_env_camera.py: calibrating on the workspace itself leaves mostly a small offset
    env_calib_pos_noise, env_calib_rot_noise = 0.004, np.radians(0.3)
    env_fov_error = 0.3  # deg: lens error left after calibrating on the workspace

    def __init__(self, object_name="lego_tree", seed=0, domain_randomization=True, start=None):
        self.cam_w, self.cam_h = WEBCAM["width"], WEBCAM["height"]
        super().__init__(object_name, seed=seed, domain_randomization=domain_randomization, camera="webcam",
                         start=list(start if start is not None else LOOK_POSE))
        self.face_robot = True  # the tag is glued on the side facing the robot
        self.qpos_obs_noise = np.radians(1.0)  # servo encoders are precise; the image policy needed more
        m = self.model
        self.tag_geom = m.geom("tag").id
        self.back_tag_geom = m.geom("tag_back").id
        self.fixed_jaw = m.body("Fixed_Jaw").id
        self.state_dim = 20
        s = TAG["size"] / 2
        self.tag_obj_pts = np.array([[-s, s, 0], [s, s, 0], [s, -s, 0], [-s, -s, 0]], np.float32)
        self.K_nominal = intrinsics(self.base_fovy, self.cam_w, self.cam_h)
        self.ray_groups = np.array([1, 1, 1, 1, 0, 0], np.uint8)
        self.env_cam = m.camera("envcam").id
        self.env_fovy_nominal = m.cam_fovy[self.env_cam]

    def _extend(self, scene, obj, name):
        e = ENV_CAMERA
        scene.worldbody.add_camera(name="envcam", pos=e["pos"], fovy=vfov(e["hfov"], e["width"], e["height"]),
                                   xyaxes=look_at(np.array(e["pos"]), np.array(e["target"])))
        scene.body("Fixed_Jaw").add_camera(
            name="webcam", pos=WEBCAM["pos"], fovy=vfov(WEBCAM["hfov"], WEBCAM["width"], WEBCAM["height"]),
            xyaxes=look_at(np.array(WEBCAM["pos"]), np.array(WEBCAM["target"]), up=(0, 0, 1)))
        half = TAG["size"] / 2 + TAG["margin"]
        for geom, tag_id, side in (("tag", TAG["id"], 1), ("tag_back", BACK_TAG_ID, -1)):
            obj.add_texture(name=geom, type=mujoco.mjtTexture.mjTEXTURE_2D, file=str(tag_png(tag_id)))
            mat = obj.add_material(name=geom)
            mat.textures[mujoco.mjtTextureRole.mjTEXROLE_RGB] = geom
            obj.body(name).add_geom(name=geom, type=mujoco.mjtGeom.mjGEOM_BOX, size=[half, 0.0003, half],
                                    pos=[0, front_face_y(obj, name, side) + side * 0.0004, TAG["centre_height"]],
                                    material=geom, contype=0, conaffinity=0, group=2, density=0)
        self.tag_depth = front_face_y(obj, name) - front_face_y(obj, name, -1) + 0.0008  # front to back tag

    # ── Episode ────────────────────────────────────────────────────────────
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
        return r - 0.3 * closed * (info["tcp_dist"] > 0.04)

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


def back_to_front(pos, normal, depth):
    """Back tag (position, outward normal) -> where the front tag is and the way it faces. A small, distant tag's
    tilt is the least certain part of its pose, and the object stands upright, so the normal is taken level."""
    level = np.array([normal[0], normal[1], 0.0])
    level /= max(np.linalg.norm(level), 1e-9)
    return pos - level * depth, -level


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
