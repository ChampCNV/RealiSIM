"""Real arm <-> simulation joint mapping.

In calibration step 1 you hold the real arm in the same pose the sim shows (middle of every joint's range);
that held pose is raw 2047 on every motor and is matched to the sim's middle pose. So per joint:

    sim_angle_rad = sim_middle + sign * radians(real_deg - held_deg)

`held_deg` (where the held pose sits in calibrated degrees) comes from the calibration file; `sign` (+1/-1)
is set in calibrate.py's direction step. Using the held pose (not the middle of the recorded range) avoids a
jump when a real joint's range is shorter than the model's.
"""
import json
from pathlib import Path

import mujoco
import numpy as np

ROOT = Path(__file__).resolve().parent.parent
MENAGERIE = ROOT / "third_party" / "mujoco_menagerie"
SCENES = {"so100": MENAGERIE / "trs_so_arm100" / "scene.xml", "so101": MENAGERIE / "robotstudio_so101" / "scene.xml"}
ARM_SCENE = SCENES["so100"]
# real motor name -> simulation joint, per arm model (same order as the motor IDs 1-6)
SIM_JOINTS = {"so100": {"shoulder_pan": "Rotation", "shoulder_lift": "Pitch", "elbow_flex": "Elbow",
                        "wrist_flex": "Wrist_Pitch", "wrist_roll": "Wrist_Roll", "gripper": "Jaw"},
              "so101": {m: m for m in ("shoulder_pan", "shoulder_lift", "elbow_flex", "wrist_flex", "wrist_roll",
                                       "gripper")}}
JOINTS = SIM_JOINTS["so100"]  # the motor names (keys) are the same for every model
HALF_TURN, MAX_POS = 2047, 4095


ROBOT_XML = {"so101": ("robotstudio_so101/so101.xml", "base"), "so100": ("trs_so_arm100/so_arm100.xml", "Base")}
# the closed gripper's tip is the mean of these geoms (touch calibration, fingertip distances)
TIP_GEOMS = {"so100": ["fixed_jaw_pad_1", "moving_jaw_pad_1"],
             "so101": [f"{jaw}_jaw_sph_tip{i}" for jaw in ("fixed", "moving") for i in (1, 2, 3)]}


def arm_placement(model_name):
    """(pos, quat) to put an arm model into the robot frame every part of this project uses, the SO-100's:
    reaching straight out along -y at its zero pose, turning about the same vertical axis. From the model
    itself: its pan axis and where its fingertips point at the middle of every joint's range."""
    if model_name == "so100":
        return np.zeros(3), np.array([1.0, 0, 0, 0])
    ref, axis = {}, {}
    for name in ("so100", model_name):
        xml, _ = ROBOT_XML[name]
        m = mujoco.MjModel.from_xml_path(str(MENAGERIE / xml))
        d = mujoco.MjData(m)
        d.qpos[:6] = (m.jnt_range[:6, 0] + m.jnt_range[:6, 1]) / 2
        mujoco.mj_kinematics(m, d)
        axis[name] = d.xanchor[0].copy()
        ref[name] = d.geom_xpos[[m.geom(g).id for g in TIP_GEOMS[name]]].mean(0) - axis[name]
    turn = np.arctan2(-1.0, 0.0) - np.arctan2(ref[model_name][1], ref[model_name][0])  # its reach -> -y
    c, s = np.cos(turn), np.sin(turn)
    R = np.array([[c, -s, 0], [s, c, 0], [0, 0, 1.0]])
    pos = axis["so100"] - R @ axis[model_name]
    pos[2] = 0.0  # bases on the table
    return pos, np.array([np.cos(turn / 2), 0, 0, np.sin(turn / 2)])


def arm_model():
    """The active arm's model (robot/arms.py; arms set up before that are SO-100s)."""
    cfg = ROOT / "robot" / "config.json"
    return json.loads(cfg.read_text()).get("arm_model", "so100") if cfg.exists() else "so100"


def require_arm_model(needed="so100", what="this"):
    """Stop if the active arm is another model than the simulation this script (or trained model) uses: driving
    an SO-101 with SO-100 geometry would put the gripper somewhere else than the sim thinks."""
    active = arm_model()
    if active != needed:
        raise SystemExit(f"{what} uses the {needed.upper()} model, but the active arm is an {active.upper()} "
                         f"(robot/arms.py list). Models trained for the {needed.upper()} cannot drive it; switch arms "
                         f"with robot/arms.py use <name>, or use a model trained for the {active.upper()}.")


def load_sim(model_name=None):
    model = mujoco.MjModel.from_xml_path(str(SCENES[model_name or arm_model()]))
    return model, mujoco.MjData(model)


def sim_pose(model, key):
    return model.key_qpos[model.key(key).id][:6].copy()


class SimMap:
    def __init__(self, path, calibration=None):
        self.path = Path(path)
        model, _ = load_sim()
        self.middle = (model.jnt_range[:6, 0] + model.jnt_range[:6, 1]) / 2
        self.span_deg = dict(zip(JOINTS, np.degrees(model.jnt_range[:6, 1] - model.jnt_range[:6, 0])))
        data = json.loads(self.path.read_text()) if self.path.exists() else {}
        self.sign = np.array([data.get(m, {}).get("sign", 1) for m in JOINTS], float)
        # extra per-joint shift (deg), e.g. so the sim's fully closed gripper is the real one's (gripper_check.py)
        self.offset = np.array([data.get(m, {}).get("offset_deg", 0.0) for m in JOINTS], float)
        self.ready = self.path.exists()
        self.set_calibration(calibration)

    def set_calibration(self, calibration):
        """held_deg per joint: the calibrated angle of the pose held in calibration step 1 (raw 2047)."""
        self.held = np.zeros(6) if not calibration else np.array(
            [(HALF_TURN - (calibration[m]["range_min"] + calibration[m]["range_max"]) / 2) * 360 / MAX_POS
             for m in JOINTS])

    def to_sim(self, real_deg):
        """dict of real joint degrees -> array of 6 sim joint angles (rad)."""
        d = np.array([real_deg[m] for m in JOINTS])
        return self.middle + self.sign * np.radians(d - self.held - self.offset)

    def to_real(self, sim_rad):
        """array of 6 sim joint angles (rad) -> dict of real joint degrees."""
        d = self.sign * np.degrees(np.asarray(sim_rad) - self.middle) + self.held + self.offset
        return dict(zip(JOINTS, d))

    def flip(self, j):
        self.sign[j] *= -1

    def save(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        names = SIM_JOINTS[arm_model()]
        self.path.write_text(json.dumps({m: dict(sim_joint=names[m], sign=int(s), offset_deg=round(float(o), 2))
                                         for m, s, o in zip(JOINTS, self.sign, self.offset)}, indent=2))
        self.ready = True
