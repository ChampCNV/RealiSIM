#!/usr/bin/env python3
"""RealiSIM upstream release checker: one mechanical integration check of a candidate photo2grasp checkout.

Runs the RealiSIM adapters against a candidate checkout (an isolated git worktree, never the live repo) with the
repository's own built-in fixture assets, and prints one line `RELEASE_CHECK {json}`:

  source      candidate HEAD/tree, tracked worktree clean before and after
  cli         photo2grasp.py --help, make_asset.py --help (upstream CLI imports)
  introspect  adapter/introspect.py: argparse contract, stage labels, objects.json, assets
  api         make_scene / sim2real signatures used by the adapters (recorded for drift review)
  scene       sim_worker bundle + seeded drop test (celsius_can, seed 0) and posed drop test (lego_tree), no hand
  legacy_pkg  gym_export with a legacy explicit env id -> clean uv venv install -> smoke (check_env, seeded reset)
              + seeded 30-step rollout fingerprint
  current_pkg gym_export with the current default env id (module chosen by the generator; tolerant of renames)
  generic_pkg workcell_export (SO-ARM100 from the pinned Menagerie cache + one fixture object) -> install -> smoke

No paid GPU call, no network except uv's package index, no hardware, no SpacetimeDB, no Firebase, no user projects.
Fixtures are repository sample assets (QA fixtures): never counted as product usage.
It establishes adapter/upstream contract compatibility only: not real spatial accuracy or physical safety.
"""
import argparse, hashlib, json, os, re, resource, shutil, subprocess, sys, time, zipfile
from pathlib import Path

ROOT = Path("/workspace/photo2grasp")
RELEASES = ROOT / "releases"
PY = str(ROOT / ".venv" / "bin" / "python")
MENAGERIE_SO100 = ROOT / "cache" / "menagerie" / "trs_so_arm100"
FIXTURES = ("celsius_can", "lego_tree")
LEGACY_ENV_ID = "Photo2Grasp/PickLift-{name}-v1"        # explicit legacy id: must keep the photo2grasp_env module
CURRENT_ENV_ID = "RealiSIM/PickLift-{name}-v1"          # current default id (module decided by the generator)
GENERIC_ENV_ID = "RealiSIM/Workcell_release_gate-v1"
TAIL = 1500
FORBIDDEN = (ROOT / "data2", ROOT / "stdb", ROOT / "main")   # never written by this checker

ROLLOUT_SRC = r"""
import hashlib, json, sys, importlib
import numpy as np, gymnasium as gym
mod = importlib.import_module(sys.argv[1]); env_id = sys.argv[2]
assert getattr(mod, "ENV_ID", env_id) == env_id, "package ENV_ID differs from the requested explicit id"
env = gym.make(env_id)
o, info = env.reset(seed=11)
rng = np.random.default_rng(11)
h = hashlib.sha256(np.round(np.asarray(o, dtype=np.float64), 5).tobytes())
ret, keys = 0.0, set(info)
for _ in range(30):
    o, r, te, tr, info = env.step(rng.uniform(-1, 1, env.action_space.shape).astype(np.float32))
    h.update(np.round(np.asarray(o, dtype=np.float64), 5).tobytes()); ret += float(r); keys |= set(info)
    if te or tr: break
o1, _ = env.reset(seed=5); o2, _ = env.reset(seed=5)
print("ROLLOUT " + json.dumps(dict(obs_hash=h.hexdigest(), ret=round(ret, 4), info_keys=sorted(keys), obs_shape=list(np.shape(o)),
      act_shape=list(env.action_space.shape), seeded_reset=bool(np.allclose(o1, o2)))))
env.close()
"""

API_SRC = r"""
import inspect, json, sys
repo = sys.argv[1]; sys.path[:0] = [repo, repo + "/sim2real", repo + "/pipeline"]
out = {}
import make_scene
for f in ("build_scene", "reset", "drop_test"):
    fn = getattr(make_scene, f, None)
    out["make_scene." + f] = str(inspect.signature(fn)) if fn else None
for modname, cls in (("lift_env", "LiftEnv"), ("tag_env", "TagLiftEnv")):
    try:
        m = __import__(modname); c = getattr(m, cls, None)
        out[modname + "." + cls] = str(inspect.signature(c.__init__)) if c else None
        out[modname + ".imports"] = sorted({l.split()[1].split(".")[0] for l in open(m.__file__).read().splitlines()
                                            if l.startswith(("import ", "from "))})
    except Exception as e:
        out[modname + "." + cls] = "IMPORT ERROR: %s: %s" % (type(e).__name__, e)
print("API " + json.dumps(out))
"""


def clean_env(extra=None):
    """Minimal environment: no credentials, no PYTHONPATH shims (no TRELLIS proxy), bounded threads."""
    keep = {k: os.environ[k] for k in ("PATH", "HOME", "LANG", "LC_ALL", "TMPDIR", "UV_CACHE_DIR", "XDG_CACHE_HOME") if k in os.environ}
    keep.update(OMP_NUM_THREADS="2", OPENBLAS_NUM_THREADS="2", MKL_NUM_THREADS="2", PYTHONDONTWRITEBYTECODE="1",
                MUJOCO_GL="disable", PYTHONHASHSEED="0", UV_NO_PROGRESS="1")
    keep.update(extra or {})
    return keep


def limits():
    os.nice(15)
    resource.setrlimit(resource.RLIMIT_AS, (6 << 30, 6 << 30))      # 6 GiB address space per step
    resource.setrlimit(resource.RLIMIT_FSIZE, (2 << 30, 2 << 30))


def run(args, timeout, cwd, env=None, limit=True, log=None):
    t0 = time.time()
    try:
        r = subprocess.run([str(a) for a in args], cwd=str(cwd), capture_output=True, text=True, timeout=timeout,
                           env=env or clean_env(), preexec_fn=limits if limit else None)
        code, out, err = r.returncode, r.stdout or "", r.stderr or ""
    except subprocess.TimeoutExpired as e:
        code, out, err = 124, (e.stdout or b"").decode(errors="replace") if isinstance(e.stdout, bytes) else (e.stdout or ""), "TIMEOUT after %ss" % timeout
    if log is not None:
        log.write("$ %s\n[exit %s, %.1fs]\n%s\n%s\n" % (" ".join(map(str, args))[:600], code, time.time() - t0, out[-6000:], err[-6000:]))
        log.flush()
    return code, out, err, round(time.time() - t0, 1)


def marker(out, tag):
    for line in out.splitlines():
        if line.startswith(tag + " "):
            return json.loads(line[len(tag) + 1:])
    return None


def sanitize(s):
    s = re.sub(r"(hf_[A-Za-z0-9]{8,}|sk-[A-Za-z0-9_-]{8,}|eyJ[A-Za-z0-9_-]{20,}\.[A-Za-z0-9_.-]+)", "[redacted]", str(s))
    return s[-TAIL:]


def sha_tree(d):
    h = hashlib.sha256()
    for p in sorted(Path(d).rglob("*")):
        if p.is_file() and "__pycache__" not in p.parts:
            h.update(p.relative_to(d).as_posix().encode() + b"\0" + hashlib.sha256(p.read_bytes()).digest())
    return h.hexdigest()


def adapter_fingerprint(adir):
    files = {}
    for p in sorted(Path(adir).rglob("*.py")):
        if "__pycache__" in p.parts:
            continue
        files[p.relative_to(adir).as_posix()] = hashlib.sha256(p.read_bytes()).hexdigest()
    agg = hashlib.sha256(json.dumps(files, sort_keys=True).encode()).hexdigest()
    return dict(sha256=agg, files=files)


def git(repo, *a):
    r = subprocess.run(["git", "-C", str(repo), *a], capture_output=True, text=True, timeout=120)
    return r.returncode, r.stdout.strip(), r.stderr.strip()


class Check:
    def __init__(self, a):
        self.a = a
        self.repo = Path(a.repo).resolve()
        self.adapter = Path(a.adapter_dir).resolve()
        self.work = Path(a.work).resolve()
        for f in FORBIDDEN:
            if self.work == f or f in self.work.parents or self.repo == f:
                raise SystemExit(f"refusing: {self.work if self.work != f else self.repo} is a live path (data, database or live repo)")
        if RELEASES not in self.work.parents:
            raise SystemExit("refusing: --work must be under " + str(RELEASES))
        shutil.rmtree(self.work, ignore_errors=True)
        self.work.mkdir(parents=True)
        self.log = open(self.work / "check.log", "w")
        self.steps, self.blockers, self.drift = {}, [], []
        self.res = dict(started_at=time.time())

    def step(self, name, fn):
        t0 = time.time()
        try:
            detail = fn() or {}
            ok = detail.pop("_ok", True)
            err = detail.pop("_error", None)
        except Exception as e:
            ok, detail, err = False, {}, "%s: %s" % (type(e).__name__, e)
        rec = dict(ok=bool(ok), seconds=round(time.time() - t0, 1), **detail)
        if err:
            rec["error"] = sanitize(err)
        self.steps[name] = rec
        self.log.write(f"== {name}: {'ok' if ok else 'FAIL'} {rec.get('error', '')}\n"); self.log.flush()
        return ok

    # ── steps ────────────────────────────────────────────────────────────────
    def source(self):
        _, head, _ = git(self.repo, "rev-parse", "HEAD")
        _, tree, _ = git(self.repo, "rev-parse", "HEAD^{tree}")
        _, st, _ = git(self.repo, "status", "--porcelain", "--untracked-files=no")
        self.res.update(candidate_sha=head, candidate_tree=tree)
        if self.a.expect_sha and not head.startswith(self.a.expect_sha):
            return dict(_ok=False, _error=f"HEAD {head} is not the expected candidate {self.a.expect_sha}", sha=head)
        return dict(_ok=not st, _error=("tracked files modified: " + st[:300]) if st else None, sha=head, tree=tree)

    def fixtures(self):
        have, src = {}, {}
        for n in FIXTURES:
            d = self.repo / "assets" / n
            if (d / f"{n}.xml").exists():
                have[n], src[n] = d, "candidate"
            else:
                snap = RELEASES / "fixtures" / n
                if (snap / f"{n}.xml").exists():
                    have[n], src[n] = snap, "pinned snapshot (removed upstream)"
        self.fx = have
        missing = [n for n in FIXTURES if n not in have]
        if missing:
            self.blockers.append("validation fixtures unavailable: " + ", ".join(missing))
        objs = json.loads((self.repo / "objects.json").read_text()) if (self.repo / "objects.json").exists() else {}
        self.entries = {n: objs.get(n) for n in FIXTURES}
        if not all(self.entries.values()):
            snap = RELEASES / "fixtures" / "objects_entries.json"
            if snap.exists():
                pinned = json.loads(snap.read_text())
                for n in FIXTURES:
                    self.entries[n] = self.entries[n] or pinned.get(n)
        return dict(_ok=not missing and all(self.entries.values()), source=src,
                    hashes={n: sha_tree(d) for n, d in have.items()},
                    _error=("missing fixtures: " + ", ".join(missing)) if missing else (None if all(self.entries.values()) else "objects.json entries missing"))

    def cli(self):
        out = {}
        for script in ("photo2grasp.py", "make_asset.py"):
            if not (self.repo / script).exists():
                return dict(_ok=False, _error=f"{script} missing upstream")
            code, o, e, s = run([PY, script, "--help"], 120, self.repo, log=self.log)
            out[script] = dict(exit=code, seconds=s)
            if code:
                return dict(_ok=False, _error=f"{script} --help exit {code}: {e[-800:]}", **out)
        return out

    def introspect(self):
        code, o, e, s = run([PY, self.adapter / "introspect.py", self.repo], 300, self.repo, log=self.log)
        j = json.loads(o.strip().splitlines()[-1]) if o.strip() else {}
        cli = j.get("cli") or {}
        subs = sorted(((cli.get("subcommands") or {}) if isinstance(cli.get("subcommands"), dict) else {}).keys()) if cli else []
        contract = dict(stages=[(x.get("index"), x.get("label")) for x in j.get("stages") or []], assets=j.get("assets"),
                        cli=json.dumps(cli, sort_keys=True), make_asset=json.dumps(j.get("make_asset"), sort_keys=True))
        self.res["introspect_contract_sha256"] = hashlib.sha256(json.dumps(contract, sort_keys=True).encode()).hexdigest()
        self.res["introspect_summary"] = dict(stages=contract["stages"], assets=contract["assets"], subcommands=subs)
        cli_text = contract["cli"]
        ok = code == 0 and j.get("ok") and not j.get("errors") and '"asset"' in cli_text and len(contract["stages"]) >= 3
        return dict(_ok=ok, _error=None if ok else f"introspection: exit {code}, errors={j.get('errors')}, stages={len(contract['stages'])} {e[-400:]}",
                    stage_count=len(contract["stages"]), has_asset_step='"asset"' in cli_text)

    def api(self):
        code, o, e, s = run([PY, "-c", API_SRC, self.repo], 180, self.work, log=self.log)
        j = marker(o, "API")
        if not j:
            return dict(_ok=False, _error="cannot import make_scene / sim2real: " + e[-800:])
        self.res["api"] = j
        bad = [k for k, v in j.items() if isinstance(v, str) and v.startswith("IMPORT ERROR")]
        return dict(_ok=not bad and bool(j.get("make_scene.build_scene")), _error=("; ".join(f"{k} {j[k]}" for k in bad)) or None)

    def scene(self):
        out = {}
        assets = self.work / "assets"
        assets.mkdir(exist_ok=True)
        for n, d in self.fx.items():
            shutil.copytree(d, assets / n, dirs_exist_ok=True)
        n = "celsius_can"
        b = self.work / "scene_bundle"
        code, o, e, s = run([PY, self.adapter / "sim_worker.py", "bundle", "--repo", self.repo, "--assets", assets, "--names", n, "--out", b],
                            600, "/tmp", log=self.log)
        j = marker(o, "SIM_RESULT")
        if not j or not j.get("ok"):
            return dict(_ok=False, _error="bundle failed: " + (e or o)[-900:])
        out["bundle"] = dict(model_hash=j.get("model_hash"), places=j.get("places"))
        self.res["model_hash_celsius_can"] = j.get("model_hash")
        code, o, e, s = run([PY, self.adapter / "sim_worker.py", "simulate", "--repo", self.repo, "--assets", assets, "--names", n,
                             "--out", self.work / "scene_sim0", "--seed", "0"], 600, "/tmp", log=self.log)
        j0 = marker(o, "SIM_RESULT")
        if not j0:
            return dict(_ok=False, _error="seeded drop test failed: " + (e or o)[-900:], **out)
        out["drop_seed0"] = dict(result=j0.get("result"), metrics=j0.get("metrics"))
        n2 = "lego_tree"
        code, o, e, s = run([PY, self.adapter / "sim_worker.py", "bundle", "--repo", self.repo, "--assets", assets, "--names", n2,
                             "--out", self.work / "scene_bundle2"], 600, "/tmp", log=self.log)
        jb = marker(o, "SIM_RESULT") or {}
        x, y = (jb.get("places") or {}).get(n2, [0.0, -0.25])[:2]
        code, o, e, s = run([PY, self.adapter / "sim_worker.py", "simulate", "--repo", self.repo, "--assets", assets, "--names", n2,
                             "--out", self.work / "scene_sim1", "--x", x, "--y", y, "--yaw", 0], 600, "/tmp", log=self.log)
        j1 = marker(o, "SIM_RESULT")
        out["drop_posed_lego_tree"] = dict(result=(j1 or {}).get("result"), model_hash=jb.get("model_hash"))
        ok = j0.get("result") == "pass" and bool(j1) and j1.get("result") == "pass"
        return dict(_ok=ok, _error=None if ok else f"drop tests: seed0={j0.get('result')} posed={(j1 or {}).get('result')} {(e or '')[-400:]}", **out)

    def install_and_smoke(self, tag, archive, module, env_id, rollout):
        venv = self.work / ("venv_" + tag)
        env = clean_env()
        code, o, e, s = run(["bash", "-c", f"uv venv '{venv}' --python 3.11 -q && uv pip install -q --python '{venv}/bin/python' '{archive}'"],
                            1200, "/tmp", env=env, limit=False, log=self.log)
        rec = dict(install=dict(ok=code == 0, seconds=s))
        if code:
            return dict(_ok=False, _error="clean install failed: " + e[-800:], **rec)
        code, o, e, s = run([venv / "bin" / "python", "-m", module + ".scripts.smoke_test"], 900, "/tmp", env=env, log=self.log)
        sm = marker(o, "SMOKE_RESULT") or dict(ok=False, error=e[-600:])
        rec["smoke"] = {k: sm.get(k) for k in ("ok", "check_env", "seeded_reset_reproducible", "steps", "observation_space", "action_space", "env_id", "error") if k in sm}
        ok = bool(sm.get("ok")) and sm.get("check_env") == "passed" and sm.get("seeded_reset_reproducible") is not False
        if ok and rollout:
            code, o, e, s = run([venv / "bin" / "python", "-c", ROLLOUT_SRC, module, env_id], 600, "/tmp", env=env, log=self.log)
            ro = marker(o, "ROLLOUT")
            rec["rollout"] = ro or dict(error=e[-600:])
            ok = bool(ro) and ro.get("seeded_reset")
        shutil.rmtree(venv, ignore_errors=True)
        return dict(_ok=ok, _error=None if ok else "smoke/rollout failed: " + json.dumps(rec)[-900:], **rec)

    def single_pkg(self, tag, env_id, expect_module=None, rollout=False):
        n = "celsius_can"
        w = self.work / tag
        w.mkdir(exist_ok=True)
        (w / "entry.json").write_text(json.dumps(self.entries[n]))
        (w / "prov.json").write_text(json.dumps(dict(repo_url="https://github.com/adityabangde/photo2grasp", commit=self.res.get("candidate_sha", "")[:7],
                                                     robot_profile="so100-menagerie", robot_profile_note="release-gate fixture", asset_version=0,
                                                     asset_version_id="release_gate_fixture", scene_version=0, scene_version_id="release_gate_fixture")))
        code, o, e, s = run([PY, self.adapter / "gym_export.py", "--repo", self.repo, "--asset-dir", self.work / "assets" / n, "--name", n,
                             "--objects-entry", w / "entry.json", "--out", w / "out", "--version", "0.0.1", "--env-id", env_id,
                             "--provenance", w / "prov.json"], 600, "/tmp", log=self.log)
        res = marker(o, "EXPORT_RESULT")
        if not res:
            return dict(_ok=False, _error="assembly failed: " + (e or o)[-900:])
        archive = Path(res["archive"])
        with zipfile.ZipFile(archive) as z:
            roots = sorted({x.split("/")[0] for x in z.namelist()})
        module = res.get("module") or res.get("module_name") or (roots[0] if len(roots) == 1 else None)
        rec = dict(module=module, env_id=env_id, files=res.get("file_count"), archive_bytes=archive.stat().st_size,
                   archive_roots=roots, legacy_ids=res.get("legacy_ids"))
        if expect_module and module != expect_module:
            return dict(_ok=False, _error=f"explicit legacy env id must keep module {expect_module}, got {module}", **rec)
        r = self.install_and_smoke(tag, archive, module, env_id, rollout)
        if tag == "legacy_pkg" and r.get("rollout"):
            self.res["legacy_rollout_fingerprint"] = {k: r["rollout"].get(k) for k in ("obs_hash", "ret", "info_keys", "obs_shape", "act_shape")}
        return dict(r, **rec)

    def generic_pkg(self):
        if not (MENAGERIE_SO100 / "scene.xml").exists():
            self.blockers.append("pinned Menagerie SO-ARM100 model unavailable for the generic export check")
            return dict(_ok=False, _error="robot fixture missing")
        w = self.work / "generic"
        w.mkdir(exist_ok=True)
        ga = w / "assets"
        shutil.copytree(self.work / "assets" / "celsius_can", ga / "celsius_can")
        (w / "prov.json").write_text(json.dumps(dict(project_name="release gate fixture", scene_version="fixture", scene_version_id="fixture",
                                                     asset_versions=[], robot=dict(name="SO-ARM100", library_id="trs_so_arm100",
                                                     source="MuJoCo Menagerie (pinned cache)", license="Apache-2.0", note="fixture"),
                                                     objects={"celsius_can": dict(summary="repository sample asset")})))
        code, o, e, s = run([PY, self.adapter / "workcell_export.py", "--robot-dir", MENAGERIE_SO100, "--assets", ga, "--names", "celsius_can",
                             "--out", w / "out", "--version", "0.0.1", "--env-id", GENERIC_ENV_ID, "--slug", "release_gate",
                             "--provenance", w / "prov.json"], 900, "/tmp", log=self.log)
        res = marker(o, "EXPORT_RESULT")
        if not res:
            return dict(_ok=False, _error="workcell assembly failed: " + (e or o)[-900:])
        module = res.get("module") or res.get("module_name")
        r = self.install_and_smoke("generic", Path(res["archive"]), module, GENERIC_ENV_ID, False)
        return dict(r, module=module, files=res.get("file_count"))

    def main(self):
        a = self.a
        self.res["adapter_fingerprint_start"] = adapter_fingerprint(self.adapter)
        self.step("source", self.source)
        self.step("fixtures", self.fixtures)
        if not self.blockers:
            self.step("cli", self.cli)
            self.step("introspect", self.introspect)
            self.step("api", self.api)
            if self.step("scene", self.scene):
                self.step("legacy_pkg", lambda: self.single_pkg("legacy_pkg", LEGACY_ENV_ID.format(name="celsius_can"), "photo2grasp_env", rollout=True))
                if not a.quick:
                    self.step("current_pkg", lambda: self.single_pkg("current_pkg", CURRENT_ENV_ID.format(name="celsius_can")))
                    self.step("generic_pkg", self.generic_pkg)
        _, st, _ = git(self.repo, "status", "--porcelain", "--untracked-files=no")
        self.steps["source_after"] = dict(ok=not st, error=("checker modified tracked files: " + st[:300]) if st else None)
        end = adapter_fingerprint(self.adapter)
        self.res["adapter_fingerprint"] = end
        stable = end["sha256"] == self.res["adapter_fingerprint_start"]["sha256"]
        if not stable:
            self.blockers.append("adapter files changed during the check; result invalidated")
        self.res["adapter_fingerprint_start"] = self.res["adapter_fingerprint_start"]["sha256"]
        self.res.update(steps=self.steps, blockers=self.blockers, adapter_stable=stable, quick=bool(a.quick),
                        passed=stable and not self.blockers and all(s.get("ok") for s in self.steps.values()),
                        finished_at=time.time(), seconds=round(time.time() - self.res["started_at"], 1),
                        scope="adapter/upstream contract compatibility with repository sample fixtures; not spatial accuracy, physical fidelity or hardware safety")
        self.log.close()
        if not a.keep:
            for p in self.work.iterdir():
                if p.is_dir():
                    shutil.rmtree(p, ignore_errors=True)
        print("RELEASE_CHECK " + json.dumps(self.res, default=str))
        return 0 if self.res["passed"] else 1


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--repo", required=True, help="candidate checkout (isolated worktree)")
    ap.add_argument("--adapter-dir", default=str(Path(__file__).resolve().parent))
    ap.add_argument("--work", required=True, help="scratch dir under /workspace/photo2grasp/releases")
    ap.add_argument("--expect-sha", default="")
    ap.add_argument("--quick", action="store_true", help="legacy package only (skip current-id and generic packages)")
    ap.add_argument("--keep", action="store_true", help="keep scratch outputs")
    sys.exit(Check(ap.parse_args()).main())
