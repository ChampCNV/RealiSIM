"""Drive the STAGED workflow.tick_build through queued -> building -> accepting -> layout -> composing for a 3-arm draft
with in-memory stubs (no database, no jobs, no GPU). Verifies: one import per unique model, arm statuses, primary
confirmation, and the exact multi-arm workcell request (names, pinned imports, measured vs default poses).
Run with /workspace/.venv/bin/python."""
import copy, json, sys, types
from pathlib import Path

STAGE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(STAGE / "backend"))
sys.path.insert(1, "/workspace/photo2grasp/backend")
import robots as ROBOTS  # noqa: E402
import workflow as WF    # noqa: E402

FAIL = []


def check(c, m):
    print(("PASS " if c else "FAIL ") + m)
    if not c:
        FAIL.append(m)


STATE = dict(st={}, wf=None)
RUNS, CREATED = {}, []


def new_run(owner, pid, kind, inp):
    rid = f"run_{len(RUNS) + 1:03d}"
    RUNS[rid] = dict(id=rid, kind=kind, status="queued", input=inp, output_json={}, created_at=len(RUNS))
    return rid


def create_ext_run(uid, p, pid, kind, b):
    CREATED.append((kind, copy.deepcopy(b)))
    return new_run(uid, pid, kind, b)


def one(t, pid, rid):
    return RUNS[rid]


AV = dict(id="av_1", status="needs_review", object_name="mug", run_id="run_av", manifest_id="m")
svc = types.SimpleNamespace(ROBOTS=ROBOTS, STATE={}, measurement_problems=lambda m: [], rows=lambda t, pid: list(RUNS.values()), one=one,
                            new_run=new_run, create_ext_run=create_ext_run, project_for=lambda uid, pid, write=False: dict(owner_uid="u", name="t"),
                            asset_version=lambda pid, a: AV, upload_row=lambda pid, x: dict(id=x, sha256="s" + x),
                            db=types.SimpleNamespace(sql=lambda q: [dict(owner_uid="u")]))
WF.S = svc
WF.load = lambda pid: (dict(owner_uid="u"), STATE["st"], copy.deepcopy(STATE["wf"]))


def mutate(pid, f):
    wf = copy.deepcopy(STATE["wf"])
    r = f(wf, dict(owner_uid="u"), STATE["st"])
    STATE["wf"] = wf
    return r, wf


WF.mutate = mutate
WF.derived_upload = lambda pid, owner, c: c["upload_id"]
WF.ensure_source = lambda pid, owner, x: "/tmp/x"
WF.persist_source = lambda *a: None
WF.record_source = lambda *a: None
WF._ck = lambda: types.SimpleNamespace(apply_timing=lambda s, st: s.update(status=st), run_recovery=lambda r, c: dict(requires_ack=False))


def loop(method, path, uid, body=None):
    if path.endswith("/robot"):
        ri = RUNS[body["import_run_id"]]["output_json"]["robot_import"]
        STATE["st"]["robot"] = dict(library_id=ri["library_id"], manifest_id=ri["manifest_id"], import_run_id=body["import_run_id"])
    if path.endswith("/review"):
        AV["status"] = "accepted"
    return {}


WF.loop = loop

wf = WF.new_workflow("workspace")
wf["revision"] = 7
wf["table"] = dict(length_m=dict(value=1.2, source="measured"), width_m=dict(value=1.0, source="measured"), height_m=dict(value=0.75, source="measured"), note="")
wf["objects"] = [dict(id="obj_1", name="mug", include=True, fields={}, crops=[dict(id="c1", upload_id="up_c1", sha256="x", time_s=1, box=[0, 0, 1, 1])],
                      selected_crop_ids=["c1"], extra_upload_ids=[])]
wf["robot"].update(selected_library_id="robotstudio_so101", selected_origin="user")
wf["robot"]["arms"] = [dict(id="a1", name="robot_1", label="Left", decision="use", library_id="trs_so_arm100", library_origin="analysis", origin="analysis"),
                       dict(id="a2", name="robot_2", label="Right", decision="use", library_id="trs_so_arm100", library_origin="analysis", origin="analysis"),
                       dict(id="a3", name="panda", label="Panda", decision="use", library_id="franka_emika_panda", library_origin="user", origin="user"),
                       dict(id="a4", name="robot_4", label="Excluded", decision="exclude", library_id="trs_so_arm100", library_origin="analysis", origin="analysis")]
wf["layout"]["alignment"]["robots"] = {"robot_1": dict(method="table_measured", x_m=-0.5, y_m=-0.3, yaw_deg=0),
                                       "panda": dict(method="table_measured", x_m=0.5, y_m=0.0, yaw_deg=180)}
# object asset already cached -> reused (no new mesh call), mirrors the real cache-key rule
m = WF.measurements(wf["objects"][0])
mk = WF.sha_json(dict(name="mug", inputs=sorted(["sup_c1"])))
wf["asset_cache"] = {WF.sha_json(dict(mesh=mk, m=m)): "av_1"}
snap_inst = WF.robot_instances(wf)
wf["build"] = dict(id="bld_1", phase="queued", mode="workspace", created_by="u", inputs=dict(mode="workspace", primary_upload_id=None, draft_revision=7,
                   objects=[dict(id="obj_1", name="mug", measurements=m, crops=wf["objects"][0]["crops"], extra_upload_ids=[])],
                   robot=dict(selected_library_id="robotstudio_so101", selected_origin="user", arms=wf["robot"]["arms"], instances=snap_inst, primary_robot="robot_1")),
                   steps=WF.mk_steps("workspace"), objects={}, robot=None, layout=None, preview=None, export=None, acknowledge_estimates=False)
STATE["wf"] = wf

WF.tick_build("prj")
b = STATE["wf"]["build"]
check(b["phase"] == "building", f"queued -> building ({b.get('error')})")
imports = [k for k, _ in CREATED if k == "import_robot"]
libs = sorted(x["library_id"] for k, x in CREATED if k == "import_robot")
check(libs == ["franka_emika_panda", "robotstudio_so101"], f"one import per unique model, global SO-101 beats stale SO-100 hint: {libs}")
check([a["name"] for a in b["robots"]] == ["robot_1", "robot_2", "panda"] and "robot_4" not in json.dumps(b["robots"]), "build.robots lists every used arm (excluded arm not composed)")
check(b["objects"]["obj_1"]["reused"] == "asset", "object asset reused: cache key independent of arm count")
check(all(a["status"] == "importing" for a in b["robots"]), "arm statuses: importing")
WF.tick_build("prj")
check(STATE["wf"]["build"]["phase"] == "building", "waits for imports")
for rid, r in RUNS.items():
    if r["kind"] == "import_robot":
        lid = r["input"]["library_id"]
        r.update(status="complete", output_json=dict(robot_import=dict(id="rbt_" + lid, library_id=lid, name=lid, manifest_id="man_" + lid, commit="c", license="L")))
WF.tick_build("prj")
b = STATE["wf"]["build"]
check(all(a["status"] == "ready" for a in b["robots"]) and b["robot"]["confirmed"], "all arm models ready, primary confirmed")
check(STATE["st"]["robot"]["library_id"] == "robotstudio_so101", "primary model confirmed into settings.robot (legacy contract)")
WF.tick_build("prj")                     # like the legacy single-arm flow, the phase advances on the next tick after confirmation
b = STATE["wf"]["build"]
check(b["phase"] == "accepting", f"building -> accepting ({b['phase']})")
WF.tick_build("prj")
WF.tick_build("prj")
b = STATE["wf"]["build"]
check(b["phase"] == "composing" and b["layout"]["provisional"] and b["layout"]["robots"]["robot_2"]["source"] == "default", "layout: robot_2 default slot keeps the build provisional")
pz = b["layout"]["robots"]["panda"]
check(pz["source"] == "measured" and abs(pz["pose"]["x_m"] - 1.0) < 1e-6 and abs(pz["pose"]["y_m"] - 0.3) < 1e-6, f"layout: panda measured pose in primary frame {pz['pose']}")
WF.tick_build("prj")
wc = [x for k, x in CREATED if k == "workcell"]
check(len(wc) == 1 and [r["name"] for r in wc[0]["robots"]] == ["robot_1", "robot_2", "panda"], "composing: one workcell request with all three arms")
req = {r["name"]: r for r in wc[0]["robots"]}
check(req["robot_1"]["pose"] is None and req["robot_2"]["pose"] is None and req["robot_2"]["pose_source"] == "default", "request: primary origin, robot_2 default (provisional)")
check(req["panda"]["pose_source"] == "measured" and req["panda"]["pose"]["x_m"] == 1.0, "request: panda measured pose")
ir = {r["library_id"]: r["import_run_id"] for r in wc[0]["robots"]}
check(req["robot_1"]["import_run_id"] == req["robot_2"]["import_run_id"] and len(set(ir.values())) == 2, "request: same-model arms share one pinned import run")
print(json.dumps(dict(ok=not FAIL, failed=FAIL)))
sys.exit(1 if FAIL else 0)
