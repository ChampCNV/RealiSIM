"""RealiSIM Gymnasium package generator for the single-object pick-and-lift task (runs in the repo's Python environment).

    python gym_export.py --repo REPO --asset-dir DIR --name NAME --objects-entry entry.json --out OUTDIR
                         --version 1.0.0 --env-id RealiSIM/PickLift-NAME-v1 --provenance prov.json [--module M]

Builds a self-contained, pip-installable package for the single-object SO-100 pick-and-lift task. The repo's own
sim2real/lift_env.py + tag_env.py (TagLiftEnv, the task the repo trains) are vendored with path patches; every
patch is asserted, so a repo change that breaks vendoring fails the export loudly instead of shipping silently.
The Gymnasium wrapper exposes ONLY the actor observation; the critic's privileged state goes to `info`.
"""
import argparse, hashlib, json, re, shutil, sys, textwrap, zipfile
from pathlib import Path

PINS = {"gymnasium": "1.4.0", "mujoco": "3.16.0", "numpy": "2.2.6", "opencv-python-headless": "5.0.0.93", "PyYAML": "6.0.3"}
TRAIN_PINS = {"stable-baselines3": "2.9.0"}


def patch(text, old, new, what, count=1):
    if old not in text:
        raise SystemExit(f"EXPORT-ERROR vendoring patch no longer applies ({what}): the repository code changed")
    return text.replace(old, new) if count == 0 else text.replace(old, new, count)


def yaml_dump(d, indent=0):
    out = []
    for k, v in d.items():
        pad = "  " * indent
        if isinstance(v, dict):
            out.append(f"{pad}{k}:")
            out.append(yaml_dump(v, indent + 1))
        else:
            out.append(f"{pad}{k}: {json.dumps(v)}")
    return "\n".join(out)


ENV_TEMPLATE = """
# RealiSIM pick-and-lift: Gymnasium environment for the SO-100 (MuJoCo Menagerie trs_so_arm100) and the
# reconstructed object "@NAME@". Wraps TagLiftEnv vendored from the upstream photo2grasp repository.
# Observation (actor / policy): the 20-D `state` vector only.
# Privileged critic state (asymmetric actor-critic): info["privileged_state"]; never part of the observation.
import inspect
from pathlib import Path

import gymnasium as gym
import numpy as np
import yaml
from gymnasium import spaces

from .vendor import tag_env as _tag

PACKAGE_ROOT = Path(__file__).resolve().parents[1]
OBJECT_NAME = "@NAME@"
# Conservative bounds; observations are clipped into them (documented in README).
OBS_LOW = np.array([-4.0] * 12 + [-2.0, -2.0, -1.0, -1.0, -1.0, 0.0, 0.0, 0.0], np.float32)
OBS_HIGH = np.array([4.0] * 12 + [2.0, 2.0, 2.0, 1.0, 1.0, 1.0, 1.0, 3.0], np.float32)


def load_config():
    cfg = PACKAGE_ROOT / "config"
    return yaml.safe_load((cfg / "task.yaml").read_text()), yaml.safe_load((cfg / "randomization.yaml").read_text())


class PickLiftEnv(gym.Env):
    metadata = {"render_modes": ["rgb_array"], "render_fps": 10}

    def __init__(self, render_mode=None, domain_randomization=True, area=None):
        task, rnd = load_config()
        self.task, self.randomization = task, rnd
        self.render_mode = render_mode
        cls = type("ConfiguredTagLiftEnv", (_tag.TagLiftEnv,), dict(
            mass_scale=tuple(rnd["mass_scale"]), friction_range=tuple(rnd["friction_range"]),
            motor_strength=tuple(rnd["motor_strength"]), init_qpos_noise=rnd["init_qpos_noise"],
            corner_noise_px=rnd["tag_corner_noise_px"], miss_prob=rnd["tag_miss_prob"],
            max_steps=task["max_episode_steps"], lift_height=task["lift_height_m"]))
        kw = {"hand": None} if "hand" in inspect.signature(_tag.TagLiftEnv.__init__).parameters else {}
        self._env = cls(OBJECT_NAME, seed=0, domain_randomization=domain_randomization,
                        area=area or rnd["placement_area_m"], **kw)  # no human hand: the legacy pick-and-lift task
        self.action_space = spaces.Box(-1.0, 1.0, (6,), np.float32)
        self.observation_space = spaces.Box(OBS_LOW, OBS_HIGH, dtype=np.float32)
        self._renderer = None

    def _obs(self, raw):
        state = np.asarray(raw["state"], np.float32)
        return np.clip(state, OBS_LOW, OBS_HIGH), {"privileged_state": np.asarray(raw["priv"], np.float32)}

    def reset(self, *, seed=None, options=None):
        super().reset(seed=seed)
        if seed is not None:
            self._env.rng = np.random.default_rng(seed)
        return self._obs(self._env.reset())

    def step(self, action):
        action = np.asarray(action, np.float64).reshape(6)
        raw, reward, terminated, truncated, info = self._env.step(action)
        obs, extra = self._obs(raw)
        info = {k: (bool(v) if isinstance(v, (bool, np.bool_)) else float(v)) for k, v in info.items()}
        if getattr(self._env, "hand", None) is None:
            info.pop("touching_hand", None)   # repo >= 9fa727d adds it for hand tasks; keep the legacy info keys
        info.update(extra)
        return obs, float(reward), bool(terminated), bool(truncated), info

    def render(self):
        if self.render_mode != "rgb_array":
            return None
        import mujoco
        if self._renderer is None:  # needs an OpenGL backend (MUJOCO_GL=egl or osmesa)
            self._renderer = mujoco.Renderer(self._env.model, 480, 640)
        self._renderer.update_scene(self._env.data, camera="third")
        return self._renderer.render()

    def close(self):
        if self._renderer is not None:
            self._renderer.close()
            self._renderer = None
"""

INIT_TEMPLATE = """
# RealiSIM Gymnasium package: @NAME@, version @VERSION@. `import @MODULE@` registers @ENVID@.
import gymnasium as gym

ENV_ID = "@ENVID@"
__version__ = "@VERSION@"


def register_envs():
    if ENV_ID not in gym.registry:
        gym.register(id=ENV_ID, entry_point="@MODULE@.environment.pick_lift:PickLiftEnv",
                     max_episode_steps=@MAXSTEPS@)


register_envs()
"""

SMOKE_TEMPLATE = """
# Smoke test: Gymnasium env checker, reset/step against the declared spaces, seeding, clean close.
# Prints one JSON line (SMOKE_RESULT ...) and exits non-zero on failure.
import json, sys, time, warnings

import gymnasium as gym
import numpy as np
from gymnasium.utils.env_checker import check_env

import @MODULE@

res = {"env_id": @MODULE@.ENV_ID, "gymnasium": gym.__version__}
try:
    import mujoco
    res["mujoco"] = mujoco.__version__
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        check_env(gym.make(@MODULE@.ENV_ID).unwrapped, skip_render_check=True)
    res["check_env"] = "passed"
    res["check_env_warnings"] = sorted({str(x.message)[:160] for x in caught})
    env = gym.make(@MODULE@.ENV_ID)
    obs, info = env.reset(seed=123)
    assert env.observation_space.contains(obs), "reset observation outside observation_space"
    assert obs.shape == (20,), "the actor observation must not include the privileged state"
    assert np.asarray(info["privileged_state"]).shape == (13,)
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

EVAL_TEMPLATE = """
# Success rate over N seeded episodes, with a random policy or a Stable-Baselines3 model.
#   python scripts/evaluate.py --episodes 20 [--model sac_picklift.zip]
import argparse

import gymnasium as gym

import @MODULE@

ap = argparse.ArgumentParser()
ap.add_argument("--episodes", type=int, default=20)
ap.add_argument("--model")
a = ap.parse_args()
policy = None
if a.model:
    from stable_baselines3 import SAC
    policy = SAC.load(a.model)
env = gym.make(@MODULE@.ENV_ID)
wins = 0
for ep in range(a.episodes):
    obs, info = env.reset(seed=ep)
    done, success = False, False
    while not done:
        act = policy.predict(obs, deterministic=True)[0] if policy else env.action_space.sample()
        obs, r, term, trunc, info = env.step(act)
        done = term or trunc
        success |= bool(info.get("success"))
    wins += success
print(f"success {wins}/{a.episodes} = {100 * wins / a.episodes:.1f}%")
"""

TRAIN_TEMPLATE = """
# Minimal training example: Stable-Baselines3 SAC on the actor observation.
#   pip install "<this package>[train]" && python scripts/train_example.py --steps 200000
# The repository's own trainer (sim2real/train_tag.py) adds an asymmetric critic fed with info["privileged_state"];
# this example keeps to the standard API and never feeds privileged state to the policy.
import argparse

import gymnasium as gym
from stable_baselines3 import SAC

import @MODULE@

ap = argparse.ArgumentParser()
ap.add_argument("--steps", type=int, default=200_000)
ap.add_argument("--out", default="sac_picklift")
a = ap.parse_args()
env = gym.make(@MODULE@.ENV_ID)
model = SAC("MlpPolicy", env, verbose=1, seed=0)
model.learn(total_timesteps=a.steps)
model.save(a.out)
print("saved", a.out + ".zip")
"""


MODULE = "realisim_env"   # set in main(): realisim_env for new exports, photo2grasp_env for legacy env ids


def fill(t, **kw):
    kw.setdefault("MODULE", MODULE)
    for k, v in kw.items():
        t = t.replace("@" + k + "@", str(v))
    return t.lstrip()


def main():
    ap = argparse.ArgumentParser()
    for opt in ("--repo", "--asset-dir", "--name", "--objects-entry", "--out", "--version", "--env-id", "--provenance"):
        ap.add_argument(opt, required=True)
    ap.add_argument("--area", default="default")
    ap.add_argument("--module", default="", help="python package name; default: from the env id (legacy Photo2Grasp/* ids keep photo2grasp_env)")
    a = ap.parse_args()
    repo, asset_dir, name = Path(a.repo), Path(a.asset_dir), a.name
    prov = json.loads(Path(a.provenance).read_text())
    entry = json.loads(Path(a.objects_entry).read_text())
    out = Path(a.out)
    shutil.rmtree(out, ignore_errors=True)
    global MODULE
    legacy = a.env_id.startswith("Photo2Grasp/")
    MODULE = a.module or ("photo2grasp_env" if legacy else "realisim_env")
    if not re.fullmatch(r"[a-z][a-z0-9_]{1,40}", MODULE):
        raise SystemExit("EXPORT-ERROR invalid module name")
    pkg = out / MODULE
    (pkg / "environment" / "vendor").mkdir(parents=True)

    # 1. vendored task code (patched paths, relative imports)
    lift = (repo / "sim2real" / "lift_env.py").read_text()
    lift = patch(lift, "ROOT = Path(__file__).resolve().parent.parent", "ROOT = Path(__file__).resolve().parents[2]  # package root (vendored)", "ROOT")
    lift = patch(lift, 'ROOT / "third_party" / "mujoco_menagerie" / "trs_so_arm100" / "scene.xml"', 'ROOT / "robot" / "trs_so_arm100" / "scene.xml"', "ARM_SCENE")
    lift = patch(lift, 'ROOT / "assets" / name / f"{name}.xml"', 'ROOT / "assets" / name / "model.xml"', "asset xml")
    tag = (repo / "sim2real" / "tag_env.py").read_text()
    tag = patch(tag, "from lift_env import", "from .lift_env import", "tag_env imports", count=0)
    tag = patch(tag, 'ROOT / "objects.json"', 'ROOT / "config" / "objects.json"', "objects.json path", count=0)
    vendored = [("lift_env.py", lift), ("tag_env.py", tag)]
    # Repo >= 9fa727d: tag_env imports the human-hand model (sim2real/hand_sim.py: numpy + mujoco only). Vendor it so
    # the package still imports; hands/ data is NOT shipped and the env is always built without a hand (legacy task).
    if "from hand_sim import" in tag:
        hs = (repo / "sim2real" / "hand_sim.py").read_text()
        hs = patch(hs, "from lift_env import", "from .lift_env import", "hand_sim imports", count=0)
        if re.search(r"^\s*(import|from)\s+(?!json|mujoco|numpy|\.lift_env)\w", hs, re.M):
            raise SystemExit("EXPORT-ERROR vendoring: sim2real/hand_sim.py gained imports the package does not ship")
        tag = patch(tag, "from hand_sim import", "from .hand_sim import", "tag_env hand_sim import", count=0)
        vendored = [("lift_env.py", lift), ("tag_env.py", tag), ("hand_sim.py", hs)]
    for fname, src in vendored:
        head = f"# Vendored from {prov.get('repo_url')} @ {prov.get('commit')} (sim2real/{fname}) by RealiSIM.\n# Path patches only; see NOTICE.md.\n"
        (pkg / "environment" / "vendor" / fname).write_text(head + src)
    (pkg / "environment" / "vendor" / "__init__.py").write_text("")

    # 2. asset (immutable version restored from SpacetimeDB), robot model + license, config
    adst = pkg / "assets" / name
    shutil.copytree(asset_dir, adst)
    if (adst / f"{name}.xml").exists():
        (adst / f"{name}.xml").rename(adst / "model.xml")
    shutil.copytree(repo / "third_party" / "mujoco_menagerie" / "trs_so_arm100", pkg / "robot" / "trs_so_arm100",
                    ignore=shutil.ignore_patterns("*.png"))
    (pkg / "config").mkdir()
    (pkg / "config" / "objects.json").write_text(json.dumps({name: entry}, indent=2))

    # 3. task parameters read from the vendored code itself, so the config describes what actually runs
    sys.path.insert(0, str(out))
    import importlib
    tag_mod = importlib.import_module(MODULE + ".environment.vendor.tag_env")
    area = dict(tag_mod.WIDE_AREA if a.area == "wide" else tag_mod.DEFAULT_AREA)
    C = tag_mod.TagLiftEnv
    rnd = dict(mass_scale=list(C.mass_scale), friction_range=list(C.friction_range), motor_strength=list(C.motor_strength),
               init_qpos_noise=float(C.init_qpos_noise), tag_corner_noise_px=float(C.corner_noise_px),
               tag_miss_prob=float(C.miss_prob), placement_area_m=area)
    task = dict(env_id=a.env_id, package_version=a.version, object=name, robot_profile=prov.get("robot_profile"),
                control_dt_s=float(C.control_dt), max_episode_steps=int(C.max_steps), lift_height_m=float(C.lift_height),
                tipped_angle_rad=float(C.tipped_angle), action_scale_rad=[float(x) for x in C.action_scale],
                observation="20-D actor state: 6 noisy joints, 6 joint targets, tag x/y/z + facing (cos, sin), visible, ever seen, age (s)",
                privileged_state="13-D critic-only: true joints, object position, tcp minus object, grasped (info['privileged_state'])",
                source=dict(repo=prov.get("repo_url"), commit=prov.get("commit"), asset_version=prov.get("asset_version"),
                            scene_version=prov.get("scene_version")))
    (pkg / "config" / "task.yaml").write_text("# RealiSIM pick-and-lift task (read by environment/pick_lift.py)\n" + yaml_dump(task) + "\n")
    (pkg / "config" / "randomization.yaml").write_text("# Domain randomization applied on every reset (read by environment/pick_lift.py)\n" + yaml_dump(rnd) + "\n")
    try:  # pre-generate tag textures so the installed package never writes into site-packages
        ids = tag_mod.object_tags(name)
        for tid, back in ((ids[0], False), (ids[1], True), (tag_mod.TAG["id"], False), (tag_mod.BACK_TAG_ID, True)):
            if tid is not None:
                tag_mod.tag_png(tid, back)
    except Exception as e:
        print("note: could not pre-generate tag textures:", e, file=sys.stderr)
    for p in list(pkg.rglob("__pycache__")):
        shutil.rmtree(p, ignore_errors=True)

    # 4. environment, registration, scripts
    (pkg / "environment" / "pick_lift.py").write_text(fill(ENV_TEMPLATE, NAME=name))
    (pkg / "environment" / "__init__.py").write_text("from .pick_lift import OBJECT_NAME, PickLiftEnv\n")
    (pkg / "__init__.py").write_text(fill(INIT_TEMPLATE, NAME=name, VERSION=a.version, ENVID=a.env_id, MAXSTEPS=int(C.max_steps)))
    (pkg / "scripts").mkdir()
    (pkg / "scripts" / "__init__.py").write_text("")
    (pkg / "scripts" / "smoke_test.py").write_text(fill(SMOKE_TEMPLATE))
    (pkg / "scripts" / "evaluate.py").write_text(fill(EVAL_TEMPLATE))
    (pkg / "scripts" / "train_example.py").write_text(fill(TRAIN_TEMPLATE))

    dist = MODULE.replace("_", "-") + "-" + re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")
    deps = ",\n    ".join(f'"{k}=={v}"' for k, v in PINS.items())
    train = ", ".join(f'"{k}=={v}"' for k, v in TRAIN_PINS.items())
    pyproject = (
        '[build-system]\nrequires = ["setuptools>=68"]\nbuild-backend = "setuptools.build_meta"\n\n'
        f'[project]\nname = "{dist}"\nversion = "{a.version}"\n'
        f'description = "RealiSIM Gymnasium environment: SO-100 pick-and-lift of {name} (MuJoCo)"\n'
        'readme = "README.md"\nrequires-python = ">=3.10,<3.13"\n'
        f'dependencies = [\n    {deps},\n]\n\n'
        f'[project.optional-dependencies]\ntrain = [{train}]\n\n'
        f'[tool.setuptools]\npackage-dir = {{"{MODULE}" = "."}}\n'
        f'packages = ["{MODULE}", "{MODULE}.environment", "{MODULE}.environment.vendor", "{MODULE}.scripts"]\n\n'
        '[tool.setuptools.package-data]\n'
        f'{MODULE} = ["assets/**/*", "robot/**/*", "config/*", "README.md", "NOTICE.md", "MANIFEST.sha256"]\n')
    (pkg / "pyproject.toml").write_text(pyproject)

    pins_md = "\n".join(f"| {k} | {v} |" for k, v in {**PINS, **TRAIN_PINS}.items())
    scale = [float(x) for x in C.action_scale]
    readme = f"""# RealiSIM pick-and-lift: `{name}`

Gymnasium environment `{a.env_id}` (package version {a.version}): the SO-100 arm picks up and lifts the
reconstructed object **{name}** in MuJoCo. Generated by RealiSIM from your capture, using the upstream photo2grasp pipeline.

```bash
pip install {MODULE}-{name}-{a.version}.zip
python -m {MODULE}.scripts.smoke_test      # env checker + spaces + seeding
```

```python
import gymnasium as gym
import {MODULE}  # registers {a.env_id}

env = gym.make("{a.env_id}")      # or, without the import: gym.make("{MODULE}:{a.env_id}")
obs, info = env.reset(seed=0)
obs, reward, terminated, truncated, info = env.step(env.action_space.sample())
```

## Spaces

| | |
|---|---|
| Action | `Box(-1, 1, (6,), float32)`: change of the 6 joint targets per 0.1 s step, scaled by {scale} rad |
| Observation | `Box((20,), float32)`: 6 noisy joint angles, 6 joint targets, AprilTag x/y/z in the robot frame, tag facing (cos, sin), tag visible now, tag ever seen, seconds since last seen. Clipped to the declared bounds. |
| Privileged (critic only) | `info["privileged_state"]`, 13-D: true joints, object position, fingertip minus object, grasped. Never part of the observation. |
| Episode | {int(C.max_steps)} steps of {float(C.control_dt)} s, then truncated. `info["success"]`: grasped, upright and lifted {float(C.lift_height) * 100:.0f} cm. |

AprilTag detection is simulated geometrically as in the repository (wrist and environment camera, pixel noise,
missed frames, `solvePnP` with the nominal lens). Domain randomization: `config/randomization.yaml`.

## Robot profile

{prov.get("robot_profile_note")}

## Provenance

- Environment code: `sim2real/lift_env.py` and `sim2real/tag_env.py`{" (plus `sim2real/hand_sim.py`, imported by tag_env; no hand is used: hand data is not shipped)" if any(f == "hand_sim.py" for f, _ in vendored) else ""} from {prov.get("repo_url")} at `{prov.get("commit")}` (path patches only).
- Asset: `{name}` version {prov.get("asset_version")}; scene version {prov.get("scene_version")}; MuJoCo {prov.get("mujoco")}.
- Robot: MuJoCo Menagerie `trs_so_arm100` (Apache-2.0, `robot/trs_so_arm100/LICENSE`).
- Every file and its SHA-256 is listed in `MANIFEST.sha256` (run `sha256sum -c MANIFEST.sha256` in this folder).

## Pinned dependencies

| Package | Version |
|---|---|
{pins_md}
"""
    (pkg / "README.md").write_text(readme)
    (pkg / "NOTICE.md").write_text(f"""# Notices

- `robot/trs_so_arm100/`: MuJoCo Menagerie (Google DeepMind), Apache License 2.0. Full text: `robot/trs_so_arm100/LICENSE`.
- `environment/vendor/`: from {prov.get("repo_url")} at {prov.get("commit")}. That repository has no license file;
  confirm redistribution rights with its authors before publishing this package outside your team.
- `assets/{name}/`: generated from your capture by RealiSIM with the upstream photo2grasp pipeline (TRELLIS mesh, CoACD collision hulls).
- Physical properties and where each value came from: `assets/{name}/properties.json`, `config/objects.json`.
""")
    files = sorted(p for p in pkg.rglob("*") if p.is_file())
    (pkg / "MANIFEST.sha256").write_text("\n".join(f"{hashlib.sha256(p.read_bytes()).hexdigest()}  {p.relative_to(pkg).as_posix()}" for p in files) + "\n")
    archive = out / f"{MODULE}-{name}-{a.version}.zip"
    with zipfile.ZipFile(archive, "w", zipfile.ZIP_DEFLATED) as z:
        for p in sorted(pkg.rglob("*")):
            if p.is_file():
                z.write(p, MODULE + "/" + p.relative_to(pkg).as_posix())
    allf = [p for p in pkg.rglob("*") if p.is_file()]
    print("EXPORT_RESULT " + json.dumps(dict(ok=True, archive=str(archive), package_dir=str(pkg), dist=dist,
          file_count=len(allf), bytes=sum(p.stat().st_size for p in allf), dependencies=PINS, optional=TRAIN_PINS,
          module=MODULE, module_name=MODULE, archive_root=MODULE, archive_name=archive.name, env_id=a.env_id,
          manifest_path=MODULE + "/MANIFEST.sha256", smoke_module=MODULE + ".scripts.smoke_test",
          smoke_script="scripts/smoke_test.py", legacy_ids=legacy, brand="RealiSIM")))


if __name__ == "__main__":
    main()
