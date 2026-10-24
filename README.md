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

`runs/tag_envcam_2250k/actor_last.pt` (wrist + environment camera) picks the Lego tree up on the real SO-100.

## The pipeline: one command, step by step

| Step | Command | What happens |
|---|---|---|
| 1. Object | `python photo2grasp.py asset celsius_can inputs\can_1.mp4 inputs\can_2.mp4` | Any number of videos or photos. The best 4 frames are picked by fixed rules (sharp, whole object, different sides). Real size, mass and friction come from `objects.json`, or you are asked (optional `--claude`: Claude looks them up online through the Anthropic API). TRELLIS builds the mesh, then cleanup, real size, collision shapes and a MuJoCo asset, plus a drop test |
| 2. Look | `python photo2grasp.py check celsius_can` | The object next to the SO-100 in the MuJoCo viewer |
| (once) Arm | `python photo2grasp.py robot` | LeRobot-style setup: USB port, motor IDs, joint limits and directions (sim shown), gripper alignment, camera check. Skips what is already done |
| 3. Camera | `python photo2grasp.py calibrate celsius_can` | Touch tag 0 with the gripper tip at 4 spots: the arm measures, so no ruler. Finds the environment camera's position and the real printed tag size |
| 4. Train | `python photo2grasp.py train celsius_can --area wide [--colab]` | Checks the gripper can hold the object, places the tags, finds a look pose, then trains (starting from the trained Lego-tree model). `--colab` packs everything for a free Colab T4 instead (no Google Drive) |
| (Colab) | `python photo2grasp.py import tag_run.zip` | Unpacks a run downloaded from Colab into `runs/` |
| 5. Test | `python photo2grasp.py test runs\<run>\actor_last.pt` | 100 sim attempts (aim for at least 80 %) and a preview with both simulated cameras |
| 6. Run | `python photo2grasp.py run runs\<run>\actor_last.pt --env-only` | Real robot: zero pose, look pose, find tag, pick up, slowly. Between attempts: `c` re-calibrates the camera. It warns when the two cameras disagree (the camera moved), keeps the gripper above the table and within reach, and logs every attempt to `runs/real_logs/` |

Placement areas (measured from the base's rotation axis to the object): `default` 14.5-23.5 cm in front and ±8 cm
sideways; `wide` 12-28 cm and ±15 cm. Tags: AprilTag 36h11 from `outputs/apriltags/`, ID 0 facing the robot
and ID 1 on the back. On round objects, glue them on a small flat card first.

Table scene from a video (separate tool):

```powershell
python scene/reconstruct.py inputs/scene/1.mp4 outputs/scene
python scene/find_objects.py outputs/scene --table 48 37 65         # table length, width, height in cm
python scene/object_views.py outputs/scene
python scene/build_scene.py outputs/scene
python scene/view_scene.py outputs/scene
```

Details for each robot step are in [robot/README.md](robot/README.md).

## Repository

| Path | What it is |
|---|---|
| `photo2grasp.py` | The whole pipeline, step by step (see above) |
| `make_asset.py`, `make_scene.py` | Videos/photos → MuJoCo asset; drop test + viewer |
| `objects.json` | Real size, mass, friction (with sources) per object |
| `pipeline/` | Frame picking from videos, Claude object lookup, TRELLIS client, background removal, mesh cleanup → MJCF |
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

Set `HF_TOKEN` (Hugging Face) for TRELLIS and, for the automatic object lookup, `ANTHROPIC_API_KEY`, both as
environment variables. Never commit them.

## Credits

- SO-100 model: [MuJoCo Menagerie](https://github.com/google-deepmind/mujoco_menagerie) (`trs_so_arm100`)
- 3D generation: [TRELLIS](https://github.com/microsoft/TRELLIS) (Microsoft) via Hugging Face Spaces
- Physics: [MuJoCo](https://mujoco.org); collision shapes: [CoACD](https://github.com/SarahWeiii/CoACD)
- Robot setup and calibration procedure modelled on [LeRobot](https://github.com/huggingface/lerobot)
- Sim-to-real ideas from [Squint](https://arxiv.org/abs/2602.21203) and [ManiSkill](https://github.com/haosulab/ManiSkill)
- Tag detection: OpenCV ArUco module (AprilTag 36h11)
