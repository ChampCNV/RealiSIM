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
ARM_SCENE = ROOT / "third_party" / "mujoco_menagerie" / "trs_so_arm100" / "scene.xml"
# real motor name -> SO-100 simulation joint (same order as the motor IDs 1-6)
JOINTS = {"shoulder_pan": "Rotation", "shoulder_lift": "Pitch", "elbow_flex": "Elbow",
          "wrist_flex": "Wrist_Pitch", "wrist_roll": "Wrist_Roll", "gripper": "Jaw"}
HALF_TURN, MAX_POS = 2047, 4095


def load_sim():
    model = mujoco.MjModel.from_xml_path(str(ARM_SCENE))
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
        self.path.write_text(json.dumps({m: dict(sim_joint=JOINTS[m], sign=int(s), offset_deg=round(float(o), 2))
                                         for m, s, o in zip(JOINTS, self.sign, self.offset)}, indent=2))
        self.ready = True
