"""Step 3: digital twin. Torque OFF: you move the real arm by hand and the SO-100 simulation follows live.

    python robot/twin.py

Keys go in THIS TERMINAL (click it first; no Enter needed). If a sim joint moves the opposite way to the
real one, press that joint's number to flip its direction:
    1 shoulder_pan   2 shoulder_lift   3 elbow_flex   4 wrist_flex   5 wrist_roll   6 gripper
    s save now       q quit
The directions are also saved automatically when you quit or close the viewer window.
"""
import json
import sys
import time
from pathlib import Path

import mujoco
import mujoco.viewer

from arm import SOArm, read_key as terminal_key
from sim_map import JOINTS, SimMap, load_sim

ROOT = Path(__file__).resolve().parent.parent


def main():
    cfg = json.loads((Path(__file__).parent / "config.json").read_text())
    arm = SOArm(cfg["port"], ROOT / cfg["calibration"], cfg["baudrate"])
    if arm.calibration is None:
        sys.exit("No calibration yet: run robot/calibrate.py first.")
    mapping = SimMap(ROOT / cfg["sim_mapping"], arm.calibration)
    model, data = load_sim()
    names = list(JOINTS)

    def save():
        mapping.save()
        print(f"saved to {mapping.path}: " + ", ".join(f"{n} {int(s):+d}" for n, s in zip(names, mapping.sign)))

    arm.connect()
    arm.torque(False)
    print(__doc__)
    try:
        with mujoco.viewer.launch_passive(model, data) as viewer:
            while viewer.is_running():
                key = terminal_key()
                if key and key in "123456":
                    j = int(key) - 1
                    mapping.flip(j)
                    print(f"flipped {names[j]} -> direction {int(mapping.sign[j]):+d}")
                elif key and key.lower() == "s":
                    save()
                elif key and key.lower() == "q":
                    break
                data.qpos[:6] = mapping.to_sim(arm.read_degrees())
                mujoco.mj_forward(model, data)
                viewer.sync()
                time.sleep(0.02)
    finally:
        save()  # always keep what you set
        arm.disconnect(torque_off=True)


if __name__ == "__main__":
    main()
