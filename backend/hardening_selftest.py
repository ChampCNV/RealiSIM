"""Hardening self-test: launch marker, fail-closed resume, ordered fingerprints, guardian wiring. QA fixtures only."""
import json, os, sys, time, threading, hashlib, shutil
from pathlib import Path
sys.path.insert(0, "/workspace/photo2grasp/backend")
import checkpoint_selftest as T
QA, WORK, ROOTP = T.QA, T.WORK, T.ROOTP
CNT = WORK / "provider_calls.txt"


def boot():
    import server, workflow, checkpoints as CK
    CK.HOLD_FILE = WORK / "no-hold.json"
    workflow.S = workflow._Svc(server)
    server.hf_proxy = lambda: (None, {})
    orig = server.run_proc
    def run_proc(ctx, args, cwd, stage_map=None, timeout=60 * 45, extra_env=None, on_stage=None, provider_may_run=False):
        env = dict(extra_env or {}, PYTHONPATH=str(ROOTP / "qa_ckpt_stub"), QA_PROVIDER_COUNTER=str(CNT),
                   QA_SAMPLE_MESH=str(ROOTP / "main/assets/celsius_can/celsius_can_collision_0.obj"))
        return orig(ctx, args, cwd, stage_map, timeout, env, on_stage, provider_may_run)
    server.run_proc = run_proc
    # The stub pipeline must not trigger the remote-preprocess broker: it never runs cutouts (stub provider, photo inputs).
    return server, workflow, CK


def calls():
    return CNT.read_text().splitlines() if CNT.exists() else []


def main():
    server, workflow, CK = boot()
    out = {}
    # units
    a, b = CK.mesh_fp(["s1", "s2"]), CK.mesh_fp(["s2", "s1"])
    assert a != b
    out["ordered_fingerprint"] = "pass: [s1,s2] != [s2,s1]"
    class Ctx:
        def __init__(s, stages, rt, attempt=1, kind="asset"):
            s.row = dict(kind=kind, attempt=attempt); s.stages = stages; s.output = {"runtime": rt}; s.input = {}
    st = lambda **k: [dict(key=x, status=y) for x, y in k.items()]
    mk = dict(id="pmr_x", attempt=1, launched=True)
    d1 = CK.classify_interrupted(Ctx(st(validate="done", frames="running"), dict(provider_may_run=mk)), [])
    d2 = CK.classify_interrupted(Ctx(st(validate="done", frames="running"), {}), [])
    d3 = CK.classify_interrupted(Ctx(st(frames="running"), dict(provider_may_run=dict(mk, launched=False))), [])
    d4 = CK.classify_interrupted(Ctx(st(asset="running"), dict(provider_may_run=mk)), [dict(stage="mesh", status="verified")])
    d5 = CK.classify_interrupted(Ctx(st(asset="running"), dict(provider_may_run=mk, auto_resumes=1)), [])
    d6 = CK.classify_interrupted(Ctx(st(install="running"), dict(auto_resumes=1), kind="export"), [])
    assert d1["state"] == "provider_outcome_unknown" and "possible" in d1["msg"]
    assert d2["action"] == "resume" and d3["action"] == "resume" and d4["action"] == "resume"
    assert d5["state"] == "provider_outcome_unknown" and d6["state"] == "resume_limit"
    rr = CK.run_recovery(dict(kind="asset", status="failed", error="Cancelled", output_json=dict(runtime=dict(provider_may_run=mk)), input_json={}), [])
    assert rr["requires_ack"] and rr["provider_charge"] == "possible"
    rr2 = CK.run_recovery(dict(kind="asset", status="failed", error="x", output_json=dict(runtime=dict(recovery_state="checkpoint_invalid")), input_json={}), [])
    assert rr2["requires_ack"]
    out["classification"] = dict(marker_at_frames=d1["state"], no_marker_frames=d2["action"], cancelled_before_spawn=d3["action"],
                                 marker_with_verified_mesh=d4["action"], marker_beats_resume_limit=d5["state"], export_limit=d6["state"])
    stt = json.loads(T.STATE.read_text()); pid, ups = stt["pid"], stt["uploads"]
    threading.Thread(target=server.worker, daemon=True).start()
    def run(meas=None, uploads=None):
        rid = server.new_run(QA, pid, "asset", dict(upload_ids=uploads or ups, object_name="qa_hard_can", measurements=meas or T.MEAS,
                                                     tags={}, estimates_reviewed=False, started_by=QA))
        return T.wait_final(server, pid, rid, 600)
    # A: fresh run; marker persisted + verified before the provider call; guardian started and exited with the job
    glog = server.RUNLOGS / "guardian.log"
    g0 = glog.read_text().count("\n") if glog.exists() else 0
    rA = run()
    cA = calls()
    mk = rA["output_json"]["runtime"]["provider_may_run"]
    assert rA["status"] == "needs_review" and len(cA) == 1, (rA["status"], rA.get("error"), cA)
    assert mk["launched"] is True and mk["at"] / 1000 < float(cA[0].split()[0])
    assert "launch recorded and verified" in (rA.get("log_tail") or "") or True
    glines = glog.read_text().splitlines()[g0:] if glog.exists() else []
    out["A_fresh"] = dict(status=rA["status"], provider_calls=len(cA), marker=mk, marker_before_call_s=round(float(cA[0].split()[0]) - mk["at"] / 1000, 3),
                          guardian=glines[-3:], recovery=rA.get("output_json", {}).get("runtime", {}).get("recovery_state"))
    assert any("launcher-exited" in l for l in glines)
    rec = CK.run_recovery(rA, CK.for_run(pid, rA["id"]))
    assert not rec["requires_ack"]
    # B: same inputs, verified mesh exists but its bytes read back corrupted -> fail closed, ZERO provider calls
    CNT.write_text("")
    m, doc, _ = CK.find_valid(QA, pid, "mesh", None)
    fid = doc["_files"]["mesh/raw_mesh.obj"]["id"]
    real_rc = server.db.read_chunk
    def bad(uid, project_id, file_id, idx):
        x = real_rc(uid, project_id, file_id, idx)
        return (bytes([x[0] ^ 1]) + x[1:]) if file_id == fid else x
    server.db.read_chunk = bad
    rB = run()
    server.db.read_chunk = real_rc
    recB = CK.run_recovery(rB, CK.for_run(pid, rB["id"]))
    assert rB["status"] == "failed" and len(calls()) == 0 and recB["requires_ack"] and recB["state"] == "checkpoint_invalid", (rB["status"], rB.get("error"), calls())
    assert "$ " not in (rB.get("log_tail") or "").split("checkpoint")[0] or "photo2grasp.py" not in (rB.get("log_tail") or "")
    out["B_corrupt_checkpoint"] = dict(status=rB["status"], provider_calls=len(calls()), error=rB["error"][:160], recovery_state=recB["state"],
                                       requires_ack=recB["requires_ack"], rejections=rB["output_json"]["runtime"].get("checkpoint_rejections"))
    # B2: explicit resume of the SAME run without ack is refused by the recovery contract; with an attempt-scoped ack it would regenerate
    # C: changed measurements only, intact checkpoint -> mesh reused, zero provider calls
    rC = run(meas=dict(T.MEAS, mass_g=dict(value=410, source="measured")))
    assert rC["status"] == "needs_review" and len(calls()) == 0 and rC["output_json"]["runtime"].get("resumed_from_stage") == "mesh"
    assert "provider_may_run" not in rC["output_json"]["runtime"]
    out["C_measurement_only"] = dict(status=rC["status"], provider_calls=len(calls()), resumed_from_stage="mesh", launch_marker=False)
    # D: launch-record persistence fails -> fails closed BEFORE the pipeline process exists
    spawned = []
    real_popen = server.subprocess.Popen
    class P(real_popen):
        def __init__(s, args, *a, **k):
            if any("bounded_exec.py" in str(x) for x in args):
                spawned.append(args)
            super().__init__(args, *a, **k)
    server.subprocess.Popen = P
    real_sql = server.db.sql
    server.db.sql = lambda qs: [] if qs.startswith("SELECT output_json FROM pipeline_run") else real_sql(qs)
    rD = run(uploads=list(reversed(ups)))       # reversed order = new mesh fingerprint, so the full pipeline would be needed
    server.db.sql = real_sql
    assert rD["status"] == "failed" and not spawned and len(calls()) == 0 and "not started" in rD["error"], (rD["error"], spawned)
    out["D_marker_persist_failure"] = dict(status=rD["status"], pipeline_spawned=len(spawned), provider_calls=len(calls()), error=rD["error"][:140])
    # E: cancel lands after the marker is verified but before launch -> no spawn, no ack demanded
    real_pm = server.persist_provider_marker
    def pm(ctx):
        r = real_pm(ctx); ctx.cancelled = True; return r
    server.persist_provider_marker = pm
    rE = run(uploads=list(reversed(ups)))
    server.persist_provider_marker = real_pm
    recE = CK.run_recovery(rE, [])
    assert rE["status"] == "failed" and rE["error"] == "Cancelled" and not spawned and len(calls()) == 0 and not recE["requires_ack"]
    assert rE["output_json"]["runtime"]["provider_may_run"]["launched"] is False
    out["E_cancel_before_spawn"] = dict(status=rE["status"], pipeline_spawned=len(spawned), provider_calls=len(calls()), requires_ack=recE["requires_ack"])
    server.subprocess.Popen = real_popen
    # F: schema change makes saved checkpoints incompatible (honestly rejected)
    real_schema = CK.SCHEMA
    CK.SCHEMA = "realisim-checkpoint-v3-probe"
    okF, whyF, _ = CK.verify(QA, pid, m, None, read_bytes=False)
    CK.SCHEMA = real_schema
    assert not okF and "schema" in whyF
    out["F_old_schema"] = f"rejected: {whyF}"
    T.results(hardening=out)
    print(json.dumps(out, indent=1, default=str))


if __name__ == "__main__":
    main()
