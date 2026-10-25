"""Line up the real gripper with the simulated one: the sim's fully closed jaw = the real fully closed jaw.

    python robot/gripper_check.py

Torque stays OFF. Squeeze the gripper fully closed by hand and press ENTER: the difference between the real
closed angle and where the sim's closed jaw maps today is saved as the gripper's offset in
robot/calibration/sim_mapping.json (without it, "closed" in the policy can stop a few degrees short and never
squeeze the object). Then open/close it by hand: the printed sim jaw angle should go from about 1.0 (open)
down to about -0.17 (closed). Ctrl+C quits.
"""
import json
import sys
import time
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parent.parent
from arm import SOArm  # noqa: E402
from sim_map import JOINTS, SimMap, load_sim  # noqa: E402


def main():
    cfg = json.loads((Path(__file__).parent / "config.json").read_text())
    arm = SOArm(cfg["port"], ROOT / cfg["calibration"], cfg["baudrate"])
    mapping = SimMap(ROOT / cfg["sim_mapping"], arm.calibration)
    model, _ = load_sim()
    j = list(JOINTS).index("gripper")
    sim_closed = model.jnt_range[j, 0]  # the sim jaw's fully closed limit (rad)
    arm.connect()
    arm.torque(False)
    try:
        input("Squeeze the gripper FULLY CLOSED by hand, keep holding, press ENTER...")
        real_closed = np.median([arm.read_degrees()["gripper"] for _ in range(10)])
        mapping.offset[j] = 0.0
        sim = mapping.middle.copy()
        sim[j] = sim_closed
        expected = mapping.to_real(sim)["gripper"]  # where sim-closed lands on the real gripper without offset
        mapping.offset[j] = real_closed - expected
        mapping.save()
        print(f"real closed {real_closed:.1f} deg, sim closed mapped to {expected:.1f} deg -> gripper offset "
              f"{mapping.offset[j]:+.1f} deg saved to {mapping.path}")
        print("Now open and close it by hand: sim jaw should read ~1.0 open ... ~-0.17 closed. Ctrl+C quits.")
        while True:
            q = mapping.to_sim(arm.read_degrees())[j]
            print(f"   sim jaw {q:+.2f} rad", end="\r")
            time.sleep(0.1)
    except KeyboardInterrupt:
        print()
    finally:
        arm.disconnect(torque_off=True)


if __name__ == "__main__":
    sys.exit(main())
