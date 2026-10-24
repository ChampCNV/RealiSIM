# Photo2Grasp

**From phone photos of a real object to a robot arm that picks it up, trained only in simulation.**

Take a few photos of an object (or a video of a table scene). Photo2Grasp turns it into a simulation-ready
MuJoCo object with real size, mass and friction, trains a pick-up policy for the low-cost **SO-100** arm
entirely in simulation, and runs that policy on the real arm with no real-world training data.

![Assets made from photos](outputs/demo/asset_grid.png)

## How it works

```
photos ─► TRELLIS 3D mesh ─► clean + straighten + scale to real size ─► CoACD collision shapes
       ─► mass / inertia / friction ─► MuJoCo asset ─► SO-100 training scene
                                                            │
             reinforcement learning in simulation (SAC) ◄───┘
                                │
                                ▼
   real SO-100: webcam ─► AprilTag ─► object position ─► policy ─► joint commands
```

**1. Photos → MuJoCo object** (`make_asset.py`, `pipeline/`)
- 3D mesh from one or more photos with **TRELLIS** (Hugging Face Space).
- Generic mesh cleanup, no per-object tweaks: background removal, stable resting pose, real-world size,
  convex collision pieces (CoACD), mass from density x volume, textured visual mesh.
- Real size, mass and friction come from known data (`objects.json`) with the source written down.

**2. Video of a table → MuJoCo scene** (`scene/`)
- COLMAP camera poses and point cloud from a phone video.
- Finds the table plane and metric scale from the table's real size, then every object standing on it.
- Builds each object with TRELLIS and places the table, objects and SO-100 in one MuJoCo scene.

**3. Simulation training, AprilTag tracking** (`sim2real/`)
- The policy never sees images. It gets joint angles plus the object's position from an **AprilTag** on the
  object, so lighting and background differences between sim and reality don't matter.
- The tag detection is simulated the way the real robot does it:
  - project the tag into a randomized webcam;
  - keep it only if it would really be detected (in view, large enough, not too slanted, not hidden by the
    gripper);
  - add pixel noise and missed frames;
  - solve its pose with OpenCV (`solvePnP`) using the nominal lens and camera mount.
  So camera, mount and calibration errors on the real robot are already part of training.
- Two cameras: a webcam on the gripper (tag on the front of the object) and a fixed webcam across the
  table (tag on the back, which the arm never blocks).
- Before each attempt the arm holds a "look" pose and scans left/right/up/down until it finds the tag.
  Afterwards it tracks the tag live, 10 times a second.
- Domain randomization: object position and angle, mass, friction, motor strength, camera mounts and
  lenses, joint-reading noise.
- Small MLP policies trained with SAC and an asymmetric critic (the critic sees the true object state during
  training only). Trains on a CPU or a free Colab T4, no rendering needed.

**4. Real SO-100** (`robot/`)
- Motor ID setup and calibration following LeRobot's procedure, with the MuJoCo arm shown alongside.
- Digital twin (torque off): the real arm drives the sim.
- Runs the trained policy with slow, speed-limited moves: zero pose → look pose → scan → pick up.

## Results (simulation)

AprilTag lift policy, 2M training steps, 200 test attempts with random placement and randomization:

| Outcome | Share |
|---|---|
| Picked up and lifted 5 cm, upright | **87.5%** |
| Knocked the object over | 10% |
| Missed the grasp | 2.5% |

The policy in `runs/tag_colab_2m/actor_best.pt` uses the wrist camera only. The two-camera version (wrist +
environment camera) continues training from it. Real-robot testing is in progress.

## Quick start

```powershell
# watch the trained policy in MuJoCo (both simulated webcams + the real OpenCV detector on them)
cd sim2real
..\.venv\Scripts\python.exe view_tag_env.py ..\runs\tag_colab_2m\actor_best.pt

# make a MuJoCo object from photos (add its size/mass/friction to objects.json first)
.\.venv\Scripts\python.exe make_asset.py so101_motor_holder inputs\printed_frame.jpg
.\.venv\Scripts\python.exe make_scene.py so101_motor_holder        # drop test + viewer

# table scene from a video
python scene/reconstruct.py inputs/scene/1.mp4 outputs/scene
python scene/find_objects.py outputs/scene --table 48 37 65         # table length, width, height in cm
python scene/object_views.py outputs/scene
python scene/build_scene.py outputs/scene
python scene/view_scene.py outputs/scene
```

### Train the AprilTag policy

```powershell
cd sim2real
..\.venv\Scripts\python.exe train_tag.py --steps 2000000
```

Or on Colab without Google Drive: run `python colab/build_zip.py`, then follow `colab/colab_cell.py`. It
uploads the zip, trains on a T4, downloads a backup every 250k steps, and can resume from a backup if the
session disconnects.

### Run on the real SO-100

Hardware: SO-100 arm (Feetech STS3215 servos), a USB webcam glued above the gripper jaws, optionally a
second webcam across the table, and AprilTags 36h11 printed from `outputs/apriltags/` (ID 0 on the front of
the object, ID 1 on the back, 25 mm, centre 4 cm above the table).

```powershell
.\.venv\Scripts\python.exe robot\find_port.py        # find the arm's port -> robot/config.json
.\.venv\Scripts\python.exe robot\setup_motors.py     # once per arm: motor IDs
.\.venv\Scripts\python.exe robot\calibrate.py        # joint limits + directions, sim shown alongside
.\.venv\Scripts\python.exe robot\twin.py             # digital twin, torque off

# check the tag numbers with torque off, then run slowly
.\.venv\Scripts\python.exe robot\run_tag_policy.py runs\tag_colab_2m\actor_best.pt --dry-run
.\.venv\Scripts\python.exe robot\run_tag_policy.py runs\tag_colab_2m\actor_best.pt --speed 0.3
```

Details for each robot step are in [robot/README.md](robot/README.md).

## Repository

| Path | What it is |
|---|---|
| `make_asset.py`, `make_scene.py` | Photos → MuJoCo asset; drop test + viewer |
| `objects.json` | Real size, mass, friction (with sources) per object |
| `pipeline/` | TRELLIS client, background removal, mesh cleanup → MJCF |
| `scene/` | Video → table scene in MuJoCo (COLMAP, table plane, objects) |
| `assets/` | Generated MuJoCo objects (Lego tree, table, bottle, ...) |
| `sim2real/` | `tag_env.py` (task + simulated AprilTag detection), `train_tag.py` (SAC), `view_tag_env.py` |
| `robot/` | Real SO-100: motor setup, calibration, digital twin, policy runner |
| `colab/` | Colab training without Google Drive (zip builder + cell) |
| `runs/tag_colab_2m/` | Trained AprilTag policy + training log |
| `outputs/apriltags/` | Printable AprilTag sheet (IDs 0-20, 25 mm) |

## Setup

Windows, Python 3.10, NVIDIA GPU recommended (developed on a 4 GB laptop GPU).

```powershell
py -3.10 -m venv .venv
.\.venv\Scripts\python.exe -m pip install torch torchvision --index-url https://download.pytorch.org/whl/cu118
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\python.exe -m pip uninstall -y opencv-python-headless
git clone --depth 1 --filter=blob:none --sparse https://github.com/google-deepmind/mujoco_menagerie.git third_party/mujoco_menagerie
git -C third_party/mujoco_menagerie sparse-checkout set trs_so_arm100
```

Set `HF_TOKEN` (Hugging Face) as an environment variable for TRELLIS. Never commit it.

## Credits

- SO-100 model: [MuJoCo Menagerie](https://github.com/google-deepmind/mujoco_menagerie) (`trs_so_arm100`)
- 3D generation: [TRELLIS](https://github.com/microsoft/TRELLIS) (Microsoft) via Hugging Face Spaces
- Physics: [MuJoCo](https://mujoco.org); collision shapes: [CoACD](https://github.com/SarahWeiii/CoACD)
- Robot setup and calibration procedure modelled on [LeRobot](https://github.com/huggingface/lerobot)
- Sim-to-real ideas from [Squint](https://arxiv.org/abs/2602.21203) and [ManiSkill](https://github.com/haosulab/ManiSkill)
- Tag detection: OpenCV ArUco module (AprilTag 36h11)
