"""Multi-arm backend unit tests (run with the backend interpreter: /workspace/.venv/bin/python).
Pure functions only: no database, no services, no user project. Synthetic fixture mirrors the real bi-arm shape
(two analysis arms hinting SO-100, global SO-101 chosen by the user)."""
import ast, copy, json, math, sys, types
from pathlib import Path

STAGE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(STAGE / "backend"))
sys.path.insert(1, "/workspace/photo2grasp/backend")
import robots as ROBOTS  # noqa: E402  (read-only catalog)
import workflow as WF    # noqa: E402

WF.S = types.SimpleNamespace(ROBOTS=ROBOTS, STATE={}, measurement_problems=lambda m: [], rows=lambda t, pid: [])
FAIL = []


def check(cond, msg):
    print(("PASS " if cond else "FAIL ") + msg)
    if not cond:
        FAIL.append(msg)


def field(v, src="measured"):
    return dict(value=v, source=src, origin="user")


def fixture():
    wf = WF.new_workflow("workspace")
    wf["table"] = dict(length_m=field(1.2), width_m=field(0.8), height_m=field(0.75), note="")
    wf["objects"] = []
    wf["robot"].update(selected_library_id="robotstudio_so101", selected_origin="user", single_arm_only=True)
    wf["robot"].pop("multi_arm", None)
    wf["robot"]["arms"] = [
        dict(id="arm_a", name="robot_1", label="Left arm", decision="use", decision_origin="user", library_id="trs_so_arm100", library_origin="analysis", origin="analysis"),
        dict(id="arm_b", name="robot_2", label="Right arm", decision="use", decision_origin="user", library_id="trs_so_arm100", library_origin="analysis", origin="analysis"),
    ]
    return wf


wf = fixture()
check("single_arm_only" not in WF.new_workflow("workspace")["robot"], "new workflows no longer advertise single_arm_only")
check(WF.arm_library(wf, wf["robot"]["arms"][0]) == ("robotstudio_so101", "global_user"), "global user SO-101 overrides stale SO-100 analysis hint")
wf2 = copy.deepcopy(wf)
wf2["robot"]["arms"][1].update(library_id="universal_robots_ur5e", library_origin="user")
check(WF.arm_library(wf2, wf2["robot"]["arms"][1])[0] == "universal_robots_ur5e", "explicit per-arm user model wins")
check(wf2["robot"]["arms"][0]["library_id"] == "trs_so_arm100", "analysis evidence preserved (not relabelled)")
rd = WF.readiness(wf, None, [])
codes = [b["code"] for b in rd["blockers"]]
check("multi_arm" not in codes, f"two used arms are not blocked (blockers: {codes})")
check(any("2 arms will be simulated" in x and "provisional for: Right arm" in x for x in rd["warnings"]), "warning names arms without measured base pose")
inst = WF.robot_instances(wf)
check([i["name"] for i in inst] == ["robot_1", "robot_2"] and all(i["library_id"] == "robotstudio_so101" for i in inst), "two instances, both SO-101")
check(WF.primary_arm(wf) == "robot_1", "primary defaults to first used arm")
eff = WF.effective_layout(wf)
check(eff["provisional"] and eff["robots"]["robot_2"]["pose"] is None and eff["robots"]["robot_2"]["source"] == "default", "missing arm pose stays provisional default")
check(eff["robots"]["robot_1"]["source"] == "origin", "primary is the origin")
# measured poses (table frame) -> primary base frame
wf3 = copy.deepcopy(wf)
wf3["layout"]["alignment"]["robots"] = {"robot_1": dict(method="table_measured", x_m=-0.5, y_m=-0.3, yaw_deg=0),
                                        "robot_2": dict(method="table_measured", x_m=-0.5, y_m=0.3, yaw_deg=0)}
eff3 = WF.effective_layout(wf3)
p = eff3["robots"]["robot_2"]["pose"]
check(eff3["robots"]["robot_2"]["source"] == "measured" and abs(p["x_m"]) < 1e-6 and abs(p["y_m"] - 0.6) < 1e-6, f"measured arm pose converted to primary frame {p}")
check(not eff3["robots"]["robot_2"]["provisional"], "measured arm pose is not provisional")
# rotated primary
wf4 = copy.deepcopy(wf3)
wf4["layout"]["alignment"]["robots"]["robot_1"]["yaw_deg"] = 90
p4 = WF.effective_layout(wf4)["robots"]["robot_2"]["pose"]
check(abs(p4["x_m"] - 0.6) < 1e-6 and abs(p4["y_m"]) < 1e-6 and abs(p4["yaw_deg"] + 90) < 1e-6, f"rotated primary frame transform {p4}")
# video anchors: compose table_in_base of both arms
wf5 = copy.deepcopy(wf)
T1 = dict(x_m=-0.5, y_m=-0.3, yaw_deg=30.0)
T2 = dict(x_m=0.4, y_m=0.35, yaw_deg=-120.0)
wf5["layout"]["result"] = dict(status="complete", robots=[dict(name="robot_1", table_in_base=WF.se2_inv(T1), anchor=dict(source="reconstructed")),
                                                          dict(name="robot_2", table_in_base=WF.se2_inv(T2), anchor=dict(source="reconstructed"))])
pv = WF.arm_poses(wf5, wf5["layout"]["result"], "robot_1")["robot_2"]
import numpy as np  # independent matrix check
def _m(p):
    r = math.radians(p["yaw_deg"]); return np.array([[math.cos(r), -math.sin(r), p["x_m"]], [math.sin(r), math.cos(r), p["y_m"]], [0, 0, 1.0]])
_M = np.linalg.inv(_m(T1)) @ _m(T2)
exp = dict(x_m=_M[0, 2], y_m=_M[1, 2], yaw_deg=math.degrees(math.atan2(_M[1, 0], _M[0, 0])))
check(pv["source"] == "video_reconstructed" and all(abs(pv["pose"][k] - exp[k]) < 1e-3 for k in exp), f"video anchors composed {pv['pose']} == {exp}")
# primary excluded -> blocked; three arms allowed; a third arm without pose is provisional
wf6 = copy.deepcopy(wf)
wf6["robot"]["arms"].append(dict(id="arm_c", name="robot_3", label="Third", decision="use", library_id="franka_emika_panda", library_origin="user", origin="user"))
check(len(WF.robot_instances(wf6)) == 3, "three used arms -> three instances (no cap)")
wf6["layout"]["alignment"]["primary_robot"] = "robot_9"
check("primary_not_used" in [b["code"] for b in WF.readiness(wf6, None, [])["blockers"]], "primary must be a used arm")
# selections carry the resolved model
sel = WF.selections(wf)
check(all(r["library_id"] == "robotstudio_so101" for r in sel["robots"]), "layout selections use resolved models")
# single used arm keeps the legacy single path
wf7 = copy.deepcopy(wf)
wf7["robot"]["arms"][1]["decision"] = "exclude"
check(len(WF.robot_instances(wf7)) == 1 and "robots" not in WF.effective_layout(wf7), "one used arm keeps the legacy single-arm layout")
wf8 = WF.new_workflow("workspace")
check([i["library_id"] for i in WF.robot_instances(wf8)] == ["robotstudio_so101"] and WF.robot_instances(wf8)[0]["synthetic"], "no arms: assumed SO-101 single synthetic instance")

# server wire helpers, exercised with stubs (no database)
src = (STAGE / "backend" / "server.py").read_text()
tree = ast.parse(src)
want = {"pinned_robot_import", "norm_arm_instances", "arms_arg", "_num"}
ns = {}
code = "\n\n".join(ast.get_source_segment(src, n) for n in tree.body if isinstance(n, ast.FunctionDef) and n.name in want)
import re as _re
seg = [ast.get_source_segment(src, n) for n in tree.body if isinstance(n, ast.Assign) and any(getattr(t, "id", "") in ("ARM_NAME_RE", "POSE_SOURCES_ARM") for t in n.targets)]


class HTTPErr(Exception):
    pass


RUNS = {"run_i1": dict(id="run_i1", kind="import_robot", status="complete", output_json=dict(robot_import=dict(id="rbt_1", library_id="robotstudio_so101", name="SO-101 (5-DOF)", manifest_id="man_1", commit="abc", license="Apache-2.0", model_hash="h1"))),
        "run_i2": dict(id="run_i2", kind="import_robot", status="complete", output_json=dict(robot_import=dict(id="rbt_2", library_id="universal_robots_ur5e", name="UR5e", manifest_id="man_2", commit="abc", license="BSD-3-Clause", model_hash="h2"))),
        "run_x": dict(id="run_x", kind="import_robot", status="failed", output_json={})}
ns.update(re=_re, math=math, json=json, jerr=lambda code, msg, **k: HTTPErr(f"{code}: {msg}"), one=lambda t, pid, rid: RUNS[rid],
          restore_robot=lambda uid, pid, r: Path("/cache/robots") / r["robot_import_id"])
exec("\n".join(seg) + "\n\n" + code, ns)
arms = ns["norm_arm_instances"]("prj", [dict(name="robot_1", import_run_id="run_i1", library_id="robotstudio_so101"),
                                        dict(name="robot_2", import_run_id="run_i1", pose=dict(x_m=0, y_m=0.5, yaw_deg=0), pose_source="measured"),
                                        dict(name="robot_3", import_run_id="run_i2", pose=None)])
check([a["arm"] for a in arms] == ["robot_1", "robot_2", "robot_3"] and arms[0]["pose"] is None and arms[2]["pose_source"] == "default", "server pins every arm, primary at origin, missing pose = default")
check(arms[0]["robot_import_id"] == arms[1]["robot_import_id"] == "rbt_1" and arms[2]["manifest_id"] == "man_2", "same-model arms share one pinned import")
wire = json.loads(ns["arms_arg"]("u", "prj", arms))
check(len({w["robot_dir"] for w in wire}) == 2 and [w["name"] for w in wire] == ["robot_1", "robot_2", "robot_3"], "adapter wire: one restored folder per unique model")
for bad, why in (([dict(name="a", import_run_id="run_i1")], "fewer than two arms"),
                 ([dict(name="a", import_run_id="run_i1"), dict(name="a", import_run_id="run_i1")], "duplicate names"),
                 ([dict(name="a", import_run_id="run_i1"), dict(name="b", import_run_id="run_x")], "failed import"),
                 ([dict(name="a", import_run_id="run_i1"), dict(name="b", import_run_id="run_i2", library_id="trs_so_arm100")], "model mismatch")):
    try:
        ns["norm_arm_instances"]("prj", bad)
        check(False, "rejects " + why)
    except HTTPErr:
        check(True, "rejects " + why)
many = [dict(name=f"arm_{i}", import_run_id="run_i1", pose=dict(x_m=0.1 * i, y_m=0, yaw_deg=0)) for i in range(12)]
check(len(ns["norm_arm_instances"]("prj", many)) == 12, "twelve arms accepted (no artificial count cap)")
# PUT: new arms (no count cap), explicit model -> per-arm user choice, collision-free names, records keep decisions
wp = fixture()
for k in range(3):
    WF.apply_put(wp, dict(robot=dict(arms=[dict(new=True, label="Robot", name="robot_2", library_id=("franka_emika_panda" if k == 0 else None), decision="use")])), "u", None)
names = [a["name"] for a in wp["robot"]["arms"]]
check(len(names) == 5 and len(set(names)) == 5, f"PUT adds arms without a cap, unique names {names}")
newa = wp["robot"]["arms"][2]
check(newa["library_origin"] == "user" and WF.arm_library(wp, newa)[0] == "franka_emika_panda", "new arm with a chosen model = explicit per-arm choice")
check(WF.arm_library(wp, wp["robot"]["arms"][3])[0] == "robotstudio_so101", "new arm without a model inherits the global user choice")
check(len(WF.robot_instances(wp)) == 5, "five used arms -> five composed instances")

# mesh/asset cache keys depend only on the object's inputs and measurements, never on robots or arm count
wsrc = (STAGE / "backend" / "workflow.py").read_text()
check('mesh_key = sha_json(dict(name=o["name"], inputs=shas))' in wsrc and 'asset_key = sha_json(dict(mesh=mesh_key, m=o["measurements"]))' in wsrc,
      "mesh/asset cache keys unchanged and independent of arm count")
print(json.dumps(dict(ok=not FAIL, failed=FAIL)))
sys.exit(1 if FAIL else 0)
