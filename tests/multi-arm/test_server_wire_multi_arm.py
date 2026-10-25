"""Exercise the STAGED server run kinds (create_ext_run workcell -> run_workcell -> run_workcell_export) end to end with
in-memory stubs for SpacetimeDB/run rows. Real adapters, real MuJoCo, real clean-venv install. No database, no user data.
Run with the backend interpreter (/workspace/.venv/bin/python)."""
import ast, hashlib, io, json, math, os, re, shutil, subprocess, sys, tempfile, time, zipfile
from pathlib import Path

STAGE = Path(__file__).resolve().parent.parent
LIVE = Path("/workspace/photo2grasp")
PY = str(LIVE / ".venv" / "bin" / "python")
TMP = Path(tempfile.mkdtemp(prefix="realisim_ma_server_"))
FAIL = []


def check(c, m):
    print(("PASS " if c else "FAIL ") + m)
    if not c:
        FAIL.append(m)


src = (STAGE / "backend" / "server.py").read_text()
tree = ast.parse(src)
want = {"tool", "file_map", "files_of", "media", "sh", "_num", "norm_table", "norm_placements", "pinned_robot_import", "norm_arm_instances",
        "arms_arg", "run_workcell", "run_workcell_export", "create_ext_run"}
consts = {"MEDIA", "ARM_NAME_RE", "POSE_SOURCES_ARM", "TABLE_SOURCES", "PLACE_SOURCES", "OBS_NOTE", "FIDELITY_NOTE"}
parts = []
for n in tree.body:
    if isinstance(n, ast.FunctionDef) and n.name in want:
        parts.append(ast.get_source_segment(src, n))
    elif isinstance(n, ast.Assign) and any(getattr(t, "id", "") in consts for t in n.targets):
        parts.append(ast.get_source_segment(src, n))


class HTTPErr(Exception):
    pass


class DB:
    def __init__(self):
        self.blobs = {}

    def store_dir(self, uid, pid, kind, oid, ver, entries, deps=None):
        files = []
        for i, (p, b, mt) in enumerate(entries):
            fid = f"f_{kind}_{oid}_{i}"
            self.blobs[fid] = b
            files.append(dict(path=p, id=fid, size=len(b), sha256=hashlib.sha256(b).hexdigest()))
        return f"man_{kind}_{oid}", files

    def read_bytes(self, uid, pid, fid):
        return None, self.blobs[fid]


R = TMP / "robots"
for lid in ("robotstudio_so101", "trs_so_arm100"):
    subprocess.run([PY, str(LIVE / "adapter" / "robot_tool.py"), "stage", "--id", lid, "--out", str(R / lid)], capture_output=True, cwd="/tmp", check=True)
A = TMP / "assets"
shutil.copytree(LIVE / "data2" / "cache" / "assets" / "av_9ecb85c5c0414aa9" / "qa_mug", A / "qa_mug")
IMPORTS = {"rbt_s101": "robotstudio_so101", "rbt_s100": "trs_so_arm100"}
RUNS = {
    "run_imp1": dict(id="run_imp1", kind="import_robot", status="complete", output_json=dict(robot_import=dict(id="rbt_s101", library_id="robotstudio_so101", name="SO-101 (5-DOF)", manifest_id="man_r1", commit="0" * 40, license="Apache-2.0", model_hash="h1", stats={}))),
    "run_imp2": dict(id="run_imp2", kind="import_robot", status="complete", output_json=dict(robot_import=dict(id="rbt_s100", library_id="trs_so_arm100", name="SO-ARM100 (5-DOF)", manifest_id="man_r2", commit="0" * 40, license="Apache-2.0", model_hash="h2", stats={}))),
}
NEW = []
counter = [0]


def new_id(p):
    counter[0] += 1
    return f"{p}_{counter[0]:04d}"


def new_run(owner, pid, kind, inp):
    rid = new_id("run")
    RUNS[rid] = dict(id=rid, kind=kind, status="queued", input=inp, output_json={}, project_id=pid, created_at=time.time())
    NEW.append(rid)
    return rid


ns = dict(re=re, json=json, math=math, os=os, io=io, time=time, zipfile=zipfile, hashlib=hashlib, shutil=shutil, subprocess=subprocess, Path=Path,
          jerr=lambda code, msg, **k: HTTPErr(f"{code}: {msg}"), one=lambda t, pid, rid: RUNS[rid], rows=lambda t, pid: list(RUNS.values()),
          restore_robot=lambda uid, pid, r: R / IMPORTS[r["robot_import_id"]], assets_dir=lambda uid, pid, avs, key: A,
          asset_version=lambda pid, a: dict(id=a, status="accepted", object_name="qa_mug", properties_json=dict(material="ceramic")),
          db=DB(), REPO="/tmp", new_id=new_id, new_run=new_run, now_ms=lambda: int(time.time() * 1000), NICE=[], PY=PY, CACHE=TMP / "cache", VALIDATE=TMP / "validate",
          MAX_PACKAGE=100 * 1024 * 1024, WORKCELL_PY=STAGE / "adapter" / "workcell.py", WORKCELL_EXPORT_PY=STAGE / "adapter" / "workcell_export.py",
          ROBOTS=type("R", (), dict(BY_ID={k: {} for k in IMPORTS.values()}))(), SLUG=re.compile(r"^[a-z][a-z0-9_]{1,39}$"))
exec("\n\n".join(parts), ns)


class Ctx:
    def __init__(self, rid):
        self.row = dict(RUNS[rid], project_id="prj_test")
        self.input, self.uid, self.output, self.log, self.stages = RUNS[rid]["input"], "qa_tester", {}, [], {}

    def stage(self, k, st, note=""):
        self.stages[k] = (st, note)

    def write(self, t):
        self.log.append(t)

    def check(self):
        pass

    def push(self, **k):
        pass


try:
    p = dict(owner_uid="qa_tester", name="Bi arm test", settings_json=dict(robot=dict(library_id="robotstudio_so101", name="SO-101 (5-DOF)", robot_import_id="rbt_s101", manifest_id="man_r1")))
    tbl = dict(length_m=1.2, width_m=1.0, height_m=0.75, source="measured")
    req = dict(asset_version_ids=["av_1"], table=tbl, placements={},
               robots=[dict(name="robot_1", label="Left", library_id="robotstudio_so101", import_run_id="run_imp1", pose=None, pose_source="origin"),
                       dict(name="robot_2", label="Right", library_id="robotstudio_so101", import_run_id="run_imp1", pose=None, pose_source="default"),
                       dict(name="robot_3", label="Third", library_id="trs_so_arm100", import_run_id="run_imp2", pose=dict(x_m=0.0, y_m=-0.5, yaw_deg=90), pose_source="measured")])
    rid = ns["create_ext_run"]("qa_tester", p, "prj_test", "workcell", req)
    inp = RUNS[rid]["input"]
    check(len(inp["robots"]) == 3 and inp["robot"]["robot_import_id"] == "rbt_s101", "create_ext_run pins 3 arms; primary kept in legacy robot field")
    t0 = time.time()
    ctx = Ctx(rid)
    ns["run_workcell"](ctx)
    wc = ctx.output["workcell"]
    RUNS[rid].update(status="complete", output_json=dict(workcell=wc))
    check(wc["result"] == "pass" and [r["name"] for r in wc["robots"]] == ["robot_1", "robot_2", "robot_3"], f"run_workcell composed + simulated 3 arms ({time.time() - t0:.1f} s)")
    check(wc["robots"][1]["pose_source"] == "default" and wc["robots"][1]["pose"] is not None, "default slot pinned into the record (provisional label kept)")
    check(wc["robots"][2]["base_pose"]["y_m"] == -0.5 and wc["robots"][2]["pose_source"] == "measured", "measured pose preserved")
    check(all(r["sim"] for r in wc["robots"]), "per-arm simulation metrics stored")
    wcj = json.loads(next(b for f, b in ns["db"].blobs.items() if f.startswith("f_workcell_") and b.startswith(b'{\n "id"')))
    check([r["name"] for r in wcj["robots"]] == ["robot_1", "robot_2", "robot_3"] and len({r["robot_manifest_id"] for r in wcj["robots"]}) == 2, "stored workcell.json pins each arm (2 unique model manifests)")
    exr = new_run("qa_tester", "prj_test", "workcell_export", dict(workcell_run_id=rid, package_version="1.0.1", env_id="RealiSIM/Workcell_bi_arm_test-v1", slug="bi_arm_test", project_name="Bi arm test"))
    ns["sh"] = lambda cmd, cwd=None, timeout=None, env=None: (lambda r: (r.returncode, r.stdout, r.stderr))(subprocess.run(cmd, shell=True, capture_output=True, text=True, cwd=cwd, timeout=timeout, env=env))
    t0 = time.time()
    ctx2 = Ctx(exr)
    ns["run_workcell_export"](ctx2)
    ex = ctx2.output["export"]
    check(ex["status"] == "ready" and [r["name"] for r in ex["robots"]] == ["robot_1", "robot_2", "robot_3"], f"run_workcell_export: clean install + smoke + read-back ({time.time() - t0:.1f} s)")
    sm = ex["validation"]["smoke"]
    check(sm["check_env"] == "passed" and len(sm["arms"]) == 3 and sm["action_space"].endswith("(18,), float32)"), f"smoke: {sm['action_space']} {sm['observation_space']}")
    bp = {r["name"]: r["base_pose"] for r in ex["robots"]}
    check(bp["robot_2"] == wc["robots"][1]["base_pose"], "export reproduces the simulated default pose exactly")
    check(set(ex["end_effectors"]) == {"robot_1", "robot_2", "robot_3"} and ex["end_effector"] == ex["end_effectors"]["robot_1"], "export per-arm EE map + primary compat field")
except Exception as e:
    import traceback
    traceback.print_exc()
    FAIL.append(repr(e))
finally:
    shutil.rmtree(TMP, ignore_errors=True)
    print("cleanup:", not TMP.exists())
print(json.dumps(dict(ok=not FAIL, failed=FAIL)))
sys.exit(1 if FAIL else 0)
