"""Checkpoint/recovery self-test (QA fixtures only; owner uid qa_tester). Subcommands: unit | setup | phase1 | phase2 | api | cleanup."""
import json, os, sys, time, threading, tempfile, shutil, hashlib
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent))
QA = "qa_tester"
WORK = Path("/tmp/ckqa")
WORK.mkdir(exist_ok=True)
STATE = WORK / "state.json"
ROOTP = Path("/workspace/photo2grasp")
RESULTS = WORK / "results.json"


def results(**kw):
    r = json.loads(RESULTS.read_text()) if RESULTS.exists() else {}
    r.update(kw)
    RESULTS.write_text(json.dumps(r, indent=1, default=str))


def boot(hold_off=True):
    import server, workflow, checkpoints as CK
    if hold_off:
        CK.HOLD_FILE = WORK / "no-hold.json"      # test process only; the real hold file is untouched
    workflow.S = workflow._Svc(server)
    server.hf_proxy = lambda: (None, {})
    orig = server.run_proc
    def run_proc(ctx, args, cwd, stage_map=None, timeout=60 * 45, extra_env=None, on_stage=None, provider_may_run=False):
        env = dict(extra_env or {}, PYTHONPATH=str(ROOTP / "qa_ckpt_stub"), QA_PROVIDER_COUNTER=str(WORK / "provider_calls.txt"),
                   QA_SAMPLE_MESH=str(ROOTP / "main/assets/celsius_can/celsius_can_collision_0.obj"))
        if os.environ.get("QA_HOLD_FILE"):
            env["QA_HOLD_FILE"] = os.environ["QA_HOLD_FILE"]
        return orig(ctx, args, cwd, stage_map, timeout, env, on_stage, provider_may_run)
    server.run_proc = run_proc
    return server, workflow, CK


def scope_db(server, pid):
    real = server.db.sql
    def sql(qs):
        if qs.strip() == "SELECT * FROM pipeline_run":
            qs = f"SELECT * FROM pipeline_run WHERE project_id = '{pid}'"
        return real(qs)
    server.db.sql = sql


def unit():
    import checkpoints as CK
    out = {}
    # timing transitions
    s = dict(key="mesh", status="pending")
    CK.apply_timing(s, "running", 1000); st0 = s["started_at"]
    CK.apply_timing(s, "running", 1500)                  # repeated running update must not reset
    assert s["started_at"] == st0 == 1000 and s["active_since_at"] == 1000
    CK.apply_timing(s, "waiting", 3000); assert s["elapsed_ms"] == 2000 and s["active_since_at"] is None and s.get("finished_at") is None
    CK.apply_timing(s, "running", 4000); assert s["started_at"] == 1000 and s["active_since_at"] == 4000
    CK.apply_timing(s, "done", 4500); assert s["elapsed_ms"] == 2500 and s["finished_at"] == 4500
    s2 = dict(key="asset", status="running", started_at=10, active_since_at=10, elapsed_ms=0)
    CK.close_interrupted(s2, 70); assert s2["elapsed_ms"] == 60 and s2["active_since_at"] is None and s2["interrupted_at"] == 70
    s3 = dict(key="legacy", status="running", started=5)
    CK.apply_timing(s3, "done", 9); assert "elapsed_ms" not in s3 and "started_at" not in s3   # no fabricated old timing
    out["timing"] = "pass"
    # hold fail-closed
    tmp = Path(tempfile.mkdtemp())
    CK.HOLD_FILE = tmp / "hold.json"
    assert CK.hold_state() is None
    CK.HOLD_FILE.write_text('{"enabled": false}'); assert CK.hold_state() is None
    CK.HOLD_FILE.write_text('{"enabled": true, "reason": "x"}'); assert CK.hold_state()["reason"] == "x"
    assert CK.admission()["code"] == "maintenance_hold"
    CK.HOLD_FILE.write_text('{not json'); assert CK.hold_state()["enabled"] is True     # unreadable -> held
    CK.HOLD_FILE.unlink()
    m = CK.memory_state(); out["memory_state"] = {k: (round(v / 2**30, 2) if isinstance(v, int) else v) for k, v in m.items()}
    old = CK.MIN_FREE_BYTES; CK.MIN_FREE_BYTES = 10 ** 15
    assert CK.admission()["code"] == "memory_pressure"; CK.MIN_FREE_BYTES = old
    out["hold_admission"] = "pass"
    # fingerprints: measurement change keeps mesh, changes asset; input/algorithm change invalidates mesh
    a = CK.mesh_fp(["s1", "s2"]); b = CK.mesh_fp(["s2", "s1"]); c = CK.mesh_fp(["s1", "s3"])
    assert a != b and a != c      # v2: input order is part of the mesh fingerprint
    e1 = dict(size_m=[.1, .1, .1], mass_kg=.2, friction=[.6, .005, .0001], source="x")
    e2 = dict(e1, mass_kg=.3)
    assert CK.asset_fp("m", e1, "o") != CK.asset_fp("m", e2, "o") and CK.asset_fp("m", e1, "o") == CK.asset_fp("m", dict(e1, source="y"), "o")
    real = CK.algo_hashes
    CK.algo_hashes = lambda stage, wt=None: {"pipeline/trellis_mesh.py": "changed"}
    assert CK.mesh_fp(["s1", "s2"]) != a; CK.algo_hashes = real
    out["fingerprints"] = "pass"
    # classification
    class Ctx:
        def __init__(self, kind, stages, output=None, inp=None):
            self.row = dict(kind=kind); self.stages = stages; self.output = output or {}; self.input = inp or {}
    st = lambda **k: [dict(key=x, status=y) for x, y in k.items()]
    assert CK.classify_interrupted(Ctx("asset", st(frames="done", mesh="running")), [])["state"] == "provider_outcome_unknown"
    assert CK.classify_interrupted(Ctx("asset", st(frames="done", mesh="done", asset="running")), [dict(stage="mesh")])["action"] == "resume"
    assert CK.classify_interrupted(Ctx("asset", st(frames="running")), [])["action"] == "resume"
    assert CK.classify_interrupted(Ctx("asset", st(frames="done", mesh="done", asset="running"), {"runtime": {"auto_resumes": 1}}), [dict(stage="mesh")])["state"] == "resume_limit"
    assert CK.classify_interrupted(Ctx("export", st(install="running")), [])["action"] == "resume"
    assert CK.classify_interrupted(Ctx("detect_robot", st(analyze="running")), [])["state"] == "interrupted"
    legacy = dict(kind="asset", status="failed", error="The processing worker restarted during mesh. Your inputs ...", output_json={}, input_json={})
    rr = CK.run_recovery(legacy, []); assert rr["requires_ack"] and rr["state"] == "provider_outcome_unknown"
    assert not CK.run_recovery(legacy, [dict(stage="mesh")])["requires_ack"]
    out["classification"] = "pass"
    # DB startup failure + duplicate enqueue
    import server
    server.CK.HOLD_FILE = Path(tempfile.mkdtemp()) / "none.json"
    calls = {"n": 0}
    class FakeDB:
        def sql(self, q):
            calls["n"] += 1
            if calls["n"] <= 2:
                raise server.StdbError("database unreachable: connection refused")
            return []
    real_db, real_sleep = server.db, server.time.sleep
    server.db = FakeDB(); server.time.sleep = lambda s: None
    try:
        server.recover()
        assert server.STATE["recovery_complete"] and calls["n"] == 3
    finally:
        server.db, server.time.sleep = real_db, real_sleep
    out["db_startup_retry"] = f"pass (completed after {calls['n'] - 1} failed attempts)"
    pushed = []
    class Dummy:
        def __init__(s): s.row = dict(id="run_qa_dup", status="queued"); s.cancelled = False
        def push(s, **k): pushed.append(k)
    d = Dummy()
    assert server.enqueue(d) is True and server.enqueue(d) is False and server.QUEUE.count("run_qa_dup") == 1
    server.QUEUE.remove("run_qa_dup"); server.LIVE.pop("run_qa_dup", None)
    out["duplicate_enqueue"] = "pass"
    results(unit=out)
    print(json.dumps(out, indent=1))


def setup():
    server, workflow, CK = boot()
    from stdb import new_id
    pid = new_id("prj")
    server.db.call("create_project", uid=QA, id=pid, name="QA checkpoint fixture (delete me)", robot_profile="so100-menagerie",
                   settings_json=json.dumps(dict(units="metric", project_mode="object", qa=True)))
    ups = []
    for i, f in enumerate(sorted((ROOTP / "main/outputs/celsius_grape/frames").glob("view_*.jpg"))):
        upid = new_id("up")
        d = server.UPLOADS / upid; d.mkdir(parents=True, exist_ok=True)
        data = f.read_bytes(); (d / "source.jpg").write_bytes(data)
        server.put_upload(QA, dict(id=upid, project_id=pid, capture_mode="object", filename=f"qa_view_{i}.jpg", media_type="image/jpeg",
                                   size=len(data), sha256=hashlib.sha256(data).hexdigest(), status="ready", chunk_size=len(data), chunks_total=1,
                                   chunks_received=1, meta_json=dict(kind="image"), error=""))
        ups.append(upid)
    STATE.write_text(json.dumps(dict(pid=pid, uploads=ups)))
    print(pid, ups)


MEAS = dict(length_cm=dict(value=6.6, source="measured"), width_cm=dict(value=6.6, source="measured"), height_cm=dict(value=12, source="measured"),
            mass_g=dict(value=350, source="measured"), friction=dict(value=0.6, source="supplied"))


def phase1():
    server, workflow, CK = boot()
    stt = json.loads(STATE.read_text())
    rid = server.new_run(QA, stt["pid"], "asset", dict(upload_ids=stt["uploads"], object_name="qa_ckpt_can", measurements=MEAS, tags={},
                                                       estimates_reviewed=False, started_by=QA))
    stt["run1"] = rid; stt["worker1"] = CK.WORKER_ID; STATE.write_text(json.dumps(stt))
    print("run", rid, flush=True)
    threading.Thread(target=server.worker, daemon=True).start()
    while True:
        time.sleep(1)


def wait_final(server, pid, rid, timeout=900):
    t0 = time.time()
    while time.time() - t0 < timeout:
        r = server.one("pipeline_run", pid, rid)
        if r["status"] in ("needs_review", "complete", "failed"):
            return r
        time.sleep(2)
    raise TimeoutError(rid)


def phase2():
    server, workflow, CK = boot()
    stt = json.loads(STATE.read_text())
    pid, rid = stt["pid"], stt["run1"]
    scope_db(server, pid)
    CK.LEASE_MS = 8000
    before = server.one("pipeline_run", pid, rid)
    threading.Thread(target=server.worker, daemon=True).start()
    t0 = time.time(); server.recover(); rec_s = time.time() - t0
    r = wait_final(server, pid, rid)
    calls = (WORK / "provider_calls.txt").read_text().splitlines() if (WORK / "provider_calls.txt").exists() else []
    rt = r["output_json"].get("runtime") or {}
    stg = {s["key"]: s for s in r["stages_json"]}
    b4 = {s["key"]: s for s in before["stages_json"]}
    chk = dict(status=r["status"], attempt=r["attempt"], provider_calls=len(calls), runtime=rt,
               recovery_seconds=round(rec_s, 1), asset_version_id=r["output_json"].get("asset_version_id"),
               before_status=before["status"], before_stages={k: (v["status"], v.get("started_at"), v.get("elapsed_ms")) for k, v in b4.items()},
               after_stages={k: {x: v.get(x) for x in ("status", "started_at", "active_since_at", "elapsed_ms", "finished_at", "restored", "interrupted_at")} for k, v in stg.items()})
    assert before["status"] == "running", "phase1 must have been killed while running"
    assert r["status"] == "needs_review", r.get("error")
    assert len(calls) == 1, calls
    assert rt.get("resumed_from_stage") == "mesh" and rt.get("recovery_state") == "resumed" and r["attempt"] == 2
    assert stg["asset"]["started_at"] == b4["asset"]["started_at"], "stage start must be preserved across resume"
    assert stg["asset"]["elapsed_ms"] >= (b4["asset"].get("elapsed_ms") or 0)
    for k, v in stg.items():
        if v.get("started_at") and v.get("finished_at"):
            assert v["finished_at"] >= v["started_at"], k
    results(resume=chk)
    # changed measurement: reuses the verified mesh checkpoint (no provider call), rebuilds asset
    rid2 = server.new_run(QA, pid, "asset", dict(upload_ids=stt["uploads"], object_name="qa_ckpt_can", measurements=dict(MEAS, mass_g=dict(value=420, source="measured")),
                                                 tags={}, estimates_reviewed=False, started_by=QA))
    r2 = wait_final(server, pid, rid2)
    calls2 = (WORK / "provider_calls.txt").read_text().splitlines()
    assert r2["status"] == "needs_review", r2.get("error")
    assert len(calls2) == 1, "measurement change must not repeat the provider call"
    assert (r2["output_json"].get("runtime") or {}).get("resumed_from_stage") == "mesh"
    assert r2["output_json"]["asset_version_id"] != r["output_json"]["asset_version_id"]
    results(measurement_change=dict(run=rid2, status=r2["status"], provider_calls=len(calls2), new_asset_version=r2["output_json"]["asset_version_id"]))
    # corruption / mismatch / stale revision checks against the real stored checkpoint
    m, doc, _ = CK.find_valid(QA, pid, "mesh", None)
    assert m
    fid = next(f["id"] for p, f in doc["_files"].items() if p == "mesh/raw_mesh.obj")
    real_rc = server.db.read_chunk
    def bad(uid, project_id, file_id, idx):
        b = real_rc(uid, project_id, file_id, idx)
        return (bytes([b[0] ^ 1]) + b[1:]) if file_id == fid and idx == 0 else b
    server.db.read_chunk = bad
    ok, why, _ = CK.verify(QA, pid, m, doc["fingerprint"])
    server.db.read_chunk = real_rc
    assert not ok and ("match" in why or "hash" in why), why
    m2, _, rej = CK.find_valid(QA, pid, "mesh", CK.mesh_fp(["0" * 64]))
    assert m2 is None
    real_alg = CK.algo_hashes
    CK.algo_hashes = lambda stage, wt=None: dict(real_alg(stage, wt), **{"pipeline/trellis_mesh.py": "different-revision"})
    stale = CK.mesh_fp(doc["meta"]["inputs"])
    CK.algo_hashes = real_alg
    assert CK.find_valid(QA, pid, "mesh", stale)[0] is None
    ok2, why2, _ = CK.verify(QA, pid, m, "f" * 64)
    assert not ok2 and "fingerprint" in why2
    results(integrity=dict(corrupted_bytes=f"rejected: {why}", input_mismatch="rejected (no compatible checkpoint)", stale_revision="rejected",
                           wrong_fingerprint=f"rejected: {why2}", valid_restore="verified checkpoint re-read and re-hashed",
                           checkpoint_id=m["id"], checkpoint_files=sorted(doc["_files"])))
    stt.update(run2=rid2); STATE.write_text(json.dumps(stt))
    print("phase2 ok")


def api():
    """HTTP contract: ack gate, idempotent retry, hold admission, GET payload. Uses an in-process aiohttp test client."""
    import asyncio
    from aiohttp import web
    from aiohttp.test_utils import TestClient, TestServer
    server, workflow, CK = boot()
    stt = json.loads(STATE.read_text()); pid = stt["pid"]
    from stdb import new_id
    rid = new_id("run")   # QA failed run that mimics a legacy interrupted-mesh failure (no mesh checkpoint for these inputs)
    server.RunCtx(dict(id=rid, project_id=pid, kind="asset", status="failed", stage="mesh", input_json=dict(upload_ids=stt["uploads"][:1], object_name="qa_ack_probe",
                  measurements=MEAS, tags={}, started_by=QA), output_json={}, commit="", attempt=1, started_at=1, finished_at=2,
                  error="The processing worker restarted during mesh. Your inputs and saved artifacts are preserved."), QA).push(force=True)
    out = {}
    async def go():
        app = web.Application()
        app.router.add_post("/api/projects/{pid}/runs/{rid}/{action}", server.run_action)
        app.router.add_get("/api/projects/{pid}", server.get_project)
        c = TestClient(TestServer(app)); await c.start_server()
        H = {"X-Sender-Uid": QA}
        r = await c.post(f"/api/projects/{pid}/runs/{rid}/retry", json={}, headers=H); j = await r.json()
        out["retry_without_ack"] = [r.status, j.get("code")]
        assert r.status == 409 and j["code"] == "ack_required"
        r = await c.post(f"/api/projects/{pid}/runs/{rid}/resume", json={}, headers=H); j = await r.json()
        out["resume_without_ack"] = [r.status, j.get("code")]; assert r.status == 409
        CK.HOLD_FILE = WORK / "hold-on.json"; CK.HOLD_FILE.write_text('{"enabled": true, "reason": "qa hold"}')
        r = await c.post(f"/api/projects/{pid}/runs/{rid}/retry", json={"acknowledge_provider_repeat": True}, headers=H); j = await r.json()
        out["retry_during_hold"] = [r.status, j.get("code")]; assert r.status == 503 and j["code"] == "maintenance_hold"
        r = await c.get(f"/api/projects/{pid}", headers=H); g = await r.json()
        out["get_during_hold"] = [r.status, g.get("maintenance")]; assert r.status == 200 and g["maintenance"]["hold"]
        CK.HOLD_FILE = WORK / "no-hold.json"
        r = await c.post(f"/api/projects/{pid}/runs/{rid}/retry", json={"acknowledge_provider_repeat": True}, headers=H); j1 = await r.json()
        r = await c.post(f"/api/projects/{pid}/runs/{rid}/retry", json={"acknowledge_provider_repeat": True}, headers=H); j2 = await r.json()
        out["idempotent_retry"] = [j1.get("id"), j2.get("id"), j2.get("deduplicated")]
        assert j1["id"] == j2["id"] and j2.get("deduplicated")
        r = await c.post(f"/api/projects/{pid}/runs/{j1['id']}/cancel", json={}, headers=H)   # never executes (no worker here)
        out["cancel_new_retry"] = r.status
        r = await c.get(f"/api/projects/{pid}", headers=H); g = await r.json()
        runs = {x["id"]: x for x in g["pipeline_run"]}
        out["get_payload"] = dict(checkpoints=len(g["checkpoints"]), stages=sorted({c["stage"] for c in g["checkpoints"]}),
                                  run1_recovery=runs[stt["run1"]]["recovery"], probe_recovery=runs[rid]["recovery"])
        assert runs[rid]["recovery"]["requires_ack"] and "mesh" in runs[stt["run1"]]["recovery"]["saved_stages"]
        await c.close()
    asyncio.run(go())
    # workflow ack gate for a legacy failed build
    cur = dict(id="bld_qa", phase="failed", objects={"o1": dict(name="qa_ack_probe", run_id=rid)})
    gate = workflow.build_ack_needed(pid, cur)
    assert gate and gate["objects"][0]["run_id"] == rid
    out["workflow_ack_gate"] = gate
    results(api=out)
    print(json.dumps(out, indent=1, default=str))


def cleanup():
    import server
    stt = json.loads(STATE.read_text()); pid = stt["pid"]
    db = server.db
    for m in db.sql(f"SELECT id FROM artifact_manifest WHERE project_id = '{pid}'"):
        db.delete_manifest(QA, pid, m["id"])
    for f in db.sql(f"SELECT id FROM artifact_file WHERE project_id = '{pid}'"):
        db.call("delete_file", uid=QA, project_id=pid, file_id=f["id"])
    for u in db.sql(f"SELECT id FROM upload WHERE project_id = '{pid}'"):
        shutil.rmtree(server.UPLOADS / u["id"], ignore_errors=True)
    db.call("delete_project", uid=QA, id=pid)
    wt = server.PROJECTS / pid / "repo"
    if wt.exists():
        server.sh(["git", "worktree", "remove", "--force", str(wt)])
    shutil.rmtree(server.PROJECTS / pid, ignore_errors=True)
    left = db.sql(f"SELECT id FROM pipeline_run WHERE project_id = '{pid}'")
    results(cleanup=dict(project=pid, remaining_runs=len(left)))
    print("cleaned", pid, len(left))


if __name__ == "__main__":
    dict(unit=unit, setup=setup, phase1=phase1, phase2=phase2, api=api, cleanup=cleanup)[sys.argv[1]]()
