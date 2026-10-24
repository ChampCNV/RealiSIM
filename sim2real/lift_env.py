"""SO-100 lift task in MuJoCo, following the Squint recipe for zero-shot RGB sim-to-real.

What the policy (actor) gets, i.e. only things the real robot also has:
  - an RGB image from the wrist camera (default) or a fixed third-person camera,
    rendered at 128x128 and area-downsampled (64x64 by default; Squint used 16x16)
  - noisy joint angles + the current joint targets
Background pixels (everything except robot and object) are replaced by a photo of the real table.
The critic additionally gets privileged state (object/fingertip positions), used only during training.

Domain randomization per episode: object position/yaw, mass, friction, camera pose + field of view,
lighting, robot colour, starting joint angles. Joint-angle noise every step.
"""
from pathlib import Path

import cv2
import mujoco
import numpy as np

ROOT = Path(__file__).resolve().parent.parent
ARM_SCENE = ROOT / "third_party" / "mujoco_menagerie" / "trs_so_arm100" / "scene.xml"
ARM_JOINTS = ["Rotation", "Pitch", "Elbow", "Wrist_Pitch", "Wrist_Roll", "Jaw"]
JAW_OPEN, JAW_CLOSED = 1.0, -0.1
# Fixed third-person camera: front-right of the arm, above the table, looking at the workspace.
# Measure these on the real setup (metres, from the arm base) and put the same numbers here.
CAMERA_POS = np.array([0.30, -0.62, 0.30])
CAMERA_TARGET = np.array([0.0, -0.18, 0.04])


# Wrist camera on the fixed jaw (in the Fixed_Jaw body frame: fingertips at y ~ -0.1, jaws close along x,
# z points sideways out of the jaw). 5 cm out from the jaw, aimed just past the fingertips.
# The real camera has to be mounted at this same spot.
WRIST_POS = np.array([-0.005, -0.02, 0.05])
WRIST_TARGET = np.array([-0.005, -0.14, 0.0])

# ArUco card glued on the object's face toward the robot (DICT_ARUCO_ORIGINAL id 0, as on the real tree).
# Black square measured (5.4 cm); paper size and height estimated from photos: measure and correct if needed.
MARKER_CARD = dict(dictionary=cv2.aruco.DICT_ARUCO_ORIGINAL, id=0, black=0.054, width=0.086, height=0.084,
                   bottom=0.012, black_centre=0.055)  # metres; bottom/black_centre = heights above the table
MARKER_YAW = np.deg2rad(30)  # the card faces the robot within +-30 deg (place the real tree the same way)


def marker_card_png():
    """Texture of the paper card: white paper with the black ArUco square where it sits on the real card."""
    c = MARKER_CARD
    path = ROOT / "assets" / "aruco" / f"card_{c['id']}.png"
    if not path.exists():
        px = 4000  # pixels per metre
        w, h, b = round(c["width"] * px), round(c["height"] * px), round(c["black"] * px)
        card = np.full((h, w), 255, np.uint8)
        mk = cv2.aruco.generateImageMarker(cv2.aruco.getPredefinedDictionary(c["dictionary"]), c["id"], b)
        mk = cv2.rotate(mk, cv2.ROTATE_90_COUNTERCLOCKWISE)  # glued like the real one: white bar at the bottom
        top = h - round((c["black_centre"] - c["bottom"]) * px) - b // 2  # image rows run top -> down
        card[top:top + b, (w - b) // 2:(w - b) // 2 + b] = mk
        card = card[:, ::-1].copy()  # MuJoCo shows this box face mirrored left-right: pre-mirror it
        path.parent.mkdir(parents=True, exist_ok=True)
        cv2.imwrite(str(path), card)
    return path


def front_face_y(obj, name, side=1):
    """The object's front face (+y, toward the robot; side=-1: back face), from its mesh files: they are in the
    body frame, while the compiled model's mesh vertices are re-centred."""
    ys = np.concatenate([np.loadtxt([ln[2:] for ln in (ROOT / "assets" / name / mesh.file).read_text().splitlines()
                                     if ln.startswith("v ")])[:, 1] * mesh.scale[1] for mesh in obj.meshes])
    return ys.max() if side > 0 else ys.min()


# Per-camera randomization (position noise m, rotation noise rad, field-of-view noise deg), as in Squint
CAMERA_NOISE = {"third": (0.025, np.deg2rad(3), 5.0), "wrist": (0.002, np.deg2rad(1), 1.0),
                "webcam": (0.012, np.deg2rad(6), 6.0)}  # webcam glued by hand: mount only roughly known


def look_at(pos, target, up=(0, 0, 1)):
    """MuJoCo camera xyaxes for a camera at `pos` looking at `target`, with `up` pointing up in the image."""
    z = (pos - target) / np.linalg.norm(pos - target)
    x = np.cross(up, z)
    x /= np.linalg.norm(x)
    return np.concatenate([x, np.cross(z, x)])


def start_pose(model, start="zero"):
    """Arm pose at the start of every episode.

    "zero": middle of every joint's range = where a LeRobot-style calibrated real arm reads 0 deg on all motors
    "home": the SO-100 model's raised "home" keyframe, gripper open (what the first runs used)
    a list of 6 joint angles (rad): that pose (e.g. a "look" pose chosen so the camera sees the workspace)
    """
    if not isinstance(start, str):
        return np.array(start, float)
    if start == "zero":
        return (model.jnt_range[:6, 0] + model.jnt_range[:6, 1]) / 2
    q = model.key_qpos[model.key("home").id][:6].copy()
    q[5] = JAW_OPEN
    return q


class LiftEnv:
    render_size = 128
    control_dt = 0.1  # 10 Hz, like Squint
    max_steps = 80  # 8 s: enough to get from the zero pose to the object and lift it
    action_scale = np.array([0.1, 0.1, 0.1, 0.1, 0.1, 0.3])  # max joint-target change per step (rad)

    # Domain randomization ranges
    spawn_center = np.array([0.0, -0.22])
    spawn_half = np.array([0.09, 0.06])  # object anywhere in an 18 x 12 cm area in front of the arm
    mass_scale = (0.8, 1.2)
    friction_range = (0.3, 0.8)
    motor_strength = (0.8, 1.2)  # actuator stiffness: stand-in for not having identified the real servos
    qpos_obs_noise = np.deg2rad(5)
    init_qpos_noise = 0.05
    tipped_angle = np.deg2rad(45)  # object counts as knocked over / not upright beyond this tilt
    lift_height = 0.05  # success: held, at least 5 cm up, still upright

    def __init__(self, object_name, background=None, seed=0, domain_randomization=True,
                 camera="wrist", image_size=64, start="zero", marker=False):
        self.rng = np.random.default_rng(seed)
        self.marker = marker
        self.face_robot = marker  # object turned so its front (+y) faces the robot
        self.dr = domain_randomization
        self.image_size = image_size
        self.cam_pos_noise, self.cam_rot_noise, self.cam_fovy_noise = CAMERA_NOISE[camera]
        self.object_name = object_name
        self.model = self._build(object_name)
        self.data = mujoco.MjData(self.model)
        m = self.model
        self.n_substeps = int(round(self.control_dt / m.opt.timestep))

        self.qadr = np.array([m.jnt_qposadr[m.joint(j).id] for j in ARM_JOINTS])
        self.obj_body = m.body(object_name).id
        self.obj_qadr = m.jnt_qposadr[m.body_jntadr[self.obj_body]]
        self.obj_geoms = np.where(m.geom_bodyid == self.obj_body)[0]
        self.obj_collision = np.array([g for g in self.obj_geoms if m.geom_contype[g]])
        self.floor = m.geom("floor").id
        self.robot_bodies = set(range(m.body("Base").id, m.nbody)) - {self.obj_body}
        self.robot_geoms = np.array([g for g in range(m.ngeom) if m.geom_bodyid[g] in self.robot_bodies])
        self.fixed_jaw_geoms = set(np.where(m.geom_bodyid == m.body("Fixed_Jaw").id)[0])
        self.moving_jaw_geoms = set(np.where(m.geom_bodyid == m.body("Moving_Jaw").id)[0])
        self.tip_geoms = [m.geom("fixed_jaw_pad_2").id, m.geom("moving_jaw_pad_2").id]
        self.keep_geoms = np.zeros(m.ngeom + 1, bool)  # pixels of these geoms are not replaced by the background
        self.keep_geoms[self.robot_geoms] = True
        self.keep_geoms[self.obj_geoms] = True

        self.start = start
        self.rest_qpos = start_pose(m, start)
        # Curriculum: fraction of episodes that really start from `start`; the rest start from the easier
        # "home" pose right above the workspace. The trainer raises it to 1.0 over the course of training.
        self.start_prob = 1.0
        self.ctrl_low, self.ctrl_high = m.actuator_ctrlrange[:, 0], m.actuator_ctrlrange[:, 1]

        # Defaults to restore before re-randomizing each episode
        self.base_mass = m.body_mass[self.obj_body]
        self.base_inertia = m.body_inertia[self.obj_body].copy()
        self.cam_id = m.camera(camera).id
        self.base_cam_pos = m.cam_pos[self.cam_id].copy()
        self.base_cam_quat = m.cam_quat[self.cam_id].copy()
        self.base_fovy = m.cam_fovy[self.cam_id]
        self.robot_visual = np.array([g for g in self.robot_geoms if m.geom_group[g] == 2])
        # Real colours come from the materials (white plastic, black motors); randomize brightness around them.
        self.base_robot_rgba = m.mat_rgba[m.geom_matid[self.robot_visual]].copy()
        self.obj_mats = np.unique([m.geom_matid[g] for g in self.obj_geoms if m.geom_matid[g] >= 0])
        # white paper stays white (only brighter/darker): keep the card out of the per-channel colour shift
        self.obj_mats = np.array([i for i in self.obj_mats if m.material(i).name != "marker_card"], int)
        self.card_mats = np.array([m.material("marker_card").id] if marker else [], int)
        self.base_card_rgba = m.mat_rgba[self.card_mats].copy()
        self.base_obj_mat_rgba = m.mat_rgba[self.obj_mats].copy()
        self.base_light_diffuse = m.light_diffuse.copy()
        self.base_light_dir = m.light_dir.copy()
        self.base_kp = m.actuator_gainprm[:, 0].copy()

        self._renderer = None  # created on first render (training without images needs no OpenGL)
        if background is None:  # default: plain dark-grey table mat
            self.base_background = np.full((self.render_size, self.render_size, 3), 40, np.uint8)
        else:
            bg = cv2.cvtColor(cv2.imread(str(background)), cv2.COLOR_BGR2RGB)
            self.base_background = cv2.resize(bg, (self.render_size, self.render_size), interpolation=cv2.INTER_AREA)
        self.background = self.base_background

        self.state_dim = 12
        self.priv_dim = 13
        self.action_dim = 6

    def _build(self, name):
        scene = mujoco.MjSpec.from_file(str(ARM_SCENE))
        obj = mujoco.MjSpec.from_file(str(ROOT / "assets" / name / f"{name}.xml"))
        if self.marker:
            self._add_marker_card(obj, name)
        self._extend(scene, obj, name)
        scene.worldbody.add_frame(pos=[0, -0.22, 0]).attach_body(obj.body(name), "", "")
        for key in list(scene.keys):  # keyframes are rebuilt below for the extra free joint
            scene.delete(key)
        # Fixed third-person camera: in front of and above the arm, looking at the workspace
        scene.worldbody.add_camera(name="third", pos=CAMERA_POS, xyaxes=look_at(CAMERA_POS, CAMERA_TARGET), fovy=50)
        # Wrist camera rides on the fixed jaw; Squint's wrist camera field of view is 71 degrees
        scene.body("Fixed_Jaw").add_camera(name="wrist", pos=WRIST_POS, fovy=71,
                                           xyaxes=look_at(WRIST_POS, WRIST_TARGET, up=(0, 0, 1)))
        model = scene.compile()
        arm = mujoco.MjModel.from_xml_path(str(ARM_SCENE))
        adr = model.jnt_qposadr[-1]
        for i in range(arm.nkey):
            qpos = list(arm.key_qpos[i])
            qpos[adr:adr] = [0, -0.22, 0, 1, 0, 0, 0]
            scene.add_key(name=arm.key(i).name, qpos=qpos, ctrl=list(arm.key_ctrl[i]))
        return scene.compile()

    def _extend(self, scene, obj, name):
        """Hook for subclasses: add cameras/geoms before the scene is compiled."""

    @staticmethod
    def _add_marker_card(obj, name):
        """Paper card with the ArUco marker on the object's +y face (visual only: thin paper, no collision)."""
        c = MARKER_CARD
        front = front_face_y(obj, name)
        obj.add_texture(name="marker_card", type=mujoco.mjtTexture.mjTEXTURE_2D, file=str(marker_card_png()))
        mat = obj.add_material(name="marker_card")
        mat.textures[mujoco.mjtTextureRole.mjTEXROLE_RGB] = "marker_card"
        body, z = obj.body(name), c["bottom"] + c["height"] / 2
        visual = dict(type=mujoco.mjtGeom.mjGEOM_BOX, contype=0, conaffinity=0, group=2, density=0)
        # printed face, and a plain white back so the paper's back side doesn't show the marker
        body.add_geom(size=[c["width"] / 2, 0.0002, c["height"] / 2], pos=[0, front + 0.0008, z],
                      material="marker_card", **visual)
        body.add_geom(size=[c["width"] / 2, 0.0002, c["height"] / 2], pos=[0, front + 0.0003, z],
                      rgba=[0.95, 0.95, 0.95, 1], **visual)

    # ── Episode ────────────────────────────────────────────────────────────
    def reset(self):
        m, d = self.model, self.data
        mujoco.mj_resetData(m, d)
        if self.dr:
            self._randomize()
        easy = self.start != "home" and self.rng.uniform() > self.start_prob
        self.rest_qpos = start_pose(m, "home" if easy else self.start)
        q0 = self.rest_qpos + (self.rng.normal(0, self.init_qpos_noise, 6) if self.dr else 0)
        d.qpos[self.qadr] = q0
        xy = self.spawn_center + self.rng.uniform(-1, 1, 2) * self.spawn_half
        if self.face_robot:  # card (object +y) turned toward the robot base, within +-MARKER_YAW
            yaw = np.arctan2(xy[0], -xy[1]) + self.rng.uniform(-1, 1) * MARKER_YAW
        else:
            yaw = self.rng.uniform(-np.pi, np.pi)
        d.qpos[self.obj_qadr:self.obj_qadr + 7] = [xy[0], xy[1], 0.002, np.cos(yaw / 2), 0, 0, np.sin(yaw / 2)]
        self.target = np.clip(q0, self.ctrl_low, self.ctrl_high)
        d.ctrl[:] = self.target
        mujoco.mj_forward(m, d)
        for _ in range(50):  # let the object settle on the table
            mujoco.mj_step(m, d)
        self.steps = 0
        return self._obs()

    def _randomize(self):
        m, r = self.model, self.rng
        s = r.uniform(*self.mass_scale)
        m.body_mass[self.obj_body] = self.base_mass * s
        m.body_inertia[self.obj_body] = self.base_inertia * s
        m.geom_friction[self.obj_collision, 0] = r.uniform(*self.friction_range)
        m.cam_pos[self.cam_id] = self.base_cam_pos + r.uniform(-1, 1, 3) * self.cam_pos_noise
        axis = r.normal(size=3)
        dq = np.zeros(4)
        mujoco.mju_axisAngle2Quat(dq, axis / np.linalg.norm(axis), r.uniform(0, self.cam_rot_noise))
        q = np.zeros(4)
        mujoco.mju_mulQuat(q, dq, self.base_cam_quat)
        m.cam_quat[self.cam_id] = q
        m.cam_fovy[self.cam_id] = self.base_fovy + r.uniform(-1, 1) * self.cam_fovy_noise
        # Lighting: brightness, warm/cool tint, direction, plus the camera headlight (ambient + diffuse)
        tint = r.uniform(0.92, 1.08, 3)  # warm/cool shift; stronger tints turned the white robot purple/green
        m.light_diffuse[:] = self.base_light_diffuse * r.uniform(0.3, 1.3) * tint
        m.light_dir[:] = self.base_light_dir
        m.light_dir[:, :2] = r.uniform(-0.6, 0.6, (m.nlight, 2))
        m.vis.headlight.ambient[:] = r.uniform(0.1, 0.6) * tint
        m.vis.headlight.diffuse[:] = r.uniform(0.2, 0.8) * tint
        # Colours: robot shade, object shade, background brightness/tint (real mats are never exactly grey 40)
        m.geom_rgba[self.robot_visual] = self.base_robot_rgba
        m.geom_rgba[self.robot_visual, :3] *= r.uniform(0.75, 1.0)
        m.mat_rgba[self.obj_mats] = self.base_obj_mat_rgba
        m.mat_rgba[self.obj_mats, :3] *= r.uniform(0.8, 1.1, (len(self.obj_mats), 3))
        m.mat_rgba[self.card_mats] = self.base_card_rgba
        m.mat_rgba[self.card_mats, :3] *= r.uniform(0.75, 1.0)
        bg = self.base_background.astype(np.float32) * r.uniform(0.5, 1.8) * r.uniform(0.85, 1.15, 3)
        self.background = np.clip(bg, 0, 255).astype(np.uint8)
        # Motor strength (position-servo stiffness)
        kp = self.base_kp * r.uniform(*self.motor_strength, len(self.base_kp))
        m.actuator_gainprm[:, 0] = kp
        m.actuator_biasprm[:, 1] = -kp

    def step(self, action):
        m, d = self.model, self.data
        action = np.clip(action, -1, 1)
        self.target = np.clip(self.target + action * self.action_scale, self.ctrl_low, self.ctrl_high)
        d.ctrl[:] = self.target
        for _ in range(self.n_substeps):
            mujoco.mj_step(m, d)
        self.steps += 1
        info = self._evaluate()
        reward = self._reward(info)
        truncated = self.steps >= self.max_steps
        return self._obs(), reward, False, truncated, info

    # ── Task logic (same structure as Squint's Lift) ───────────────────────
    def _contacts(self):
        fixed = moving = robot_floor = False
        for c in self.data.contact[: self.data.ncon]:
            g1, g2 = c.geom1, c.geom2
            if g2 in self.obj_geoms:
                g1, g2 = g2, g1
            if g1 in self.obj_geoms:
                fixed |= g2 in self.fixed_jaw_geoms
                moving |= g2 in self.moving_jaw_geoms
            elif self.floor in (g1, g2):
                other = g2 if g1 == self.floor else g1
                robot_floor |= other in self.robot_geoms and self.model.geom_bodyid[other] != self.model.body("Base").id
        return fixed and moving, robot_floor

    def tcp(self):
        return self.data.geom_xpos[self.tip_geoms].mean(0)

    def _evaluate(self):
        grasped, touching_table = self._contacts()
        height = self.data.xpos[self.obj_body][2]  # body origin = bottom of the object
        dist_rest = np.linalg.norm(self.target[:5] - self.rest_qpos[:5])
        lifted = height > 0.01
        # Assets come out of the pipeline resting upright, so tilt = angle between object z and world z
        tilt = np.arccos(np.clip(self.data.xmat[self.obj_body][8], -1, 1))
        upright = bool(tilt < self.tipped_angle)
        # Tilted counts as knocked over even while held: otherwise "knock it over, then grab it lying down"
        # dodges the penalty and still collects grasp reward (this is what the first overnight run learned).
        knocked_over = not upright
        return dict(grasped=grasped, touching_table=touching_table, lifted=lifted, knocked_over=knocked_over,
                    upright=upright, height=float(height),
                    tcp_dist=np.linalg.norm(self.data.xipos[self.obj_body] - self.tcp()),  # reach the centre of mass
                    dist_rest=dist_rest,
                    success=bool(grasped and upright and height > self.lift_height))

    def _reward(self, info):
        r = 1 - np.tanh(5 * info["tcp_dist"])
        # grasp and lift rewards only count while the object is upright: tipping it over earns nothing
        r += info["grasped"] * info["upright"]
        r += 3.0 * info["grasped"] * info["upright"] * min(1.0, info["height"] / self.lift_height)
        r += 2.0 * info["success"]
        r -= 1.0 * info["touching_table"]
        r -= 0.5 * (not info["lifted"])
        r -= 2.0 * info["knocked_over"]  # every step the object lies knocked over costs extra
        return float(r)

    # ── Observations ───────────────────────────────────────────────────────
    def render_rgb(self):
        """128x128 camera image with the background swapped for the table photo (Squint's greenscreen)."""
        if self._renderer is None:
            self._renderer = mujoco.Renderer(self.model, self.render_size, self.render_size)
        r = self._renderer
        r.update_scene(self.data, camera=self.cam_id)
        rgb = r.render()
        r.enable_segmentation_rendering()
        r.update_scene(self.data, camera=self.cam_id)
        geom_id = r.render()[:, :, 0]
        r.disable_segmentation_rendering()
        keep = self.keep_geoms[np.where(geom_id < 0, self.model.ngeom, geom_id)]
        return np.where(keep[..., None], rgb, self.background)

    def _obs(self):
        img = cv2.resize(self.render_rgb(), (self.image_size, self.image_size), interpolation=cv2.INTER_AREA)
        qpos = self.data.qpos[self.qadr]
        noisy = qpos + (self.rng.normal(0, self.qpos_obs_noise, 6) if self.dr else 0)
        state = np.concatenate([noisy, self.target]).astype(np.float32)
        obj = self.data.xpos[self.obj_body]
        tcp = self.tcp()
        grasped, _ = self._contacts()
        priv = np.concatenate([qpos, obj, tcp - obj, [float(grasped)]]).astype(np.float32)
        return dict(image=img, state=state, priv=priv)
