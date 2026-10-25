"""RealiSIM workcell package (runs in the repo's Python environment).

The composed workcell (any robot model + measured table + reconstructed objects) as one self-contained MuJoCo scene
and a generic Gymnasium environment. No task is baked in: pass your own reward/termination, or start from tasks.py.

    python workcell_export.py --robot-dir DIR --assets A --names a,b --table L,W,H --places JSON [--front fx,fy] [--table-pose JSON]
                              --out OUT --version V --env-id ID --slug SLUG --provenance prov.json

Multi-arm: pass --robots JSON (same format as workcell.py) instead of --robot-dir. Every arm is namespaced "<name>/";
workcell.json lists each arm's joints, actuators, action slice, observation indices and end-effector body.

Prints one `EXPORT_RESULT {json}` line.
"""
import argparse, hashlib, json, math, re, shutil, sys, zipfile
from pathlib import Path
import numpy as np
import mujoco

sys.path.insert(0, str(Path(__file__).resolve().parent))
import workcell as W

PINS = {"gymnasium": "1.4.0", "mujoco": "3.16.0", "numpy": "2.2.6"}
TRAIN_PINS = {"stable-baselines3": "2.9.0"}

ENV_TEMPLATE = r"""
# RealiSIM workcell environment (generated). Robot: @ROBOT@. Objects: @OBJECTS@.
import json, math
from pathlib import Path

import gymnasium as gym
import mujoco
import numpy as np
from gymnasium import spaces

HERE = Path(__file__).resolve().parent
SCENE_XML = HERE / "scene" / "scene.xml"
META = json.loads((HERE / "workcell.json").read_text())
FREE = int(mujoco.mjtJoint.mjJNT_FREE)


class WorkcellEnv(gym.Env):
    # Generic environment over the reconstructed workcell. Action: normalized actuator controls in [-1, 1], mapped to
    # each actuator's control range (for MuJoCo Menagerie arms these are joint position targets). Observation: robot
    # joint positions and velocities, then for every object its position (3), quaternion (4) and velocity (6).
    # IMPORTANT: the observation is privileged SIMULATOR STATE (exact object poses read from MuJoCo), not a
    # deployment-ready sensor observation. A real robot needs a validated perception/state-estimation mapping first.
    # reward_fn(env, info) -> float and terminate_fn(env, info) -> bool make it any task you like (see tasks.py).
    metadata = {"render_modes": ["rgb_array"], "render_fps": 25}

    def __init__(self, reward_fn=None, terminate_fn=None, frame_skip=20, randomize_objects=True,
                 position_noise_m=0.02, yaw_noise_deg=15.0, render_mode=None, width=640, height=480):
        self.model = mujoco.MjModel.from_xml_path(str(SCENE_XML))
        self.data = mujoco.MjData(self.model)
        self.frame_skip = int(frame_skip)
        self.reward_fn, self.terminate_fn = reward_fn, terminate_fn
        self.randomize_objects = bool(randomize_objects)
        self.position_noise_m, self.yaw_noise_deg = float(position_noise_m), float(yaw_noise_deg)
        self.render_mode, self._size, self._renderer = render_mode, (int(height), int(width)), None
        m = self.model
        self.home = m.key("home").id
        self.objects = list(META["objects"])
        self.object_body = {n: m.body(n).id for n in self.objects}
        self._oq = [int(m.jnt_qposadr[m.body_jntadr[b]]) for b in self.object_body.values()]
        self._ov = [int(m.jnt_dofadr[m.body_jntadr[b]]) for b in self.object_body.values()]
        self.robot_joints = [j for j in range(m.njnt) if int(m.jnt_type[j]) != FREE]
        self._rq = np.array([m.jnt_qposadr[j] for j in self.robot_joints], dtype=int)
        self._rv = np.array([m.jnt_dofadr[j] for j in self.robot_joints], dtype=int)
        lo, hi = m.actuator_ctrlrange[:, 0].copy(), m.actuator_ctrlrange[:, 1].copy()
        for a in range(m.nu):
            if not m.actuator_ctrllimited[a]:
                j = m.actuator_trnid[a][0]
                lo[a], hi[a] = (m.jnt_range[j] if m.jnt_limited[j] else (-1.0, 1.0))
        self._lo, self._hi = lo, hi
        self.end_effector = m.body(META["end_effector_body"]).id
        self.action_space = spaces.Box(-1.0, 1.0, (m.nu,), np.float32)
        n = 2 * len(self.robot_joints) + 13 * len(self.objects)
        self.observation_space = spaces.Box(-np.inf, np.inf, (n,), np.float32)
        self._start_z = {}

    def _obs(self):
        d = self.data
        parts = [d.qpos[self._rq], d.qvel[self._rv]]
        for qa, va in zip(self._oq, self._ov):
            parts += [d.qpos[qa:qa + 7], d.qvel[va:va + 6]]
        return np.concatenate(parts).astype(np.float32)

    def _info(self):
        d = self.data
        pos = {n: d.xpos[b].copy() for n, b in self.object_body.items()}
        up = {n: float(d.xmat[b].reshape(3, 3)[2, 2]) for n, b in self.object_body.items()}
        return {"time": float(d.time), "object_position": pos, "object_up_z": up, "start_height": dict(self._start_z),
                "end_effector": d.xpos[self.end_effector].copy()}

    def reset(self, *, seed=None, options=None):
        super().reset(seed=seed)
        mujoco.mj_resetDataKeyframe(self.model, self.data, self.home)
        if self.randomize_objects:
            for qa in self._oq:
                self.data.qpos[qa:qa + 2] += self.np_random.uniform(-self.position_noise_m, self.position_noise_m, 2)
                yaw = math.radians(self.np_random.uniform(-self.yaw_noise_deg, self.yaw_noise_deg))
                q = np.empty(4)
                mujoco.mju_mulQuat(q, np.array([math.cos(yaw / 2), 0, 0, math.sin(yaw / 2)]), self.data.qpos[qa + 3:qa + 7].copy())
                self.data.qpos[qa + 3:qa + 7] = q
        mujoco.mj_forward(self.model, self.data)
        self._start_z = {n: float(self.data.xpos[b][2]) for n, b in self.object_body.items()}
        return self._obs(), self._info()

    def step(self, action):
        a = np.clip(np.asarray(action, dtype=np.float64).reshape(self.model.nu), -1.0, 1.0)
        self.data.ctrl[:] = self._lo + (a + 1.0) * 0.5 * (self._hi - self._lo)
        mujoco.mj_step(self.model, self.data, nstep=self.frame_skip)
        obs, info = self._obs(), self._info()
        reward = float(self.reward_fn(self, info)) if self.reward_fn else 0.0
        terminated = bool(self.terminate_fn(self, info)) if self.terminate_fn else False
        return obs, reward, terminated, False, info

    def render(self):
        if self.render_mode != "rgb_array":
            return None
        if self._renderer is None:  # needs an OpenGL backend (MUJOCO_GL=egl or osmesa on headless machines)
            self._renderer = mujoco.Renderer(self.model, *self._size)
        self._renderer.update_scene(self.data)
        return self._renderer.render()

    def close(self):
        if self._renderer is not None:
            self._renderer.close()
            self._renderer = None
"""

TASKS_TEMPLATE = r"""
# Example tasks for the RealiSIM workcell. Each is a pair of callables you pass to the environment:
#   env = gym.make(ENV_ID, reward_fn=lift_reward("box"), terminate_fn=lift_success("box"))
# Write your own the same way: f(env, info) -> float (reward) or bool (terminate). env.model / env.data are MuJoCo.
import numpy as np


def reach_reward(obj):
    # Dense reward for moving the end effector to an object.
    def f(env, info):
        return -float(np.linalg.norm(info["end_effector"] - info["object_position"][obj]))
    return f


def lift_reward(obj, height=0.05):
    # Reach shaping plus progress lifting the object to `height` metres above where it started.
    def f(env, info):
        d = float(np.linalg.norm(info["end_effector"] - info["object_position"][obj]))
        dz = float(info["object_position"][obj][2] - info["start_height"][obj])
        return -d + 5.0 * float(np.clip(dz / height, 0.0, 1.0))
    return f


def lift_success(obj, height=0.05):
    def f(env, info):
        return bool(info["object_position"][obj][2] - info["start_height"][obj] > height and info["object_up_z"][obj] > 0.9)
    return f


def push_reward(obj, target_xy):
    # Push an object to a point on the table (world x, y in metres).
    target = np.asarray(target_xy, dtype=float)
    def f(env, info):
        return -float(np.linalg.norm(info["object_position"][obj][:2] - target))
    return f
"""

INIT_TEMPLATE = r"""
# RealiSIM workcell package @MODULE@ @VERSION@. `import @MODULE@` registers @ENVID@.
import gymnasium as gym

from .env import META, SCENE_XML, WorkcellEnv
from . import tasks

ENV_ID = "@ENVID@"
__version__ = "@VERSION@"

if ENV_ID not in gym.registry:
    gym.register(id=ENV_ID, entry_point="@MODULE@.env:WorkcellEnv", max_episode_steps=@MAXSTEPS@)
"""

SMOKE_TEMPLATE = r"""
# Smoke test: Gymnasium env checker, reset/step against the declared spaces, seeding, a task reward, clean close.
import json, sys, time, warnings

import gymnasium as gym
import numpy as np
from gymnasium.utils.env_checker import check_env

import @MODULE@ as pkg

res = {"env_id": pkg.ENV_ID, "gymnasium": gym.__version__}
try:
    import mujoco
    res["mujoco"] = mujoco.__version__
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        check_env(gym.make(pkg.ENV_ID).unwrapped, skip_render_check=True)
    res["check_env"] = "passed"
    res["check_env_warnings"] = sorted({str(x.message)[:160] for x in caught})
    obj = pkg.META["objects"][0]
    env = gym.make(pkg.ENV_ID, reward_fn=pkg.tasks.lift_reward(obj), terminate_fn=pkg.tasks.lift_success(obj))
    obs, info = env.reset(seed=123)
    assert env.observation_space.contains(obs), "reset observation outside observation_space"
    t0, steps, total = time.time(), 0, 0.0
    for _ in range(40):
        obs, r, term, trunc, info = env.step(env.action_space.sample())
        assert env.observation_space.contains(obs) and isinstance(r, float)
        assert isinstance(term, bool) and isinstance(trunc, bool)
        total += r
        steps += 1
        if term or trunc:
            break
    res.update(steps=steps, episode_return=round(total, 3), step_ms=round(1000 * (time.time() - t0) / max(steps, 1), 2))
    o1, _ = env.reset(seed=7)
    o2, _ = env.reset(seed=7)
    res["seeded_reset_reproducible"] = bool(np.allclose(o1, o2))
    res["observation_space"] = str(env.observation_space)
    res["action_space"] = str(env.action_space)
    env.close()
    res["closed"] = True
    res["ok"] = True
except Exception as e:
    import traceback
    res["ok"] = False
    res["error"] = f"{type(e).__name__}: {e}"
    res["trace"] = traceback.format_exc()[-1500:]
print("SMOKE_RESULT " + json.dumps(res))
sys.exit(0 if res.get("ok") else 1)
"""

POLICY_TEMPLATE = r"""
# Bring your own policy: replace `policy` with anything that maps an observation to an action in [-1, 1]
# (a trained network, a scripted controller, a planner). Runs one episode and reports the return.
import gymnasium as gym
import numpy as np

import @MODULE@ as pkg

obj = pkg.META["objects"][0]
env = gym.make(pkg.ENV_ID, reward_fn=pkg.tasks.lift_reward(obj), terminate_fn=pkg.tasks.lift_success(obj))


def policy(obs):
    return env.action_space.sample()  # <- your policy here


obs, info = env.reset(seed=0)
total, done = 0.0, False
while not done:
    obs, r, terminated, truncated, info = env.step(policy(obs))
    total += r
    done = terminated or truncated
print(f"return {total:.2f} · lifted {obj}: {terminated}")
env.close()
"""

TRAIN_TEMPLATE = r"""
# Example: train PPO on the lift task (pip install "@DIST@[train]"). Any algorithm or library works the same way.
import gymnasium as gym
from stable_baselines3 import PPO

import @MODULE@ as pkg

obj = pkg.META["objects"][0]
env = gym.make(pkg.ENV_ID, reward_fn=pkg.tasks.lift_reward(obj), terminate_fn=pkg.tasks.lift_success(obj))
model = PPO("MlpPolicy", env, verbose=1, seed=0)
model.learn(total_timesteps=200_000)
model.save("ppo_lift_" + obj)
"""


MULTI_ENV_SUFFIX = r"""

# ── Multi-arm additions (generated for @NARMS@ arms) ────────────────────────────────────────────────────────────────
# One scene, several namespaced arms ("<arm>/joint"). The action is still one Box over ALL actuators, in model order;
# META["robots"][i]["action_slice"] is arm i's slice, "observation_qpos_indices"/"observation_qvel_indices" its entries
# in the observation vector. info["end_effectors"][arm] is each arm's end-effector position ("end_effector" = primary).
_BaseWorkcellEnv = WorkcellEnv


class WorkcellEnv(_BaseWorkcellEnv):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        m = self.model
        self.arms = {r["name"]: r for r in META["robots"]}
        self.arm_names = [r["name"] for r in META["robots"]]
        self.end_effectors = {n: m.body(r["end_effector_body"]).id for n, r in self.arms.items()}
        self.arm_action_slices = {n: slice(*r["action_slice"]) for n, r in self.arms.items()}

    def arm_action(self, per_arm, default=0.0):
        # Build the full normalized action from {arm_name: array}; arms not given get `default`.
        a = np.full(self.model.nu, float(default), dtype=np.float32)
        for n, v in per_arm.items():
            sl = self.arm_action_slices[n]
            a[sl] = np.asarray(v, dtype=np.float32).reshape(sl.stop - sl.start)
        return a

    def _info(self):
        info = super()._info()
        info["end_effectors"] = {n: self.data.xpos[b].copy() for n, b in self.end_effectors.items()}
        return info
"""

MULTI_TASKS_SUFFIX = r"""


def arm_reach_reward(obj, arm):
    # Multi-arm: dense reward for moving one named arm's end effector to an object.
    def f(env, info):
        return -float(np.linalg.norm(info["end_effectors"][arm] - info["object_position"][obj]))
    return f


def nearest_arm_reach_reward(obj):
    # Multi-arm: reward the arm currently closest to the object.
    def f(env, info):
        return -min(float(np.linalg.norm(p - info["object_position"][obj])) for p in info["end_effectors"].values())
    return f
"""

MULTI_SMOKE_CHECK = r"""    arms = pkg.META["robots"]
    raw = env.unwrapped
    assert sorted(raw.end_effectors) == sorted(r["name"] for r in arms), "an arm is missing from the environment"
    assert sum(r["action_slice"][1] - r["action_slice"][0] for r in arms) == env.action_space.shape[0], "action slices do not cover the action"
    obs, info = env.reset(seed=11)
    for r in arms:
        assert all(raw.model.joint(j).name == j for j in r["joints"]), r["name"] + ": joint mapping"
        a = raw.arm_action({r["name"]: np.ones(r["action_slice"][1] - r["action_slice"][0])}, default=0.0)
        obs, *_ = env.step(a)
        assert env.observation_space.contains(obs)
    res["arms"] = [dict(name=r["name"], actuators=r["action_slice"][1] - r["action_slice"][0], end_effector=r["end_effector_body"],
                        base_pose=r["base_pose"]) for r in arms]
    res["end_effector_positions"] = {n: [round(float(v), 4) for v in p] for n, p in info["end_effectors"].items()}
"""


def end_effector(m, info):
    """Deepest body of the robot (not the table, not an object)."""
    skip = set(info["places"]) | {"workcell_table", "world"}
    depth = {0: 0}
    best, bd = 0, -1
    for b in range(1, m.nbody):
        depth[b] = depth[int(m.body_parentid[b])] + 1
        name = m.body(b).name
        root = b
        while int(m.body_parentid[root]) != 0:
            root = int(m.body_parentid[root])
        if m.body(root).name in skip or name in skip:
            continue
        if depth[b] > bd:
            best, bd = b, depth[b]
    return m.body(best).name


LEGACY_NAMESPACES = {"RoboticsWorkspace": "workcell_", "Reel2Real": "reel2real_"}


def name_nested_defaults(xml):
    """MjSpec.to_xml() writes the root default class of every attached spec as a nested <default> without a class
    name, which the XML parser rejects. Give each one a unique name (nothing references them by name)."""
    import xml.etree.ElementTree as ET
    root = ET.fromstring(xml)
    n = 0
    for parent in root.iter("default"):
        for ch in list(parent):
            if ch.tag == "default" and not ch.get("class"):
                n += 1
                ch.set("class", f"attached_default_{n}")
    if not n:
        return xml
    ET.indent(root, "  ")
    return ET.tostring(root, encoding="unicode") + "\n"


def main():
    ap = argparse.ArgumentParser()
    for opt in ("--assets", "--names", "--out", "--version", "--env-id", "--slug", "--provenance"):
        ap.add_argument(opt, required=True)
    ap.add_argument("--robot-dir", default="")
    ap.add_argument("--robots", default="", help="multi-arm: JSON list (or @file) as in workcell.py; first = primary")
    ap.add_argument("--table", default=""); ap.add_argument("--places", default=""); ap.add_argument("--front", default="")
    ap.add_argument("--table-pose", default="", help='JSON {"x_m","y_m","yaw_deg"} in the robot-base frame (same as workcell.py)')
    ap.add_argument("--max-steps", type=int, default=250)
    ap.add_argument("--module", default="", help="python package name; default realisim_env (legacy env-id namespaces keep theirs)")
    a = ap.parse_args()
    names = [n for n in a.names.split(",") if n]
    prov = json.loads(Path(a.provenance).read_text())
    robots = W.parse_robots(a.robots) if a.robots else None
    if robots and len(robots) == 1:       # one arm: legacy unprefixed package
        a.robot_dir, robots = robots[0]["robot_dir"], None
    if not a.robot_dir and not robots:
        raise SystemExit("--robot-dir or --robots is required")
    if robots:
        a.robot_dir = robots[0]["robot_dir"]
    slug = re.sub(r"[^a-z0-9_]+", "_", a.slug.lower()).strip("_")[:40] or "workcell"
    # New exports: RealiSIM namespace, python package realisim_env. Explicit legacy env ids keep their old package names.
    legacy = next((pre for ns, pre in LEGACY_NAMESPACES.items() if a.env_id.startswith(ns + "/")), None)
    module = a.module or ((legacy + slug) if legacy else "realisim_env")
    if not re.fullmatch(r"[a-z][a-z0-9_]{1,60}", module):
        raise SystemExit("invalid module name")
    dist = module.replace("_", "-") if legacy or a.module else "realisim-env-" + slug.replace("_", "-")
    archive_name = f"{module}-{a.version}.zip" if legacy or a.module else f"{module}-{slug}-{a.version}.zip"
    out = Path(a.out)
    shutil.rmtree(out, ignore_errors=True)
    pkg = out / module
    scene_dir = pkg / "scene"
    (scene_dir / "assets").mkdir(parents=True)

    table = [float(v) for v in a.table.split(",")] if a.table else None
    places = json.loads(a.places) if a.places else None
    front = [float(v) for v in a.front.split(",")] if a.front else None
    if robots:
        spec, m, info = W.compose_multi(robots, a.assets, names, table, places, a.table_pose or None)
    else:
        spec, m, info = W.compose(a.robot_dir, a.assets, names, table, places, front, a.table_pose or None)
    d = W.reset(m, info, names, 0.0)
    k = spec.add_key()
    k.name = "home"
    k.qpos = d.qpos.tolist()
    k.ctrl = d.ctrl.tolist()

    # Vendor every mesh/texture/hfield into scene/assets. MjSpec.to_xml() compiles the spec, so the files must stay
    # resolvable while it runs: point the spec at the absolute copies, serialise, then rewrite those absolute paths to
    # package-relative ones in the XML text (and fail if any absolute path or outside reference survives).
    adir = (scene_dir / "assets").resolve()
    used, names_taken = {}, set()
    def place(src):
        src = Path(src).resolve()
        if str(src) in used:
            return used[str(src)]
        if not src.is_file():
            raise SystemExit(f"model references a missing file: {src}")
        nm = re.sub(r"[^A-Za-z0-9._-]", "_", src.name)
        cand, i = nm, 1
        while cand in names_taken:
            cand, i = f"{i}_{nm}", i + 1
        names_taken.add(cand)
        shutil.copy2(src, adir / cand)
        used[str(src)] = str(adir / cand)
        return used[str(src)]
    for coll in (spec.meshes, spec.textures, getattr(spec, "hfields", [])):
        for el in coll:
            if getattr(el, "file", ""):
                el.file = place(el.file)
    spec.meshdir = ""
    spec.texturedir = ""
    spec.modelname = "realisim_" + slug
    xml = spec.to_xml()
    xml = xml.replace(f'"{adir}/', '"assets/')
    xml = name_nested_defaults(xml)
    if str(adir) in xml or re.search(r'file="/', xml):
        raise SystemExit("exported scene still references absolute paths")
    for ref in re.findall(r'file="([^"]+)"', xml):
        if not (scene_dir / ref).is_file():
            raise SystemExit(f"exported scene references a file outside the package: {ref}")
    (scene_dir / "scene.xml").write_text(xml)
    m2 = mujoco.MjModel.from_xml_path(str(scene_dir / "scene.xml"))   # standalone check: loads from the package alone
    assert m2.nq == m.nq and m2.nu == m.nu and m2.nkey >= 1, "exported scene does not match the composed model"
    arms_meta = None
    if robots:
        # re-resolve every arm's maps on the standalone exported model (names survive the XML round trip)
        maps = W.arm_maps(m2, [dict(name=r["name"], prefix=r["prefix"], standalone=r["standalone"]) for r in info["robots"]])
        assert m2.ntendon == m.ntendon and m2.neq == m.neq and m2.nsensor == m.nsensor and m2.njnt == m.njnt, "exported scene lost arm elements"
        provr = {r.get("name"): r for r in prov.get("robots") or []}
        arms_meta = []
        for r, mp in zip(info["robots"], maps):
            pr = provr.get(r["name"]) or {}
            arms_meta.append(dict(name=r["name"], label=r.get("label") or pr.get("label"), library_id=r.get("library_id") or pr.get("library_id"),
                                  model_name=pr.get("model_name"), primary=r["primary"], namespace=r["prefix"], base_pose=r["base_pose"],
                                  base_pose_frame="primary robot base (world)", pose_source=r["pose_source"],
                                  end_effector_body=mp["end_effector_body"], joints=mp["joints"], actuators=mp["actuators"],
                                  action_slice=mp["action_slice"], qpos_indices=mp["qpos_indices"], qvel_indices=mp["qvel_indices"],
                                  observation_qpos_indices=mp["observation_qpos_indices"], observation_qvel_indices=mp["observation_qvel_indices"],
                                  home_qpos=r["home_qpos"], reach_m=r["stats"]["reach_m"],
                                  source=pr.get("source"), license=pr.get("license"), robot_import_id=pr.get("robot_import_id"), model_hash=pr.get("model_hash")))
        ee = arms_meta[0]["end_effector_body"]
    else:
        ee = end_effector(m2, info)

    meta = dict(name=prov.get("project_name"), objects=names, places=info["places"], table=info["table"],
                robot=dict(prov.get("robot") or {}, stats=info["robot"]), end_effector_body=ee, scene_version=prov.get("scene_version"),
                scene_version_id=prov.get("scene_version_id"), asset_versions=prov.get("asset_versions"), mujoco=mujoco.__version__,
                generated_by="RealiSIM")
    if arms_meta:
        meta.update(robots=arms_meta, primary_robot=arms_meta[0]["name"], end_effectors={r["name"]: r["end_effector_body"] for r in arms_meta},
                    robot=dict(prov.get("robot") or {}, stats=info["robot"], instances=len(arms_meta)))
    (pkg / "workcell.json").write_text(json.dumps(meta, indent=2))
    robot_name = (prov.get("robot") or {}).get("name") or Path(a.robot_dir).name
    if arms_meta:
        robot_name = ", ".join(f"{r['name']} ({r.get('model_name') or r.get('library_id') or 'robot'})" for r in arms_meta)
    fill = lambda s: (s.replace("@MODULE@", module).replace("@VERSION@", a.version).replace("@ENVID@", a.env_id)
                      .replace("@MAXSTEPS@", str(a.max_steps)).replace("@DIST@", dist).replace("@ROBOT@", robot_name).replace("@OBJECTS@", ", ".join(names)))
    smoke_t = SMOKE_TEMPLATE
    if arms_meta:
        anchor = '    env.close()\n    res["closed"] = True'
        assert anchor in smoke_t
        smoke_t = smoke_t.replace(anchor, MULTI_SMOKE_CHECK + anchor)
    (pkg / "env.py").write_text(fill(ENV_TEMPLATE) + (MULTI_ENV_SUFFIX.replace("@NARMS@", str(len(arms_meta))) if arms_meta else ""))
    (pkg / "tasks.py").write_text(fill(TASKS_TEMPLATE) + (MULTI_TASKS_SUFFIX if arms_meta else ""))
    (pkg / "__init__.py").write_text(fill(INIT_TEMPLATE))
    (pkg / "scripts").mkdir()
    (pkg / "scripts" / "__init__.py").write_text("")
    (pkg / "scripts" / "smoke_test.py").write_text(fill(smoke_t))
    (pkg / "scripts" / "my_policy.py").write_text(fill(POLICY_TEMPLATE))
    (pkg / "scripts" / "train_ppo.py").write_text(fill(TRAIN_TEMPLATE))
    lic = Path(a.robot_dir) / "LICENSE"
    (pkg / "licenses").mkdir()
    if lic.exists():
        shutil.copy2(lic, pkg / "licenses" / "robot_LICENSE")
    for r in robots or []:                 # every distinct arm model ships its own license
        rl = Path(r["robot_dir"]) / "LICENSE"
        if rl.exists():
            tag = re.sub(r"[^A-Za-z0-9_.-]", "_", str(r.get("library_id") or Path(r["robot_dir"]).name))
            shutil.copy2(rl, pkg / "licenses" / f"robot_{tag}_LICENSE")
    deps = ",\n    ".join(f'"{k}=={v}"' for k, v in PINS.items())
    train = ", ".join(f'"{k}=={v}"' for k, v in TRAIN_PINS.items())
    (pkg / "pyproject.toml").write_text(
        '[build-system]\nrequires = ["setuptools>=68"]\nbuild-backend = "setuptools.build_meta"\n\n'
        f'[project]\nname = "{dist}"\nversion = "{a.version}"\n'
        f'description = "RealiSIM workcell: {robot_name} with {", ".join(names)} (MuJoCo + Gymnasium)"\n'
        'readme = "README.md"\nrequires-python = ">=3.10,<3.13"\n'
        f'dependencies = [\n    {deps},\n]\n\n'
        f'[project.optional-dependencies]\ntrain = [{train}]\n\n'
        f'[tool.setuptools]\npackage-dir = {{"{module}" = "."}}\npackages = ["{module}", "{module}.scripts"]\n\n'
        f'[tool.setuptools.package-data]\n{module} = ["scene/*.xml", "scene/assets/*", "workcell.json", "licenses/*", "README.md", "MANIFEST.sha256"]\n')
    t = info["table"]
    obj_rows = "\n".join(f"| `{n}` | {info['places'][n][0]:.3f}, {info['places'][n][1]:.3f} | {(prov.get('objects') or {}).get(n, {}).get('summary', '')} |" for n in names)
    rob = prov.get("robot") or {}
    (pkg / "README.md").write_text(f"""# RealiSIM workcell: {prov.get('project_name') or slug}

A MuJoCo simulation of a real workcell, reconstructed by RealiSIM: the **{robot_name}**, a {t['length_m']:.2f} x {t['width_m']:.2f} m
table at {t['height_m']:.2f} m, and {len(names)} object(s) at measured scale. Gymnasium id `{a.env_id}`, package {a.version}.
No task is baked in: run or train any script or policy.

```bash
pip install {archive_name}
python -m {module}.scripts.smoke_test     # env checker, spaces, seeding
python -m {module}.scripts.my_policy      # plug in your own policy
```

```python
import gymnasium as gym
import {module} as wc

env = gym.make(wc.ENV_ID, reward_fn=wc.tasks.lift_reward("{names[0]}"), terminate_fn=wc.tasks.lift_success("{names[0]}"))
obs, info = env.reset(seed=0)
obs, reward, terminated, truncated, info = env.step(env.action_space.sample())
```

Or use the raw scene with any MuJoCo tool: `{module}/scene/scene.xml` (keyframe `home` is the captured start state).

## Spaces

| | |
|---|---|
| Action | `Box(-1, 1, ({m2.nu},))`: normalized actuator controls mapped to each actuator's range (joint position targets for Menagerie arms), held for `frame_skip` = 20 physics steps ({20 * m2.opt.timestep:.3f} s) |
| Observation | robot joint positions and velocities ({2 * (m2.njnt - len(names))} values), then per object position, quaternion and velocity (13 values each). **Simulator state, not sensor data** |
| Reward / termination | yours: `reward_fn(env, info)`, `terminate_fn(env, info)`. Examples in `tasks.py`: reach, lift, push |
| Episode | {a.max_steps} steps, then truncated. Objects are jittered ±2 cm and ±15° at reset (`randomize_objects=False` to disable) |

## Limits (read before deploying)

- **Observations are simulator state.** Object poses and velocities come straight from MuJoCo. They are not deployment-ready
  sensor observations; a real robot needs a validated perception or state-estimation mapping that produces them.
- **The smoke test checks the Gymnasium API contract only** (env checker, spaces, seeding, reset/step, close). It does not
  establish that physics, geometry, friction or control match the real workcell.
- The robot is a MuJoCo Menagerie simulation model, not calibrated against your arm. Actuator ranges and gains are the model's.
- No code in this package sends commands to hardware.

## Workcell

| Object | Placement x, y (m, robot frame) | Properties |
|---|---|---|
{obj_rows}

Robot base at the origin on the table top (z = 0). Table-top centre at ({t['center'][0]:.3f}, {t['center'][1]:.3f}) m, length axis at {t['yaw_deg']:.1f} deg ({'from the workcell layout' if t.get('pose_source') == 'given' else 'default placement in front of the robot'}). End effector body used by the example tasks: `{ee}`.

## Provenance

- Robot model: {rob.get('source', 'MuJoCo Menagerie')} `{rob.get('library_id', Path(a.robot_dir).name)}` ({rob.get('license', 'see licenses/robot_LICENSE')}). {rob.get('note', '')}
- Objects: generated from your captures by RealiSIM with the upstream photo2grasp pipeline (https://github.com/adityabangde/photo2grasp; TRELLIS mesh, CoACD collision hulls); mass, size and friction carry their sources in RealiSIM.
- Scene version {prov.get('scene_version')} ({prov.get('scene_version_id')}), validated by a physics drop test before export; MuJoCo {mujoco.__version__}.
- Reproducible: every file of this package is stored and hash-verified in RealiSIM's SpacetimeDB; `MANIFEST.sha256` lists them.
""")
    if arms_meta:
        rows_ = "\n".join(f"| `{r['name']}`{' (primary)' if r['primary'] else ''} | {r.get('model_name') or r.get('library_id') or ''} | "
                           f"{r['base_pose']['x_m']:.3f}, {r['base_pose']['y_m']:.3f}, {r['base_pose']['yaw_deg']:.1f}° | {r['pose_source']} | "
                           f"{r['action_slice'][0]}–{r['action_slice'][1] - 1} | `{r['end_effector_body']}` |" for r in arms_meta)
        with open(pkg / "README.md", "a") as fh:
            fh.write(f"""
## Arms ({len(arms_meta)})

Every arm is namespaced (`<arm>/...` for bodies, joints, actuators, tendons, meshes). Base poses are in the primary arm's
base frame (the world frame of this scene). A pose source of `default` means the position was NOT measured: it is a
provisional slot beside the primary arm. Per-arm joint, actuator, observation and action indices are in `workcell.json`
under `robots`; `env.unwrapped.arm_action({{"<arm>": a}})` builds the full action, `info["end_effectors"]` gives each tip.

| Arm | Model | Base x, y (m), yaw | Pose source | Action indices | End effector |
|---|---|---|---|---|---|
{rows_}
""")
    files = sorted(p for p in pkg.rglob("*") if p.is_file())
    (pkg / "MANIFEST.sha256").write_text("\n".join(f"{hashlib.sha256(p.read_bytes()).hexdigest()}  {p.relative_to(pkg).as_posix()}" for p in files) + "\n")
    archive = out / archive_name
    with zipfile.ZipFile(archive, "w", zipfile.ZIP_DEFLATED) as z:
        for p in sorted(pkg.rglob("*")):
            if p.is_file():
                z.write(p, module + "/" + p.relative_to(pkg).as_posix())
    allf = [p for p in pkg.rglob("*") if p.is_file()]
    print("EXPORT_RESULT " + json.dumps(dict(ok=True, archive=str(archive), package_dir=str(pkg), module=module, dist=dist,
          file_count=len(allf), bytes=sum(p.stat().st_size for p in allf), dependencies=PINS, optional=TRAIN_PINS, end_effector=ee,
          module_name=module, archive_root=module, archive_name=archive.name, env_id=a.env_id, manifest_path=module + "/MANIFEST.sha256",
          smoke_module=module + ".scripts.smoke_test", smoke_script="scripts/smoke_test.py", legacy_ids=bool(legacy), brand="RealiSIM",
          **(dict(robots=[{k: r[k] for k in ("name", "library_id", "primary", "base_pose", "pose_source", "end_effector_body", "joints", "actuators", "action_slice")} for r in arms_meta],
                  end_effectors={r["name"]: r["end_effector_body"] for r in arms_meta}, primary_robot=arms_meta[0]["name"]) if arms_meta else {}))))


if __name__ == "__main__":
    main()
