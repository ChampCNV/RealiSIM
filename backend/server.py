#!/usr/bin/env python3
"""RealiSIM workspace backend (PRD §7 "web orchestration layer").

Authenticated API (Firebase ID token -> ingress forward-auth -> X-Sender-Uid) + background job queue that runs the
photo2grasp repository non-interactively in per-project git worktrees, a server-authoritative MuJoCo worker, the
Gymnasium exporter/validator, and SpacetimeDB as the system of record for metadata, job state, versions and every
generated file (chunked, read back and hash-verified). New source captures are also persisted in SpacetimeDB;
processing-worker copies are restorable caches, and historical sources are persisted when used again.
"""
import asyncio, hashlib, json, math, os, re, shutil, signal, subprocess, sys, threading, time, traceback, zipfile, io
from pathlib import Path
from aiohttp import web

sys.path.insert(0, str(Path(__file__).resolve().parent))
from stdb import Stdb, StdbError, new_id, q  # noqa: E402
import checkpoints as CK  # noqa: E402

ROOT = Path("/workspace/photo2grasp")
REPO = ROOT / "main"
PY = str(ROOT / ".venv" / "bin" / "python")
ADAPTER = ROOT / "adapter"
DATA = ROOT / "data2"
UPLOADS = DATA / "uploads"
PROJECTS = DATA / "projects"
CACHE = DATA / "cache"
RUNLOGS = DATA / "runs"
VALIDATE = DATA / "validate"
for d in (UPLOADS, PROJECTS, CACHE, RUNLOGS, VALIDATE):
    d.mkdir(parents=True, exist_ok=True)
PORT = int((ROOT / "backend" / "port").read_text().strip())
REPO_URL = "https://github.com/adityabangde/photo2grasp"
UPLOAD_CHUNK = 4 * 1024 * 1024
MAX_UPLOAD = 1024 * 1024 * 1024          # 1 GiB per source file
MAX_PACKAGE = 100 * 1024 * 1024          # 100 MiB per Gymnasium package (load-test before raising)
VIDEO_EXT = {".mp4", ".mov", ".m4v", ".webm", ".avi", ".mkv"}
IMAGE_EXT = {".jpg", ".jpeg", ".png", ".webp"}
SLUG = re.compile(r"^[a-z][a-z0-9_]{1,39}$")
NICE = ["nice", "-n", "12"] + (["taskset", "-c", "1-6"] if shutil.which("taskset") and (os.cpu_count() or 1) >= 8 else [])
ROBOT_PROFILES = [
    dict(id="so100-menagerie", name="SO-100 · MuJoCo Menagerie trs_so_arm100", available=True,
         status="Simulation profile", note="The repository's simulation model. It is the SO-100, not the SO-101 hardware target."),
    dict(id="so101-follower-x2", name="SO-101 follower × 2 (legacy preset)", available=False, status="Dual-arm preset unavailable",
         note="This older dual-arm reference preset is not implemented. The workspace pipeline separately supports a single SO-101 simulation model; hardware calibration remains a separate task."),
]
ROBOT_NOTE = ("Simulation profile: SO-100 from MuJoCo Menagerie (trs_so_arm100), as used by the upstream photo2grasp "
              "repository. It is not a validated model of the SO-101 follower arms; do not assume the trained behaviour "
              "transfers to SO-101 hardware without a validated SO-101 model and configuration.")
STAGES = {
    "asset": [("validate", "Upload validation"), ("frames", "Frame extraction"), ("mesh", "Mesh generation"),
              ("asset", "Asset build"), ("review", "Property review"), ("scene", "Scene build"),
              ("simulate", "Simulation validation")],
    "rebuild": [("restore", "Restore mesh"), ("asset", "Asset build"), ("review", "Property review"),
                ("scene", "Scene build"), ("simulate", "Simulation validation")],
    "export": [("assemble", "Assemble package"), ("install", "Install in clean environment"),
               ("check", "Gymnasium env checker + smoke test"), ("persist", "Store in SpacetimeDB"),
               ("verify", "Read back and verify hashes")],
    "scene_capture": [("validate", "Upload validation"), ("frames", "Frame extraction + camera poses (COLMAP)"),
                      ("objects", "Table plane, scale and objects"), ("views", "Object views"),
                      ("review", "Object properties review"), ("mesh", "Mesh generation"), ("scene", "Scene build"),
                      ("simulate", "Simulation validation")],
}

db = Stdb()
CK.bind(sys.modules[__name__])
STATE = dict(repo={}, intro={}, busy=False, current=None, running={})
LOCK = threading.Lock()
QUEUE = []
QCOND = threading.Condition()
LIVE = {}            # run_id -> RunCtx
SIM_SEM = threading.Semaphore(2)

# ── parallel job workers ─────────────────────────────────────────────────────────
# STATE["running"] maps run_id -> project_id for every job a worker is executing. It is mutated ONLY under QCOND.
# STATE["busy"] / STATE["current"] are kept as derived backward-compatible views (busy = anything running,
# current = one running rid or None). Never two jobs of the same project at once (shared per-project worktree).
WORKERS_F = ROOT / "releases" / "workers.json"
WORKERS_DEFAULT, WORKERS_MIN, WORKERS_MAX = 3, 1, 6
HEAVY_STAGGER_S = 10           # after a heavy start, others wait so the next admission sees its memory
LIGHT_KINDS = ("detect_robot",)  # Claude call only: no local heavy allocation, not staggered
HELD = {}                      # rid -> last held code written to that run's log (shared by all workers; under QCOND)
ADMIT = dict(last_heavy_start=0.0)
WT_LOCK = threading.Lock()     # git worktree add/prune share the main repo's .git
DEPS_LOCK = threading.Lock()   # one-time uv install into the shared repo venv
_RESTORE_LOCKS = {}
_RESTORE_GUARD = threading.Lock()


def worker_config():
    """N from env REALISIM_WORKERS, else releases/workers.json {"workers": N, "kind_limits": {kind: n}}; clamp 1..6."""
    cfg = {}
    try:
        cfg = json.loads(WORKERS_F.read_text()) or {}
    except Exception:
        cfg = {}
    raw = os.environ.get("REALISIM_WORKERS") or cfg.get("workers", WORKERS_DEFAULT)
    try:
        n = int(raw)
    except Exception:
        n = WORKERS_DEFAULT
    limits = {}
    for k, v in ((cfg.get("kind_limits") or {}) if isinstance(cfg.get("kind_limits"), dict) else {}).items():
        try:
            limits[str(k)] = max(1, int(v))
        except Exception:
            pass
    return dict(workers=max(WORKERS_MIN, min(WORKERS_MAX, n)), kind_limits=limits)


WORKER_CFG = worker_config()


def _sync_running_compat():
    """Call with QCOND held."""
    run = STATE["running"]
    STATE["busy"] = bool(run)
    STATE["current"] = next(iter(run), None) if run else None


def running_ids():
    with QCOND:
        return dict(STATE["running"])


def project_running(pid):
    """Run ids of this project currently executing."""
    with QCOND:
        return [r for r, p in STATE["running"].items() if p == pid]


def _restore_lock(key):
    with _RESTORE_GUARD:
        return _RESTORE_LOCKS.setdefault(key, threading.Lock())


def now_ms():
    return time.time() * 1000


def log(*a):
    print(time.strftime("%H:%M:%S"), *a, flush=True)


def sh(cmd, cwd=REPO, timeout=600, env=None):
    r = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True, timeout=timeout, shell=isinstance(cmd, str), env=env)
    return r.returncode, (r.stdout or "").strip(), (r.stderr or "").strip()


def jerr(status, msg):
    cls = {400: web.HTTPBadRequest, 403: web.HTTPForbidden, 404: web.HTTPNotFound, 409: web.HTTPConflict,
           422: web.HTTPUnprocessableEntity, 500: web.HTTPInternalServerError, 503: web.HTTPServiceUnavailable}.get(status, web.HTTPBadRequest)
    return cls(text=json.dumps({"error": msg}), content_type="application/json")


def pj(row, *keys):
    for k in keys:
        v = row.get(k)
        if isinstance(v, str) and v[:1] in "[{":
            try:
                row[k] = json.loads(v)
            except Exception:
                pass
    return row


# ── access ─────────────────────────────────────────────────────────────────────
def uid_of(request):
    uid = request.headers.get("X-Sender-Uid")
    if not uid:
        raise jerr(403, "sign in required")
    return uid


def project_for(uid, pid, write=False):
    rows = db.sql(f"SELECT * FROM project WHERE id = {q(pid)}")
    if not rows:
        raise jerr(404, "project not found")
    p = pj(rows[0], "settings_json")
    if p["owner_uid"] == uid:
        p["role"] = "owner"
        return p
    for m in db.sql(f"SELECT * FROM project_member WHERE project_id = {q(pid)}"):
        if m["uid"] == uid and (not write or m["role"] == "editor"):
            p["role"] = m["role"]
            return p
    raise jerr(404, "project not found")  # do not reveal existence to unauthorised users


# ── repo sync + introspection ──────────────────────────────────────────────────
def git_info():
    _, out, _ = sh(["git", "log", "-8", "--format=%H%x1f%h%x1f%s%x1f%an%x1f%at"])
    hist = [dict(zip(("sha", "short", "message", "author", "date"), l.split("\x1f"))) for l in out.splitlines() if l.count("\x1f") == 4]
    for h in hist:
        h["date"] = int(h["date"]) * 1000
    return dict(commit=hist[0] if hist else None, history=hist, url=REPO_URL, branch="main")


def introspect():
    r = subprocess.run([PY, str(ADAPTER / "introspect.py"), str(REPO)], cwd=REPO, capture_output=True, text=True, timeout=300)
    try:
        STATE["intro"] = json.loads(r.stdout.strip().splitlines()[-1])
    except Exception:
        STATE["intro"] = dict(ok=False, errors=[(r.stderr or "introspection failed")[-600:]])


def mujoco_version():
    _, out, _ = sh([PY, "-c", "import mujoco; print(mujoco.__version__)"])
    return out.strip()


def release_idle():
    if STATE["running"] or STATE["busy"] or QUEUE:
        return False, "pipeline job running or queued"
    if any(c.row.get("status") in ("queued", "running", "needs_review") for c in list(LIVE.values())):
        return False, "pipeline context is still active"
    try:
        if db.sql("SELECT id FROM pipeline_run WHERE status = 'needs_review'"):
            return False, "a pipeline run is waiting for property review"
    except Exception:
        return False, "paused pipeline state could not be checked"
    wf = sys.modules.get("workflow")
    if wf is not None:
        # A reviewed preview awaiting user approval is not executing code.
        for pid in list(getattr(wf, "ACTIVE", set())):
            try:
                _, _, draft = wf.load(pid)
                phase = ((draft or {}).get("build") or {}).get("phase")
                if phase in getattr(wf, "ACTIVE_PHASES", ("queued", "building", "accepting", "layout", "composing", "exporting")):
                    return False, "workspace pipeline is active"
            except Exception:
                return False, "workflow state could not be checked"
        try:
            for row in db.sql("SELECT settings_json FROM project"):
                settings = row.get("settings_json") or {}
                settings = json.loads(settings) if isinstance(settings, str) else settings
                draft = settings.get("workflow") or {}
                if (draft.get("analysis") or {}).get("status") in ("queued", "running") or ((draft.get("layout") or {}).get("run") or {}).get("status") in ("queued", "running"):
                    return False, "capture analysis or spatial recovery is active"
        except Exception:
            return False, "workspace jobs could not be checked"
    import release_gate
    return release_gate.default_idle()


def sync_repo():
    """Fetch/check in isolation. Promotion is disabled until operator activates the validated gate."""
    try:
        import release_gate
        release_gate.tick_async(idle_probe=release_idle, guard=LOCK)
        _, head, _ = sh(["git", "rev-parse", "HEAD"])
        if not STATE.get("intro") or STATE.get("introspected_sha") != head:
            introspect()
            STATE["introspected_sha"] = head
        gate = release_gate.public_status()
        STATE["repo"] = dict(git_info(), checked_at=now_ms(),
                             pending=gate.get("candidate", {}).get("sha") != head,
                             release_gate=gate, mujoco=mujoco_version())
    except Exception as e:
        log("release gate check failed", type(e).__name__, str(e)[:200])


AUTOUPDATE_F = ROOT / "releases" / "autoupdate.json"
POLL = dict(interval_s=60, polls=0, started=0, coalesced=0, last_poll_at=None, last_started_at=None)


def poll_seconds():
    """Upstream poll cadence, re-read every cycle from releases/autoupdate.json (clamped 30..600 s, default 60)."""
    try:
        v = int(json.loads(AUTOUPDATE_F.read_text()).get("poll_seconds", 60))
    except Exception:
        v = 60
    return max(30, min(600, v))


def sync_loop():
    """Best-effort fixed-cadence poll: each cycle starts one serialized gate tick (fetch, check, promote). If the previous
    tick is still busy (a long check or promotion), the cycle coalesces into it instead of starting a second one."""
    while True:
        t0 = time.time()
        POLL["interval_s"] = poll_seconds()
        thr = getattr(sys.modules.get("release_gate"), "_THREAD", None)
        busy = bool(thr and thr.is_alive())
        sync_repo()
        POLL["polls"] += 1
        POLL["last_poll_at"] = int(t0 * 1000)
        if busy:
            POLL["coalesced"] += 1
        else:
            POLL["started"] += 1
            POLL["last_started_at"] = int(t0 * 1000)
        try:
            tmp = ROOT / "releases" / "poll.json.tmp"
            tmp.write_text(json.dumps(dict(POLL, pid=os.getpid(), updated_at=int(time.time() * 1000))))
            os.replace(tmp, ROOT / "releases" / "poll.json")
        except Exception:
            pass
        time.sleep(max(1.0, POLL["interval_s"] - (time.time() - t0)))


def ensure_worktree(pid):
    """Per-project isolated workspace: a git worktree of the repo at the current commit (outputs/ persist there)."""
    wt = PROJECTS / pid / "repo"
    import release_gate
    head = release_gate.running_sha()
    if not head:
        raise RuntimeError("The pipeline revision has not been validated yet; wait for the release check.")
    if not (wt / ".git").exists():
        wt.parent.mkdir(parents=True, exist_ok=True)
        with WT_LOCK:                       # concurrent jobs of different projects share the main repo's .git
            sh(["git", "worktree", "prune"])
            code, _, err = sh(["git", "worktree", "add", "--detach", "-f", str(wt), head])
        if code:
            raise RuntimeError("could not create project workspace: " + err[-300:])
    else:
        sh(["git", "checkout", "-q", "-f", "--detach", head], cwd=wt)
    tp = wt / "third_party"
    if not tp.exists():
        tp.symlink_to(REPO / "third_party")
    return wt, head[:7]


# ── uploads ────────────────────────────────────────────────────────────────────
def upload_row(pid, upid):
    rows = db.sql(f"SELECT * FROM upload WHERE id = {q(upid)}")
    if not rows or rows[0]["project_id"] != pid:
        return None
    return pj(rows[0], "meta_json")


def put_upload(uid, row, **changes):
    r = {**row, **changes}
    meta = r.get("meta_json")
    db.call("put_upload", uid=uid, id=r["id"], project_id=r["project_id"], capture_mode=r["capture_mode"], filename=r["filename"],
            media_type=r["media_type"], size=float(r["size"]), sha256=r.get("sha256") or "", status=r["status"],
            chunk_size=int(r["chunk_size"]), chunks_total=int(r["chunks_total"]), chunks_received=int(r["chunks_received"]),
            meta_json=meta if isinstance(meta, str) else json.dumps(meta or {}), error=r.get("error") or "")
    return r


def received_chunks(upid):
    d = UPLOADS / upid / "chunks"
    return sorted(int(p.name) for p in d.iterdir() if p.name.isdigit()) if d.exists() else []


VALIDATE_SRC = r"""
import sys, json, cv2
p, kind, thumb = sys.argv[1], sys.argv[2], sys.argv[3]
out = {}
if kind == "video":
    cap = cv2.VideoCapture(p)
    if not cap.isOpened():
        print(json.dumps(dict(ok=False, error="the file could not be opened as a video"))); sys.exit()
    n = int(cap.get(cv2.CAP_PROP_FRAME_COUNT)); fps = cap.get(cv2.CAP_PROP_FPS) or 0
    w, h = int(cap.get(3)), int(cap.get(4))
    good, img0 = 0, None
    for k in range(8):
        cap.set(cv2.CAP_PROP_POS_FRAMES, int((k + 0.5) * max(n, 1) / 8)); ok, img = cap.read()
        if ok and img is not None:
            good += 1; img0 = img if img0 is None else img0
    if good < 6:
        print(json.dumps(dict(ok=False, error=f"only {good}/8 sampled frames could be decoded"))); sys.exit()
    out = dict(ok=True, frames=n, fps=round(fps, 3), width=w, height=h, duration=round(n / fps, 2) if fps else None, sampled_ok=good)
    if out["duration"] is not None and out["duration"] < 2:
        out = dict(ok=False, error="the video is shorter than 2 seconds; film the object from several sides")
else:
    img0 = cv2.imread(p)
    if img0 is None:
        print(json.dumps(dict(ok=False, error="the image could not be decoded"))); sys.exit()
    out = dict(ok=True, width=img0.shape[1], height=img0.shape[0])
if out.get("ok") and img0 is not None:
    s = 360 / max(img0.shape[:2]); cv2.imwrite(thumb, cv2.resize(img0, None, fx=s, fy=s), [cv2.IMWRITE_JPEG_QUALITY, 82])
print(json.dumps(out))
"""


def finalize_upload(uid, row):
    upid = row["id"]
    d = UPLOADS / upid
    have = received_chunks(upid)
    total = int(row["chunks_total"])
    missing = [i for i in range(total) if i not in have]
    if missing:
        return put_upload(uid, row, status="uploading", chunks_received=len(have), error=f"{len(missing)} chunk(s) missing")
    dst = d / ("source" + Path(row["filename"]).suffix.lower())
    h = hashlib.sha256()
    with open(dst, "wb") as out:
        for i in range(total):
            b = (d / "chunks" / str(i)).read_bytes()
            h.update(b)
            out.write(b)
    size = dst.stat().st_size
    if size != int(row["size"]):
        dst.unlink()
        shutil.rmtree(d / "chunks", ignore_errors=True)
        return put_upload(uid, row, status="failed", error=f"size mismatch after upload ({size} != {int(row['size'])}); upload again")
    sha = h.hexdigest()
    if row.get("sha256") and row["sha256"] != sha:
        return put_upload(uid, row, status="failed", error="file hash differs from the hash computed in the browser; upload again")
    shutil.rmtree(d / "chunks", ignore_errors=True)
    put_upload(uid, row, status="validating", sha256=sha, chunks_received=total, error="")
    kind = "video" if Path(row["filename"]).suffix.lower() in VIDEO_EXT else "image"
    thumb = d / "thumb.jpg"
    r = subprocess.run([PY, "-c", VALIDATE_SRC, str(dst), kind, str(thumb)], capture_output=True, text=True, timeout=300)
    try:
        v = json.loads(r.stdout.strip().splitlines()[-1])
    except Exception:
        v = dict(ok=False, error="decoder crashed: " + (r.stderr or "")[-200:])
    meta = dict(row.get("meta_json") or {}, kind=kind, **{k: v[k] for k in v if k not in ("ok", "error")})
    if not v.get("ok"):
        return put_upload(uid, row, status="invalid", sha256=sha, meta_json=meta, error=v.get("error", "invalid file"))
    if thumb.exists():
        mid, files = db.store_dir(uid, row["project_id"], "upload_thumb", upid, "1", [("thumb.jpg", thumb.read_bytes(), "image/jpeg")])
        meta["thumb_file_id"] = files[0]["id"]
    row = put_upload(uid, row, status="validating", sha256=sha, meta_json={**meta, "persistence_pending": True}, error="")
    return persist_uploaded_capture(uid, row)


def persist_uploaded_capture(uid, row):
    """Both project modes retain their validated original in SpacetimeDB. No guided draft is created."""
    import workflow
    src = next((UPLOADS / row["id"]).glob("source.*"), None)
    if src is None:
        src = workflow.ensure_source(row["project_id"], uid, row["id"])
    if src is None:
        return put_upload(uid, row, status="failed", error="The source file is missing; upload it again.")
    meta = dict(row.get("meta_json") or {})
    try:
        info = workflow.persist_source(row["project_id"], uid, row, src)
        meta.update(capture_source=info, source_file_id=info["file_id"], persistence_pending=False)
        return put_upload(uid, row, status="ready", meta_json=meta, error="")
    except Exception as e:
        log("capture persistence failed", row["id"], type(e).__name__)
        meta["persistence_pending"] = True
        return put_upload(uid, row, status="failed", meta_json=meta,
                          error="The capture decoded successfully but could not be saved durably. Retry completion; processing stays blocked until storage succeeds.")




# ── Hugging Face access for TRELLIS: the user's saved HF_TOKEN is injected server-side by Mana symbolic proxies ──
HF_HOST = "trellis-community-trellis.hf.space"
HF_STATE = dict(at=0, status="unknown", error=None)


def hf_proxy():
    """({host: {GET:{url,headers}, POST:{...}}}, status). The token never enters this process or the pipeline."""
    import symbolic_proxy
    routes = {}
    try:
        for m in ("GET", "POST"):
            t = symbolic_proxy.private({"name": f"p2g_trellis_{m.lower()}", "url": f"https://{HF_HOST}", "method": m, "allow_subpaths": True,
                                        "headers": {"authorization": "Bearer ${secrets.HF_TOKEN}", "content-type": "${override}", "accept": "${override}"}})
            routes[m] = {"url": t.url, "headers": dict(t.headers)}
        HF_STATE.update(at=time.time(), status="connected", error=None)
        return {HF_HOST: routes}, "connected"
    except Exception as e:
        msg = str(e)
        st = "missing" if ("HF_TOKEN" in msg or "secret" in msg.lower()) else "error"
        HF_STATE.update(at=time.time(), status=st, error=msg[:300])
        return None, st


def hf_status():
    if time.time() - HF_STATE["at"] > 60:
        hf_proxy()
    return dict(status=HF_STATE["status"], error=HF_STATE["error"])


# ── runs ───────────────────────────────────────────────────────────────────────
STAGE_RE = re.compile(r"^\s*\[(\d+)/(\d+)\]\s*(.*)$")
ASSET_STAGE_MAP = {1: "frames", 2: "frames", 3: "mesh", 4: "asset", 5: "asset"}
MEDIA = {".xml": "application/xml", ".json": "application/json", ".obj": "text/plain", ".png": "image/png",
         ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".glb": "model/gltf-binary", ".bin": "application/octet-stream",
         ".txt": "text/plain", ".log": "text/plain", ".py": "text/x-python", ".md": "text/markdown", ".toml": "text/plain",
         ".yaml": "text/yaml", ".zip": "application/zip", ".stl": "model/stl", ".sha256": "text/plain"}


def media(p):
    return MEDIA.get(Path(p).suffix.lower(), "application/octet-stream")


class Cancelled(Exception):
    pass


class RunCtx:
    def __init__(self, row, uid):
        self.row = row
        self.uid = uid                       # project owner: system writes act on the owner's behalf
        self.input = row["input_json"] if isinstance(row["input_json"], dict) else json.loads(row["input_json"] or "{}")
        st = row.get("stages_json")
        self.stages = st if isinstance(st, list) else (json.loads(st) if st else
                      [dict(key=k, label=l, status="pending") for k, l in STAGES[row["kind"]]])
        self.output = row.get("output_json") if isinstance(row.get("output_json"), dict) else json.loads(row.get("output_json") or "{}")
        self.text = ""
        self.logf = open(RUNLOGS / f"{row['id']}.log", "a")
        self.proc = None
        self.cancelled = False
        self.last_push = 0
        self.dirty = False
        self.lock = threading.RLock()
        self.answers = []

    def write(self, s):
        with self.lock:
            self.text += s
            self.dirty = True
        self.logf.write(s)
        self.logf.flush()

    def stage(self, key, status, detail=None, **extra):
        t = now_ms()
        with self.lock:
            for s in self.stages:
                if s["key"] == key:
                    CK.apply_timing(s, status, t)            # contract fields (started_at set once, never reset)
                    if status == "running" and not s.get("started"):
                        s["started"] = t                     # legacy keys kept for existing UI
                    if status in ("done", "failed", "skipped", "waiting"):
                        s["finished"] = t
                    if detail is not None:
                        s["detail"] = str(detail)[:300]
                    s.update(extra)
            if status == "running":
                self.row["stage"] = key
                if self.output.get("runtime") is not None or self.row.get("status") == "running":
                    self.output.setdefault("runtime", {})["in_flight_stage"] = key
            self.dirty = True
        self.push(force=True)

    def runtime(self):
        return self.output.setdefault("runtime", {})

    def push(self, force=False, strict=False, **fields):
        """Durable state. Non-forced writes only on new output (>= PROGRESS_S apart) or as a HEARTBEAT_S heartbeat:
        never per-second. The UI runs its own clock from the timing fields."""
        with self.lock:
            self.row.update(fields)
            age = time.time() - self.last_push
            if not force and not ((self.dirty and age >= CK.PROGRESS_S) or age >= CK.HEARTBEAT_S):
                return
            self.last_push = time.time()
            r = self.row
            if r.get("status") in ("running", "queued") or "runtime" in self.output:
                rt = self.runtime()
                t = now_ms()
                if r.get("status") == "running":
                    rt["heartbeat_at"] = t
                    rt["worker_instance_id"] = CK.WORKER_ID
                if self.dirty or force:
                    rt["progress_at"] = t
                if r.get("status") not in ("running",):
                    rt.pop("in_flight_stage", None)
            self.dirty = False
            done = sum(1 for s in self.stages if s["status"] in ("done", "skipped"))
            tail = "\n".join([l for l in self.text.splitlines() if l.strip()][-30:])[-4000:]
            payload = dict(uid=self.uid, id=r["id"], project_id=r["project_id"], kind=r["kind"], status=r["status"],
                           stage=r.get("stage") or "", stages_json=json.dumps(self.stages), progress=done / max(1, len(self.stages)),
                           error=r.get("error") or "", input_json=json.dumps(self.input), output_json=json.dumps(self.output),
                           log_tail=tail, commit=r.get("commit") or "", attempt=int(r.get("attempt") or 1),
                           started_at=float(r.get("started_at") or 0), finished_at=float(r.get("finished_at") or 0))
        try:
            db.call("put_run", **payload)
        except Exception as e:
            log("put_run failed", e)
            if strict:
                raise

    def check(self):
        if self.cancelled:
            raise Cancelled()


def suggest_answer(prompt, inp):
    p = prompt.lower()
    m = inp.get("measurements") or {}
    tags = inp.get("tags") or {}
    val = lambda k: (m.get(k) or {}).get("value")
    has = lambda v: v not in (None, "")
    if "size in cm" in p and all(has(val(k)) for k in ("length_cm", "width_cm", "height_cm")):
        return f"{val('length_cm')} {val('width_cm')} {val('height_cm')}"
    if ("weight" in p or "grams" in p) and has(val("mass_g")):
        return str(val("mass_g"))
    if "material" in p and has(val("material")):
        return str(val("material"))
    if "friction" in p and has(val("friction")):
        return str(val("friction"))
    f, b = tags.get("front"), tags.get("back")
    if "[r]obot" in p and "[c]amera" in p:
        if has(f) and has(b) and str(f) == str(b):
            return "b"
        return "r" if has(f) and not has(b) else ("c" if has(b) else "")
    if "robot" in p and "id" in p:
        return str(int(f)) if has(f) else ""
    if "environment camera" in p or "realsense" in p:
        return str(int(b)) if has(b) else ""
    return ""



GUARDIAN = Path(__file__).resolve().parent / "job_guardian.py"


def proc_start(pid):
    try:
        raw = open(f"/proc/{pid}/stat", "rb").read().decode("utf-8", "replace")
        return int(raw[raw.rindex(")") + 2:].split()[19])
    except (OSError, ValueError, IndexError):
        return None


def start_guardian(ctx, proc, timeout):
    """Per-job guardian in its own session: if this API process dies (or the launcher exits), it terminates only the
    job's process group. Not a daemon; it exits with the job. Failure to start it is logged, never fatal."""
    try:
        api_start, job_start = proc_start(os.getpid()), proc_start(proc.pid)
        if api_start is None or job_start is None:
            return None
        g = subprocess.Popen([sys.executable, str(GUARDIAN), "--api-pid", str(os.getpid()), "--api-start", str(api_start),
                              "--job-pid", str(proc.pid), "--job-start", str(job_start), "--max-seconds", str(int(timeout + 900)),
                              "--log", str(RUNLOGS / "guardian.log")],
                             stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                             start_new_session=True, close_fds=True)
        threading.Thread(target=g.wait, daemon=True).start()     # reap when it exits
        return g
    except Exception as e:
        ctx.write(f"   ↳ job guardian not started ({type(e).__name__}); cancellation still kills the job group\n")
        return None


def provider_marker_open(ctx):
    """This attempt launched a full pipeline (mesh request possible) and no verified mesh checkpoint was saved since."""
    m = ctx.runtime().get("provider_may_run") or {}
    if not m or m.get("launched") is False or int(m.get("attempt") or 0) != int(ctx.row.get("attempt") or 1):
        return False
    return not any(c.get("stage") == "mesh" for c in ctx.output.get("checkpoints") or [])


def persist_provider_marker(ctx):
    """Durably record, and read back from SpacetimeDB, that a full asset pipeline (which may dispatch the paid mesh
    request) is about to launch. Any failure raises before the process exists."""
    marker = dict(id=new_id("pmr"), at=now_ms(), attempt=int(ctx.row.get("attempt") or 1), command="asset",
                  worker_instance_id=CK.WORKER_ID)
    rt = ctx.runtime()
    rt["provider_markers"] = (list(rt.get("provider_markers") or []) + [marker])[-8:]
    rt["provider_may_run"] = marker
    try:
        ctx.push(force=True, strict=True)
        back = db.sql(f"SELECT output_json FROM pipeline_run WHERE id = {q(ctx.row['id'])}")
        got = ((json.loads(back[0]["output_json"] or "{}").get("runtime") or {}).get("provider_may_run") or {}) if back else {}
    except Exception as e:
        raise RuntimeError("Could not durably record the processing launch, so the paid mesh request was not started. "
                           "Nothing was charged; retry when storage is available. (" + type(e).__name__ + ")")
    if got.get("id") != marker["id"]:
        raise RuntimeError("The processing launch record could not be verified in storage, so the paid mesh request was not started. "
                           "Nothing was charged; retry when storage is available.")
    ctx.write(f"   ↳ launch recorded and verified ({marker['id']}); the mesh request may run from here\n")
    return marker


def run_proc(ctx, args, cwd, stage_map=None, timeout=60 * 45, extra_env=None, on_stage=None, provider_may_run=False):
    """Runs a pipeline command non-interactively: questions the repo asks on stdin are answered from the job's
    form values, or with the pipeline's own default; every question and answer is recorded for review."""
    env = dict(os.environ, PYTHONUNBUFFERED="1", MPLBACKEND="Agg", PYTHONIOENCODING="utf-8", OMP_NUM_THREADS="4",
               MKL_NUM_THREADS="4", OPENBLAS_NUM_THREADS="4", PYTHONPATH=str(ADAPTER / "shim"), P2G_ORT_THREADS="4")
    env.update(extra_env or {})
    # A pipeline may prepare views inside either frame selection or TRELLIS. Never load the
    # pinned BiRefNet locally alongside the API, database and search service.
    prep_dir = PROJECTS / ctx.row["project_id"] / "runtime" / ctx.row["id"]
    prep_dir.mkdir(parents=True, exist_ok=True)
    prep_status = prep_dir / ("preprocess-" + str(int(now_ms())) + ".json")
    env.update(REALISIM_REMOTE_PREPROCESS="1", REALISIM_REMOTE_JOB_ID=ctx.row["id"],
               REALISIM_REMOTE_STATUS_FILE=str(prep_status), REALISIM_REMOTE_MAX_SECONDS="1200",
               REALISIM_REMOTE_MAX_CALLS="96", REALISIM_REMOTE_CPU="4",
               REALISIM_REMOTE_MEMORY_GIB="16", REALISIM_LOCAL_DATA_BYTES=str(4 * 1024**3))
    env.pop("VIRTUAL_ENV", None)
    env.pop("HF_TOKEN", None)
    last_prep, last_safety, stop_reason = None, 0, None
    def prep_progress():
        nonlocal last_prep
        if not prep_status.exists():
            return
        try:
            snap = json.loads(prep_status.read_text())
            allowed = ("state", "calls", "provision_s", "last_round_trip_s", "box_stopped", "box_seconds", "core_seconds")
            view = {k: snap[k] for k in allowed if k in snap}
            reason = str(snap.get("reason") or "")
            if reason:
                view["reason"] = ("Compute credits are unavailable." if "402" in reason or "credit" in reason.lower()
                                  else "Isolated image preparation stopped. See the processing log for details.")
            if view != last_prep:
                ctx.runtime()["preprocessing"] = dict(view, isolated=True, cpu=4, memory_gib=16)
                ctx.push(force=True)
                last_prep = view
        except (OSError, ValueError):
            pass  # the broker atomically replaces its file; keep the last confirmed status
    marker = persist_provider_marker(ctx) if provider_may_run else None   # durable + read back BEFORE launch; raises otherwise
    try:
        ctx.check()                        # a cancel that arrived before launch can never spawn the pipeline
    except Cancelled:
        if marker:
            ctx.runtime()["provider_may_run"] = dict(marker, launched=False)
        raise
    ctx.write("$ " + " ".join(Path(a).name if "/" in str(a) else str(a) for a in args[1:]) + "\n")
    proc = subprocess.Popen(NICE + [PY, str(ADAPTER / "bounded_exec.py")] + args, cwd=cwd,
                            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                            env=env, start_new_session=True)
    ctx.proc = proc
    if marker:
        ctx.runtime()["provider_may_run"] = dict(marker, launched=True, launched_at=now_ms())
    start_guardian(ctx, proc, timeout)
    fd = proc.stdout.fileno()
    os.set_blocking(fd, False)
    partial, last = "", time.time()
    t0 = time.time()
    while True:
        if time.time() - last_safety >= 1:
            prep_progress()
            mem = CK.memory_state()
            if mem.get("known") and mem.get("free", 2**63) < 768 * 1024**2 and stop_reason is None:
                stop_reason = "Processing stopped to protect the database and saved results because local memory headroom was exhausted. Resume only after the resource issue is resolved."
            last_safety = time.time()
        if time.time() - t0 > timeout and stop_reason is None:
            stop_reason = "Processing timed out. Your saved inputs and verified checkpoints are preserved."
        if ctx.cancelled or stop_reason:
            try:
                os.killpg(proc.pid, signal.SIGTERM if ctx.cancelled else signal.SIGKILL)
            except ProcessLookupError:
                pass
        try:
            chunk = os.read(fd, 65536)
        except BlockingIOError:
            chunk = None
        if chunk:
            s = chunk.decode("utf-8", "replace").replace("\r\n", "\n")
            ctx.write(s)
            partial += s
            *lines, partial = partial.split("\n")
            for line in lines:
                mm = STAGE_RE.match(line.split("\r")[-1])
                if mm and stage_map:
                    key = stage_map.get(int(mm.group(1)))
                    if key:
                        cur = next((st for st in ctx.stages if st["key"] == key), None)
                        if on_stage and cur is not None and cur.get("status") != "running":
                            try:
                                on_stage(key)     # boundary hook: previous stage's outputs are complete on disk
                            except Exception as e:
                                ctx.write(f"   ↳ checkpoint hook failed ({type(e).__name__}: {str(e)[:200]}); continuing without it\n")
                        for st in ctx.stages:  # earlier pipeline stages are complete once a later one starts
                            if st["status"] == "running" and st["key"] != key and st["key"] in stage_map.values():
                                ctx.stage(st["key"], "done")
                        ctx.stage(key, "running", mm.group(3).strip())
            last = time.time()
            ctx.push()
            continue
        if chunk == b"":
            break
        if partial.strip() and proc.poll() is None and time.time() - last > 0.8:
            question = partial.split("\r")[-1].strip()
            ans = suggest_answer(question, ctx.input)
            how = "from your form" if ans else "pipeline default"
            ctx.answers.append(dict(question=question[-300:], answer=ans or "(default)", source=how))
            ctx.write(f"{ans}\n   ↳ answered non-interactively ({how}): {ans or '(default)'}\n")
            try:
                proc.stdin.write((ans + "\n").encode())
                proc.stdin.flush()
            except Exception:
                pass
            partial, last = "", time.time()
        time.sleep(0.15)
        ctx.push()
    proc.wait()
    ctx.proc = None
    prep_progress()
    ctx.check()
    if stop_reason:
        if ctx.row.get("stage") == "mesh" or provider_marker_open(ctx):
            ctx.runtime().update(recovery_state="provider_outcome_unknown", requires_ack=True, provider_charge="possible")
        ctx.write("\n" + stop_reason + "\n")
        raise RuntimeError(stop_reason)
    return proc.returncode


def pipeline_error(text, code):
    lines = [l.strip() for l in text.splitlines() if l.strip()]
    exc = next((l for l in reversed(lines) if re.match(r"^[\w.]*(Error|Exception|SystemExit)\b.*:", l)), None)
    return (exc.split(".")[-1] if exc and "." in exc.split(":")[0] else exc) or (lines[-1] if lines else f"pipeline exit code {code}")


def objects_entry(meas, tags):
    v = lambda k: (meas.get(k) or {}).get("value")
    src = lambda k: (meas.get(k) or {}).get("source", "unknown")
    label = {"measured": "user-measured", "supplied": "supplied data", "estimated": "estimated (reviewed)", "unknown": "unknown"}
    e = dict(size_m=[round(float(v(k)) / 100, 4) for k in ("length_cm", "width_cm", "height_cm")],
             mass_kg=round(float(v("mass_g")) / 1000, 5), friction=[float(v("friction")), 0.005, 0.0001])
    if v("material"):
        e["material"] = str(v("material"))
    if v("tag_height_cm") not in (None, ""):
        e["tag_height_m"] = round(float(v("tag_height_cm")) / 100, 4)
    t = tags or {}
    if t.get("front") not in (None, "") or t.get("back") not in (None, ""):
        e["tags"] = dict(front=None if t.get("front") in (None, "") else int(t["front"]),
                         back=None if t.get("back") in (None, "") else int(t["back"]))
    parts = [f"size {label[src('length_cm')]}", f"mass {label[src('mass_g')]}", f"friction {label[src('friction')]}"]
    notes = [f"{k}: {(meas.get(k) or {}).get('note')}" for k in meas if (meas.get(k) or {}).get("note")]
    e["source"] = "RealiSIM: " + ", ".join(parts) + (". " + "; ".join(notes) if notes else "")
    return e


REQUIRED = [("length_cm", 0.5, 200), ("width_cm", 0.5, 200), ("height_cm", 0.5, 200), ("mass_g", 0.5, 5000), ("friction", 0.05, 2.0)]


def measurement_problems(meas):
    out = []
    for k, lo, hi in REQUIRED:
        m = meas.get(k) or {}
        try:
            val = float(m.get("value"))
        except (TypeError, ValueError):
            out.append(f"{k}: missing")
            continue
        if m.get("source") in (None, "", "unknown"):
            out.append(f"{k}: source unknown")
        if not lo <= val <= hi:
            out.append(f"{k}: {val} outside {lo}–{hi}")
    return out


def files_of(base, prefix):
    base = Path(base)
    return [(prefix + p.relative_to(base).as_posix(), p.read_bytes(), media(p)) for p in sorted(base.rglob("*")) if p.is_file()]


def sim_cmd(ctx_or_none, cmd, assets_dir, names, out, extra=()):
    r = subprocess.run(NICE + [PY, str(ADAPTER / "sim_worker.py"), cmd, "--repo", str(REPO), "--assets", str(assets_dir),
                               "--names", ",".join(names), "--out", str(out), *map(str, extra)],
                       capture_output=True, text=True, timeout=600, cwd="/tmp")
    if r.returncode != 0 or "SIM_RESULT" not in r.stdout:
        raise RuntimeError("MuJoCo worker failed: " + (r.stderr or r.stdout)[-800:])
    return json.loads(r.stdout.split("SIM_RESULT ")[1].splitlines()[0])


def restore_asset(uid, pid, av):
    """Asset files of an immutable version, restored from SpacetimeDB into a rebuildable cache."""
    d = CACHE / "assets" / av["id"]
    name = av["object_name"]
    with _restore_lock("asset:" + av["id"]):    # a job and an interactive sim may restore the same version at once
        if not (d / name / f"{name}.xml").exists():
            shutil.rmtree(d, ignore_errors=True)
            db.restore_manifest(uid, pid, av["manifest_id"], d / name, prefix="asset/")
    return d


def store_asset_version(ctx, wt, name, entry, parent=None, extra_entries=()):
    pid = ctx.row["project_id"]
    adir, odir = wt / "assets" / name, wt / "outputs" / name
    entries = files_of(adir, "asset/")
    for f in sorted((odir / "frames").glob("view_*.jpg")) if (odir / "frames").exists() else []:
        entries.append(("frames/" + f.name, f.read_bytes(), "image/jpeg"))
    if (odir / "chosen_frames.jpg").exists():
        entries.append(("frames/chosen_frames.jpg", (odir / "chosen_frames.jpg").read_bytes(), "image/jpeg"))
    for f in ("raw_mesh.obj", "trellis.glb"):
        if (odir / f).exists():
            entries.append(("mesh/" + f, (odir / f).read_bytes(), media(f)))
    entries += list(extra_entries)
    # preview bundle for the review screen (object next to the SO-100, from the files just built)
    pv = CACHE / "tmp" / ctx.row["id"]
    shutil.rmtree(pv, ignore_errors=True)
    tmp_assets = CACHE / "tmp" / (ctx.row["id"] + "_assets")
    shutil.rmtree(tmp_assets, ignore_errors=True)
    shutil.copytree(adir, tmp_assets / name)
    res = sim_cmd(ctx, "bundle", tmp_assets, [name], pv)
    entries += files_of(pv, "preview/")
    entries.append(("log/pipeline.log", ctx.text.encode(), "text/plain"))
    props = json.loads((adir / "properties.json").read_text()) if (adir / "properties.json").exists() else {}
    avid = new_id("av")
    ctx.write(f"\nstoring {len(entries)} files in SpacetimeDB (512 KiB chunks, read-back verified)…\n")
    mid, files = db.store_dir(ctx.uid, pid, "asset", avid, "1", entries, deps=dict(commit=ctx.row.get("commit"), mujoco=res["engine"]["mujoco"]))
    sources = [dict(id=u["id"], filename=u["filename"], sha256=u["sha256"], size=u["size"]) for u in ctx.input.get("_uploads", [])]
    db.call("insert_asset_version", uid=ctx.uid, id=avid, project_id=pid, object_name=name, status="needs_review",
            measurements_json=json.dumps(ctx.input.get("measurements") or {}), properties_json=json.dumps(props),
            params_json=json.dumps(dict(commit=ctx.row.get("commit"), objects_entry=entry, pipeline_answers=ctx.answers,
                                        tags=ctx.input.get("tags"), engine=res["engine"], model_hash=res["model_hash"],
                                        estimates_reviewed=bool(ctx.input.get("estimates_reviewed")))),
            sources_json=json.dumps(sources), run_id=ctx.row["id"], parent_id=parent or "", manifest_id=mid)
    shutil.rmtree(pv, ignore_errors=True)
    shutil.rmtree(tmp_assets, ignore_errors=True)
    return avid, len(files)



# ── checkpoints / recovery helpers (backend/checkpoints.py) ───────────────────
def admit_new():
    """Fail closed for NEW processing jobs while the maintenance hold is enabled (reads, uploads, cancel stay open)."""
    h = CK.hold_state()
    if h:
        raise web.HTTPServiceUnavailable(text=json.dumps({
            "error": "RealiSIM is in a maintenance hold: new processing jobs are paused. Your captures, inputs and saved results are kept. "
                     + (h.get("reason") or ""), "code": "maintenance_hold", "since": h.get("at")}), content_type="application/json")


def ack_required(rec):
    return web.HTTPConflict(text=json.dumps({"error": "This run was interrupted while a paid mesh request was in flight and no mesh was saved. "
                                             "Repeating it may use GPU quota again: confirm with acknowledge_provider_repeat: true.",
                                             "code": "ack_required", "recovery": rec}), content_type="application/json")


def preserve_partial(pid, wt, name):
    """Never delete the only partial result: an existing unsaved mesh output is moved aside (newest 3 kept)."""
    odir = wt / "outputs" / name
    if odir.exists() and any((odir / f).exists() for f in ("raw_mesh.obj", "trellis.glb")):
        dest = PROJECTS / pid / "partials" / f"{name}-{int(time.time() * 1000)}"
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(odir), str(dest))
        for old in sorted(dest.parent.glob(f"{name}-*"))[:-3]:
            shutil.rmtree(old, ignore_errors=True)
    shutil.rmtree(odir, ignore_errors=True)
    shutil.rmtree(wt / "assets" / name, ignore_errors=True)


def frame_files(odir):
    out = []
    for f in sorted((odir / "frames").glob("view_*.jpg")) if (odir / "frames").exists() else []:
        out.append(("frames/" + f.name, f.read_bytes(), "image/jpeg"))
    if (odir / "chosen_frames.jpg").exists():
        out.append(("frames/chosen_frames.jpg", (odir / "chosen_frames.jpg").read_bytes(), "image/jpeg"))
    return out


def mesh_ckpt_files(odir):
    out = [("mesh/" + f, (odir / f).read_bytes(), media(f)) for f in ("raw_mesh.obj", "trellis.glb") if (odir / f).exists()]
    return out + frame_files(odir)


def save_asset_ckpt(ctx, wt, name, entry, avid):
    try:
        pid = ctx.row["project_id"]
        av = asset_version(pid, avid)
        rm = wt / "outputs" / name / "raw_mesh.obj"
        msha = CK.sha_bytes(rm.read_bytes()) if rm.exists() else ""
        files = {f["path"]: f["sha256"] for f in db.manifest_files(av["manifest_id"])}
        CK.save(ctx, "asset", CK.asset_fp(msha, entry, name, wt),
                refs=dict(asset_version=dict(id=avid, manifest_id=av["manifest_id"], files=files)),
                meta=dict(object_name=name, mesh_sha256=msha, status=av["status"]))
    except Exception as e:
        ctx.write(f"   ↳ asset checkpoint not saved ({type(e).__name__}: {str(e)[:200]}); the asset version itself is stored\n")


def finalize_from_checkpoint(ctx, stage):
    """Resume shortcut: this run already saved a verified checkpoint for `stage`; finish without redoing work."""
    pid, rid = ctx.row["project_id"], ctx.row["id"]
    m, doc, _ = CK.find_valid(ctx.uid, pid, stage, None, run_id=rid, read_bytes=False)
    if not m:
        return False
    refs, rt = doc.get("refs") or {}, ctx.runtime()
    rt.update(resumed_from_stage=stage, checkpoint_id=m["id"])
    note = f"restored from verified checkpoint {m['id']}"
    keys = {"asset": ("validate", "frames", "mesh", "restore", "asset"), "scene": ("review", "scene", "simulate"),
            "export": ("assemble", "install", "check", "persist", "verify")}[stage]
    for st in ctx.stages:
        if st["key"] in keys and st["status"] not in ("done", "skipped"):
            ctx.stage(st["key"], "done", note, restored=True)
    if stage == "asset":
        av = asset_version(pid, refs["asset_version"]["id"])
        ctx.output.update(asset_version_id=av["id"])
        ctx.stage("review", "waiting", "Review the reconstruction and its properties")
        ctx.push(force=True, status="needs_review")
    elif stage == "scene":
        ctx.output.update(scene_version_id=refs["scene_version"]["id"])
        if (doc.get("meta") or {}).get("result") != "pass":
            raise RuntimeError("Simulation validation failed (restored result). The scene version is kept for inspection.")
        ctx.push(force=True, status="complete", finished_at=now_ms())
    else:
        ctx.output.update(export_id=refs["export"]["id"])
        ctx.push(force=True, status="complete", finished_at=now_ms())
    ctx.write(f"\n[resumed: {stage} already saved ({m['id']}); no work repeated]\n")
    return True


def run_asset(ctx):
    inp, pid, name = ctx.input, ctx.row["project_id"], ctx.input["object_name"]
    if finalize_from_checkpoint(ctx, "asset"):
        return
    ctx.stage("validate", "running")
    ups = []
    for upid in inp["upload_ids"]:
        u = upload_row(pid, upid)
        if not u or u["status"] != "ready":
            raise RuntimeError(f"upload {upid} is not ready")
        import workflow
        src = workflow.ensure_source(pid, ctx.uid, upid)
        if not src:
            raise RuntimeError(f"the source file of {u['filename']} is unavailable; upload it again")
        if not (u.get("meta_json") or {}).get("source_file_id"):
            u = persist_uploaded_capture(ctx.uid, u)
            if u["status"] != "ready":
                raise RuntimeError(u.get("error") or "source persistence failed")
        ups.append((u, src))
    inp["_uploads"] = [dict(id=u["id"], filename=u["filename"], sha256=u["sha256"], size=u["size"]) for u, _ in ups]
    ctx.stage("validate", "done", f"{len(ups)} file(s) decoded")
    videos = any(src.suffix.lower() in VIDEO_EXT for _, src in ups)
    with LOCK:
        wt, commit = ensure_worktree(pid)
        ctx.row["commit"] = commit
        shas = [u["sha256"] for u, _ in ups]
        mfp = CK.mesh_fp(shas, wt)
        ck_m, ck_doc, rejected = CK.find_valid(ctx.uid, pid, "mesh", mfp)
        for r in rejected:
            ctx.write(f"   ↳ checkpoint {r['checkpoint_id']} not used: {r['reason']}\n")
        if not ck_m:
            rt = ctx.runtime()
            own = [c for c in CK.for_run(pid, ctx.row["id"]) if c.get("stage") == "mesh"]
            expected = bool(rejected) or bool(own) or rt.get("resumed_from_stage") == "mesh"
            if expected and not CK.ack_current(ctx):
                rt.update(recovery_state="checkpoint_invalid", requires_ack=True,
                          checkpoint_rejections=(rejected or [dict(checkpoint_id=c["checkpoint_id"], reason="not compatible with these inputs/algorithm") for c in own])[:6])
                raise RuntimeError("A saved mesh exists for this work but could not be verified (corrupt, unavailable or from an older "
                                   "pipeline revision), so no new mesh request was started. Confirm to regenerate the mesh "
                                   "(it may use GPU quota again) or restore the saved checkpoint first.")
        preserve_partial(pid, wt, name)
        odir = wt / "outputs" / name
        entry = objects_entry(inp["measurements"], inp.get("tags"))
        objs = json.loads((wt / "objects.json").read_text())
        objs[name] = entry
        (wt / "objects.json").write_text(json.dumps(objs, indent=2))
        inputs = []
        for i, (u, src) in enumerate(ups):
            link = PROJECTS / pid / "inputs" / f"{i + 1:02d}_{u['id']}{src.suffix}"
            link.parent.mkdir(parents=True, exist_ok=True)
            if not link.exists():
                link.symlink_to(src)
            inputs.append(str(link))
        if ck_m:
            odir.mkdir(parents=True, exist_ok=True)
            n = CK.restore(ctx.uid, pid, ck_doc, odir, "mesh/")
            CK.restore(ctx.uid, pid, ck_doc, odir / "frames", "frames/")
            if (odir / "frames" / "chosen_frames.jpg").exists():
                (odir / "frames" / "chosen_frames.jpg").rename(odir / "chosen_frames.jpg")
            if not (odir / "raw_mesh.obj").exists():
                raise RuntimeError("the verified mesh checkpoint has no raw_mesh.obj")
            ctx.runtime().update(resumed_from_stage="mesh", checkpoint_id=ck_m["id"])
            ctx.write(f"Mesh restored from verified checkpoint {ck_m['id']} ({n} file(s), hashes re-checked): no new TRELLIS request.\n")
            if videos:   # frame selection is not repeated for video inputs (same rule as a rebuild from a stored mesh)
                ctx.stage("frames", "done", "restored from checkpoint", restored=True)
            ctx.stage("mesh", "done", f"restored from verified checkpoint {ck_m['id']} (no new GPU request)", restored=True, checkpoint_id=ck_m["id"])
            ctx.stage("asset" if videos else "frames", "running")
            code = run_proc(ctx, [PY, "-u", "make_asset.py", name, *([] if videos else inputs), "--skip-mesh"], wt,
                            {4: "asset", 5: "asset"} if videos else {1: "frames", 2: "frames", 4: "asset", 5: "asset"})
        else:
            px, hfst = hf_proxy()
            ctx.write("Hugging Face access for TRELLIS: " + ("your token, via a Mana proxy (the token never reaches the worker)" if px else
                      "no token connected; using the anonymous quota") + "\n")

            def boundary(key):
                if key == "mesh" and videos and (odir / "frames").exists():
                    ff = frame_files(odir)
                    if ff:
                        CK.save(ctx, "frames", CK.frames_fp(shas, wt), files=ff, meta=dict(object_name=name, inputs=shas,
                                note="selected frames; saved result, not a resume point"))
                if key == "asset" and (odir / "raw_mesh.obj").exists():
                    CK.save(ctx, "mesh", mfp, files=mesh_ckpt_files(odir),
                            meta=dict(object_name=name, inputs=shas, algo=CK.algo_hashes("mesh", wt), views=4))
            ctx.stage("frames", "running")
            code = run_proc(ctx, [PY, "-u", "photo2grasp.py", "asset", name, *inputs], wt, ASSET_STAGE_MAP,
                            extra_env={"P2G_HF_PROXY": json.dumps(px)} if px else None, on_stage=boundary, provider_may_run=True)
        if code != 0 or not (wt / "assets" / name / f"{name}.xml").exists():
            q = re.search(r"exceeded your ZeroGPU quota.*?Try again in ([0-9:]+)", ctx.text)
            if q:
                px = hf_proxy()[0]
                raise RuntimeError(f"Hugging Face GPU quota for TRELLIS is used up (resets in {q.group(1)}). "
                                   + ("Your token's daily quota is exhausted; try later." if px else
                                      "Connect a free Hugging Face token in Settings for your own daily GPU quota, then retry."))
            raise RuntimeError(pipeline_error(ctx.text, code))
        for k in ("frames", "mesh"):
            if next(st for st in ctx.stages if st["key"] == k)["status"] != "done":
                ctx.stage(k, "done")
        ctx.stage("asset", "running", "storing the asset version")
        avid, n = store_asset_version(ctx, wt, name, entry)
        ctx.output.update(asset_version_id=avid)
        save_asset_ckpt(ctx, wt, name, entry, avid)
    ctx.stage("asset", "done", f"{n} files stored and verified")
    ctx.stage("review", "waiting", "Review the reconstruction and its properties")
    ctx.push(force=True, status="needs_review")


def run_rebuild(ctx):
    inp, pid = ctx.input, ctx.row["project_id"]
    if finalize_from_checkpoint(ctx, "asset"):
        return
    parent = asset_version(pid, inp["parent_id"])
    name = parent["object_name"]
    ctx.stage("restore", "running")
    with LOCK:
        wt, commit = ensure_worktree(pid)
        ctx.row["commit"] = commit
        odir = wt / "outputs" / name
        preserve_partial(pid, wt, name)
        odir.mkdir(parents=True)
        n = db.restore_manifest(ctx.uid, pid, parent["manifest_id"], odir, prefix="mesh/")
        db.restore_manifest(ctx.uid, pid, parent["manifest_id"], odir / "frames", prefix="frames/")
        if (odir / "frames" / "chosen_frames.jpg").exists():
            (odir / "frames" / "chosen_frames.jpg").rename(odir / "chosen_frames.jpg")
        if not (odir / "raw_mesh.obj").exists():
            raise RuntimeError("the parent version has no stored mesh to rebuild from")
        tags = inp.get("tags") or (parent["params_json"] or {}).get("tags")
        inp["tags"] = tags
        inp["_uploads"] = parent.get("sources_json") or []
        entry = objects_entry(inp["measurements"], tags)
        objs = json.loads((wt / "objects.json").read_text())
        objs[name] = entry
        (wt / "objects.json").write_text(json.dumps(objs, indent=2))
        ctx.stage("restore", "done", f"{n} mesh file(s) from version {parent['version']}")
        ctx.stage("asset", "running")
        code = run_proc(ctx, [PY, "-u", "make_asset.py", name, "--skip-mesh"], wt, {4: "asset", 5: "asset"})
        if code != 0 or not (wt / "assets" / name / f"{name}.xml").exists():
            raise RuntimeError("\n".join([l for l in ctx.text.splitlines() if l.strip()][-10:]))
        avid, nf = store_asset_version(ctx, wt, name, entry, parent=parent["id"])
        ctx.output.update(asset_version_id=avid)
        save_asset_ckpt(ctx, wt, name, entry, avid)
    ctx.stage("asset", "done", f"{nf} files stored and verified")
    ctx.stage("review", "waiting", "Review the rebuilt asset")
    ctx.push(force=True, status="needs_review")


def run_scene(ctx):
    pid = ctx.row["project_id"]
    if finalize_from_checkpoint(ctx, "scene"):
        return
    av = asset_version(pid, ctx.output["asset_version_id"])
    name = av["object_name"]
    ctx.stage("review", "done", "accepted")
    ctx.stage("scene", "running")
    adir = restore_asset(ctx.uid, pid, av)
    out = CACHE / "tmp" / (ctx.row["id"] + "_scene")
    shutil.rmtree(out, ignore_errors=True)
    b = sim_cmd(ctx, "bundle", adir, [name], out)
    svid = new_id("sv")
    mid, files = db.store_dir(ctx.uid, pid, "scene", svid, "1", files_of(out, ""), deps=dict(engine=b["engine"], model_hash=b["model_hash"]))
    ctx.stage("scene", "done", f"SO-100 + {name} · MuJoCo {b['engine']['mujoco']}")
    ctx.stage("simulate", "running", "drop test")
    tout = CACHE / "tmp" / (ctx.row["id"] + "_traj")
    shutil.rmtree(tout, ignore_errors=True)
    s = sim_cmd(ctx, "simulate", adir, [name], tout, ["--seed", "0", "--x", b["places"][name][0], "--y", b["places"][name][1], "--yaw", 0])
    ok = s["result"] == "pass"
    met = s["metrics"][name]
    validation = dict(result=s["result"], test="drop test: lifted 5 cm, released next to the SO-100, 3 s", metrics=s["metrics"],
                      engine=b["engine"], model_hash=b["model_hash"], start=s["start"])
    db.call("insert_scene_version", uid=ctx.uid, id=svid, project_id=pid, asset_version_ids=json.dumps([av["id"]]),
            robot_profile=project_for(ctx.uid, pid)["robot_profile"], params_json=json.dumps(dict(places=b["places"], commit=ctx.row.get("commit"))),
            validation_json=json.dumps(validation), manifest_id=mid, run_id=ctx.row["id"], status="validated" if ok else "validation_failed")
    simid = store_sim(ctx.uid, pid, svid, tout, s, seed=0, controls=dict(x=b["places"][name][0], y=b["places"][name][1], yaw=0, lift=0.05, source="validation"))
    ctx.output.update(scene_version_id=svid)
    try:
        CK.save(ctx, "scene", CK.fingerprint("scene", asset_version=av["id"], algo=CK.algo_hashes("scene")),
                refs=dict(scene_version=dict(id=svid, manifest_id=mid, files={f["path"]: f["sha256"] for f in files}),
                          simulation=dict(id=simid)),
                meta=dict(object_name=name, result=s["result"], asset_version_id=av["id"]))
    except Exception as e:
        ctx.write(f"   ↳ scene checkpoint not saved ({type(e).__name__}: {str(e)[:200]}); the scene version itself is stored\n")
    shutil.rmtree(out, ignore_errors=True)
    if not ok:
        raise RuntimeError(f"Simulation validation failed: {name} " + ("tipped over" if not met["upright"] else "did not come to rest")
                           + f" (tilt {met['tilt_deg']}°, speed {met['speed_mps']} m/s). The scene version is kept for inspection.")
    ctx.stage("simulate", "done", f"upright, settled · tilt {met['tilt_deg']}°")
    ctx.push(force=True, status="complete", finished_at=now_ms())


def store_sim(uid, pid, svid, tout, s, seed, controls):
    simid = new_id("sim")
    mid, _ = db.store_dir(uid, pid, "trajectory", simid, "1", files_of(tout, ""))
    db.call("insert_simulation_run", uid=uid, id=simid, project_id=pid, scene_version_id=svid, mode="drop-test", seed=float(seed),
            controls_json=json.dumps(controls), metrics_json=json.dumps(s["metrics"]), result=s["result"], manifest_id=mid,
            engine_json=json.dumps(dict(s["engine"], start=s.get("start"))))
    shutil.rmtree(tout, ignore_errors=True)
    return simid


# ── queries ────────────────────────────────────────────────────────────────────
JSONCOLS = ("measurements_json", "properties_json", "params_json", "sources_json", "review_json", "asset_version_ids",
            "validation_json", "controls_json", "metrics_json", "engine_json", "dependency_json", "files_json",
            "stages_json", "input_json", "output_json", "meta_json", "settings_json")


def rows(table, pid):
    return [pj(r, *[c for c in JSONCOLS if c in r]) for r in db.sql(f"SELECT * FROM {table} WHERE project_id = {q(pid)}")]


def one(table, pid, rid):
    r = db.sql(f"SELECT * FROM {table} WHERE id = {q(rid)}")
    if not r or r[0]["project_id"] != pid:
        raise jerr(404, f"{table} not found")
    return pj(r[0], *[c for c in JSONCOLS if c in r[0]])


def asset_version(pid, avid):
    return one("asset_version", pid, avid)


def scene_version(pid, svid):
    return one("scene_version", pid, svid)


# ── export ─────────────────────────────────────────────────────────────────────
def put_export(uid, ex, **ch):
    ex.update(ch)
    db.call("put_export", uid=uid, id=ex["id"], project_id=ex["project_id"], scene_version_id=ex["scene_version_id"],
            asset_version_id=ex["asset_version_id"], package_version=ex["package_version"], env_id=ex["env_id"],
            status=ex["status"], validation_json=json.dumps(ex.get("validation") or {}),
            dependency_json=json.dumps(ex.get("deps") or {}), manifest_id=ex.get("manifest_id") or "",
            archive_file_id=ex.get("archive_file_id") or "", run_id=ex["run_id"])


def run_export(ctx):
    pid, inp = ctx.row["project_id"], ctx.input
    if finalize_from_checkpoint(ctx, "export"):
        return
    prior = db.sql(f"SELECT * FROM gym_export WHERE id = {q(inp['export_id'])}")
    if prior and prior[0]["status"] == "ready" and prior[0]["project_id"] == pid:   # crash after the export was verified
        ctx.output.update(export_id=inp["export_id"])
        for st in ctx.stages:
            if st["status"] not in ("done", "skipped"):
                ctx.stage(st["key"], "done", "export was already verified and ready", restored=True)
        ctx.push(force=True, status="complete", finished_at=now_ms())
        return
    sv = scene_version(pid, inp["scene_version_id"])
    av = asset_version(pid, sv["asset_version_ids"][inp.get("object_index", 0)])
    name = av["object_name"]
    ex = dict(id=inp["export_id"], project_id=pid, scene_version_id=sv["id"], asset_version_id=av["id"],
              package_version=inp["package_version"], env_id=inp["env_id"], status="building", run_id=ctx.row["id"], validation={})
    put_export(ctx.uid, ex)
    try:
        if sv["status"] != "validated" or av["status"] != "accepted":
            raise RuntimeError("export needs an accepted asset version and a validated scene version")
        ctx.stage("assemble", "running")
        adir = restore_asset(ctx.uid, pid, av) / name
        work = DATA / "exports" / ex["id"]
        shutil.rmtree(work, ignore_errors=True)
        work.mkdir(parents=True)
        (work / "entry.json").write_text(json.dumps((av["params_json"] or {}).get("objects_entry") or {}))
        eng = (sv.get("validation_json") or {}).get("engine") or {}
        (work / "prov.json").write_text(json.dumps(dict(
            repo_url=REPO_URL, commit=(av["params_json"] or {}).get("commit"), robot_profile=sv["robot_profile"],
            robot_profile_note=ROBOT_NOTE, asset_version=av["version"], asset_version_id=av["id"], scene_version=sv["version"],
            scene_version_id=sv["id"], mujoco=eng.get("mujoco"))))
        r = subprocess.run(NICE + [PY, str(ADAPTER / "gym_export.py"), "--repo", str(REPO), "--asset-dir", str(adir), "--name", name,
                                   "--objects-entry", str(work / "entry.json"), "--out", str(work / "out"), "--version", ex["package_version"],
                                   "--env-id", ex["env_id"], "--provenance", str(work / "prov.json")],
                           capture_output=True, text=True, timeout=600, cwd="/tmp")
        ctx.write(r.stdout[-3000:] + r.stderr[-3000:])
        if "EXPORT_RESULT" not in r.stdout:
            raise RuntimeError("package assembly failed: " + (r.stdout + r.stderr).strip()[-600:])
        res = json.loads(r.stdout.split("EXPORT_RESULT ")[1].splitlines()[0])
        archive, pkg = Path(res["archive"]), Path(res["package_dir"])
        if archive.stat().st_size > MAX_PACKAGE:
            raise RuntimeError(f"package is {archive.stat().st_size / 1e6:.1f} MB, above the {MAX_PACKAGE / 1e6:.0f} MB limit")
        package_meta = {k: res.get(k) for k in ("module_name", "archive_root", "archive_name", "dist", "env_id", "manifest_path", "smoke_module", "brand") if res.get(k) is not None}
        archive_root = res.get("archive_root") or "photo2grasp_env"
        ex["deps"] = dict(res["dependencies"], python=">=3.10,<3.13", optional=res["optional"], **package_meta)
        ex["validation"]["package"] = package_meta
        ctx.stage("assemble", "done", f"{res['file_count']} files · {archive.stat().st_size / 1e6:.1f} MB archive")
        ctx.check()
        ctx.stage("install", "running")
        venv = VALIDATE / ex["id"]
        shutil.rmtree(venv, ignore_errors=True)
        env = dict(os.environ)
        env.pop("VIRTUAL_ENV", None)
        env.pop("PYTHONPATH", None)
        t0 = time.time()
        code, out, err = sh(f"uv venv {venv} --python 3.11 -q && uv pip install --python {venv}/bin/python '{archive}'", cwd="/tmp", timeout=1200, env=env)
        ctx.write(out[-2000:] + err[-2000:] + "\n")
        if code:
            raise RuntimeError("pip install in a clean environment failed: " + err[-500:])
        ex["validation"]["install"] = dict(ok=True, seconds=round(time.time() - t0, 1), python="3.11", fresh_venv=True)
        ctx.stage("install", "done", f"clean venv, {time.time() - t0:.0f} s")
        ctx.stage("check", "running")
        r = subprocess.run([str(venv / "bin" / "python"), str(pkg / "scripts" / "smoke_test.py")], capture_output=True, text=True,
                           timeout=900, cwd="/tmp", env=env)
        ctx.write(r.stdout[-3000:] + r.stderr[-1500:])
        smoke = json.loads(r.stdout.split("SMOKE_RESULT ")[1].splitlines()[0]) if "SMOKE_RESULT" in r.stdout else dict(ok=False, error=r.stderr[-500:])
        ex["validation"]["smoke"] = smoke
        if not smoke.get("ok"):
            raise RuntimeError("environment check failed: " + smoke.get("error", "unknown error"))
        ctx.stage("check", "done", f"check_env passed · {smoke.get('steps')} steps · {smoke.get('step_ms')} ms/step")
        shutil.rmtree(venv, ignore_errors=True)
        ctx.check()
        ctx.stage("persist", "running")
        entries = [(archive_root + "/" + p.relative_to(pkg).as_posix(), p.read_bytes(), media(p)) for p in sorted(pkg.rglob("*")) if p.is_file()]
        entries.append(("archive/" + archive.name, archive.read_bytes(), "application/zip"))
        mid, files = db.store_dir(ctx.uid, pid, "gym_package", ex["id"], ex["package_version"], entries, deps=ex["deps"])
        arch = next(f for f in files if f["path"].startswith("archive/"))
        ctx.stage("persist", "done", f"{len(files)} files, {sum(f['size'] for f in files) / 1e6:.1f} MB")
        ctx.stage("verify", "running")
        meta, data = db.read_bytes(ctx.uid, pid, arch["id"])
        z = zipfile.ZipFile(io.BytesIO(data))
        man = z.read(res.get("manifest_path") or archive_root + "/MANIFEST.sha256").decode().split("\n")
        stored = {f["path"]: f["sha256"] for f in files}
        checked = 0
        for line in filter(None, man):
            h, rel = line.split("  ", 1)
            if hashlib.sha256(z.read(archive_root + "/" + rel)).hexdigest() != h or stored.get(archive_root + "/" + rel) != h:
                raise RuntimeError(f"hash mismatch after read-back: {rel}")
            checked += 1
        ex["validation"]["persistence"] = dict(ok=True, files=len(files), manifest_lines=checked, archive_sha256=arch["sha256"])
        ctx.stage("verify", "done", f"{checked} manifest hashes match the stored bytes")
        put_export(ctx.uid, ex, status="ready", manifest_id=mid, archive_file_id=arch["id"])
        ctx.output.update(export_id=ex["id"])
        try:
            CK.save(ctx, "export", CK.fingerprint("export", export_id=ex["id"], scene_version=sv["id"], asset_version=av["id"],
                                                  package_version=ex["package_version"], algo=CK.algo_hashes("export")),
                    refs=dict(export=dict(id=ex["id"], manifest_id=mid, files={arch["path"]: arch["sha256"]})),
                    meta=dict(object_name=name, env_id=ex["env_id"], archive_sha256=arch["sha256"]))
        except Exception as e:
            ctx.write(f"   ↳ export checkpoint not saved ({type(e).__name__}: {str(e)[:200]}); the verified export itself is ready\n")
        ctx.push(force=True, status="complete", finished_at=now_ms())
        shutil.rmtree(work, ignore_errors=True)
    except Exception as e:
        ex["validation"]["error"] = str(e)[:800]
        try:
            put_export(ctx.uid, ex, status="failed")
        except Exception:
            pass
        raise


# ── tabletop-scene capture (beta) ──────────────────────────────────────────────
def ensure_scene_deps(ctx):
    with DEPS_LOCK:                          # never two concurrent installs into the shared venv
        _ensure_scene_deps(ctx)


def _ensure_scene_deps(ctx):
    code, _, _ = sh([PY, "-c", "import pycolmap, open3d"])
    if code:
        ctx.write("installing pycolmap and open3d for scene reconstruction (first time only)…\n")
        env = dict(os.environ, VIRTUAL_ENV=str(ROOT / ".venv"))
        code, out, err = sh(["uv", "pip", "install", "pycolmap==4.2.1", "open3d"], cwd=ROOT, timeout=1800, env=env)
        if code:
            raise RuntimeError("could not install the scene reconstruction dependencies: " + err[-400:])


def run_scene_capture(ctx):
    inp, pid = ctx.input, ctx.row["project_id"]
    wt, commit = ensure_worktree(pid)
    ctx.row["commit"] = commit
    work = wt / "outputs" / f"scene_{ctx.row['id']}"
    if inp.get("_next") == "build":
        return run_scene_capture_build(ctx, wt, work)
    ctx.stage("validate", "running")
    u = upload_row(pid, inp["upload_ids"][0])
    import workflow
    src = workflow.ensure_source(pid, ctx.uid, u["id"]) if u else None
    if not u or u["status"] != "ready" or not src:
        raise RuntimeError("the scene video is not ready")
    if not (u.get("meta_json") or {}).get("source_file_id"):
        u = persist_uploaded_capture(ctx.uid, u)
        if u["status"] != "ready":
            raise RuntimeError(u.get("error") or "source persistence failed")
    ctx.stage("validate", "done")
    ensure_scene_deps(ctx)
    t = inp["table"]
    with LOCK:
        ctx.stage("frames", "running")
        if run_proc(ctx, [PY, "-u", "scene/reconstruct.py", str(src), str(work)], wt):
            raise RuntimeError("COLMAP could not reconstruct the scene: film slowly around the table with plenty of overlap")
        ctx.stage("frames", "done")
        ctx.stage("objects", "running")
        if run_proc(ctx, [PY, "-u", "scene/find_objects.py", str(work), "--table", str(t["length_cm"]), str(t["width_cm"]), str(t["height_cm"])], wt):
            raise RuntimeError("could not find the table plane, its scale or the objects: the floor below the table must be visible for metric scale")
        ctx.stage("objects", "done")
        ctx.stage("views", "running")
        if run_proc(ctx, [PY, "-u", "scene/object_views.py", str(work)], wt):
            raise RuntimeError("could not crop object views")
    scene = json.loads((work / "scene.json").read_text())
    objs = []
    entries = []
    for o in scene.get("objects", []):
        views = sorted((work / "objects" / o["name"]).glob("view_*.jpg"))
        for v in views:
            entries.append((f"views/{o['name']}/{v.name}", v.read_bytes(), "image/jpeg"))
        objs.append(dict(name=o["name"], height_m=o.get("height"), x=o.get("x"), y=o.get("y"), views=len(views)))
    mid, _ = db.store_dir(ctx.uid, pid, "scene_views", ctx.row["id"], "1", entries) if entries else (None, None)
    ctx.output.update(objects=objs, views_manifest_id=mid, table=scene.get("table"))
    ctx.stage("views", "done", f"{len(objs)} object(s) found")
    ctx.stage("review", "waiting", "Give the mass, material and friction of each object")
    ctx.push(force=True, status="needs_review")


def run_scene_capture_build(ctx, wt, work):
    pid, inp = ctx.row["project_id"], ctx.input
    ctx.stage("review", "done")
    info = {}
    for o in inp["objects"]:
        m = o["measurements"]
        info[o["detected"]] = dict(name=o["name"], mass_kg=float(m["mass_g"]["value"]) / 1000, material=m.get("material", {}).get("value") or "",
                                   friction=[float(m["friction"]["value"]), 0.005, 0.0001],
                                   source="RealiSIM: mass " + m["mass_g"]["source"] + ", friction " + m["friction"]["source"])
    (work / "info.json").write_text(json.dumps(info, indent=2))
    with LOCK:
        ctx.stage("mesh", "running")
        if run_proc(ctx, [PY, "-u", "scene/build_scene.py", str(work)], wt):
            raise RuntimeError("mesh generation failed for one of the objects")
        ctx.stage("mesh", "done")
        avids = []
        for o in inp["objects"]:
            name = o["name"]
            adir = wt / "assets" / name
            props = json.loads((adir / "properties.json").read_text())
            avid = new_id("av")
            mid, _ = db.store_dir(ctx.uid, pid, "asset", avid, "1", files_of(adir, "asset/"))
            meas = dict(o["measurements"], height_cm=dict(value=round(props["size_m"][2] * 100, 2), source="measured", note="from the scan scale"))
            db.call("insert_asset_version", uid=ctx.uid, id=avid, project_id=pid, object_name=name, status="accepted",
                    measurements_json=json.dumps(meas), properties_json=json.dumps(props),
                    params_json=json.dumps(dict(commit=ctx.row.get("commit"), objects_entry=dict(props, tags=None), scene_capture=True)),
                    sources_json=json.dumps(inp.get("_uploads") or []), run_id=ctx.row["id"], parent_id="", manifest_id=mid)
            avids.append(avid)
    ctx.stage("scene", "running")
    raise RuntimeError("tabletop scene assembly in the browser viewer is not validated yet: the per-object assets were stored as accepted versions; "
                       "use object capture for the Gymnasium export")


# ── Robotics workspace: robots + workcells (additive extension; see backend-extension-contract.json) ─────────────────────────
import robots as ROBOTS  # noqa: E402

ROBOT_TOOL = ADAPTER / "robot_tool.py"
WORKCELL_PY = ADAPTER / "workcell.py"
WORKCELL_EXPORT_PY = ADAPTER / "workcell_export.py"
DETECT_MODEL = "claude-sonnet-5"
EXT_KINDS = ("detect_robot", "import_robot", "workcell", "workcell_export")
TABLE_SOURCES = ("measured", "supplied", "estimated", "unknown")
PLACE_SOURCES = ("manual", "reconstructed", "default", "copilot", "measured", "estimated")
OBS_NOTE = ("Observations are simulator state (robot joints and exact object poses read from MuJoCo). They are not "
            "deployment-ready sensor observations; a real deployment needs a validated perception/state-estimation mapping.")
FIDELITY_NOTE = ("The smoke test checks the Gymnasium API contract (spaces, seeding, reset/step, close). It does not establish "
                 "real-world fidelity of the physics, geometry or control.")
STAGES.update({
    "detect_robot": [("frames", "Frame sampling"), ("detect", "Robot detection (Claude vision)"), ("persist", "Store evidence")],
    "import_robot": [("verify", "Source, commit and license check"), ("compile", "Compile validation (MuJoCo)"),
                     ("persist", "Store model in SpacetimeDB"), ("verify_store", "Read back and verify")],
    "workcell": [("restore", "Restore robot and assets"), ("compose", "Compose workcell"), ("simulate", "Drop test"),
                 ("persist", "Store in SpacetimeDB")],
    "workcell_export": [("assemble", "Assemble package"), ("install", "Install in clean environment"),
                        ("check", "Gymnasium env checker + smoke test"), ("persist", "Store in SpacetimeDB"),
                        ("verify", "Read back and verify hashes")],
})


def tool(args, marker, timeout=900):
    """Run an adapter script in the repo environment; return its one-line JSON result (ok or not)."""
    r = subprocess.run(NICE + [PY, *map(str, args)], capture_output=True, text=True, timeout=timeout, cwd="/tmp")
    if marker not in r.stdout:
        raise RuntimeError(f"{Path(str(args[0])).name} failed: " + (r.stderr or r.stdout).strip()[-800:])
    return json.loads(r.stdout.split(marker + " ")[1].splitlines()[0])


def file_map(files, prefix):
    return {f["path"][len(prefix):]: f["id"] for f in files if f["path"].startswith(prefix)}


def _num(v, name, lo, hi):
    try:
        x = float(v)
    except (TypeError, ValueError):
        raise jerr(422, f"{name} must be a number")
    if not math.isfinite(x) or not lo <= x <= hi:
        raise jerr(422, f"{name} must be between {lo} and {hi}")
    return x


def norm_table(t):
    if not isinstance(t, dict):
        raise jerr(422, "table {length_m, width_m, height_m, source} is required (measure it; metres)")
    src = str(t.get("source") or "unknown")
    if src not in TABLE_SOURCES:
        raise jerr(422, "table.source must be one of " + ", ".join(TABLE_SOURCES))
    out = dict(length_m=_num(t.get("length_m"), "table.length_m", 0.2, 4.0), width_m=_num(t.get("width_m"), "table.width_m", 0.2, 4.0),
               height_m=_num(t.get("height_m"), "table.height_m", 0.01, 1e6), source=src, note=str(t.get("note") or "")[:300])
    if t.get("pose") is not None:
        pose = t["pose"]
        if not isinstance(pose, dict):
            raise jerr(422, "table.pose must contain x_m, y_m and yaw_deg in the robot-base frame")
        out["pose"] = {"x_m": _num(pose.get("x_m"), "table.pose.x_m", -4, 4),
                       "y_m": _num(pose.get("y_m"), "table.pose.y_m", -4, 4),
                       "yaw_deg": _num(pose.get("yaw_deg"), "table.pose.yaw_deg", -360, 360)}
    return out


def norm_placements(pl, names=None, strict=True):
    out = {}
    for n, p in (pl or {}).items():
        if names is not None and n not in names:
            if strict:
                raise jerr(422, f"placement for {n!r}, which is not one of the selected objects")
            continue
        if not isinstance(p, dict):
            raise jerr(422, f"placement {n}: expected {{x_m, y_m, yaw_deg, source}}")
        src = str(p.get("source") or "manual")
        if src not in PLACE_SOURCES:
            raise jerr(422, f"placement {n}: source must be one of " + ", ".join(PLACE_SOURCES))
        out[n] = dict(x_m=_num(p.get("x_m"), f"{n}.x_m", -3, 3), y_m=_num(p.get("y_m"), f"{n}.y_m", -3, 3),
                      yaw_deg=_num(p.get("yaw_deg", 0), f"{n}.yaw_deg", -180, 180), source=src)
    return out


def validate_workcell_settings(wc):
    if not isinstance(wc, dict):
        raise jerr(422, "settings.workcell must be an object")
    out = dict(wc)
    if wc.get("table") is not None:
        out["table"] = norm_table(wc["table"])
    if wc.get("placements") is not None:
        for n in wc["placements"]:
            if not SLUG.match(str(n)):
                raise jerr(422, f"placement key {n!r} is not an object name")
        out["placements"] = norm_placements(wc["placements"])
    return out


def restore_robot(uid, pid, robot):
    d = CACHE / "robots" / robot["robot_import_id"]
    with _restore_lock("robot:" + robot["robot_import_id"]):
        if not (d / "scene.xml").exists():
            shutil.rmtree(d, ignore_errors=True)
            n = db.restore_manifest(uid, pid, robot["manifest_id"], d, prefix="robot/")
            if not n or not (d / "scene.xml").exists():
                raise RuntimeError("the robot model files could not be restored from SpacetimeDB")
    return d


ARM_NAME_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_]{0,39}$")
POSE_SOURCES_ARM = ("origin", "measured", "default", "video_reconstructed", "video_measured", "video_estimated", "given", "manual")


def pinned_robot_import(pid, import_run_id, library_id=None):
    """Resolve an arm's model from a COMPLETED import run on the server (never trust client manifest ids)."""
    run = one("pipeline_run", pid, str(import_run_id or ""))
    ri = (run.get("output_json") or {}).get("robot_import")
    if run["kind"] != "import_robot" or run["status"] != "complete" or not ri:
        raise jerr(422, f"robot import {import_run_id} is not a completed import")
    if library_id and ri["library_id"] != library_id:
        raise jerr(422, f"robot import {import_run_id} is {ri['library_id']}, not {library_id}")
    return dict(library_id=ri["library_id"], name=ri["name"], import_run_id=run["id"], robot_import_id=ri["id"], manifest_id=ri["manifest_id"],
                commit=ri["commit"], license=ri["license"], model_hash=ri.get("model_hash"), stats=ri.get("stats"))


def norm_arm_instances(pid, arms):
    """Validated multi-arm request: >= 2 arms, unique names, every model pinned to a completed import, optional pose in
    the primary base frame (None = provisional default slot). No count cap; adapter resource limits fail explicitly."""
    if not isinstance(arms, list) or len(arms) < 2:
        raise jerr(422, "robots must list two or more arms (one arm uses the confirmed robot)")
    out, seen, pins = [], set(), {}
    for i, a in enumerate(arms):
        if not isinstance(a, dict):
            raise jerr(422, f"robots[{i}] must be an object")
        n = str(a.get("name") or "")
        if not ARM_NAME_RE.match(n) or n in seen:
            raise jerr(422, f"robots[{i}].name {n!r} must be unique, start with a letter (letters, digits, _)")
        seen.add(n)
        key = str(a.get("import_run_id") or "")
        pins[key] = pins.get(key) or pinned_robot_import(pid, key, a.get("library_id"))
        pose = a.get("pose")
        if i == 0:
            pose = None
        elif pose is not None:
            if not isinstance(pose, dict):
                raise jerr(422, f"robots[{i}].pose must be {{x_m, y_m, yaw_deg}} in the primary base frame")
            pose = dict(x_m=_num(pose.get("x_m"), f"{n}.x_m", -3, 3), y_m=_num(pose.get("y_m"), f"{n}.y_m", -3, 3),
                        yaw_deg=_num(pose.get("yaw_deg", 0), f"{n}.yaw_deg", -360, 360), z_m=_num(pose.get("z_m", 0) or 0, f"{n}.z_m", -0.5, 0.5))
        src = str(a.get("pose_source") or ("origin" if i == 0 else ("given" if pose else "default")))
        if src not in POSE_SOURCES_ARM:
            raise jerr(422, f"robots[{i}].pose_source must be one of " + ", ".join(POSE_SOURCES_ARM))
        if pose is None and i:
            src = "default"
        out.append(dict(pins[key], arm=n, label=str(a.get("label") or "")[:80] or None, pose=pose, pose_source="origin" if i == 0 else src))
    return out


def arms_arg(uid, pid, arms):
    """--robots JSON for the adapters; one restored model folder per unique import (shared by same-model arms)."""
    dirs = {}
    for a in arms:
        if a["robot_import_id"] not in dirs:
            dirs[a["robot_import_id"]] = str(restore_robot(uid, pid, a))
    return json.dumps([dict(name=a["arm"], robot_dir=dirs[a["robot_import_id"]], library_id=a["library_id"], label=a.get("label"),
                            pose=a.get("pose"), pose_source=a.get("pose_source")) for a in arms])


def assets_dir(uid, pid, avs, key):
    d = CACHE / "tmp" / (key + "_assets")
    shutil.rmtree(d, ignore_errors=True)
    d.mkdir(parents=True)
    for av in avs:
        src = restore_asset(uid, pid, av) / av["object_name"]
        (d / av["object_name"]).symlink_to(src)
    return d


DETECT_PROMPT = """You are checking whether a video of a robot workcell shows one of these supported robot arm models.
Supported catalog (use ONLY these ids):
{catalog}

Rules:
- If no robot arm is clearly visible, set robot_visible to false and return no candidates.
- If a robot arm is visible but you cannot tell which model it is, or it is not in the catalog, return no candidates
  (or only low-confidence ones) and describe it in unlisted_robot.
- Never invent ids, URLs, downloads or code. Base reasons only on what is visible in the frames.
- SO-100 and SO-101 look very similar; unless a clear distinguishing feature is visible, do not give either "high".
- score is your own rough confidence 0..1; it is not a calibrated probability.
Reply with JSON only, no prose:
{{"robot_visible": bool, "abstain_reason": string, "candidates": [{{"library_id": string, "confidence": "high"|"medium"|"low",
"score": number, "reasons": [string], "visible_cues": [string]}}], "unlisted_robot": string|null}}"""


def claude_detect(paths):
    import base64
    import requests
    import mana_router
    cat = "\n".join(f"- {r['id']}: {r['name']} by {r['maker']}; {r['dof']} DOF; gripper: {r['gripper']}; cues: {'; '.join(r['cues'])}"
                    for r in ROBOTS.CATALOG)
    content = [{"type": "image", "source": {"type": "base64", "media_type": "image/jpeg", "data": base64.b64encode(Path(p).read_bytes()).decode()}}
               for p in paths]
    content.append({"type": "text", "text": DETECT_PROMPT.format(catalog=cat)})
    an = mana_router.provider("anthropic")
    r = requests.post(an.url + "/v1/messages", headers={**an.headers, "anthropic-version": "2023-06-01"}, allow_redirects=False, timeout=240,
                      json={"model": DETECT_MODEL, "max_tokens": 1500, "messages": [{"role": "user", "content": content}]})
    if r.status_code == 402:
        raise RuntimeError("out of Mana credits for robot detection")
    if r.status_code != 200:
        raise RuntimeError(f"robot detection request failed (HTTP {r.status_code}): {r.text[:300]}")
    text = "".join(b.get("text", "") for b in r.json().get("content", []) if b.get("type") == "text")
    m = re.search(r"\{.*\}", text, re.S)
    try:
        raw = json.loads(m.group(0)) if m else None
    except Exception:
        raw = None
    return text, raw


URL_RE = re.compile(r"(https?://|www\.)\S+", re.I)


def clean_detection(raw):
    """Keep only catalog ids and short textual reasons; drop URLs/anything else. Abstain unless evidence is usable."""
    if not isinstance(raw, dict):
        return dict(abstained=True, abstain_reason="the detector did not return a usable answer", candidates=[], unlisted_robot=None)
    txt = lambda s, n=300: URL_RE.sub("[link removed]", str(s))[:n]
    cands, seen = [], set()
    for c in raw.get("candidates") or []:
        if not isinstance(c, dict):
            continue
        lid = c.get("library_id") or c.get("catalog_id")
        if lid not in ROBOTS.BY_ID or lid in seen:
            continue
        seen.add(lid)
        try:
            score = min(1.0, max(0.0, float(c.get("score"))))
        except (TypeError, ValueError):
            score = None
        conf = c.get("confidence") if c.get("confidence") in ("high", "medium", "low") else "low"
        cands.append(dict(library_id=lid, catalog_id=lid, name=ROBOTS.BY_ID[lid]["name"], confidence=conf, score=score,
                          reasons=[txt(x) for x in (c.get("reasons") or [])[:5]], visible_cues=[txt(x, 160) for x in (c.get("visible_cues") or [])[:6]]))
    order = {"high": 0, "medium": 1, "low": 2}
    cands.sort(key=lambda c: (order[c["confidence"]], -(c["score"] or 0)))
    cands = cands[:3]
    visible = bool(raw.get("robot_visible"))
    unlisted = txt(raw["unlisted_robot"]) if raw.get("unlisted_robot") else None
    if not visible:
        return dict(abstained=True, abstain_reason=txt(raw.get("abstain_reason") or "no robot arm is visible in the sampled frames"),
                    candidates=[], unlisted_robot=None)
    if not cands:
        return dict(abstained=True, abstain_reason=txt(raw.get("abstain_reason") or "a robot is visible but it does not match a supported model"),
                    candidates=[], unlisted_robot=unlisted)
    return dict(abstained=False, abstain_reason="", candidates=cands, unlisted_robot=unlisted)


def run_detect_robot(ctx):
    pid, inp = ctx.row["project_id"], ctx.input
    work = CACHE / "tmp" / ctx.row["id"]
    shutil.rmtree(work, ignore_errors=True)
    try:
        ctx.stage("frames", "running")
        u = upload_row(pid, inp["upload_ids"][0])
        src = next((UPLOADS / u["id"]).glob("source.*"), None) if u else None
        if not u or u["status"] != "ready" or not src:
            raise RuntimeError("the capture is not ready on the worker; upload it again")
        fr = tool([ROBOT_TOOL, "frames", "--src", src, "--n", int(inp.get("frames") or 4), "--out", work, "--max", 1024], "ROBOT_RESULT")
        if not fr.get("ok"):
            raise RuntimeError(fr.get("error") or "frame sampling failed")
        ctx.stage("frames", "done", f"{len(fr['frames'])} frame(s) from {u['filename']}")
        ctx.check()
        ctx.stage("detect", "running", DETECT_MODEL)
        text, raw = claude_detect(fr["frames"])
        det = clean_detection(raw)
        ctx.write(f"detector answer: {json.dumps(det)[:1500]}\n")
        ctx.stage("detect", "done", "abstained: " + det["abstain_reason"] if det["abstained"] else
                  "candidates: " + ", ".join(f"{c['library_id']} ({c['confidence']})" for c in det["candidates"]))
        ctx.stage("persist", "running")
        entries = [(f"frames/{Path(p).name}", Path(p).read_bytes(), "image/jpeg") for p in fr["frames"]]
        entries.append(("response.json", json.dumps(dict(model=DETECT_MODEL, raw_text=text, parsed=raw, cleaned=det,
                                                         upload_id=u["id"], upload_sha256=u.get("sha256"))).encode(), "application/json"))
        mid, files = db.store_dir(ctx.uid, pid, "robot_detection", ctx.row["id"], "1", entries, deps=dict(model=DETECT_MODEL))
        ctx.output.update(det, requires_confirmation=True, model=DETECT_MODEL, evidence_manifest_id=mid, frames=file_map(files, ""),
                          upload_id=u["id"], confidence_note="Scores are the model's self-reported confidence, not calibrated probabilities. "
                          "Confirm the actual model of your robot before importing.")
        ctx.stage("persist", "done", f"{len(files)} files verified")
        ctx.push(force=True, status="complete", finished_at=now_ms())
    finally:
        shutil.rmtree(work, ignore_errors=True)


def run_import_robot(ctx):
    pid, inp = ctx.row["project_id"], ctx.input
    lid = inp["library_id"]
    work = CACHE / "tmp" / ctx.row["id"]
    shutil.rmtree(work, ignore_errors=True)
    rbid = new_id("rbt")
    try:
        ctx.stage("verify", "running")
        st = tool([ROBOT_TOOL, "stage", "--id", lid, "--out", work / "robot"], "ROBOT_RESULT")
        if not st.get("ok"):
            raise RuntimeError(st.get("error") or "robot source verification failed")
        facts = st["facts"]
        ctx.stage("verify", "done", f"{facts['license']} · Menagerie {facts['commit'][:7]} · {len(st['files'])} files · {st['bytes'] / 1e6:.1f} MB")
        ctx.check()
        ctx.stage("compile", "running")
        cr = tool([WORKCELL_PY, "robot", "--robot-dir", work / "robot", "--out", work / "preview"], "WORKCELL_RESULT")
        if not cr.get("ok"):
            raise RuntimeError(cr.get("error") or "the robot model did not compile")
        stats = cr["stats"]
        ctx.stage("compile", "done", f"nq {stats['nq']} · nu {stats['nu']} · reach ≈ {stats['reach_m']} m")
        ctx.check()
        ctx.stage("persist", "running")
        entries = files_of(work / "robot", "robot/") + files_of(work / "preview", "preview/")
        entries.append(("import.json", json.dumps(dict(facts, stats=stats, files=st["files"], model_hash=cr["model_hash"])).encode(), "application/json"))
        mid, files = db.store_dir(ctx.uid, pid, "robot_model", rbid, "1", entries,
                                  deps=dict(library_id=lid, commit=facts["commit"], license=facts["license"], source=facts["repo"]))
        ctx.stage("persist", "done", f"{len(files)} files, {sum(f['size'] for f in files) / 1e6:.1f} MB")
        ctx.stage("verify_store", "running")
        back = CACHE / "robots" / rbid
        shutil.rmtree(back, ignore_errors=True)
        db.restore_manifest(ctx.uid, pid, mid, back, prefix="robot/")
        cr2 = tool([WORKCELL_PY, "robot", "--robot-dir", back, "--out", work / "preview2"], "WORKCELL_RESULT")
        if not cr2.get("ok") or cr2["model_hash"] != cr["model_hash"]:
            shutil.rmtree(back, ignore_errors=True)
            raise RuntimeError("the robot model read back from SpacetimeDB does not compile to the same model")
        ctx.stage("verify_store", "done", "restored copy compiles to the identical model")
        entry = ROBOTS.BY_ID[lid]
        ctx.output["robot_import"] = dict(id=rbid, library_id=lid, catalog_id=lid, name=entry["name"], maker=entry["maker"], commit=facts["commit"],
                                          license=facts["license"], source=facts["source"], repo=facts["repo"], manifest_id=mid,
                                          bytes=st["bytes"], file_count=len(st["files"]), stats=stats, preview=file_map(files, "preview/"),
                                          model_hash=cr["model_hash"], detection_run_id=inp.get("detection_run_id") or None, note=entry["note"],
                                          confirmed=False)
        ctx.push(force=True, status="complete", finished_at=now_ms())
    finally:
        shutil.rmtree(work, ignore_errors=True)


def run_workcell(ctx):
    pid, inp, uid = ctx.row["project_id"], ctx.input, ctx.uid
    robot = inp["robot"]
    arms = inp.get("robots") or None
    key = ctx.row["id"]
    work = CACHE / "tmp" / key
    shutil.rmtree(work, ignore_errors=True)
    try:
        ctx.stage("restore", "running")
        rdir = restore_robot(uid, pid, robot)
        robots_json = arms_arg(uid, pid, arms) if arms else None
        avs = [asset_version(pid, a) for a in inp["asset_version_ids"]]
        if any(av["status"] != "accepted" for av in avs):
            raise RuntimeError("every object must be an accepted asset version")
        adir = assets_dir(uid, pid, avs, key)
        names = [av["object_name"] for av in avs]
        ctx.stage("restore", "done", (f"{len(arms)} arms ({', '.join(a['arm'] + ': ' + a['library_id'] for a in arms)})" if arms else robot['name']) + f" + {', '.join(names)}")
        t = inp["table"]
        pl = inp.get("placements") or {}
        common = (["--robots", robots_json] if arms else ["--robot-dir", rdir]) + ["--assets", adir, "--names", ",".join(names),
                  "--table", f"{t['length_m']},{t['width_m']},{t['height_m']}"]
        if t.get("pose") is not None:
            common += ["--table-pose", json.dumps(t["pose"])]
        if pl:
            common += ["--places", json.dumps({n: [p["x_m"], p["y_m"], p["yaw_deg"]] for n, p in pl.items()})]
        ctx.stage("compose", "running")
        b = tool([WORKCELL_PY, "bundle", *common, "--out", work / "preview"], "WORKCELL_RESULT")
        if not b.get("ok"):
            raise RuntimeError(b.get("error") or "the workcell did not compose")
        if arms:
            got = [r["name"] for r in b.get("robots") or []]
            if got != [a["arm"] for a in arms]:
                raise RuntimeError(f"the composed scene has arms {got}, expected {[a['arm'] for a in arms]}")
            # pin every arm's resolved base pose (defaults included) so simulation and export use the identical scene
            res_pose = {r["name"]: r for r in b["robots"]}
            arms = [dict(a, pose=None if i == 0 else {k: res_pose[a["arm"]]["base_pose"][k] for k in ("x_m", "y_m", "yaw_deg", "z_m")},
                         pose_source=res_pose[a["arm"]]["pose_source"]) for i, a in enumerate(arms)]
            robots_json = arms_arg(uid, pid, arms)
            common[1] = robots_json
        resolved = {n: dict(x_m=v[0], y_m=v[1], yaw_deg=v[2] if len(v) > 2 else 0.0, source=(pl.get(n) or {}).get("source", "default"))
                    for n, v in b["places"].items()}
        ctx.stage("compose", "done", f"{len(names)} object(s) on a {t['length_m']:.2f} × {t['width_m']:.2f} m table" +
                  (f" · {len(b['warnings'])} warning(s)" if b.get("warnings") else ""))
        for w_ in b.get("warnings") or []:
            ctx.write("warning: " + w_ + "\n")
        ctx.check()
        ctx.stage("simulate", "running")
        seed = int(inp.get("seed") or 0)
        places_arg = json.dumps({n: [p["x_m"], p["y_m"], p["yaw_deg"]] for n, p in resolved.items()})
        base_args = common[:-2] if pl else common     # resolved placements replace the requested ones
        s = tool([WORKCELL_PY, "simulate", *base_args, "--places", places_arg, "--seed", 0, "--out", work / "traj"], "WORKCELL_RESULT")
        if not s.get("ok"):
            raise RuntimeError(s.get("error") or "the drop test did not run")
        if arms:
            ctx.write("arms simulated: " + ", ".join(f"{n} (max joint drift {v['max_joint_dev_rad']} rad)" for n, v in (s.get("arm_metrics") or {}).items()) + "\n")
        ctx.stage("simulate", "done" if s["result"] == "pass" else "failed",
                  "every object settled upright on the table" if s["result"] == "pass" else
                  "not every object settled upright on the table: " + ", ".join(n for n, m in s["metrics"].items() if not (m["upright"] and m["settled"] and m["on_table"])))
        ctx.stage("persist", "running")
        wcid = new_id("wc")
        record = dict(id=wcid, robot_import_id=robot["robot_import_id"], library_id=robot["library_id"], robot_name=robot["name"],
                      robot_manifest_id=robot["manifest_id"], asset_version_ids=inp["asset_version_ids"], objects=names,
                      table=dict(b["table"], source=t["source"], note=t.get("note", "")), placements=resolved,
                      warnings=b.get("warnings") or [], result=s["result"], metrics=s["metrics"], model_hash=b["model_hash"], seed=seed,
                      engine=b["engine"], robot_stats=b["robot"])
        if t.get("pose") is not None:
            record["table"]["pose"] = t["pose"]
        if arms:
            byn = {r["name"]: r for r in b["robots"]}
            record["robots"] = [dict(name=a["arm"], label=a.get("label"), library_id=a["library_id"], model_name=a["name"], robot_import_id=a["robot_import_id"],
                                     robot_manifest_id=a["manifest_id"], import_run_id=a["import_run_id"], commit=a["commit"], license=a["license"],
                                     model_hash=a.get("model_hash"), primary=i == 0, pose=a.get("pose"), pose_source=a["pose_source"],
                                     base_pose=byn[a["arm"]]["base_pose"], end_effector_body=byn[a["arm"]]["end_effector_body"],
                                     joints=byn[a["arm"]]["joints"], actuators=byn[a["arm"]]["actuators"], action_slice=byn[a["arm"]]["action_slice"],
                                     sim=(s.get("arm_metrics") or {}).get(a["arm"]))
                                for i, a in enumerate(arms)]
            record["primary_robot"] = arms[0]["arm"]
        entries = files_of(work / "preview", "preview/") + files_of(work / "traj", "traj/")
        entries.append(("workcell.json", json.dumps(record, indent=1).encode(), "application/json"))
        mid, files = db.store_dir(uid, pid, "workcell", wcid, "1", entries, deps=dict(robot_manifest_id=robot["manifest_id"],
                                  asset_versions=inp["asset_version_ids"], mujoco=b["engine"]["mujoco"],
                                  **({"robot_manifest_ids": sorted({a["manifest_id"] for a in arms})} if arms else {})))
        record.update(manifest_id=mid, preview=file_map(files, "preview/"), traj=file_map(files, "traj/"))
        ctx.output["workcell"] = record
        ctx.stage("persist", "done", f"{len(files)} files verified")
        ctx.push(force=True, status="complete", finished_at=now_ms())
    finally:
        shutil.rmtree(work, ignore_errors=True)
        shutil.rmtree(CACHE / "tmp" / (key + "_assets"), ignore_errors=True)


def run_workcell_export(ctx):
    pid, inp, uid = ctx.row["project_id"], ctx.input, ctx.uid
    wcrun = one("pipeline_run", pid, inp["workcell_run_id"])
    wc = (wcrun.get("output_json") or {}).get("workcell")
    key = ctx.row["id"]
    work = CACHE / "tmp" / key
    shutil.rmtree(work, ignore_errors=True)
    venv = VALIDATE / key
    ex = dict(id=new_id("wcx"), workcell_run_id=wcrun["id"], workcell_id=wc["id"], package_version=inp["package_version"], env_id=inp["env_id"],
              observation_note=OBS_NOTE, fidelity_note=FIDELITY_NOTE, validation={})
    try:
        ctx.stage("assemble", "running")
        robot = dict(robot_import_id=wc["robot_import_id"], manifest_id=wc["robot_manifest_id"])
        rdir = restore_robot(uid, pid, robot)
        warms = [dict(arm=r["name"], robot_import_id=r["robot_import_id"], manifest_id=r["robot_manifest_id"], library_id=r["library_id"],
                      label=r.get("label"), pose=r.get("pose"), pose_source=r.get("pose_source")) for r in wc.get("robots") or []]
        robots_args = ["--robots", arms_arg(uid, pid, warms)] if len(warms) >= 2 else ["--robot-dir", rdir]
        avs = [asset_version(pid, a) for a in wc["asset_version_ids"]]
        adir = assets_dir(uid, pid, avs, key)
        imp = next((r for r in rows("pipeline_run", pid) if r["kind"] == "import_robot" and
                    ((r.get("output_json") or {}).get("robot_import") or {}).get("id") == wc["robot_import_id"]), None)
        ri = (imp.get("output_json") or {}).get("robot_import") if imp else {}
        prov = dict(project_name=inp.get("project_name"), scene_version=wc["id"], scene_version_id=wc["id"], asset_versions=wc["asset_version_ids"],
                    robot=dict(name=ri.get("name") or wc.get("robot_name"), library_id=wc["library_id"], source=f"MuJoCo Menagerie @ {(ri.get('commit') or '')[:7]}",
                               license=ri.get("license"), note="Simulation model; not hardware-validated."),
                    objects={av["object_name"]: dict(summary=(av.get("properties_json") or {}).get("material") or "") for av in avs})
        if len(warms) >= 2:
            prov["robots"] = [dict(name=r["name"], label=r.get("label"), library_id=r["library_id"], model_name=r.get("model_name"),
                                   source=f"MuJoCo Menagerie @ {(r.get('commit') or '')[:7]}", license=r.get("license"), robot_import_id=r["robot_import_id"],
                                   model_hash=r.get("model_hash")) for r in wc["robots"]]
        work.mkdir(parents=True, exist_ok=True)
        (work / "prov.json").write_text(json.dumps(prov))
        t = wc["table"]
        places = json.dumps({n: [v["x_m"], v["y_m"], v["yaw_deg"]] for n, v in wc["placements"].items()})
        table_pose_args = ["--table-pose", json.dumps(t["pose"])] if t.get("pose") is not None else []
        res = tool([WORKCELL_EXPORT_PY, *table_pose_args, *robots_args, "--assets", adir, "--names", ",".join(wc["objects"]),
                    "--table", f"{t['length_m']},{t['width_m']},{t['height_m']}", "--places", places, "--out", work / "out",
                    "--version", ex["package_version"], "--env-id", ex["env_id"], "--slug", inp["slug"], "--provenance", work / "prov.json"],
                   "EXPORT_RESULT")
        archive, pkg = Path(res["archive"]), Path(res["package_dir"])
        if archive.stat().st_size > MAX_PACKAGE:
            raise RuntimeError(f"package is {archive.stat().st_size / 1e6:.1f} MB, above the {MAX_PACKAGE / 1e6:.0f} MB limit")
        ex.update(module=res["module"], module_name=res.get("module_name", res["module"]), archive_root=res.get("archive_root", res["module"]), brand="RealiSIM", dist=res["dist"], archive_name=archive.name, bytes=archive.stat().st_size,
                  dependencies=dict(res["dependencies"], python=">=3.10,<3.13", optional=res["optional"]), end_effector=res["end_effector"])
        if res.get("robots"):
            got = [r["name"] for r in res["robots"]]
            if got != [r["arm"] for r in warms]:
                raise RuntimeError(f"the exported package has arms {got}, expected {[r['arm'] for r in warms]}")
            ex.update(robots=res["robots"], end_effectors=res["end_effectors"], primary_robot=res["primary_robot"])
        ctx.stage("assemble", "done", f"{res['file_count']} files · {archive.stat().st_size / 1e6:.1f} MB archive")
        ctx.check()
        ctx.stage("install", "running")
        env = dict(os.environ)
        env.pop("VIRTUAL_ENV", None)
        env.pop("PYTHONPATH", None)
        t0 = time.time()
        shutil.rmtree(venv, ignore_errors=True)
        code, out, err = sh(f"uv venv {venv} --python 3.11 -q && uv pip install --python {venv}/bin/python '{archive}'", cwd="/tmp", timeout=1200, env=env)
        ctx.write(out[-1500:] + err[-1500:] + "\n")
        if code:
            raise RuntimeError("pip install in a clean environment failed: " + err[-500:])
        ex["validation"]["install"] = dict(ok=True, seconds=round(time.time() - t0, 1), python="3.11", fresh_venv=True)
        ctx.stage("install", "done", f"clean venv, {time.time() - t0:.0f} s")
        ctx.stage("check", "running")
        r = subprocess.run([str(venv / "bin" / "python"), "-m", f"{res['module']}.scripts.smoke_test"], capture_output=True, text=True,
                           timeout=900, cwd="/tmp", env=env)
        ctx.write(r.stdout[-3000:] + r.stderr[-1500:])
        smoke = json.loads(r.stdout.split("SMOKE_RESULT ")[1].splitlines()[0]) if "SMOKE_RESULT" in r.stdout else dict(ok=False, error=r.stderr[-500:])
        smoke.pop("trace", None)
        ex["validation"]["smoke"] = smoke
        if not smoke.get("ok"):
            raise RuntimeError("environment check failed: " + smoke.get("error", "unknown error"))
        ctx.stage("check", "done", f"check_env passed · {smoke.get('steps')} steps · {smoke.get('step_ms')} ms/step")
        ctx.check()
        ctx.stage("persist", "running")
        entries = [(res["module"] + "/" + q_.relative_to(pkg).as_posix(), q_.read_bytes(), media(q_)) for q_ in sorted(pkg.rglob("*")) if q_.is_file()]
        entries.append(("archive/" + archive.name, archive.read_bytes(), "application/zip"))
        mid, files = db.store_dir(uid, pid, "workcell_package", ex["id"], ex["package_version"], entries,
                                  deps=dict(ex["dependencies"], workcell_id=wc["id"]))
        arch = next(f for f in files if f["path"].startswith("archive/"))
        ctx.stage("persist", "done", f"{len(files)} files, {sum(f['size'] for f in files) / 1e6:.1f} MB")
        ctx.stage("verify", "running")
        _, data = db.read_bytes(uid, pid, arch["id"])
        z = zipfile.ZipFile(io.BytesIO(data))
        stored = {f["path"]: f["sha256"] for f in files}
        checked = 0
        for line in filter(None, z.read(res["module"] + "/MANIFEST.sha256").decode().split("\n")):
            h, rel = line.split("  ", 1)
            path = res["module"] + "/" + rel
            if hashlib.sha256(z.read(path)).hexdigest() != h or stored.get(path) != h:
                raise RuntimeError(f"hash mismatch after read-back: {rel}")
            checked += 1
        ex["validation"]["persistence"] = dict(ok=True, files=len(files), manifest_lines=checked, archive_sha256=arch["sha256"])
        ex.update(manifest_id=mid, archive_file_id=arch["id"], status="ready")
        ctx.output["export"] = ex
        ctx.stage("verify", "done", f"{checked} manifest hashes match the stored bytes")
        ctx.push(force=True, status="complete", finished_at=now_ms())
    except Exception as e:
        ex["validation"]["error"] = str(e)[:800]
        ex["status"] = "failed"
        ctx.output["export"] = ex
        raise
    finally:
        shutil.rmtree(work, ignore_errors=True)
        shutil.rmtree(venv, ignore_errors=True)
        shutil.rmtree(CACHE / "tmp" / (key + "_assets"), ignore_errors=True)


def create_ext_run(uid, p, pid, kind, b):
    """Validate and enqueue the robot/workcell run kinds. Runs act as the project owner, like every other job."""
    st = p["settings_json"] if isinstance(p["settings_json"], dict) else {}
    if kind == "detect_robot":
        ups = [upload_row(pid, u) for u in (b.get("upload_ids") or [])[:1]]
        if not ups or ups[0] is None or ups[0]["status"] != "ready":
            raise jerr(422, "choose one uploaded and validated video or photo of the workcell")
        n = int(_num(b.get("frames", 4), "frames", 2, 6))
        return new_run(p["owner_uid"], pid, kind, dict(upload_ids=[ups[0]["id"]], frames=n, started_by=uid))
    if kind == "import_robot":
        lid = b.get("library_id") or b.get("catalog_id")
        if lid not in ROBOTS.BY_ID:
            raise jerr(422, "choose a supported robot model from GET /api/robots (URLs and files are not accepted)")
        if b.get("user_confirmed_model") is not True:
            raise jerr(422, "confirm that this is the actual model of your robot (user_confirmed_model: true)")
        det = b.get("detection_run_id")
        if det:
            d = one("pipeline_run", pid, det)
            if d["kind"] != "detect_robot":
                raise jerr(422, "detection_run_id is not a robot detection run")
        return new_run(p["owner_uid"], pid, kind, dict(library_id=lid, detection_run_id=det or "", user_confirmed_model=True, started_by=uid))
    if kind == "workcell":
        arms = norm_arm_instances(pid, b["robots"]) if b.get("robots") is not None else None
        robot = st.get("robot")
        if arms:
            robot = dict(arms[0], name=arms[0]["name"])    # primary (legacy single-robot fields stay filled)
        if not isinstance(robot, dict) or not robot.get("manifest_id"):
            raise jerr(422, "import and confirm the robot model first")
        ids = list(dict.fromkeys(b.get("asset_version_ids") or []))
        if not 1 <= len(ids) <= 8:
            raise jerr(422, "choose 1 to 8 accepted object versions")
        avs = [asset_version(pid, a) for a in ids]
        if any(av["status"] != "accepted" for av in avs):
            raise jerr(422, "every object must be an accepted asset version (review it first)")
        names = [av["object_name"] for av in avs]
        if len(set(names)) != len(names):
            raise jerr(422, "two selected versions are the same object; choose one version per object")
        wcs = st.get("workcell") or {}
        table = norm_table(b["table"] if b.get("table") is not None else wcs.get("table"))
        if b.get("placements") is not None:
            pl = norm_placements(b["placements"], names, strict=True)
        else:
            pl = norm_placements(wcs.get("placements") or {}, names, strict=False)
        extra = dict(robots=arms) if arms else {}
        return new_run(p["owner_uid"], pid, kind, dict(robot=robot, asset_version_ids=ids, table=table, placements=pl,
                                                       seed=int(b.get("seed") or 0), started_by=uid, **extra))
    if kind == "workcell_export":
        wr = one("pipeline_run", pid, str(b.get("workcell_run_id") or ""))
        wc = (wr.get("output_json") or {}).get("workcell")
        if wr["kind"] != "workcell" or wr["status"] != "complete" or not wc:
            raise jerr(422, "choose a completed workcell build")
        if wc.get("result") != "pass":
            raise jerr(422, "this workcell did not pass the drop test; adjust placements and rebuild it")
        n = 1 + sum(1 for r in rows("pipeline_run", pid) if r["kind"] == "workcell_export")
        slug = re.sub(r"[^a-z0-9]+", "_", str(p.get("name") or "workcell").lower()).strip("_")[:28] or "workcell"
        if not slug[0].isalpha():
            slug = "w_" + slug
        ver = f"1.0.{n}"
        env_id = f"RealiSIM/Workcell_{slug}-v{n}"
        return new_run(p["owner_uid"], pid, kind, dict(workcell_run_id=wr["id"], package_version=ver, env_id=env_id, slug=slug,
                                                       project_name=p.get("name"), started_by=uid))
    raise jerr(422, "unknown run kind")


async def robot_catalog(request):   # wrapped with T() at route registration (T is defined below)
    uid_of(request)
    return web.json_response(dict(catalog=ROBOTS.public_catalog(), source=dict(repo=ROBOTS.REPO, commit=ROBOTS.COMMIT,
                                  license_allowlist=sorted(ROBOTS.LICENSES), bytes_limit=ROBOTS.MAX_BYTES),
                                  note="Supported simulation models only. Detection suggests candidates; you confirm the actual model before import. "
                                       "None of these models is validated against your physical robot."))


async def set_robot(request):   # wrapped with T() at route registration (T is defined below)
    uid = uid_of(request)
    pid = request.match_info["pid"]
    b = await request.json()
    def work():
        p = project_for(uid, pid, write=True)
        st = p["settings_json"] if isinstance(p["settings_json"], dict) else {}
        if b.get("clear"):
            st.pop("robot", None)
        else:
            if b.get("confirm") is not True:
                raise jerr(422, "confirm: true is required (a person confirms the robot model)")
            run = one("pipeline_run", pid, str(b.get("import_run_id") or ""))
            ri = (run.get("output_json") or {}).get("robot_import")
            if run["kind"] != "import_robot" or run["status"] != "complete" or not ri:
                raise jerr(422, "choose a completed robot import")
            st["robot"] = dict(library_id=ri["library_id"], catalog_id=ri["library_id"], name=ri["name"], import_run_id=run["id"],
                               robot_import_id=ri["id"], manifest_id=ri["manifest_id"], commit=ri["commit"], license=ri["license"],
                               stats=ri["stats"], preview=ri["preview"], confirmed_by=uid, confirmed_at=now_ms(),
                               detection_run_id=ri.get("detection_run_id"))
        db.call("update_project", uid=uid, id=pid, name=p["name"], archived=bool(p["archived"]), settings_json=json.dumps(st))
        return dict(robot=st.get("robot"))
    return web.json_response(await th(work))


async def workcell_view(request):   # wrapped with T() at route registration (T is defined below)
    uid = uid_of(request)
    pid = request.match_info["pid"]
    def work():
        p = project_for(uid, pid)
        st = p["settings_json"] if isinstance(p["settings_json"], dict) else {}
        runs = sorted(rows("pipeline_run", pid), key=lambda r: -(r.get("created_at") or 0))
        by = lambda k: [r for r in runs if r["kind"] == k]
        return dict(robot=st.get("robot"), workcell=st.get("workcell"), detections=by("detect_robot"), imports=by("import_robot"),
                    workcells=by("workcell"), exports=by("workcell_export"), catalog_commit=ROBOTS.COMMIT)
    return web.json_response(await th(work))


# ── worker ─────────────────────────────────────────────────────────────────────
def store_run_log(ctx):
    try:
        if ctx.text.strip():
            db.store_dir(ctx.uid, ctx.row["project_id"], "log", ctx.row["id"], str(ctx.row.get("attempt") or 1),
                         [("run.log", ctx.text.encode(), "text/plain")])
    except Exception as e:
        log("log store failed", e)


def _heartbeat(ctx, stop):
    while not stop.wait(CK.HEARTBEAT_S):
        ctx.push()          # non-forced: writes only when HEARTBEAT_S elapsed since the last durable write


def _lease_taken(ctx):
    """Another live worker instance holds this run (fresh heartbeat): never run it twice."""
    try:
        r = db.sql(f"SELECT status, output_json FROM pipeline_run WHERE id = {q(ctx.row['id'])}")
        if not r or r[0]["status"] != "running":
            return False
        rt = (json.loads(r[0]["output_json"] or "{}").get("runtime") or {})
        return bool(rt.get("worker_instance_id") and rt["worker_instance_id"] != CK.WORKER_ID
                    and now_ms() - float(rt.get("heartbeat_at") or 0) < CK.LEASE_MS)
    except Exception:
        return False


def _pick_job():
    """Call with QCOND held. Scan QUEUE in order for the first runnable rid: its project has no running job, its kind
    is under any configured kind limit and admission passes. Pops exactly that rid and marks it running.
    Returns (rid, ctx, held_list, wait_s): rid None when nothing can start now."""
    held, adm_cache, wait = [], {}, 5.0
    running = STATE["running"]
    busy_projects = set(running.values())
    kind_count = {}
    for r in running:
        c = LIVE.get(r)
        if c:
            kind_count[c.row["kind"]] = kind_count.get(c.row["kind"], 0) + 1
    limits = WORKER_CFG.get("kind_limits") or {}
    for rid in list(QUEUE):
        ctx = LIVE.get(rid)
        if not ctx or ctx.cancelled:
            QUEUE.remove(rid)
            HELD.pop(rid, None)
            continue
        if rid in running or ctx.row["project_id"] in busy_projects:
            continue                                  # same project: serialize behind its running job
        kind = ctx.row["kind"]
        if kind in limits and kind_count.get(kind, 0) >= limits[kind]:
            continue
        if kind not in adm_cache:
            adm_cache[kind] = CK.admission(kind)      # checked before any job memory is allocated (per job)
        adm = adm_cache[kind]
        if adm:
            held.append((rid, ctx, adm))
            continue
        heavy = kind not in LIGHT_KINDS
        if heavy and running:
            since = time.time() - ADMIT["last_heavy_start"]
            if since < HEAVY_STAGGER_S:               # let the last heavy start allocate before re-checking memory
                wait = min(wait, max(0.2, HEAVY_STAGGER_S - since))
                continue
        QUEUE.remove(rid)
        HELD.pop(rid, None)
        running[rid] = ctx.row["project_id"]
        if heavy:
            ADMIT["last_heavy_start"] = time.time()
        _sync_running_compat()
        return rid, ctx, held, wait
    return None, None, held, wait


def _finish_running(rid):
    with QCOND:
        STATE["running"].pop(rid, None)
        _sync_running_compat()
        QCOND.notify_all()                            # a project or kind slot may have been freed


def worker(worker_no=0):
    while True:
        with QCOND:
            while not QUEUE:
                QCOND.wait()
            rid, ctx, held, wait = _pick_job()
            fresh_held = []
            for hrid, hctx, adm in held:              # waiting message once per (run, reason), across all workers
                if HELD.get(hrid) != adm["code"]:
                    HELD[hrid] = adm["code"]
                    fresh_held.append((hctx, adm))
        for hctx, adm in fresh_held:
            try:
                hctx.runtime()["held"] = dict(adm, at=now_ms())
                hctx.write(f"\n[waiting: {adm['reason']}]\n")
                hctx.push(force=True)
            except Exception as e:
                log("held message deferred", type(e).__name__)
        if rid is None:
            with QCOND:
                QCOND.wait(wait)
            continue
        try:
            if _lease_taken(ctx):
                log("lease held by another worker; skipping", rid)
                continue
            _execute(ctx, rid)
        except Exception as e:                        # never let a worker thread die
            traceback.print_exc()
            log("worker", worker_no, "job wrapper error", rid, type(e).__name__)
        finally:
            _finish_running(rid)


def _execute(ctx, rid):
    stop = threading.Event()
    try:
        rt = ctx.runtime()
        rt.pop("held", None)
        rt.update(attempt_started_at=now_ms(), worker_instance_id=CK.WORKER_ID, heartbeat_at=now_ms())
        if rt.get("recovery_state") not in ("resumed",):
            rt["recovery_state"] = "none"
        ctx.push(force=True, status="running", started_at=ctx.row.get("started_at") or now_ms(), error="")
        threading.Thread(target=_heartbeat, args=(ctx, stop), daemon=True).start()
        kind, nxt = ctx.row["kind"], ctx.input.get("_next")
        if kind in ("asset", "rebuild") and nxt == "scene":
            run_scene(ctx)
        elif kind == "asset":
            run_asset(ctx)
        elif kind == "rebuild":
            run_rebuild(ctx)
        elif kind == "export":
            run_export(ctx)
        elif kind == "scene_capture":
            run_scene_capture(ctx)
        elif kind == "detect_robot":
            run_detect_robot(ctx)
        elif kind == "import_robot":
            run_import_robot(ctx)
        elif kind == "workcell":
            run_workcell(ctx)
        elif kind == "workcell_export":
            run_workcell_export(ctx)
    except Cancelled:
        if ctx.row["kind"] == "asset" and (provider_marker_open(ctx) or (
                any(s["key"] == "mesh" and s["status"] == "running" for s in ctx.stages)
                and not any(c.get("stage") == "mesh" for c in ctx.output.get("checkpoints") or []))):
            ctx.runtime().update(recovery_state="provider_outcome_unknown", requires_ack=True, interrupted_stage=ctx.row.get("stage") or "mesh",
                                 provider_charge="possible")
        for s in ctx.stages:
            if s["status"] in ("running", "waiting"):
                CK.apply_timing(s, "failed")
        ctx.write("\n[cancelled]\n")
        ctx.push(force=True, status="failed", error="Cancelled", finished_at=now_ms())
    except Exception as e:
        traceback.print_exc()
        for s in ctx.stages:
            if s["status"] in ("running", "waiting"):
                CK.apply_timing(s, "failed")
        ctx.write(f"\n[failed] {e}\n")
        ctx.push(force=True, status="failed", error=str(e)[:2000], finished_at=now_ms())
    finally:
        stop.set()
        try:
            if ctx.row["status"] in ("complete", "failed"):
                store_run_log(ctx)
        except Exception as e:
            log("run log persistence deferred", rid, type(e).__name__)


def enqueue(ctx):
    rid = ctx.row["id"]
    with QCOND:
        prior = LIVE.get(rid)
        if rid in QUEUE or (prior and prior is not ctx and prior.row.get("status") in ("queued", "running")) or rid in STATE["running"]:
            return False
        ctx.push(force=True, status="queued")
        LIVE[rid] = ctx
        QUEUE.append(rid)
        QCOND.notify_all()
    return True


def new_run(uid, pid, kind, inp):
    admit_new()
    row = dict(id=new_id("run"), project_id=pid, kind=kind, status="queued", stage="", input_json=inp, output_json={},
               commit="", attempt=1, started_at=0, finished_at=0, error="")
    ctx = RunCtx(row, uid)
    enqueue(ctx)
    return row["id"]


def recover_running(ctx):
    """A run found 'running' at startup: its worker is gone. Resume safe work / verified checkpoints automatically
    (bounded by MAX_AUTO_RESUMES); never replay a provider call whose outcome is unknown."""
    pid, rid = ctx.row["project_id"], ctx.row["id"]
    ckpts = CK.for_run(pid, rid)
    dec = CK.classify_interrupted(ctx, ckpts)
    rt = ctx.runtime()
    at = rt.get("heartbeat_at") or rt.get("progress_at")
    for st in ctx.stages:
        if st.get("status") == "running":
            CK.close_interrupted(st, at)
    rt.update(interrupted_stage=dec.get("stage") or None, interrupted_at=at or None, in_flight_stage=None)
    if dec["action"] == "resume":
        for st in ctx.stages:
            if st.get("status") == "running":
                st["status"] = "pending"
                st["detail"] = "interrupted by a worker restart; resuming"
        ctx.row["attempt"] = int(ctx.row.get("attempt") or 1) + 1
        rt.update(recovery_state="resumed", auto_resumes=int(rt.get("auto_resumes") or 0) + 1, resumed_from_stage=dec.get("resume_from"))
        ctx.write(f"\n[worker restarted during {dec.get('stage') or 'this run'}; resuming automatically"
                  + (f" from the verified {dec['resume_from']} checkpoint" if dec.get("resume_from") else " (no paid stage was in flight)") + "]\n")
        enqueue(ctx)
        return "resumed"
    for st in ctx.stages:
        if st.get("status") == "running":
            st["status"] = "failed"
            st["finished_at"] = at or None
    rt.update(recovery_state=dec["state"], requires_ack=dec["state"] == "provider_outcome_unknown")
    ctx.write(f"\n[worker restarted during {dec.get('stage') or 'this run'}; no automatic retry was made]\n")
    ctx.push(force=True, status="failed", error=dec["msg"], finished_at=now_ms())
    store_run_log(ctx)
    return dec["state"]


def recover():
    """Wait for durable state, resume never-started jobs, resume interrupted work only from safe/verified points."""
    STATE["recovery_complete"] = False
    delay = 1
    deferred_once = set()
    while not STATE.get("recovery_complete"):
        try:
            deferred = []
            snapshot = db.sql("SELECT * FROM pipeline_run")
            for r in snapshot:
                if r["status"] not in ("queued", "running"):
                    continue
                rid = r["id"]
                with QCOND:
                    known = rid in LIVE or rid in QUEUE or rid in STATE["running"]
                if known:
                    continue
                r = pj(r, "input_json", "output_json", "stages_json")
                owner = db.sql(f"SELECT owner_uid FROM project WHERE id = {q(r['project_id'])}")
                if not owner:
                    continue
                ctx = RunCtx(r, owner[0]["owner_uid"])
                if r["status"] == "queued":
                    ctx.write("\n[worker restored; resuming this queued job]\n")
                    enqueue(ctx)               # no prior execution, no extra attempt/charge (worker admission still applies)
                    continue
                rt = (r.get("output_json") or {}).get("runtime") or {}
                if (rt.get("worker_instance_id") and rt["worker_instance_id"] != CK.WORKER_ID and rid not in deferred_once
                        and now_ms() - float(rt.get("heartbeat_at") or 0) < CK.LEASE_MS):
                    deferred.append(rid)       # a live lease may still exist; re-check once it has expired
                    continue
                log("recovering interrupted run", rid, recover_running(ctx))
            if deferred:
                deferred_once.update(deferred)
                time.sleep(CK.LEASE_MS / 1000)
                continue
            STATE["recovery_complete"] = True
            STATE["recovery_error"] = None
            log("durable queue recovery complete")
        except Exception as e:
            STATE["recovery_error"] = type(e).__name__
            log("waiting for durable queue recovery", type(e).__name__)
            time.sleep(delay)
            delay = min(15, delay * 2)


# ── HTTP ───────────────────────────────────────────────────────────────────────
def T(fn):
    async def h(request):
        try:
            return await fn(request)
        except web.HTTPException:
            raise
        except StdbError as e:
            msg = str(e)
            raise jerr(403 if "access" in msg else 409, msg.replace("The module instance encountered a fatal error: ", ""))
        except Exception as e:
            traceback.print_exc()
            raise jerr(500, f"{type(e).__name__}: {e}")
    return h


def th(fn, *a, **k):
    return asyncio.to_thread(fn, *a, **k)


async def release_status(request):
    uid_of(request)
    import release_gate
    return web.json_response(release_gate.public_status())


_DB_READY = {"ok": False, "checked_at": 0, "checking": False, "error_kind": None}
_DB_READY_LOCK = threading.Lock()


def database_readiness():
    # A bounded real SQL read, not just HTTP/process liveness. Coalesce health callers.
    with _DB_READY_LOCK:
        if _DB_READY["checking"] or time.time() - _DB_READY["checked_at"] < 5:
            return dict(_DB_READY)
        _DB_READY["checking"] = True
    ok, error_kind = False, None
    try:
        db.sql("SELECT id FROM project LIMIT 1", timeout=(1.5, 3))
        ok = True
    except Exception as exc:
        error_kind = type(exc).__name__
    finally:
        with _DB_READY_LOCK:
            _DB_READY.update(ok=ok, checked_at=time.time(), checking=False, error_kind=error_kind)
    return dict(_DB_READY)


async def health(request):
    d = await th(database_readiness)
    recovered = bool(STATE.get("recovery_complete"))
    ready = bool(d["ok"] and recovered)
    with QCOND:
        n_running, n_queue = len(STATE["running"]), len(QUEUE)
    body = dict(ok=ready, ready=ready, app="RealiSIM", busy=bool(n_running), queue=n_queue,
                running=n_running, workers=WORKER_CFG["workers"],
                commit=(STATE["repo"].get("commit") or {}).get("short"),
                database_ready=bool(d["ok"]), recovery_complete=recovered,
                checked_at=int(d["checked_at"] * 1000), maintenance_hold=bool(CK.hold_state()), runtime_version="realisim-multiarm-2026.10.24.1",
                auto_update=dict(poll_s=POLL["interval_s"], polls=POLL["polls"], last_poll_at=POLL["last_poll_at"]))
    if not ready:
        body.update(code="database_starting" if not d["ok"] else "recovering_saved_jobs",
                    error="RealiSIM is reconnecting to its saved state. Your captures and completed results are preserved; new processing is not ready yet.")
    return web.json_response(body, status=200 if ready else 503,
                             headers={"Cache-Control": "no-store", **({"Retry-After": "5"} if not ready else {})})


@T
async def system(request):
    uid_of(request)
    intro = STATE.get("intro") or {}
    if request.query.get("hf") == "refresh":
        HF_STATE["at"] = 0
    hfs = await th(hf_status)
    return web.json_response(dict(repo=STATE["repo"], stages=STAGES, huggingface=hfs, robot_profiles=ROBOT_PROFILES, robot_note=ROBOT_NOTE,
                                  pipeline_stages=intro.get("stages"), cli=(intro.get("cli") or {}).get("steps"),
                                  limits=dict(upload_bytes=MAX_UPLOAD, package_bytes=MAX_PACKAGE, upload_chunk=UPLOAD_CHUNK, artifact_chunk=512 * 1024,
                                              video_ext=sorted(VIDEO_EXT), image_ext=sorted(IMAGE_EXT)),
                                  storage=dict(system_of_record="SpacetimeDB 2.13 (standalone)", database="photo2grasp",
                                               source_videos="New validated captures and workspace-analysis originals are stored as hash-verified SpacetimeDB manifests; worker copies are a restorable processing cache. Historical uploads persist on use.")))


def project_summary(p):
    pid = p["id"]
    files = db.sql(f"SELECT size FROM artifact_file WHERE project_id = {q(pid)}")
    avs = rows("asset_version", pid)
    svs = rows("scene_version", pid)
    exs = rows("gym_export", pid)
    runs = rows("pipeline_run", pid)
    sims = db.sql(f"SELECT id, created_at, result FROM simulation_run WHERE project_id = {q(pid)}")
    latest = lambda xs: max(xs, key=lambda r: r.get("created_at") or 0) if xs else None
    a, s, e, r, sm = latest(avs), latest(svs), latest(exs), latest(runs), latest(sims)
    thumb = None
    if a:
        f = db.sql(f"SELECT id FROM artifact_file WHERE manifest_id = {q(a['manifest_id'])} AND path = 'frames/view_0.jpg'")
        thumb = f[0]["id"] if f else None
    if not thumb:
        ups = [pj(u, "meta_json") for u in db.sql(f"SELECT meta_json FROM upload WHERE project_id = {q(pid)}")]
        thumb = next(((u["meta_json"] or {}).get("thumb_file_id") for u in ups if isinstance(u.get("meta_json"), dict) and u["meta_json"].get("thumb_file_id")), None)
    return dict(p, storage_bytes=sum(x["size"] for x in files),
                asset=a and dict(id=a["id"], version=a["version"], status=a["status"], object_name=a["object_name"]),
                scene=s and dict(id=s["id"], version=s["version"], status=s["status"]),
                export=e and dict(id=e["id"], package_version=e["package_version"], status=e["status"]),
                run=r and dict(id=r["id"], kind=r["kind"], status=r["status"], stage=r["stage"], progress=r["progress"]),
                sim=sm and dict(sm), thumb_file_id=thumb)


@T
async def list_projects(request):
    uid = uid_of(request)
    def work():
        own = db.sql(f"SELECT * FROM project WHERE owner_uid = {q(uid)}")
        shared = [m["project_id"] for m in db.sql(f"SELECT project_id FROM project_member WHERE uid = {q(uid)}")]
        ps = own + [r for pid in shared for r in db.sql(f"SELECT * FROM project WHERE id = {q(pid)}")]
        out = [project_summary(pj(p, "settings_json")) for p in ps]
        for p in out:
            p["role"] = "owner" if p["owner_uid"] == uid else "member"
        return sorted(out, key=lambda p: -p["updated_at"])
    ps = await th(work)
    return web.json_response(dict(projects=ps, storage_bytes=sum(p["storage_bytes"] for p in ps if p["role"] == "owner")))


@T
async def create_project(request):
    uid = uid_of(request)
    b = await request.json()
    prof = next((p for p in ROBOT_PROFILES if p["id"] == b.get("robot_profile", "so100-menagerie")), None)
    if not prof or not prof["available"]:
        raise jerr(422, "that robot profile is not available")
    pid = new_id("prj")
    await th(db.call, "create_project", uid=uid, id=pid, name=str(b.get("name") or "Untitled workcell")[:120], robot_profile=prof["id"],
             settings_json=json.dumps(dict(units="metric", description=str(b.get("description") or "")[:500])))
    return web.json_response(dict(id=pid))


@T
async def get_project(request):
    uid = uid_of(request)
    pid = request.match_info["pid"]
    def work():
        p = project_for(uid, pid)
        d = dict(project=p, members=rows("project_member", pid) if p["role"] == "owner" else [])
        for t in ("upload", "pipeline_run", "asset_version", "scene_version", "simulation_run", "gym_export", "artifact_manifest"):
            d[t] = sorted(rows(t, pid), key=lambda r: -(r.get("created_at") or 0))
        d["artifact_file"] = [dict(f) for f in db.sql(f"SELECT id, manifest_id, path, size, sha_256, media_type, status, chunk_count FROM artifact_file WHERE project_id = {q(pid)}")]
        for f in d["artifact_file"]:
            f["sha256"] = f.pop("sha256", f.get("sha256"))
        d["storage_bytes"] = sum(f["size"] for f in d["artifact_file"])
        for u in d["upload"]:
            if u["status"] == "uploading":
                u["received"] = received_chunks(u["id"])
        mine = project_running(pid)
        d["busy_run"] = mine[0] if mine else None        # this project's running job (at most one by scheduling)
        d["running_runs"] = mine
        try:
            ck = CK.project_summary(pid)
        except Exception:
            ck = []
        d["checkpoints"] = ck
        for r in d["pipeline_run"]:
            r["recovery"] = CK.run_recovery(r, [c for c in ck if c.get("run_id") == r["id"]])
        h = CK.hold_state()
        d["maintenance"] = dict(hold=bool(h), reason=(h or {}).get("reason"), since=(h or {}).get("at"))
        return d
    return web.json_response(await th(work))


@T
async def patch_project(request):
    uid = uid_of(request)
    pid = request.match_info["pid"]
    b = await request.json()
    def work():
        p = project_for(uid, pid, write=True)
        st = p["settings_json"] if isinstance(p["settings_json"], dict) else {}
        new = dict(b.get("settings") or {})
        if "robot" in new and new["robot"] != st.get("robot"):
            raise jerr(422, "settings.robot is set only by confirming an imported robot (POST /api/projects/{pid}/robot)")
        new.pop("robot", None)
        if new.get("workcell") is not None:
            new["workcell"] = validate_workcell_settings(new["workcell"])
        st.update(new)
        db.call("update_project", uid=uid, id=pid, name=str(b.get("name", p["name"]))[:120],
                archived=bool(b.get("archived", p["archived"])), settings_json=json.dumps(st))
    await th(work)
    return web.json_response(dict(ok=True))


@T
async def delete_project(request):
    uid = uid_of(request)
    pid = request.match_info["pid"]
    def work():
        p = project_for(uid, pid, write=True)
        if p["owner_uid"] != uid:
            raise jerr(403, "only the owner can delete a project")
        if project_running(pid):
            raise jerr(409, "a job of this project is running; cancel it first")
        for m in db.sql(f"SELECT id FROM artifact_manifest WHERE project_id = {q(pid)}"):
            db.delete_manifest(uid, pid, m["id"])
        for f in db.sql(f"SELECT id FROM artifact_file WHERE project_id = {q(pid)}"):
            db.call("delete_file", uid=uid, project_id=pid, file_id=f["id"])
        for u in db.sql(f"SELECT id FROM upload WHERE project_id = {q(pid)}"):
            shutil.rmtree(UPLOADS / u["id"], ignore_errors=True)
        db.call("delete_project", uid=uid, id=pid)
        wt = PROJECTS / pid / "repo"
        if wt.exists():
            sh(["git", "worktree", "remove", "--force", str(wt)])
        shutil.rmtree(PROJECTS / pid, ignore_errors=True)
    await th(work)
    return web.json_response(dict(ok=True))


@T
async def set_member(request):
    uid = uid_of(request)
    pid = request.match_info["pid"]
    b = await request.json()
    await th(db.call, "set_member", uid=uid, project_id=pid, member_uid=str(b["member_uid"]).strip(), role=b.get("role", "viewer"))
    return web.json_response(dict(ok=True))


@T
async def create_upload(request):
    uid = uid_of(request)
    pid = request.match_info["pid"]
    b = await request.json()
    upid = re.sub(r"[^a-zA-Z0-9_-]", "", str(b.get("id") or ""))[:64] or new_id("up")
    ext = Path(b["filename"]).suffix.lower()
    if ext not in VIDEO_EXT | IMAGE_EXT:
        raise jerr(422, f"unsupported file type {ext or '(none)'}: use MP4, MOV, M4V, WebM, AVI, MKV, JPEG, PNG or WebP")
    size = int(b["size"])
    if size <= 0 or size > MAX_UPLOAD:
        raise jerr(422, f"file size must be between 1 byte and {MAX_UPLOAD // 2 ** 20} MB")
    mode = b.get("capture_mode", "object")
    if mode not in ("object", "scene"):
        raise jerr(422, "capture mode must be object or scene")
    def work():
        project_for(uid, pid, write=True)
        prev = upload_row(pid, upid)
        if not prev and b.get("legacy_id"):
            legacy = re.sub(r"[^a-zA-Z0-9_-]", "", str(b["legacy_id"]))[:64]
            prev = upload_row(pid, legacy)
        if prev:  # resume only the same capture in this project, never another user's bytes
            if int(prev["size"]) != size or prev["filename"] != Path(b["filename"]).name[:200] or (b.get("sha256") and prev.get("sha256") and b["sha256"] != prev["sha256"]):
                raise jerr(409, "This upload id belongs to a different capture; select the file again.")
            prev["received"] = received_chunks(prev["id"])
            return prev
        (UPLOADS / upid / "chunks").mkdir(parents=True, exist_ok=True)
        row = dict(id=upid, project_id=pid, capture_mode=mode, filename=Path(b["filename"]).name[:200], media_type=str(b.get("media_type") or "")[:100],
                   size=size, sha256=str(b.get("sha256") or ""), status="uploading", chunk_size=UPLOAD_CHUNK,
                   chunks_total=max(1, math.ceil(size / UPLOAD_CHUNK)), chunks_received=0, meta_json={}, error="")
        put_upload(uid, row)
        row["received"] = []
        return row
    return web.json_response(await th(work))


@T
async def put_chunk(request):
    uid = uid_of(request)
    pid, upid, idx = request.match_info["pid"], request.match_info["upid"], int(request.match_info["idx"])
    row = await th(upload_row, pid, upid)
    if not row:
        raise jerr(404, "upload not found")
    await th(project_for, uid, pid, True)
    if row["status"] not in ("uploading", "failed"):
        raise jerr(409, f"upload is {row['status']}")
    data = await request.read()
    if not 0 <= idx < int(row["chunks_total"]):
        raise jerr(400, "chunk index out of range")
    expect = int(row["chunk_size"]) if idx < int(row["chunks_total"]) - 1 else int(row["size"]) - idx * int(row["chunk_size"])
    if len(data) != expect:
        raise jerr(400, f"chunk {idx} has {len(data)} bytes, expected {expect}")
    sha = request.headers.get("X-Chunk-Sha256", "")
    if sha and hashlib.sha256(data).hexdigest() != sha:
        raise jerr(400, f"chunk {idx} was corrupted in transit; it will be retried")
    d = UPLOADS / upid / "chunks"
    d.mkdir(parents=True, exist_ok=True)
    tmp = d / f".{idx}.tmp"
    tmp.write_bytes(data)
    tmp.replace(d / str(idx))   # atomic: a retried chunk replaces, never duplicates
    return web.json_response(dict(ok=True, idx=idx))


@T
async def complete_upload(request):
    uid = uid_of(request)
    pid, upid = request.match_info["pid"], request.match_info["upid"]
    def work():
        project_for(uid, pid, write=True)
        row = upload_row(pid, upid)
        if not row:
            raise jerr(404, "upload not found")
        if row["status"] == "ready":
            return row
        if (row.get("meta_json") or {}).get("persistence_pending"):
            return persist_uploaded_capture(uid, row)
        return finalize_upload(uid, row)
    return web.json_response(await th(work))


@T
async def get_upload(request):
    uid = uid_of(request)
    pid, upid = request.match_info["pid"], request.match_info["upid"]
    def work():
        project_for(uid, pid)
        row = upload_row(pid, upid)
        if not row:
            raise jerr(404, "upload not found")
        row["received"] = received_chunks(upid)
        return row
    return web.json_response(await th(work))


@T
async def delete_upload(request):
    uid = uid_of(request)
    pid, upid = request.match_info["pid"], request.match_info["upid"]
    def work():
        project_for(uid, pid, write=True)
        if not upload_row(pid, upid):
            raise jerr(404, "upload not found")
        db.call("delete_upload", uid=uid, project_id=pid, id=upid)
        shutil.rmtree(UPLOADS / upid, ignore_errors=True)
    await th(work)
    return web.json_response(dict(ok=True))


@T
async def create_run(request):
    uid = uid_of(request)
    pid = request.match_info["pid"]
    b = await request.json()
    def work():
        p = project_for(uid, pid, write=True)
        kind = b.get("kind", "asset")
        if kind in EXT_KINDS:
            return create_ext_run(uid, p, pid, kind, b)
        ups = [upload_row(pid, u) for u in b.get("upload_ids") or []]
        if not ups or any(u is None or u["status"] != "ready" for u in ups):
            raise jerr(422, "every capture must be uploaded and validated first")
        if kind == "asset":
            name = str(b.get("object_name") or "").strip()
            if not SLUG.match(name):
                raise jerr(422, "object name: start with a letter; lowercase letters, digits and _ (2–40 characters)")
            if name in ((STATE.get("intro") or {}).get("assets") or []):
                raise jerr(422, f"“{name}” is a built-in asset of the repository; choose another name")
            probs = measurement_problems(b.get("measurements") or {})
            if probs:
                raise jerr(422, "measurements needed before processing: " + "; ".join(probs))
            if any(u["capture_mode"] != "object" for u in ups):
                raise jerr(422, "object capture needs object videos or photos")
            inp = dict(upload_ids=[u["id"] for u in ups], object_name=name, measurements=b["measurements"], tags=b.get("tags") or {},
                       estimates_reviewed=bool(b.get("estimates_reviewed")), started_by=uid)
            return new_run(p["owner_uid"], pid, "asset", inp)
        if kind == "scene_capture":
            t = b.get("table") or {}
            try:
                dims = [float(t[k]) for k in ("length_cm", "width_cm", "height_cm")]
            except Exception:
                raise jerr(422, "table length, width and height (cm) are required for scene scale")
            if not all(10 <= d <= 400 for d in dims):
                raise jerr(422, "table dimensions must be between 10 and 400 cm")
            return new_run(p["owner_uid"], pid, "scene_capture", dict(upload_ids=[ups[0]["id"]], table=t, started_by=uid))
        raise jerr(422, "unknown run kind")
    return web.json_response(dict(id=await th(work)))


@T
async def run_action(request):
    uid = uid_of(request)
    pid, rid, action = request.match_info["pid"], request.match_info["rid"], request.match_info["action"]
    b = await request.json() if request.can_read_body else {}
    def work():
        p = project_for(uid, pid, write=True)
        run = one("pipeline_run", pid, rid)
        if action == "cancel":
            ctx = LIVE.get(rid)
            if ctx:
                ctx.cancelled = True
                if ctx.proc and ctx.proc.poll() is None:
                    os.killpg(ctx.proc.pid, signal.SIGTERM)
                with QCOND:
                    if rid in QUEUE:
                        QUEUE.remove(rid)
                        ctx.push(force=True, status="failed", error="Cancelled", finished_at=now_ms())
            elif run["status"] in ("queued", "running", "needs_review"):
                RunCtx(run, p["owner_uid"]).push(force=True, status="failed", error="Cancelled", finished_at=now_ms())
            return dict(ok=True)
        if action in ("retry", "resume"):
            if action == "resume" and run["status"] in ("queued", "running"):
                return dict(id=rid, status=run["status"], deduplicated=True)
            if run["status"] != "failed":
                raise jerr(409, f"only failed runs can be {'retried' if action == 'retry' else 'resumed'}")
            rec = CK.run_recovery(run, CK.for_run(pid, rid))
            ack = b.get("acknowledge_provider_repeat") is True
            if rec["requires_ack"] and not ack:
                raise ack_required(rec)
            admit_new()
            if action == "retry":
                active = [r for r in rows("pipeline_run", pid) if (r.get("input_json") or {}).get("_retry_of") == rid
                          and r["status"] in ("queued", "running")]
                if active:
                    return dict(id=active[0]["id"], deduplicated=True)
                inp = {k: v for k, v in (run["input_json"] or {}).items() if not k.startswith("_")}
                inp["_retry_of"] = rid
                if ack:
                    inp["_provider_repeat_ack"] = dict(by=uid, at=now_ms(), of_run=rid)
                return dict(id=new_run(p["owner_uid"], pid, run["kind"], inp))
            if run["kind"] not in ("asset", "rebuild", "export"):
                raise jerr(409, "this run kind cannot resume; use retry")
            ctx = RunCtx(run, p["owner_uid"])
            for st in ctx.stages:
                if st.get("status") in ("failed", "running", "waiting"):
                    st["status"], st["finished_at"] = "pending", None
            rt = ctx.runtime()
            rt.update(recovery_state="resumed", resumed_by=uid, resumed_at=now_ms(), requires_ack=False,
                      provider_repeat_ack=dict(by=uid, at=now_ms(), attempt=int(run.get("attempt") or 1) + 1) if ack else rt.get("provider_repeat_ack"))
            ctx.row.update(attempt=int(run.get("attempt") or 1) + 1, error="", finished_at=0)
            ctx.write(f"\n[resume requested; continuing from {rec['last_checkpoint_stage'] or 'the start (no verified checkpoint)'}]\n")
            enqueue(ctx)
            return dict(id=rid, status="queued", resume_from=rec["last_checkpoint_stage"])
        if action == "objects":   # scene capture: per-object properties after review
            if run["kind"] != "scene_capture" or run["status"] != "needs_review":
                raise jerr(409, "this run is not waiting for object properties")
            objs = b.get("objects") or []
            for o in objs:
                if not SLUG.match(o.get("name", "")):
                    raise jerr(422, f"object name {o.get('name')!r}: lowercase letters, digits and _")
                pr = [x for x in measurement_problems(dict(o["measurements"], length_cm={"value": 1, "source": "measured"},
                      width_cm={"value": 1, "source": "measured"}, height_cm={"value": 1, "source": "measured"}))]
                if pr:
                    raise jerr(422, f"{o['name']}: " + "; ".join(pr))
            admit_new()
            ctx = RunCtx(run, p["owner_uid"])
            ctx.input.update(objects=objs, _next="build")
            enqueue(ctx)
            return dict(ok=True)
        raise jerr(404, "unknown action")
    return web.json_response(await th(work))


@T
async def run_log(request):
    uid = uid_of(request)
    pid, rid = request.match_info["pid"], request.match_info["rid"]
    off = int(request.query.get("offset", 0))
    await th(project_for, uid, pid)
    ctx = LIVE.get(rid)
    if ctx and ctx.row["project_id"] == pid:
        text = ctx.text
    else:
        p = RUNLOGS / f"{rid}.log"
        text = p.read_text(errors="replace") if p.exists() else ""
    return web.json_response(dict(text=text[off:], offset=len(text), live=bool(ctx)))


@T
async def review_asset(request):
    uid = uid_of(request)
    pid, avid = request.match_info["pid"], request.match_info["avid"]
    b = await request.json()
    def work():
        p = project_for(uid, pid, write=True)
        av = asset_version(pid, avid)
        if av["status"] != "needs_review":
            raise jerr(409, f"this version is already {av['status']}")
        run = one("pipeline_run", pid, av["run_id"])
        action = b.get("action")
        review = dict(by=uid, at=now_ms(), action=action, note=str(b.get("note") or "")[:500])
        if action == "accept":
            probs = measurement_problems(av["measurements_json"] or {})
            if probs:
                raise jerr(422, "fix these before building the scene: " + "; ".join(probs))
            est = [k for k, v in (av["measurements_json"] or {}).items() if isinstance(v, dict) and v.get("source") == "estimated"]
            if est and not b.get("estimates_reviewed"):
                raise jerr(422, "confirm that you reviewed the estimated values: " + ", ".join(est))
            review["estimates_reviewed"] = est
            admit_new()
            db.call("review_asset_version", uid=uid, project_id=pid, id=avid, status="accepted", review_json=json.dumps(review))
            ctx = RunCtx(run, p["owner_uid"])
            ctx.input["_next"] = "scene"
            ctx.output["asset_version_id"] = avid
            enqueue(ctx)
            return dict(ok=True, run_id=run["id"])
        if action == "reject":
            db.call("review_asset_version", uid=uid, project_id=pid, id=avid, status="rejected", review_json=json.dumps(review))
            ctx = RunCtx(run, p["owner_uid"])
            ctx.stage("review", "failed", "rejected")
            ctx.push(force=True, status="failed", error="Rejected in property review", finished_at=now_ms())
            return dict(ok=True)
        if action == "revise":
            meas = b.get("measurements") or {}
            probs = measurement_problems(meas)
            if probs:
                raise jerr(422, "measurements needed: " + "; ".join(probs))
            rid = new_run(p["owner_uid"], pid, "rebuild", dict(parent_id=avid, measurements=meas, tags=b.get("tags"),
                                                                estimates_reviewed=bool(b.get("estimates_reviewed")), started_by=uid))
            review["next_run"] = rid
            db.call("review_asset_version", uid=uid, project_id=pid, id=avid, status="rejected", review_json=json.dumps(dict(review, reason="revised")))
            ctx = RunCtx(run, p["owner_uid"])
            ctx.stage("review", "done", "revised: rebuilt as a new version")
            ctx.push(force=True, status="complete", finished_at=now_ms())
            return dict(ok=True, run_id=rid)
        raise jerr(422, "action must be accept, reject or revise")
    return web.json_response(await th(work))


@T
async def create_sim(request):
    uid = uid_of(request)
    pid = request.match_info["pid"]
    b = await request.json()
    def work():
        p = project_for(uid, pid, write=True)
        sv = scene_version(pid, b["scene_version_id"])
        av = asset_version(pid, sv["asset_version_ids"][0])
        name = av["object_name"]
        seed = int(b.get("seed") or 0)
        extra = ["--seed", seed]
        controls = dict(seed=seed, lift=0.05, source="user")
        if b.get("x") is not None and b.get("y") is not None:
            x, y, yaw = float(b["x"]), float(b["y"]), float(b.get("yaw") or 0)
            if not (-0.3 <= x <= 0.3 and -0.45 <= y <= -0.08):
                raise jerr(422, "initial pose must be in front of the arm (x ±30 cm, y −45…−8 cm)")
            extra += ["--x", x, "--y", y, "--yaw", yaw]
            controls.update(x=x, y=y, yaw=yaw)
        admit_new()
        with SIM_SEM:
            adir = restore_asset(p["owner_uid"], pid, av)
            tout = CACHE / "tmp" / new_id("simrun")
            s = sim_cmd(None, "simulate", adir, [name], tout, extra)
            controls.update(s.get("pose") or {})
            simid = store_sim(p["owner_uid"], pid, sv["id"], tout, s, seed, controls)
        return one("simulation_run", pid, simid)
    return web.json_response(await th(work))


@T
async def create_export(request):
    uid = uid_of(request)
    pid = request.match_info["pid"]
    b = await request.json()
    def work():
        p = project_for(uid, pid, write=True)
        sv = scene_version(pid, b["scene_version_id"])
        if sv["status"] != "validated":
            raise jerr(422, "this scene version did not pass simulation validation")
        av = asset_version(pid, sv["asset_version_ids"][0])
        if av["status"] != "accepted":
            raise jerr(422, "the asset version is not accepted")
        n = 1 + sum(1 for e in rows("gym_export", pid) if e["scene_version_id"] == sv["id"])
        ver = f"1.{sv['version']}.{n}"
        env_id = f"RealiSIM/PickLift-{av['object_name']}-v{sv['version']}"
        exid = new_id("exp")
        rid = new_run(p["owner_uid"], pid, "export", dict(scene_version_id=sv["id"], export_id=exid, package_version=ver, env_id=env_id, started_by=uid))
        return dict(id=exid, run_id=rid, package_version=ver, env_id=env_id)
    return web.json_response(await th(work))


@T
async def get_file(request):
    uid = uid_of(request)
    pid, fid = request.match_info["pid"], request.match_info["fid"]
    await th(project_for, uid, pid)
    f = await th(db.sql, f"SELECT * FROM artifact_file WHERE id = {q(fid)}")
    if not f or f[0]["project_id"] != pid:
        raise jerr(404, "file not found")
    f = f[0]
    if f["status"] != "verified":
        raise jerr(409, "this file has not been verified yet")
    resp = web.StreamResponse(headers={"Content-Type": f["media_type"] or "application/octet-stream", "Content-Length": str(int(f["size"])),
                                       "ETag": '"' + f["sha256"] + '"', "X-Content-SHA256": f["sha256"],
                                       "Cache-Control": "private, max-age=31536000, immutable"})
    if request.query.get("download"):
        resp.headers["Content-Disposition"] = f'attachment; filename="{Path(f["path"]).name}"'
    await resp.prepare(request)
    h = hashlib.sha256()
    owner = (await th(db.sql, f"SELECT owner_uid FROM project WHERE id = {q(pid)}"))[0]["owner_uid"]
    for i in range(int(f["chunk_count"])):
        b = await th(db.read_chunk, owner, pid, fid, i)
        h.update(b)
        await resp.write(b)
    await resp.write_eof()
    return resp


@T
async def manifest_zip(request):
    uid = uid_of(request)
    pid, mid = request.match_info["pid"], request.match_info["mid"]
    prefix = request.query.get("prefix", "")
    name = re.sub(r"[^a-zA-Z0-9_.-]", "_", request.query.get("name", "artifact"))
    def work():
        p = project_for(uid, pid)
        m = one("artifact_manifest", pid, mid)
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
            for f in db.manifest_files(mid):
                if prefix and not f["path"].startswith(prefix):
                    continue
                _, data = db.read_bytes(p["owner_uid"], pid, f["id"])
                z.writestr(f["path"][len(prefix):], data)
        return buf.getvalue()
    data = await th(work)
    return web.Response(body=data, headers={"Content-Type": "application/zip", "Content-Disposition": f'attachment; filename="{name}.zip"'})


@web.middleware
async def cors(request, handler):
    if request.method == "OPTIONS":
        resp = web.Response()
    else:
        try:
            resp = await handler(request)
        except web.HTTPException as e:
            resp = e
    if not isinstance(resp, web.StreamResponse) or not resp.prepared:
        resp.headers["Access-Control-Allow-Origin"] = "*"
        resp.headers["Access-Control-Allow-Headers"] = "Authorization, Content-Type, X-Chunk-Sha256"
        resp.headers["Access-Control-Allow-Methods"] = "GET, POST, PUT, PATCH, DELETE, OPTIONS"
        resp.headers["Access-Control-Expose-Headers"] = "X-Content-SHA256, ETag"
    return resp


def main():
    threading.Thread(target=sync_loop, daemon=True).start()
    for i in range(WORKER_CFG["workers"]):
        threading.Thread(target=worker, args=(i,), name=f"realisim-worker-{i}", daemon=True).start()
    log("job workers", WORKER_CFG["workers"], "kind limits", WORKER_CFG.get("kind_limits") or {})
    threading.Thread(target=recover, daemon=True).start()
    app = web.Application(client_max_size=16 * 1024 * 1024, middlewares=[cors])
    r = app.router
    r.add_get("/health", health)
    r.add_get("/healthz", health)
    r.add_get("/api/system", system)
    r.add_get("/api/release", release_status)
    r.add_get("/api/projects", list_projects)
    r.add_post("/api/projects", create_project)
    r.add_get("/api/projects/{pid}", get_project)
    r.add_patch("/api/projects/{pid}", patch_project)
    r.add_delete("/api/projects/{pid}", delete_project)
    r.add_post("/api/projects/{pid}/members", set_member)
    r.add_post("/api/projects/{pid}/uploads", create_upload)
    r.add_get("/api/projects/{pid}/uploads/{upid}", get_upload)
    r.add_put("/api/projects/{pid}/uploads/{upid}/chunks/{idx}", put_chunk)
    r.add_post("/api/projects/{pid}/uploads/{upid}/complete", complete_upload)
    r.add_delete("/api/projects/{pid}/uploads/{upid}", delete_upload)
    r.add_post("/api/projects/{pid}/runs", create_run)
    r.add_post("/api/projects/{pid}/runs/{rid}/{action}", run_action)
    r.add_get("/api/projects/{pid}/runs/{rid}/log", run_log)
    r.add_post("/api/projects/{pid}/assets/{avid}/review", review_asset)
    r.add_post("/api/projects/{pid}/sims", create_sim)
    r.add_post("/api/projects/{pid}/exports", create_export)
    r.add_get("/api/projects/{pid}/files/{fid}", get_file)
    r.add_get("/api/projects/{pid}/manifests/{mid}/zip", manifest_zip)
    r.add_get("/api/robots", T(robot_catalog))
    r.add_post("/api/projects/{pid}/robot", T(set_robot))
    r.add_get("/api/projects/{pid}/workcell", T(workcell_view))
    import copilot
    copilot.register(app, sys.modules[__name__])
    import workflow
    workflow.register(app, sys.modules[__name__])
    import support
    support.register(app, sys.modules[__name__])
    import public_beta
    public_beta.register(app, sys.modules[__name__])
    r.add_route("OPTIONS", "/{tail:.*}", lambda req: web.Response())
    log("RealiSIM pipeline backend on", PORT)
    web.run_app(app, host="127.0.0.1", port=PORT, print=None)


if __name__ == "__main__":
    main()
