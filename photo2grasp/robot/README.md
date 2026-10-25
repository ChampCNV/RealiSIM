# Real SO-100 control

Small, self-contained control code for the SO-100 (and SO-101) follower arm, modelled on LeRobot's
`so_follower` but without its framework. LeRobot 0.6 needs Python 3.12; this uses Feetech's own SDK so it
runs in the same Python 3.10 environment as the trained policy, and it writes calibration files in
LeRobot's format. Run everything from the project folder in a terminal (the steps are interactive).

| Step | Command | What happens |
|---|---|---|
| 0 | `.\.venv\Scripts\python.exe robot\find_port.py` | Finds the arm's USB port (skips Bluetooth ports) and lists cameras -> `config.json` |
| 1 | `.\.venv\Scripts\python.exe robot\setup_motors.py` | Once per arm: connect ONE servo at a time (gripper first); each gets its ID (6..1) and 1 Mbps |
| 2 | `.\.venv\Scripts\python.exe robot\calibrate.py` | Once: hold the middle pose, then move every joint through its full range |
| 3 | `.\.venv\Scripts\python.exe robot\twin.py` | Torque off: move the real arm, the sim follows. Keys 1-6 flip a joint that moves the wrong way, S saves |
| 4 | `.\.venv\Scripts\python.exe robot\camera_setup.py` | Pick the wrist camera, fix exposure, see the 64x64 policy view, S saves |
| 5 | `.\.venv\Scripts\python.exe robot\run_policy.py runs\<run>\actor_best.pt --dry-run` | Policy runs on the live camera, arm stays limp |
| 5 | `.\.venv\Scripts\python.exe robot\run_policy.py runs\<run>\actor_best.pt --speed 0.3` | Real run with torque: slow start, per-step limit, Ctrl+C folds the arm and turns torque off |

## Calibration: the "middle" pose
Torque is off, so you move the arm by hand. Put every joint roughly in the middle of its range: an
upside-down "L" (upper arm straight up, forearm pointing forward and level, wrist level, wrist roll
centred, gripper half open). Then move each joint (except wrist roll) through its FULL range when asked:
the middle of the recorded range becomes 0 deg, which is the simulation's zero pose.

## Files
| File | What it does |
|---|---|
| `config.json` | USB port, wrist camera index/size/exposure/field of view, file locations |
| `arm.py` | Driver: read/write joints (degrees), torque, motor settings, calibration |
| `setup_motors.py` | Servo ID + baud-rate setup, one servo at a time |
| `camera.py` | OpenCV camera with fixed exposure + `policy_view` (same image processing as the simulation) |
| `sim_map.py` | `sim_rad = sim_middle + sign * radians(real_deg)` per joint |
| `twin.py`, `camera_setup.py`, `run_policy.py`, `find_port.py`, `calibrate.py` | The steps above |

## Before the first real run
- Wrist camera mounted where the simulation has it: outside of the fixed jaw, ~5 cm out, aimed just past
  the fingertips.
- Training used a plain dark background with random brightness: a dark matte mat under the workspace.
- Lego tree 16-28 cm in front of the arm base, within ~9 cm left/right (the training spawn area).
- Start with `--dry-run`, then `--speed 0.3`. Keep a hand near the power switch.
