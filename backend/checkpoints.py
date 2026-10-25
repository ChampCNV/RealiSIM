"""RealiSIM resilience: maintenance hold + memory admission, stage timing, durable stage checkpoints, crash recovery.

Checkpoints are immutable SpacetimeDB artifact manifests (kind="checkpoint"). Every byte is written through
Stdb.store_dir, which reads each chunk back and compares SHA-256 before the file is verified and the manifest sealed.
A checkpoint carries checkpoint.json (schema, stage, fingerprint, run/object identity, input hashes, algorithm hashes,
commit, produced-file hashes and references to other verified manifests) plus, for the frames/mesh boundaries, the
produced bytes themselves. Restore re-reads and re-hashes every byte; a checkpoint whose bytes, fingerprint or
references do not verify is never used and never called saved. Local files are expendable caches.
"""
import hashlib, json, os, time, uuid
from pathlib import Path

ROOT = Path("/workspace/photo2grasp")
HOLD_FILE = ROOT / "maintenance-hold.json"
SCHEMA = "realisim-checkpoint-v2"   # v2: ordered input fingerprints; v1 checkpoints are incompatible
STAGE_ORDER = ["frames", "mesh", "asset", "scene", "export"]
RESUME_POINTS = ("mesh", "asset", "scene", "export")   # frames: saved result only (see contract)
TERMINAL = ("done", "failed", "skipped", "cancelled")
WORKER_ID = "wrk_" + uuid.uuid4().hex[:12]
HEARTBEAT_S = 30
PROGRESS_S = 3
LEASE_MS = 90_000
MAX_AUTO_RESUMES = 1
MIN_FREE_BYTES = 3 * 1024 ** 3          # admission: free cgroup headroom required before a heavy job starts
S = None                                 # server module (bound by bind())


def bind(server):
    global S
    S = server


def now_ms():
    return int(time.time() * 1000)


def sha_bytes(b):
    return hashlib.sha256(b).hexdigest()


def sha_json(o):
    return sha_bytes(json.dumps(o, sort_keys=True, separators=(",", ":")).encode())


# ── maintenance hold + memory admission ────────────────────────────────────────
def hold_state():
    """Fail closed: an unreadable hold file counts as an active hold."""
    if not HOLD_FILE.exists():
        return None
    try:
        h = json.loads(HOLD_FILE.read_text() or "{}")
    except Exception:
        return dict(enabled=True, reason="maintenance hold file unreadable (fail-closed)")
    if not isinstance(h, dict):
        return dict(enabled=True, reason="maintenance hold file malformed (fail-closed)")
    return h if h.get("enabled") else None


def _cg(name):
    try:
        v = Path("/sys/fs/cgroup/" + name).read_text().strip()
        return None if v == "max" else int(v)
    except Exception:
        return None


def memory_state():
    mx, cur = _cg("memory.max"), _cg("memory.current")
    inactive = 0
    try:
        for line in Path("/sys/fs/cgroup/memory.stat").read_text().splitlines():
            k, v = line.split()
            if k == "inactive_file":
                inactive = int(v)
    except Exception:
        pass
    if not mx or cur is None:
        return dict(known=False)
    used = max(0, cur - inactive)
    return dict(known=True, max=mx, used=used, free=mx - used)


def admission(kind=None):
    """None when a heavy job may start now; else a dict(reason, code). Checked BEFORE any job memory is allocated."""
    h = hold_state()
    if h:
        return dict(code="maintenance_hold", reason=h.get("reason") or "maintenance hold", since=h.get("at"))
    m = memory_state()
    need = MIN_FREE_BYTES
    try:
        need = int((hold_state_raw() or {}).get("min_free_bytes") or need)
    except Exception:
        pass
    if m.get("known") and m["free"] < need:
        return dict(code="memory_pressure", reason=f"waiting for memory headroom ({m['free'] / 2**30:.1f} GiB free, "
                    f"{need / 2**30:.1f} GiB required before a reconstruction job starts)")
    return None


def hold_state_raw():
    try:
        return json.loads(HOLD_FILE.read_text())
    except Exception:
        return None


# ── stage timing (shared by pipeline_run.stages_json and workflow build.steps) ─
def apply_timing(s, status, t=None):
    """Contract fields: started_at (first start, set once), active_since_at (current interval or None), elapsed_ms
    (accumulated before the current interval), finished_at (terminal). Repeated 'running' updates never reset."""
    t = t or now_ms()
    prev = s.get("status")
    if status == "running":
        if prev != "running" or not s.get("active_since_at"):
            if not s.get("started_at"):
                s["started_at"] = t
            s["active_since_at"] = t
            s["elapsed_ms"] = int(s.get("elapsed_ms") or 0)
            s["finished_at"] = None
    else:
        if s.get("active_since_at"):
            s["elapsed_ms"] = int(s.get("elapsed_ms") or 0) + max(0, int(t - s["active_since_at"]))
            s["active_since_at"] = None
        if status in TERMINAL:
            s["finished_at"] = t
    s["status"] = status
    return s


def close_interrupted(s, at):
    """Close an interval left open by a dead worker at the last durable heartbeat (never invent a later end)."""
    if s.get("active_since_at"):
        if at and at >= s["active_since_at"]:
            s["elapsed_ms"] = int(s.get("elapsed_ms") or 0) + int(at - s["active_since_at"])
        else:
            s["elapsed_incomplete"] = True
        s["active_since_at"] = None
        s["interrupted_at"] = at or None


# ── fingerprints ───────────────────────────────────────────────────────────────
ALGO_FILES = {
    "frames": ["pipeline/video_frames.py", "pipeline/cutout.py"],
    "mesh": ["pipeline/trellis_mesh.py", "pipeline/cutout.py", "pipeline/video_frames.py"],
    "asset": ["make_asset.py", "pipeline/mesh_to_mjcf.py", "pipeline/object_info.py", "pipeline/tag_ids.py"],
}
ADAPTER_FILES = {"scene": ["adapter/sim_worker.py"], "export": ["adapter/gym_export.py"]}


def algo_hashes(stage, wt=None):
    out = {}
    base = Path(wt) if wt else ROOT / "main"
    for rel in ALGO_FILES.get(stage, []):
        p = base / rel
        out[rel] = sha_bytes(p.read_bytes()) if p.exists() else "missing"
    for rel in ADAPTER_FILES.get(stage, []):
        p = ROOT / rel
        out[rel] = sha_bytes(p.read_bytes()) if p.exists() else "missing"
    return out


def fingerprint(stage, **parts):
    return sha_json(dict(schema=SCHEMA, stage=stage, **parts))


def mesh_fp(input_shas, wt=None, views=4):
    """Inputs keep their given order: the first image is TRELLIS's primary view, so order can change the mesh."""
    return fingerprint("mesh", inputs=list(input_shas), input_order="as_given", algo=algo_hashes("mesh", wt), views=views)


def frames_fp(input_shas, wt=None, views=4):
    return fingerprint("frames", inputs=list(input_shas), input_order="as_given", algo=algo_hashes("frames", wt), views=views)


def asset_fp(mesh_sha, entry, name, wt=None):
    e = {k: v for k, v in (entry or {}).items() if k != "source"}
    return fingerprint("asset", mesh=mesh_sha, entry=e, name=name, algo=algo_hashes("asset", wt))


# ── save / find / verify / restore ─────────────────────────────────────────────
def _manifests(pid):
    rows = S.db.sql(f"SELECT * FROM artifact_manifest WHERE project_id = {S.q(pid)}")
    out = []
    for m in rows:
        if m.get("kind") != "checkpoint":
            continue
        for k in ("dependency_json", "files_json"):
            if isinstance(m.get(k), str):
                try:
                    m[k] = json.loads(m[k] or "{}")
                except Exception:
                    m[k] = {}
        out.append(m)
    return sorted(out, key=lambda m: -(m.get("created_at") or 0))


def summary(m, verified=None):
    d = m.get("dependency_json") or {}
    return dict(checkpoint_id=m["id"], stage=d.get("stage"), fingerprint=d.get("fingerprint"), run_id=d.get("run_id"),
                attempt=d.get("attempt"), object_name=d.get("object_name"), status=m.get("status"),
                saved_at=d.get("saved_at") or m.get("created_at"), files=m.get("file_count"), bytes=m.get("total_bytes"),
                resumable=d.get("stage") in RESUME_POINTS, refs=d.get("refs") or {}, verified=verified)


def save(ctx, stage, fp, files=(), refs=None, meta=None):
    """Store a checkpoint (idempotent per stage+fingerprint). Returns summary; raises when it could not be verified."""
    pid = ctx.row["project_id"]
    ref_id = f"ckpt_{stage}_{fp[:32]}"
    for m in _manifests(pid):
        if m.get("ref_id") == ref_id and m.get("status") == "verified":
            ok, why, _ = verify(ctx.uid, pid, m, fp, read_bytes=False)
            if ok:
                return _note(ctx, summary(m, verified=True), reused=True)
    files = list(files)
    produced = [dict(path=p, sha256=sha_bytes(b), size=len(b)) for p, b, _ in files]
    doc = dict(schema=SCHEMA, stage=stage, fingerprint=fp, project_id=pid, run_id=ctx.row["id"], kind=ctx.row["kind"],
               attempt=int(ctx.row.get("attempt") or 1), object_name=(meta or {}).get("object_name"),
               commit=ctx.row.get("commit") or "", saved_at=now_ms(), worker_instance_id=WORKER_ID,
               produced=produced, refs=refs or {}, meta=meta or {})
    entries = files + [("checkpoint.json", json.dumps(doc, indent=1, sort_keys=True).encode(), "application/json")]
    deps = dict(stage=stage, fingerprint=fp, run_id=ctx.row["id"], attempt=doc["attempt"], object_name=doc["object_name"],
                saved_at=doc["saved_at"], refs=refs or {}, schema=SCHEMA)
    mid, stored = S.db.store_dir(ctx.uid, pid, "checkpoint", ref_id, str(doc["attempt"]), entries, deps=deps)
    m = dict(id=mid, status="verified", file_count=len(stored), total_bytes=sum(f["size"] for f in stored),
             dependency_json=deps, created_at=now_ms())
    return _note(ctx, summary(m, verified=True), reused=False)


def _note(ctx, s, reused):
    s["reused"] = reused
    with ctx.lock:
        rt = ctx.output.setdefault("runtime", {})
        rt["checkpoint_id"] = s["checkpoint_id"]
        rt["checkpoint_stage"] = s["stage"]
        lst = [c for c in ctx.output.get("checkpoints") or [] if c.get("checkpoint_id") != s["checkpoint_id"]]
        lst.append({k: s[k] for k in ("checkpoint_id", "stage", "fingerprint", "saved_at", "files", "bytes", "resumable", "reused")})
        ctx.output["checkpoints"] = lst[-12:]
    ctx.write(f"   ↳ checkpoint {s['stage']}: {'reused verified' if reused else 'saved and read-back verified'} ({s['checkpoint_id']})\n")
    ctx.push(force=True)
    return s


def verify(uid, pid, m, expect_fp=None, read_bytes=True):
    """(ok, reason, payload). Re-reads checkpoint.json (hash-checked), checks schema/fingerprint, every produced file's
    stored hash, and that every referenced manifest is still verified with matching file hashes."""
    try:
        if m.get("status") != "verified":
            return False, "manifest not verified", None
        files = {f["path"]: f for f in S.db.manifest_files(m["id"])}
        cj = files.get("checkpoint.json")
        if not cj or cj.get("status") != "verified":
            return False, "checkpoint.json missing", None
        _, raw = S.db.read_bytes(uid, pid, cj["id"])
        doc = json.loads(raw)
        if doc.get("schema") != SCHEMA:
            return False, "schema mismatch", None
        if expect_fp and doc.get("fingerprint") != expect_fp:
            return False, "input/algorithm fingerprint mismatch", None
        for p in doc.get("produced") or []:
            f = files.get(p["path"])
            if not f or f.get("status") != "verified" or f.get("sha256") != p["sha256"]:
                return False, f"produced file missing or altered: {p['path']}", None
            if read_bytes:
                meta, data = S.db.read_bytes(uid, pid, f["id"])
                if sha_bytes(data) != p["sha256"]:
                    return False, f"bytes do not match: {p['path']}", None
        for name, r in (doc.get("refs") or {}).items():
            if not isinstance(r, dict) or not r.get("manifest_id"):
                continue
            mm = S.db.sql(f"SELECT id, status, project_id FROM artifact_manifest WHERE id = {S.q(r['manifest_id'])}")
            if not mm or mm[0]["status"] != "verified" or mm[0]["project_id"] != pid:
                return False, f"referenced {name} manifest is not verified", None
            want = r.get("files") or {}
            if want:
                have = {f["path"]: f.get("sha256") for f in S.db.manifest_files(r["manifest_id"])}
                for path, sha in want.items():
                    if have.get(path) != sha:
                        return False, f"referenced {name} file altered: {path}", None
        doc["_files"] = files
        return True, "verified", doc
    except Exception as e:
        return False, f"verification error: {type(e).__name__}: {str(e)[:160]}", None


def find_valid(uid, pid, stage, fp, run_id=None, read_bytes=True):
    """Newest verified compatible checkpoint (or None) plus the list of rejected candidates with reasons."""
    rejected = []
    for m in _manifests(pid):
        d = m.get("dependency_json") or {}
        if d.get("stage") != stage:
            continue
        if fp and d.get("fingerprint") != fp:
            continue
        if run_id and d.get("run_id") != run_id:
            continue
        ok, why, doc = verify(uid, pid, m, fp or d.get("fingerprint"), read_bytes=read_bytes)
        if ok:
            return m, doc, rejected
        rejected.append(dict(checkpoint_id=m["id"], reason=why))
    return None, None, rejected


def restore(uid, pid, doc, dest, prefix):
    """Write verified checkpoint bytes under `prefix` into dest; returns count."""
    n = 0
    files = doc["_files"]
    want = {p["path"]: p["sha256"] for p in doc.get("produced") or []}
    for path, sha in want.items():
        if not path.startswith(prefix):
            continue
        meta, data = S.db.read_bytes(uid, pid, files[path]["id"])
        if sha_bytes(data) != sha:
            raise RuntimeError(f"checkpoint bytes changed during restore: {path}")
        p = Path(dest) / path[len(prefix):]
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(data)
        n += 1
    return n


def for_run(pid, rid):
    return [summary(m) for m in _manifests(pid) if (m.get("dependency_json") or {}).get("run_id") == rid]


def project_summary(pid):
    return [summary(m) for m in _manifests(pid)]


# ── recovery classification ────────────────────────────────────────────────────
LEGACY_INTERRUPT = "The processing worker restarted during "


def run_recovery(run, ckpts=None):
    """Recovery state for a pipeline_run row (works for legacy rows without runtime). Never auto-replays a provider call
    whose outcome is unknown."""
    out = run.get("output_json") if isinstance(run.get("output_json"), dict) else {}
    inp = run.get("input_json") if isinstance(run.get("input_json"), dict) else {}
    rt = out.get("runtime") or {}
    stages = run.get("stages_json") if isinstance(run.get("stages_json"), list) else []
    ckpts = ckpts if ckpts is not None else []
    stages_saved = sorted({c["stage"] for c in ckpts if c.get("stage") and c.get("status", "verified") == "verified"},
                          key=lambda s: STAGE_ORDER.index(s) if s in STAGE_ORDER else 99)
    last = stages_saved[-1] if stages_saved else None
    state = rt.get("recovery_state") or "none"
    err = run.get("error") or ""
    if state == "none" and err.startswith(LEGACY_INTERRUPT):
        state = "provider_outcome_unknown" if "mesh" in err.split(LEGACY_INTERRUPT, 1)[1][:20] else "interrupted"
    if state == "none" and err == "Cancelled" and run.get("kind") == "asset" and run.get("stage") == "mesh":
        state = "provider_outcome_unknown"     # cancelled while the mesh stage was active: outcome unknown, confirm before repeating
    marker = rt.get("provider_may_run")
    if marker and marker.get("launched") is False:
        marker = None                          # recorded but cancelled before the process was spawned
    if marker and run.get("kind") == "asset" and "mesh" not in stages_saved and (
            state in ("interrupted", "resume_limit") or (state == "none" and (err == "Cancelled" or err.startswith(LEGACY_INTERRUPT)))):
        state = "provider_outcome_unknown"     # a full pipeline may have dispatched the mesh request (possible, not known)
    requires_ack = (state == "provider_outcome_unknown" and "mesh" not in stages_saved) or state == "checkpoint_invalid"
    resumable = run.get("status") == "failed" and run.get("kind") in ("asset", "rebuild", "export") and \
        (bool(last) or state in ("interrupted", "resume_limit", "provider_outcome_unknown"))
    return dict(state=state, requires_ack=requires_ack, resumable=resumable, last_checkpoint_stage=last,
                checkpoint_id=rt.get("checkpoint_id"), resumed_from_stage=rt.get("resumed_from_stage"),
                auto_resumes=int(rt.get("auto_resumes") or 0), in_flight_stage=rt.get("in_flight_stage"),
                provider_may_run=bool(marker), provider_charge="possible" if (marker and "mesh" not in stages_saved) else None,
                heartbeat_at=rt.get("heartbeat_at"), progress_at=rt.get("progress_at"),
                next_after_review=inp.get("_next"), saved_stages=stages_saved)


def classify_interrupted(ctx, ckpts):
    """Decision for a run found 'running' at startup: auto_resume (safe local work or verified checkpoint) or fail."""
    kind = ctx.row["kind"]
    running = [s["key"] for s in ctx.stages if s.get("status") == "running"]
    saved = {c["stage"] for c in ckpts if c.get("status", "verified") == "verified"}
    rt = ctx.output.get("runtime") or {}
    nxt = ctx.input.get("_next")
    if kind == "asset" and nxt != "scene":
        marker = rt.get("provider_may_run")
        if marker and marker.get("launched") is not False and int(marker.get("attempt") or 0) == int(ctx.row.get("attempt") or 1) and "mesh" not in saved:
            return dict(action="fail", state="provider_outcome_unknown", stage=",".join(running) or "mesh",
                        msg="The worker restarted after the full pipeline was launched and before a mesh was saved, so the mesh request "
                            "(TRELLIS GPU) may have been dispatched; a charge is possible, not known. Your inputs are preserved. "
                            "Confirm to repeat it (it may use GPU quota again).")
        if int(rt.get("auto_resumes") or 0) >= MAX_AUTO_RESUMES:
            return dict(action="fail", state="resume_limit", stage=",".join(running),
                        msg="The worker was interrupted again after an automatic resume. Saved checkpoints are kept; press Resume to continue explicitly.")
        if "mesh" in running and "mesh" not in saved:
            return dict(action="fail", state="provider_outcome_unknown", stage="mesh",
                        msg="The worker restarted while the mesh request (TRELLIS GPU) was in flight; its outcome is unknown and no mesh was saved. "
                            "Your inputs are preserved. Confirm to repeat the mesh request (it may use GPU quota again).")
        if "asset" in running and "mesh" not in saved:
            return dict(action="fail", state="provider_outcome_unknown", stage="asset",
                        msg="The worker restarted after the mesh returned but before it was saved. Confirm to repeat the mesh request.")
        return dict(action="resume", state="resumed", stage=",".join(running), resume_from=max(saved, key=STAGE_ORDER.index) if saved else None)
    if int(rt.get("auto_resumes") or 0) >= MAX_AUTO_RESUMES:
        return dict(action="fail", state="resume_limit", stage=",".join(running),
                    msg="The worker was interrupted again after an automatic resume. Saved checkpoints are kept; press Resume to continue explicitly.")
    if kind in ("rebuild", "export") or (kind in ("asset", "rebuild") and nxt == "scene"):
        return dict(action="resume", state="resumed", stage=",".join(running), resume_from=max(saved, key=STAGE_ORDER.index) if saved else None)
    return dict(action="fail", state="interrupted", stage=",".join(running),
                msg="The processing worker restarted during " + (",".join(running) or "this run") +
                    ". Your inputs and saved artifacts are preserved. Retry explicitly.")


def ack_current(ctx):
    """An explicit repeat acknowledgement that applies to THIS attempt (never inherited by a later one)."""
    a = (ctx.output.get("runtime") or {}).get("provider_repeat_ack") or {}
    if a and int(a.get("attempt") or 0) == int(ctx.row.get("attempt") or 1):
        return a
    a = ctx.input.get("_provider_repeat_ack") or {}
    return a if a and int(ctx.row.get("attempt") or 1) == 1 else None
