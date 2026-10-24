"""Step 2: guided calibration with the simulation open the whole time (LeRobot's procedure + joint directions).

    python robot/calibrate.py

Asks what to do first:
  1  full calibration: joint limits + joint directions (first time)
  2  joint limits only
  3  joint directions only (limits already done)
Answer prompts by pressing keys in THIS TERMINAL (Enter / y / n). Torque stays off: you move the arm by hand.
"""
import json
import sys
import time
from pathlib import Path

import mujoco
import mujoco.viewer
import numpy as np

from arm import FULL_TURN_MOTORS, HOMING_POSITION_DIAGRAM, MAX_POS, MOTORS, SOArm, read_key
from sim_map import JOINTS, SimMap, load_sim

ROOT = Path(__file__).resolve().parent.parent


class Guide:
    """Keeps the sim window updated while waiting for keys in the terminal."""

    def __init__(self, viewer, model, data):
        self.viewer, self.model, self.data = viewer, model, data

    def show(self, qpos):
        self.data.qpos[:6] = qpos
        mujoco.mj_forward(self.model, self.data)
        self.viewer.sync()

    def wait(self, keys, pose=None):
        """Wait for one of `keys`; meanwhile show `pose()` (live) or `pose` (fixed) in the sim."""
        while self.viewer.is_running():
            self.show(pose() if callable(pose) else pose)
            key = read_key()
            if key and key.lower() in keys:
                return key.lower()
            time.sleep(0.02)
        raise SystemExit("viewer closed")


def joint_limits(arm, mapping, guide, pid):
    arm.configure(**pid)
    arm.start_calibration()
    mapping.set_calibration(None)  # while recording, angles are measured from the held pose itself
    print(HOMING_POSITION_DIAGRAM)
    print("Video walkthrough: https://huggingface.co/docs/lerobot/main/en/so101#calibration-video")
    print("STEP 1/2 - Move the real arm to the middle of every joint's range: make it look like the SIM window.\n"
          "          Hold it still and press ENTER...")
    guide.wait("\r", mapping.middle)
    offsets = arm.set_middle()

    recorded = [m for m in MOTORS if m not in FULL_TURN_MOTORS]
    print(f"STEP 2/2 - Move {recorded} one by one through their ENTIRE range (end stop to end stop, gently).\n"
          "          The sim follows (directions are checked later). % = share of the joint's expected range\n"
          "          (the sim model's) covered so far: aim for ~90-100% on every joint. Press ENTER when done.")
    raw = arm.read_raw()
    lo, hi = dict(raw), dict(raw)
    covered = lambda m: (hi[m] - lo[m]) * 360 / MAX_POS / mapping.span_deg[m]  # noqa: E731

    def live():
        raw = arm.read_raw()
        for m in recorded:
            lo[m], hi[m] = min(lo[m], raw[m]), max(hi[m], raw[m])
        print("  " + "  ".join(f"{m} {100 * covered(m):3.0f}%" for m in recorded) + "   ", end="\r")
        # after set_middle, raw 2047 = the pose held in step 1, which lines up with the sim's zero pose
        return mapping.to_sim({m: (raw[m] - MAX_POS // 2) * 360 / MAX_POS for m in MOTORS})

    while True:
        guide.wait("\r", live)
        short = [m for m in recorded if covered(m) < 0.8]
        if not short:
            break
        # The middle of the recorded range becomes 0 deg = the sim's middle: an incomplete range shifts it.
        print(f"\n  {short} covered less than 80% of their expected range: their zero would be off and the sim "
              "would not line up.\n  Keep moving them and press ENTER again, or press 'a' to accept anyway.")
        if guide.wait("\ra", live) == "a":
            break
    print()
    arm.finish_calibration(offsets, lo, hi)
    mapping.set_calibration(arm.calibration)  # same reference as during recording: no jump


def joint_directions(arm, mapping, guide):
    if arm.calibration is None:
        raise SystemExit("Joint limits are not calibrated yet: choose option 1 or 2 first.")
    live = lambda: mapping.to_sim(arm.read_degrees())  # noqa: E731
    print("\nJOINT DIRECTIONS - for each joint, move ONLY that joint slowly back and forth on the real arm.")
    for j, (motor, sim_joint) in enumerate(JOINTS.items()):
        while True:
            print(f"  [{j + 1}/6] {motor}: does the sim move the SAME way as the real arm?  y = yes   n = no (flip)")
            if guide.wait("yn", live) == "y":
                break
            mapping.flip(j)
            print(f"        flipped {motor} -> direction {int(mapping.sign[j]):+d}; check it again")
    mapping.save()
    print(f"directions saved to {mapping.path}")
    print("\nCHECK - move the whole arm freely; the sim should copy every joint. Press ENTER to finish.")
    guide.wait("\r", live)


def main():
    print(__doc__)
    choice = input("What do you want to do? [1/2/3, q to quit]: ").strip().lower()
    if choice not in ("1", "2", "3"):
        return
    cfg = json.loads((Path(__file__).parent / "config.json").read_text())
    arm = SOArm(cfg["port"], ROOT / cfg["calibration"], cfg["baudrate"])
    mapping = SimMap(ROOT / cfg["sim_mapping"], arm.calibration)
    model, data = load_sim()
    arm.connect()
    arm.torque(False)
    try:
        with mujoco.viewer.launch_passive(model, data) as viewer:
            guide = Guide(viewer, model, data)
            if choice in ("1", "2"):
                joint_limits(arm, mapping, guide, cfg.get("pid", {}))
            if choice in ("1", "3"):
                joint_directions(arm, mapping, guide)
            print("\nAll done. You can close the sim window.")
    finally:
        arm.disconnect(torque_off=True)


if __name__ == "__main__":
    main()
