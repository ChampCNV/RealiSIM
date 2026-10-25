"""support: RealiSIM help / history / troubleshooting backend.

Canonical state lives in SpacetimeDB only: artifact manifests of kind 'support', one immutable, read-back-verified JSON
entry per fact (case, job, job_event, resolution, audit). A job's state is a fold over its entries; each manifest's
dependency_json carries {type,id,job_id,case_id,event,status,at,...} so the queue is derivable with SQL alone.

Elasticsearch (elastic_index.py) is a derived index over canonical rows and pinned official repo docs. It is rebuilt or
reconciled from SpacetimeDB at any time; every hit is revalidated against SpacetimeDB for the caller before it is
returned. Retrieval is BM25 + exact error fingerprints (not semantic).

Factory investigations are executed by a separate worker (droid_support.py / support-worker.py, run as an MPM node so
it receives the owner's Factory credential context). This module never sees that key. The worker reaches loopback-only
/internal/support/* routes (token file + no ingress forwarding headers), claims one job at a time under a durable lease,
and may only use the scoped tools below. Proposals are validated by copilot.build_proposal and stored as a copilot turn,
so the existing human approval flow applies them; nothing is applied automatically.

    import support; support.register(app, sys.modules[__name__])   # after copilot.register
"""
import asyncio, fcntl, hashlib, hmac, json, os, re, secrets, subprocess, threading, time, traceback, uuid
from collections import defaultdict, deque
from pathlib import Path

from aiohttp import web

import elastic_index as EI

HOME = Path(os.environ.get("REALISIM_SUPPORT_HOME", "/workspace/photo2grasp/support"))
REPO = Path("/workspace/photo2grasp/main")
REPO_URL = "https://github.com/adityabangde/photo2grasp"
RUNBOOKS = HOME / "runbooks"            # approved public runbooks only (approved.json pins sha256 per file)
SYSTEM_UID_PREFIX = "system:"
LEASE_TTL = 120
LIMITS = dict(timeout_s=300, max_turns=12, max_tool_calls=24, search_max_hits=8, query_max_chars=300, max_answer_chars=4000, max_proposals=4, max_citations=12)
QUOTA = dict(asks_per_user_hour=6, asks_per_user_day=20, open_jobs_per_project=1, open_jobs_global=12, search_per_user_minute=60)
OPEN = ("waiting_for_connection", "queued", "running")
TERMINAL = ("complete", "failed", "interrupted")
FAIL_CODES = ("missing_key", "auth", "timeout", "sdk_error", "tool_error", "policy", "access_revoked", "other")
OUTCOMES = ("resolved", "workaround", "not_resolved")
PRESENCE_FRESH_S = 300
RECONCILE_EVERY_S = int(os.environ.get("REALISIM_SUPPORT_RECONCILE_S", "120"))

S = None
SANDBOX_UID = None
IX = EI.Index(alias=os.environ.get("REALISIM_SUPPORT_ALIAS", EI.ALIAS),
              prefix=os.environ.get("REALISIM_SUPPORT_PREFIX", EI.PREFIX))
STATE_LOCK = threading.RLock()          # serialises queue transitions (claim / ask / finish) inside this process
_LOCKFILE = None
CLAIM_OK = False
STARTED_AT = time.time()
HEARTBEATS = {}                         # job_id -> lease expiry (s); in-memory extension of the durable lease
_ENTRY = {}                             # manifest file id -> decoded immutable entry
_RATE = defaultdict(deque)
_STATUS_CACHE = {"at": 0, "v": None}
_HELP = {"commit": None, "ids": set(), "docs": []}
_RECON = {"last": None, "running": False, "pending": set(), "event": threading.Event()}


# ── small utilities ──────────────────────────────────────────────────────────
def now_ms():
    return int(time.time() * 1000)


def q(s):
    return "'" + str(s).replace("'", "''") + "'"


def sha(s, n=16):
    return hashlib.sha256(str(s).encode()).hexdigest()[:n]


def err(status, msg, **extra):
    cls = {400: web.HTTPBadRequest, 403: web.HTTPForbidden, 404: web.HTTPNotFound, 409: web.HTTPConflict,
           422: web.HTTPUnprocessableEntity, 429: web.HTTPTooManyRequests, 502: web.HTTPBadGateway,
           503: web.HTTPServiceUnavailable}.get(status, web.HTTPBadRequest)
    return cls(text=json.dumps(dict(extra, error=msg)), content_type="application/json")


def uid_of(request):
    uid = request.headers.get("X-Sender-Uid")
    if not uid:
        raise err(403, "sign in required")
    return uid


def th(fn, *a, **k):
    def safe():
        try:
            return fn(*a, **k)
        except StopIteration as e:
            raise RuntimeError("internal lookup failed") from e
    return asyncio.to_thread(safe)


def H(fn):
    async def h(request):
        try:
            return await fn(request)
        except web.HTTPException:
            raise
        except EI.IndexUnavailable as e:
            raise err(503, "search index unavailable", detail=EI.sanitize(str(e), 200))
        except Exception as e:
            msg = str(e)
            if type(e).__name__ == "StdbError":
                raise err(403 if "access" in msg else 409, EI.sanitize(msg.replace("The module instance encountered a fatal error: ", ""), 300))
            traceback.print_exc()
            raise err(400, f"{type(e).__name__}: {EI.sanitize(msg, 300)}")
    return h


async def body_of(request):
    try:
        b = await request.json()
    except Exception:
        raise err(422, "JSON body required")
    if not isinstance(b, dict):
        raise err(422, "JSON object required")
    return b


def rate(uid, kind, n, win):
    dq = _RATE[(uid, kind)]
    t = time.time()
    while dq and dq[0] < t - win:
        dq.popleft()
    if len(dq) >= n:
        raise err(429, f"limit reached ({n} {kind} per {win // 60 or 1} min); try again shortly")
    dq.append(t)


def jl(v):
    if isinstance(v, str) and v[:1] in "[{":
        try:
            return json.loads(v)
        except Exception:
            return {}
    return v if v is not None else {}


def log(*a):
    print(time.strftime("%H:%M:%S"), "[support]", *a, flush=True)


# ── access (fresh SpacetimeDB reads every time) ──────────────────────────────
def project_for(uid, pid, write=False):
    return S.project_for(uid, pid, write)


def project_row(pid):
    r = S.db.sql(f"SELECT id, owner_uid, name, archived, settings_json FROM project WHERE id = {q(pid)}")
    if not r:
        return None
    p = r[0]
    p["settings_json"] = jl(p.get("settings_json")) or {}
    return p


def is_system(p):
    return str(p.get("owner_uid", "")).startswith(SYSTEM_UID_PREFIX) or bool((p.get("settings_json") or {}).get("system"))


def is_test(p):
    st = p.get("settings_json") or {}
    return str(p.get("owner_uid", "")).startswith("qa_") or st.get("is_test") is True


def accessible_pids(uid):
    """Projects the uid can read right now (owner or member), excluding system projects."""
    owned = S.db.sql(f"SELECT id, owner_uid, settings_json FROM project WHERE owner_uid = {q(uid)}")
    mem = S.db.sql(f"SELECT project_id FROM project_member WHERE uid = {q(uid)}")
    out = {p["id"] for p in owned if not is_system(dict(p, settings_json=jl(p.get("settings_json"))))}
    for m in mem:
        p = project_row(m["project_id"])
        if p and not is_system(p):
            out.add(p["id"])
    return out


def billed_ok(uid, p):
    return p["owner_uid"] == SANDBOX_UID or uid == SANDBOX_UID


def request_billing_ok(request, uid, p, spend=False):
    if billed_ok(uid, p):
        return True
    import public_beta
    if spend:
        return public_beta.factory_allowed(request)
    return public_beta.claude_available(request, uid, p, resource="factory_calls")["available"]


# ── canonical support entries (SpacetimeDB) ──────────────────────────────────
def store(actor_uid, pid, etype, entry, **index):
    data = json.dumps(entry, separators=(",", ":"), default=str).encode()
    deps = {k: v for k, v in index.items() if v is not None}
    deps.update(type=etype, id=entry["id"], at=entry.get("at"))
    mid, files = S.db.store_dir(actor_uid, pid, "support", entry["id"], etype,
                                [(f"support/{etype}/{entry['id']}.json", data, "application/json")], deps=deps)
    _ENTRY[files[0]["id"]] = entry
    return mid


def support_rows(pid=None):
    w = "kind = 'support'" + (f" AND project_id = {q(pid)}" if pid else "")
    rows = S.db.sql(f"SELECT id, project_id, ref_id, version, status, dependency_json, files_json, created_at FROM artifact_manifest WHERE {w}")
    out = []
    for r in rows:
        if r.get("status") != "verified":
            continue
        d = jl(r.get("dependency_json")) or {}
        d["_manifest"] = r["id"]
        d["_pid"] = r["project_id"]
        d["_files"] = jl(r.get("files_json")) or []
        d["_created"] = float(r.get("created_at") or 0)
        out.append(d)
    out.sort(key=lambda d: (d.get("at") or d["_created"], d["_created"]))
    return out


def entry_body(uid, d):
    """Read an entry's immutable JSON through the access-checked read_chunk procedure, as `uid`."""
    fid = d["_files"][0]["id"]
    if fid not in _ENTRY:
        _, data = S.db.read_bytes(uid, d["_pid"], fid)
        _ENTRY[fid] = json.loads(data.decode())
    return json.loads(json.dumps(_ENTRY[fid]))


def fold_jobs(rows):
    jobs, cases, resolutions = {}, {}, {}
    for d in rows:
        t = d.get("type")
        if t == "case":
            cases[d["id"]] = dict(id=d["id"], project_id=d["_pid"], by=d.get("by"), at=d.get("at"), run_id=d.get("run_id"), _row=d)
        elif t == "job":
            jobs[d["id"]] = dict(id=d["id"], project_id=d["_pid"], case_id=d.get("case_id"), by=d.get("by"), at=d.get("at"),
                                 status=d.get("status") or "queued", run_id=d.get("run_id"), idem=d.get("idem"), events=0, _row=d)
        elif t == "job_event":
            j = jobs.get(d.get("job_id"))
            if not j:
                continue
            ev = d.get("event")
            if ev == "progress":
                j["events"] += 1
                j.setdefault("_progress", []).append(d)
                continue
            if j["status"] in TERMINAL:
                continue                                 # first terminal event wins
            if ev == "claimed":
                j.update(status="running", lease_hash=d.get("lease_hash"), lease_expires_at=d.get("lease_expires_at"),
                         worker_id=d.get("worker_id"), claimed_at=d.get("at"))
            elif ev in TERMINAL:
                j.update(status=ev, finished_at=d.get("at"), idem_final=d.get("idem"), error_code=d.get("error_code"), _final=d)
        elif t == "resolution":
            resolutions.setdefault(d.get("case_id"), []).append(dict(id=d["id"], case_id=d.get("case_id"), outcome=d.get("outcome"),
                                                                     at=d.get("at"), by=d.get("by"), idem=d.get("idem"), _row=d))
    return jobs, cases, resolutions


# ── Factory worker presence (operational, local file; not project data) ──────
def _presence_file():
    return HOME / "provider-status.json"


def presence():
    try:
        return json.loads(_presence_file().read_text())
    except Exception:
        return {}


def set_presence(p):
    HOME.mkdir(parents=True, exist_ok=True)
    tmp = _presence_file().with_suffix(".tmp")
    tmp.write_text(json.dumps(p))
    os.replace(tmp, _presence_file())


def factory_view():
    p = presence()
    age = time.time() - (p.get("last_seen") or 0)
    if not p:
        st = "not_connected"
    elif age > PRESENCE_FRESH_S:
        st = "worker_offline"
    elif not p.get("factory_key_present"):
        st = "missing_key"
    else:
        st = "ready"
    return dict(status=st, ready=st == "ready", last_seen=p.get("last_seen"), worker_id=p.get("worker_id"),
                sdk=p.get("sdk"), cli=p.get("cli"),
                note={"not_connected": "No Factory worker has connected yet.",
                      "worker_offline": "The Factory worker has not checked in for over 5 minutes.",
                      "missing_key": "The Factory worker is running but no Factory API key is connected.",
                      "ready": "Factory Droid worker connected."}[st])


def effective_status(j):
    s = j["status"]
    if s in ("queued", "waiting_for_connection"):
        return "queued" if factory_view()["ready"] else "waiting_for_connection"
    return s


def lease_expiry(j):
    durable = float(j.get("lease_expires_at") or 0) / 1000
    return max(durable, HEARTBEATS.get(j["id"], 0), STARTED_AT + LEASE_TTL)


# ── canonical → derived documents ────────────────────────────────────────────
def _stages_text(stages):
    out = []
    for s in stages or []:
        if isinstance(s, dict):
            out.append(f"{s.get('label') or s.get('key')}: {s.get('status')}" + (f" ({s.get('detail')})" if s.get("detail") else "") +
                       (f" error: {s.get('error')}" if s.get("error") else ""))
    return "\n".join(out)


def _failed_stage(r):
    for s in r.get("stages_json") or []:
        if isinstance(s, dict) and s.get("status") in ("failed", "error"):
            return s.get("key")
    return r.get("stage") or ""


def run_log_tail(r, nbytes=12000):
    rl = S.get("RUNLOGS")
    f = Path(rl) / f"{r['id']}.log" if rl else None
    text = ""
    try:
        if f and f.exists():
            with open(f, "rb") as fh:
                fh.seek(max(0, f.stat().st_size - nbytes))
                text = fh.read().decode(errors="replace")
    except OSError:
        text = ""
    text = text or (r.get("log_tail") or "")
    lines = [l for l in text.splitlines() if l.strip()][-60:]
    return "\n".join(lines)


def base_doc(p, table, rid, kind, **kw):
    d = dict(canonical_id=f"{table}:{rid}", canonical_table=table, record_id=rid, kind=kind, visibility="project",
             project_id=p["id"], is_test=is_test(p),
             provenance=dict(source="spacetimedb", database="photo2grasp", table=table, id=rid))
    d.update({k: v for k, v in kw.items() if v not in (None, "")})
    return d


def doc_run(p, r):
    failed = r.get("status") == "failed"
    err_text = ""
    if failed:
        err_text = (r.get("error") or "") + "\n" + run_log_tail(r)
    stage = _failed_stage(r) if failed else (r.get("stage") or "")
    ec = EI.error_class(err_text) if failed else ""
    title = f"{r.get('kind')} run {r.get('status')}" + (f" at {stage}" if stage else "") + (f": {ec}" if ec else "")
    body = "\n".join(x for x in [f"Pipeline run of kind {r.get('kind')}, status {r.get('status')}, stage {r.get('stage')}.",
                                  f"Repository commit {str(r.get('commit') or '')[:12]}, attempt {r.get('attempt')}.",
                                  _stages_text(r.get("stages_json"))] if x)
    return base_doc(p, "pipeline_run", r["id"], "pipeline_error" if failed else "pipeline_run", title=title, body=body,
                    error_text=err_text.strip() or None, stage=stage, run_kind=r.get("kind"), status=r.get("status"),
                    error_class=ec, error_fingerprint=EI.fingerprint(err_text) if failed else None,
                    commit=str(r.get("commit") or "")[:12], created_at=int(float(r.get("created_at") or 0)))


def _meas_text(m):
    out = []
    for k, v in (m or {}).items():
        if isinstance(v, dict):
            out.append(f"{k} = {v.get('value')} ({v.get('source') or 'unknown'})")
    return "; ".join(out)


def doc_asset(p, a):
    rv = a.get("review_json") if isinstance(a.get("review_json"), dict) else {}
    body = f"Object {a.get('object_name')} version {a.get('version')} is {a.get('status')}. Measurements: {_meas_text(a.get('measurements_json'))}."
    if rv.get("note"):
        body += f" Review note: {rv.get('note')}"
    return base_doc(p, "asset_version", a["id"], "asset_version", title=f"{a.get('object_name')} v{a.get('version')}: {a.get('status')}",
                    body=body, status=a.get("status"), stage="asset", created_at=int(float(a.get("created_at") or 0)))


def doc_scene(p, s):
    return base_doc(p, "scene_version", s["id"], "scene_version", title=f"Scene v{s.get('version')}: {s.get('status')}",
                    body=f"Scene version {s.get('version')} ({s.get('robot_profile')}) status {s.get('status')}. Validation: {json.dumps(s.get('validation_json'), default=str)[:1500]}",
                    status=s.get("status"), stage="scene", created_at=int(float(s.get("created_at") or 0)))


def doc_sim(p, s):
    return base_doc(p, "simulation_run", s["id"], "simulation_run", title=f"Simulation {s.get('mode')}: {s.get('result')}",
                    body=f"Simulation {s.get('mode')} seed {s.get('seed')} result {s.get('result')}. Metrics: {json.dumps(s.get('metrics_json'), default=str)[:1500]}",
                    status=s.get("result"), stage="simulate", created_at=int(float(s.get("created_at") or 0)))


def doc_export(p, e):
    return base_doc(p, "gym_export", e["id"], "gym_export", title=f"Gymnasium export {e.get('package_version')}: {e.get('status')}",
                    body=f"Export {e.get('env_id')} package {e.get('package_version')} status {e.get('status')}. Validation: {json.dumps(e.get('validation_json'), default=str)[:1500]}",
                    status=e.get("status"), stage="export", created_at=int(float(e.get("created_at") or 0)))


def docs_workflow(p):
    wf = (p.get("settings_json") or {}).get("workflow")
    out = []
    if not isinstance(wf, dict):
        return out
    for part in ("analysis", "build"):
        sec = wf.get(part) or {}
        errs = [sec.get("error")] + [f"{s.get('label')}: {s.get('error')}" for s in (sec.get("steps") or []) if isinstance(s, dict) and s.get("error")]
        errs = [e for e in errs if e]
        if not errs:
            continue
        t = "\n".join(errs)
        ec = EI.error_class(t)
        out.append(base_doc(p, "workflow", f"{p['id']}#{part}", "workflow_error", title=f"Workflow {part} error: {ec}",
                            body=f"Guided workflow draft revision {wf.get('revision')}, {part} status {sec.get('status') or sec.get('phase')}.",
                            error_text=t, stage=part, status=sec.get("status") or sec.get("phase"), error_class=ec,
                            error_fingerprint=EI.fingerprint(t), created_at=int(float(sec.get("updated_at") or sec.get("at") or 0)) or None))
    return out


def doc_case(p, c, resolved):
    rc = c.get("run_context") or {}
    return base_doc(p, "support_case", c["id"], "support_case", title=f"Support question: {c.get('question', '')[:140]}",
                    body=f"{c.get('question', '')}\nRun context: {rc.get('kind', '')} {rc.get('status', '')} at {rc.get('stage', '')}: {rc.get('error_class', '')}",
                    status="resolved" if resolved else "open", stage=rc.get("stage"), error_class=rc.get("error_class"),
                    error_fingerprint=rc.get("fingerprint"), case_id=c["id"], created_at=int(c.get("at") or 0))


def doc_resolution(p, res, case):
    rc = (case or {}).get("run_context") or {}
    steps = res.get("fix_steps") or ""
    if isinstance(steps, list):
        steps = "\n".join(f"- {s}" for s in steps)
    return base_doc(p, "resolution", res["id"], "resolution", title=f"{res.get('outcome', '').replace('_', ' ').capitalize()}: {res.get('summary', '')[:140]}",
                    body=f"{res.get('summary', '')}\n{steps}\nOriginal question: {(case or {}).get('question', '')}",
                    error_text=rc.get("error_excerpt"), outcome=res.get("outcome"), status=res.get("outcome"),
                    stage=rc.get("stage"), error_class=res.get("error_class") or rc.get("error_class"),
                    error_fingerprint=res.get("error_fingerprint") or rc.get("fingerprint"), case_id=res.get("case_id"),
                    created_at=int(res.get("at") or 0))


def project_docs(p):
    pid = p["id"]
    rows = S.rows
    docs = docs_workflow(p)
    for r in rows("pipeline_run", pid):
        docs.append(doc_run(p, r))
    for a in rows("asset_version", pid):
        docs.append(doc_asset(p, a))
    for s in rows("scene_version", pid):
        docs.append(doc_scene(p, s))
    for s in rows("simulation_run", pid):
        docs.append(doc_sim(p, s))
    for e in rows("gym_export", pid):
        docs.append(doc_export(p, e))
    srows = support_rows(pid)
    _, cases, resolutions = fold_jobs(srows)
    bodies = {}
    for d in srows:
        if d.get("type") in ("case", "resolution"):
            try:
                bodies[d["id"]] = entry_body(p["owner_uid"], d)
            except Exception as ex:
                log("unreadable support entry", d["id"], type(ex).__name__)
    for cid in cases:
        if cid in bodies:
            docs.append(doc_case(p, bodies[cid], bool(resolutions.get(cid))))
    for cid, rs in resolutions.items():
        for res in rs:
            if res["id"] in bodies:
                docs.append(doc_resolution(p, bodies[res["id"]], bodies.get(cid)))
    return docs


# ── official repository help docs (public, pinned to the checked-out commit) ─────────────────────────────
def _git(*args):
    r = subprocess.run(["git", *args], cwd=REPO, capture_output=True, text=True, timeout=30)
    if r.returncode:
        raise RuntimeError(r.stderr.strip()[:200])
    return r.stdout


def _sections(text):
    cur, buf, out = "overview", [], []
    for line in text.splitlines():
        m = re.match(r"^(#{1,3})\s+(.*)", line)
        if m and buf:
            out.append((cur, "\n".join(buf).strip()))
            buf = []
        if m:
            cur = m.group(2).strip()
        buf.append(line)
    if buf:
        out.append((cur, "\n".join(buf).strip()))
    res = []
    for title, body in out:
        for i in range(0, max(1, len(body)), 3000):
            res.append((title + (f" (part {i // 3000 + 1})" if len(body) > 3000 else ""), body[i:i + 3000]))
    return [(t, b) for t, b in res if b.strip()]


def help_docs():
    """Allow-listed public docs: git-tracked Markdown in the upstream repo at HEAD, plus approved runbooks whose
    sha256 matches their approval record. Private workspace notes/build plans are never read here."""
    docs = []
    try:
        commit = _git("rev-parse", "HEAD").strip()
        files = [f for f in _git("ls-files", "*.md").split("\n") if f]
        for path in files[:40]:
            blob = _git("rev-parse", f"HEAD:{path}").strip()
            text = _git("show", f"HEAD:{path}")
            for title, body in _sections(text):
                slug = re.sub(r"[^a-z0-9]+", "-", title.lower()).strip("-")[:60] or "section"
                cid = f"help_doc:{path}#{slug}@{commit[:12]}"
                docs.append(dict(canonical_id=cid, canonical_table="help_doc", record_id=f"{path}#{slug}", kind="help_doc",
                                 visibility="public", title=f"{path} · {title}", body=body, commit=commit[:12], is_test=False,
                                 provenance=dict(source="repository", repo=REPO_URL, commit=commit, path=path, blob=blob, section=title,
                                                 url=f"{REPO_URL}/blob/{commit}/{path}")))
        _HELP["commit"] = commit
    except Exception as ex:
        log("help docs unavailable:", EI.sanitize(str(ex), 160))
    try:
        appr = json.loads((RUNBOOKS / "approved.json").read_text()) if (RUNBOOKS / "approved.json").exists() else {}
        for name, meta in (appr.get("runbooks") or {}).items():
            f = RUNBOOKS / Path(name).name
            if not f.exists():
                continue
            data = f.read_bytes()
            if hashlib.sha256(data).hexdigest() != meta.get("sha256"):
                log("runbook not indexed (changed since approval):", f.name)
                continue
            for title, body in _sections(data.decode(errors="replace")):
                slug = re.sub(r"[^a-z0-9]+", "-", title.lower()).strip("-")[:60] or "section"
                docs.append(dict(canonical_id=f"help_doc:runbooks/{f.name}#{slug}@{meta['sha256'][:12]}", canonical_table="help_doc",
                                 record_id=f"runbooks/{f.name}#{slug}", kind="help_doc", visibility="public", title=f"Runbook · {title}",
                                 body=body, is_test=False, provenance=dict(source="approved_runbook", file=f.name, sha256=meta["sha256"],
                                                                           approved_by=meta.get("approved_by"), approved_at=meta.get("approved_at"))))
    except Exception as ex:
        log("runbooks unreadable:", type(ex).__name__)
    _HELP["ids"] = {d["canonical_id"] for d in docs}
    _HELP["docs"] = docs
    return docs


def all_projects():
    out = []
    for p in S.db.sql("SELECT id, owner_uid, name, archived, settings_json FROM project"):
        p["settings_json"] = jl(p.get("settings_json")) or {}
        if not is_system(p):
            out.append(p)
    return out


def desired_docs(pids=None):
    docs = []
    if pids is None:
        docs += help_docs()
    for p in all_projects():
        if pids is not None and p["id"] not in pids:
            continue
        try:
            docs += project_docs(p)
        except Exception as ex:
            log("project docs failed", p["id"], type(ex).__name__, EI.sanitize(str(ex), 160))
            raise
    return docs


def reconcile(pids=None, refresh=False):
    """Make the derived index equal to the canonical sources: upsert changed/missing documents, delete documents whose
    canonical record no longer exists (deleted project, deleted record, revoked/old help doc)."""
    t0 = time.time()
    IX.ensure()
    want = {}
    for d in desired_docs(pids):
        want[d["canonical_id"]] = d
    have = IX.existing(project_ids=pids) if pids is not None else IX.existing()
    if pids is not None:
        have = {k: v for k, v in have.items() if not k.startswith("help_doc:")}
    changed = [d for cid, d in want.items() if have.get(cid, (None,))[0] != IX._prep(d)["content_hash"]]
    gone = [cid for cid in have if cid not in want]
    res = IX.upsert(changed, refresh=refresh) if changed else dict(created=0, updated=0, noop=0, errors=0)
    deleted = IX.delete(gone, refresh=refresh) if gone else 0
    out = dict(at=now_ms(), scope="all" if pids is None else sorted(pids), desired=len(want), changed=len(changed),
               deleted=deleted, seconds=round(time.time() - t0, 2), **res)
    if pids is None:
        _RECON["last"] = out
    return out


def rebuild_all():
    """Drop-and-rebuild from canonical sources (alias swap); proves the index is derived."""
    return IX.rebuild(desired_docs(None))


def request_reconcile(pid):
    _RECON["pending"].add(pid)
    _RECON["event"].set()


def _reconcile_loop():
    last_full = 0
    while True:
        _RECON["event"].wait(timeout=15)
        _RECON["event"].clear()
        try:
            pend = set(_RECON["pending"])
            _RECON["pending"].clear()
            if pend:
                reconcile(pend)
            if time.time() - last_full > RECONCILE_EVERY_S:
                reconcile(None)
                last_full = time.time()
        except EI.IndexUnavailable as e:
            log("index unavailable:", EI.sanitize(str(e), 160))
        except Exception:
            traceback.print_exc()


# ── revalidation of every hit against SpacetimeDB ────────────────────────────
def fresh_doc(uid, cid, allowed):
    """Rebuild the document for `cid` from its canonical source as `uid`; None if it no longer exists or is not
    visible to `uid` (deleted project/record, revoked membership)."""
    if cid.startswith("help_doc:"):
        if not _HELP["ids"]:
            help_docs()
        return next((d for d in _HELP["docs"] if d["canonical_id"] == cid), None)
    table, _, rid = cid.partition(":")
    if table == "workflow":
        pid = rid.split("#")[0]
        if pid not in allowed:
            return None
        p = project_row(pid)
        return next((d for d in docs_workflow(p) if d["canonical_id"] == cid), None) if p else None
    if table in ("pipeline_run", "asset_version", "scene_version", "simulation_run", "gym_export"):
        rows = S.db.sql(f"SELECT * FROM {table} WHERE id = {q(rid)}")
        if not rows or rows[0]["project_id"] not in allowed:
            return None
        p = project_row(rows[0]["project_id"])
        if not p:
            return None
        r = S.pj(rows[0], *[c for c in S.JSONCOLS if c in rows[0]])
        return {"pipeline_run": doc_run, "asset_version": doc_asset, "scene_version": doc_scene,
                "simulation_run": doc_sim, "gym_export": doc_export}[table](p, r)
    if table in ("support_case", "resolution"):
        m = S.db.sql(f"SELECT id, project_id, ref_id, version, status, dependency_json, files_json, created_at FROM artifact_manifest WHERE ref_id = {q(rid)}")
        m = [x for x in m if x.get("status") == "verified" and x["project_id"] in allowed]
        if not m:
            return None
        pid = m[0]["project_id"]
        p = project_row(pid)
        srows = support_rows(pid)
        _, cases, resolutions = fold_jobs(srows)
        d = next((x for x in srows if x["id"] == rid), None)
        if not p or not d:
            return None
        body = entry_body(uid, d)                       # access-checked read as the caller
        if table == "support_case":
            return doc_case(p, body, bool(resolutions.get(rid)))
        cd = next((x for x in srows if x["id"] == body.get("case_id")), None)
        return doc_resolution(p, body, entry_body(uid, cd) if cd else None)
    return None


def canonical_exists(cid):
    """Gateway-level existence check (no user scope): distinguishes 'deleted' (heal the index) from 'not yours' (just drop)."""
    if cid.startswith("help_doc:"):
        return cid in _HELP["ids"]
    table, _, rid = cid.partition(":")
    try:
        if table == "workflow":
            p = project_row(rid.split("#")[0])
            return bool(p) and any(d["canonical_id"] == cid for d in docs_workflow(p))
        if table in ("pipeline_run", "asset_version", "scene_version", "simulation_run", "gym_export"):
            return bool(S.db.sql(f"SELECT id FROM {table} WHERE id = {q(rid)}"))
        if table in ("support_case", "resolution"):
            return any(m.get("status") == "verified" for m in S.db.sql(f"SELECT id, status FROM artifact_manifest WHERE ref_id = {q(rid)}"))
    except Exception:
        return True                                     # unknown: never delete on an error
    return False


def revalidate(uid, hits, allowed, keep_body=False):
    out, stale, refresh = [], [], []
    for h in hits:
        cid = h.get("canonical_id", "")
        if h.get("visibility") != "public" and h.get("project_id") not in allowed:
            continue                                     # outside the caller's fresh scope: never returned
        try:
            fd = fresh_doc(uid, cid, allowed)
        except Exception as ex:
            log("revalidate failed", cid[:60], type(ex).__name__)
            fd = None
        if fd is None:
            if not canonical_exists(cid):
                stale.append(cid)                        # deleted at the source: drop and remove from the index
            continue
        prep = IX._prep(fd)
        if prep["content_hash"] != h.get("content_hash"):
            refresh.append(fd)
        snippet = h.get("snippet") if prep["content_hash"] == h.get("content_hash") else ""
        if not snippet:
            snippet = (prep.get("error_text") or prep.get("body") or "")[:400]
        c = {k: prep.get(k) for k in ("canonical_id", "kind", "title", "project_id", "stage", "status", "error_class",
                                      "error_fingerprint", "outcome", "created_at", "case_id", "commit")}
        c["snippet"] = EI.sanitize(snippet, 600)
        c["score"] = h.get("score")
        c["provenance"] = prep.get("provenance")
        c["verified_at"] = now_ms()
        if keep_body:
            c["body"] = prep.get("body")
            c["error_text"] = prep.get("error_text")
        out.append(c)
    if stale or refresh:
        def heal():
            try:
                if stale:
                    IX.delete(stale)
                if refresh:
                    IX.upsert(refresh)
            except Exception:
                pass
        threading.Thread(target=heal, daemon=True).start()
    return out, len(hits) - len(out)


# ── run context (exact failed-run context for troubleshooting) ───────────────
def run_context(pid, run_id):
    if not run_id:
        return None
    rows = S.db.sql(f"SELECT * FROM pipeline_run WHERE id = {q(run_id)}")
    if not rows or rows[0]["project_id"] != pid:
        raise err(404, "run not found in this project")
    r = S.pj(rows[0], *[c for c in S.JSONCOLS if c in rows[0]])
    err_text = ((r.get("error") or "") + "\n" + run_log_tail(r)).strip() if r.get("status") == "failed" else ""
    lines = [l for l in err_text.splitlines() if l.strip()]
    exc = next((l for l in reversed(lines) if EI._EXC.search(l)), lines[-1] if lines else "")
    return dict(id=r["id"], kind=r.get("kind"), status=r.get("status"), stage=_failed_stage(r) if err_text else r.get("stage"),
                error_class=EI.error_class(err_text) if err_text else None, fingerprint=EI.fingerprint(err_text) if err_text else None,
                error_excerpt=EI.sanitize(exc, 400) if exc else None, error=EI.sanitize(r.get("error"), 1200) or None,
                log_tail=EI.sanitize("\n".join(lines[-30:]), 3000) if lines else None,
                commit=str(r.get("commit") or "")[:12], attempt=r.get("attempt"), created_at=r.get("created_at"),
                stages=[(s.get("key"), s.get("status")) for s in (r.get("stages_json") or []) if isinstance(s, dict)])


def clean_filters(f):
    f = f if isinstance(f, dict) else {}
    out = {}
    for k in EI.FILTER_FIELDS:
        v = f.get(k)
        if v:
            vals = v if isinstance(v, list) else [v]
            vals = [str(x)[:80] for x in vals if isinstance(x, (str, int, float)) and re.fullmatch(r"[\w.:\-]{1,80}", str(x))][:12]
            if vals:
                out[k] = vals
    for k in ("since_ms", "until_ms"):
        try:
            if f.get(k) is not None:
                out[k] = int(float(f[k]))
        except (TypeError, ValueError):
            pass
    return out


def do_search(uid, pid, query, scope="project", filters=None, size=10, run_id=None, keep_body=False):
    allowed = {pid}
    if scope == "my_projects":
        allowed = accessible_pids(uid) | {pid}
    filters = clean_filters(filters)
    rc = run_context(pid, run_id) if run_id else None
    fp = (rc or {}).get("fingerprint") or (filters.get("error_fingerprint") or [None])[0]
    qtext = str(query or "").strip()[:300]
    if not qtext and rc:
        qtext = " ".join(x for x in [rc.get("error_class"), rc.get("error_excerpt"), rc.get("stage")] if x)[:300]
    try:
        size = max(1, min(int(size or 8), 8))
    except (TypeError, ValueError):
        size = 8
    res = IX.search(qtext, allowed, True, filters, size=min(20, size * 2))
    hits, dropped = revalidate(uid, res["hits"], allowed, keep_body)
    prior = []
    pr = None
    if fp:
        pr = IX.search("", allowed, False, {"kind": ["resolution"], "error_fingerprint": [fp]}, size=5, facets=False)
    if (not pr or not pr["hits"]) and qtext:
        pr = IX.search(qtext, allowed, False, {"kind": ["resolution"]}, size=5, facets=False)
    if pr:
        prior, d2 = revalidate(uid, pr["hits"], allowed, keep_body)
        dropped += d2
        for x in prior:
            x["match"] = "same error fingerprint" if fp and x.get("error_fingerprint") == fp else "similar text (BM25)"
    return dict(query=qtext, scope=scope, retrieval="bm25", semantic=False, fingerprint=fp, run_context=rc,
                total=res["total"], hits=hits[:size], facets=res["facets"], prior_resolutions=prior,
                dropped_stale=dropped, searched_projects=len(allowed))


# ── views ─────────────────────────────────────────────────────────────────────
def elastic_status(force=False):
    if force or time.time() - _STATUS_CACHE["at"] > 10:
        _STATUS_CACHE["v"] = IX.status()
        _STATUS_CACHE["at"] = time.time()
    return dict(_STATUS_CACHE["v"] or {})


def job_entries(uid, j):
    """Durable safe step log + final result for a job (bodies read access-checked as `uid`)."""
    steps, final = [], None
    for d in j.get("_progress", []):
        try:
            b = entry_body(uid, d)
            steps.append({k: b.get(k) for k in ("at", "tool", "summary", "result_count", "status")})
        except Exception:
            pass
    if j.get("_final"):
        try:
            final = entry_body(uid, j["_final"])
        except Exception:
            final = None
    return steps, final


def job_view(uid, j, detail=False):
    v = {k: j.get(k) for k in ("id", "case_id", "project_id", "at", "run_id", "claimed_at", "finished_at", "error_code")}
    v["status"] = effective_status(j)
    v["provider"] = "factory-droid"
    v["steps_count"] = j.get("events", 0)
    if j["status"] == "running":
        v["lease_expires_at"] = int(lease_expiry(j) * 1000)
    if detail:
        steps, final = job_entries(uid, j)
        v["steps"] = steps
        if final:
            res = final.get("result") or {}
            v["result"] = dict(answer=res.get("answer"), runtime=res.get("runtime"), rejected_citations=res.get("rejected_citations"),
                               rejected_proposals=res.get("rejected_proposals"), copilot_turn_id=res.get("copilot_turn_id"))
            allowed = {j["project_id"]}
            cites, dropped = revalidate(uid, [dict(canonical_id=c, project_id=j["project_id"] if not c.startswith("help_doc:") else None,
                                                   visibility="public" if c.startswith("help_doc:") else "project")
                                              for c in res.get("citations") or []], allowed)
            v["result"]["citations"] = cites
            v["result"]["citations_dropped_since"] = dropped
            v["result"]["proposals"] = proposals_for(uid, j["project_id"], res.get("copilot_turn_id"))
            if final.get("error"):
                v["error"] = final.get("error")
            v["uncertain_charge"] = final.get("uncertain_charge")
    if v["status"] == "waiting_for_connection":
        v["waiting_reason"] = factory_view()["note"]
    return v


def proposals_for(uid, pid, turn_id):
    if not turn_id:
        return []
    try:
        import copilot
        turns, _, _ = copilot.thread_state(copilot.load_entries(uid, pid))
        t = next((t for t in turns if t.get("id") == turn_id), None)
        return [{k: p.get(k) for k in ("id", "kind", "target", "title", "changes", "status", "rationale", "evidence", "decision")}
                for p in (t or {}).get("proposals", [])]
    except Exception as ex:
        log("proposal status unavailable", type(ex).__name__)
        return []


def quota_view(uid, all_rows=None):
    rows = all_rows if all_rows is not None else support_rows()
    t = now_ms()
    mine = [d for d in rows if d.get("type") == "job" and d.get("by") == uid]
    hour = sum(1 for d in mine if (d.get("at") or 0) > t - 3600_000)
    day = sum(1 for d in mine if (d.get("at") or 0) > t - 86400_000)
    return dict(asks_left_hour=max(0, QUOTA["asks_per_user_hour"] - hour), asks_left_day=max(0, QUOTA["asks_per_user_day"] - day), limits=QUOTA)


# ── public handlers ──────────────────────────────────────────────────────────
@H
async def get_support(request):
    uid = uid_of(request)
    pid = request.match_info["pid"]

    def work():
        p = project_for(uid, pid)
        allrows = support_rows()
        rows = [d for d in allrows if d["_pid"] == pid]
        jobs, cases, resolutions = fold_jobs(rows)
        es = elastic_status()
        if uid != SANDBOX_UID:
            es = {k: v for k, v in es.items() if k not in ("documents", "by_kind", "index", "heap_used_mb", "heap_max_mb")}
        fv = factory_view()
        case_list = []
        for cid, c in sorted(cases.items(), key=lambda kv: -(kv[1].get("at") or 0))[:30]:
            try:
                body = entry_body(uid, c["_row"])
            except Exception:
                continue
            cj = sorted([j for j in jobs.values() if j.get("case_id") == cid], key=lambda j: -(j.get("at") or 0))
            rs = resolutions.get(cid) or []
            case_list.append(dict(id=cid, at=c.get("at"), question=body.get("question"), run_id=body.get("run_id"),
                                  run_context={k: (body.get("run_context") or {}).get(k) for k in ("kind", "status", "stage", "error_class", "error_excerpt")},
                                  status="resolved" if rs else "open", latest_job=job_view(uid, cj[0]) if cj else None,
                                  resolution=({k: rs[-1].get(k) for k in ("id", "outcome", "at")} if rs else None)))
        can_write = p.get("role") in ("owner", "editor")
        return dict(
            provider=dict(elastic=es, factory=fv, system_of_record="SpacetimeDB"),
            capabilities=dict(search=bool(es.get("connected")), retrieval="bm25", semantic=False, facets=["kind", "stage", "error_class", "status", "created_day"],
                              fingerprints=True, prior_resolutions=True,
                              ask=can_write and request_billing_ok(request, uid, p), ask_reason=None if can_write and request_billing_ok(request, uid, p) else
                              ("view-only access" if not can_write else "Factory investigations run on the workspace owner's Factory account"),
                              resolve=can_write, factory_ready=fv["ready"]),
            quotas=quota_view(uid, allrows), limits=LIMITS,
            cases=case_list,
            jobs=[job_view(uid, j) for j in sorted(jobs.values(), key=lambda j: -(j.get("at") or 0))[:20]],
            index=dict(last_reconcile=(_RECON["last"] or {}).get("at"), help_commit=(_HELP["commit"] or "")[:12] or None))
    return web.json_response(await th(work))


@H
async def post_search(request):
    uid = uid_of(request)
    pid = request.match_info["pid"]
    b = await body_of(request)

    def work():
        project_for(uid, pid)
        rate(uid, "searches", QUOTA["search_per_user_minute"], 60)
        scope = b.get("scope") if b.get("scope") in ("project", "my_projects") else "project"
        return do_search(uid, pid, b.get("query"), scope, b.get("filters"), b.get("size") or b.get("limit") or 8, b.get("run_id"))
    return web.json_response(await th(work))


@H
async def post_ask(request):
    uid = uid_of(request)
    pid = request.match_info["pid"]
    b = await body_of(request)
    question = str(b.get("question") or "").strip()
    if not question or len(question) > 2000:
        raise err(422, "question must be 1-2000 characters")
    run_id = str(b.get("run_id") or "") or None
    case_id = str(b.get("case_id") or "") or None
    key = str(b.get("idempotency_key") or "")[:80] or f"auto:{sha(question + '|' + str(run_id) + '|' + str(case_id))}:{int(time.time() // 600)}"

    def work(case_id=case_id):
        p = project_for(uid, pid, write=True)
        if not request_billing_ok(request, uid, p, spend=True):
            raise err(403, "Factory investigations are unavailable for this account or its usage allowance is exhausted")
        jid = "job_" + sha(f"{uid}|{pid}|{key}")
        with STATE_LOCK:
            allrows = support_rows()
            jobs, cases, _ = fold_jobs(allrows)
            if jid in jobs:
                j = jobs[jid]
                if j["project_id"] != pid:
                    raise err(409, "idempotency key already used")
                return dict(idempotent=True, replayed=True, deduplicated=True, job=job_view(uid, j), case=dict(id=j["case_id"]))
            qv = quota_view(uid, allrows)
            if qv["asks_left_hour"] <= 0 or qv["asks_left_day"] <= 0:
                raise err(429, "Factory investigation limit reached for now", quotas=qv)
            open_jobs = [j for j in jobs.values() if j["status"] in OPEN]
            if sum(1 for j in open_jobs if j["project_id"] == pid) >= QUOTA["open_jobs_per_project"]:
                raise err(409, "an investigation is already open for this project", job=job_view(uid, next(j for j in open_jobs if j["project_id"] == pid)))
            if len(open_jobs) >= QUOTA["open_jobs_global"]:
                raise err(429, "the investigation queue is full; try again later")
            rc = run_context(pid, run_id) if run_id else None
            t = now_ms()
            if case_id:
                c = cases.get(case_id)
                if not c or c["project_id"] != pid:
                    raise err(404, "case not found")
            else:
                case_id = "cas_" + sha("case|" + jid)
                __import__("public_beta").mark_dispatched(request)
                store(uid, pid, "case", dict(id=case_id, at=t, by=uid, question=question, run_id=run_id, run_context=rc, origin="ask"),
                      by=uid, run_id=run_id)
            __import__("public_beta").mark_dispatched(request)
            status = "queued" if factory_view()["ready"] else "waiting_for_connection"
            store(uid, pid, "job", dict(id=jid, at=t, by=uid, case_id=case_id, question=question, run_id=run_id, run_context=rc,
                                        limits=LIMITS, status=status, idem=sha(key, 24)),
                  by=uid, case_id=case_id, run_id=run_id, status=status, idem=sha(key, 24))
            store(uid, pid, "audit", dict(id="sau_" + uuid.uuid4().hex[:16], at=t, by=uid, action="ask", target=jid, case_id=case_id),
                  by=uid, action="ask", job_id=jid)
            jobs, _, _ = fold_jobs(support_rows(pid))
        request_reconcile(pid)
        return dict(idempotent=False, replayed=False, job=job_view(uid, jobs[jid]), case=dict(id=case_id))
    return web.json_response(await th(work), status=202)


@H
async def get_job(request):
    uid = uid_of(request)
    pid, jid = request.match_info["pid"], request.match_info["jid"]

    def work():
        project_for(uid, pid)
        jobs, _, _ = fold_jobs(support_rows(pid))
        j = jobs.get(jid)
        if not j:
            raise err(404, "job not found")
        return dict(job=job_view(uid, j, detail=True))
    return web.json_response(await th(work))


@H
async def post_resolve(request):
    uid = uid_of(request)
    pid, cid = request.match_info["pid"], request.match_info["cid"]
    b = await body_of(request)
    outcome = b.get("outcome")
    summary = str(b.get("summary") or "").strip()
    steps = b.get("fix_steps") or []
    if outcome not in OUTCOMES:
        raise err(422, "outcome must be one of " + ", ".join(OUTCOMES))
    if not summary or len(summary) > 2000:
        raise err(422, "summary must be 1-2000 characters")
    if isinstance(steps, str):
        steps = [s.strip() for s in steps.splitlines() if s.strip()]
    if not isinstance(steps, list) or len(steps) > 12:
        raise err(422, "fix_steps must be a list of at most 12 steps")
    steps = [str(s)[:300] for s in steps]
    key = str(b.get("idempotency_key") or "")[:80] or f"auto:{sha(outcome + summary)}"

    def work():
        project_for(uid, pid, write=True)
        rid = "res_" + sha(f"{uid}|{cid}|{key}")
        with STATE_LOCK:
            rows = support_rows(pid)
            jobs, cases, resolutions = fold_jobs(rows)
            c = cases.get(cid)
            if not c:
                raise err(404, "case not found")
            prev = next((r for r in resolutions.get(cid, []) if r["id"] == rid), None)
            if prev:
                return dict(idempotent=True, replayed=True, resolution={k: prev.get(k) for k in ("id", "case_id", "outcome", "at")})
            jid = b.get("job_id")
            if jid and (jid not in jobs or jobs[jid].get("case_id") != cid):
                raise err(404, "job not found for this case")
            case = entry_body(uid, c["_row"])
            rc = case.get("run_context") or {}
            t = now_ms()
            res = dict(id=rid, at=t, by=uid, case_id=cid, outcome=outcome, summary=summary, fix_steps=steps, job_id=jid,
                       run_id=case.get("run_id"), error_fingerprint=rc.get("fingerprint"), error_class=rc.get("error_class"),
                       confirmed_by_user=True)
            store(uid, pid, "resolution", res, by=uid, case_id=cid, outcome=outcome, idem=sha(key, 24))
            store(uid, pid, "audit", dict(id="sau_" + uuid.uuid4().hex[:16], at=t, by=uid, action="resolve", target=cid, resolution_id=rid, outcome=outcome),
                  by=uid, action="resolve", case_id=cid)
        rec = reconcile({pid})                 # reindex the derived case/resolution documents now
        return dict(idempotent=False, replayed=False, resolution={k: res.get(k) for k in ("id", "case_id", "outcome", "at", "error_fingerprint")}, reindexed=rec)
    return web.json_response(await th(work))


@H
async def support_status(request):
    uid = uid_of(request)
    es = elastic_status(force=True)
    if uid != SANDBOX_UID:
        es = {k: v for k, v in es.items() if k not in ("documents", "by_kind", "index", "heap_used_mb", "heap_max_mb")}
    return web.json_response(dict(elastic=es, factory=factory_view(), claim_owner=CLAIM_OK,
                                  last_reconcile=_RECON["last"] if uid == SANDBOX_UID else None))


# ── loopback worker interface ────────────────────────────────────────────────
def _worker_token():
    return (HOME / "worker.token").read_text().strip()


def worker_guard(request):
    fwd = [h for h in ("X-Forwarded-For", "X-Forwarded-Host", "X-Forwarded-Prefix", "X-Forwarded-Proto", "X-Real-IP", "Forwarded") if request.headers.get(h)]
    peer = (request.transport.get_extra_info("peername") or ("",))[0] if request.transport else ""
    if fwd or peer not in ("127.0.0.1", "::1"):
        raise web.HTTPNotFound()
    tok = request.headers.get("X-RealiSIM-Worker", "") or request.headers.get("X-RealiSIM-Worker-Token", "")
    try:
        good = _worker_token()
    except OSError:
        raise err(503, "worker token not provisioned")
    if not tok or not hmac.compare_digest(tok.encode(), good.encode()):
        raise web.HTTPNotFound()


def _owner(pid):
    p = project_row(pid)
    if not p:
        raise err(410, "project no longer exists")
    return p


def job_event(j, event, body, **idx):
    """Durable job event, written by the gateway as the project owner (system actor)."""
    p = _owner(j["project_id"])
    e = dict(id="jev_" + uuid.uuid4().hex[:16], at=now_ms(), job_id=j["id"], case_id=j.get("case_id"), event=event, actor="support-system", **body)
    store(p["owner_uid"], j["project_id"], "job_event", e, job_id=j["id"], case_id=j.get("case_id"), event=event, **idx)
    return e


def sweep(jobs=None):
    """Expired leases become interrupted (durable). Never requeued automatically."""
    jobs = jobs if jobs is not None else fold_jobs(support_rows())[0]
    n = 0
    for j in jobs.values():
        if j["status"] == "running" and time.time() > lease_expiry(j):
            try:
                job_event(j, "interrupted", dict(error=dict(code="lease_expired", message="The Factory worker stopped responding; the investigation was interrupted and not retried automatically.")),
                          error_code="lease_expired")
                j["status"] = "interrupted"
                HEARTBEATS.pop(j["id"], None)
                n += 1
            except Exception as ex:
                log("sweep failed", j["id"], type(ex).__name__)
    return n


def _sweep_loop():
    while True:
        time.sleep(20)
        try:
            if CLAIM_OK:
                with STATE_LOCK:
                    sweep()
        except Exception:
            traceback.print_exc()


def running_job(jid, lease_token):
    jobs, _, _ = fold_jobs(support_rows())
    j = jobs.get(jid)
    if not j:
        raise err(404, "job not found")
    if j["status"] != "running":
        raise err(409, f"job is {j['status']}", job_status=j["status"])
    if not lease_token or not hmac.compare_digest(sha(lease_token, 64), str(j.get("lease_hash") or "")):
        raise err(409, "lease token does not match the current lease")
    if time.time() > lease_expiry(j):
        raise err(409, "lease expired")
    return j


def asker_access(j, write=False):
    try:
        return project_for(j["by"], j["project_id"], write)
    except web.HTTPException:
        raise err(403, "the requesting user no longer has access to this project")


def _note_presence(b):
    p = dict(worker_id=str(b.get("worker_id") or "")[:80], factory_key_present=bool(b.get("factory_key_present")),
             sdk={k: str(v)[:60] for k, v in (b.get("sdk") or {}).items()} if isinstance(b.get("sdk"), dict) else None,
             cli={k: str(v)[:60] for k, v in (b.get("cli") or {}).items()} if isinstance(b.get("cli"), dict) else None,
             last_seen=time.time())
    set_presence(p)
    return p


def W(fn):
    async def h(request):
        worker_guard(request)
        return await H(fn)(request)
    return h


@W
async def w_presence(request):
    b = await body_of(request)

    def work():
        _note_presence(b)
        jobs, _, _ = fold_jobs(support_rows())
        c = lambda s: sum(1 for j in jobs.values() if j["status"] == s)
        return dict(queued=c("queued") + c("waiting_for_connection"), running=c("running"), claim_owner=CLAIM_OK)
    return web.json_response(await th(work))


@W
async def w_claim(request):
    b = await body_of(request)
    if not CLAIM_OK:
        raise err(503, "this process does not own the support claim lock")

    def work():
        pres = _note_presence(b)
        if not pres["factory_key_present"]:
            raise err(409, "factory key missing: jobs stay waiting_for_connection", job_status="waiting_for_connection")
        with STATE_LOCK:
            jobs, _, _ = fold_jobs(support_rows())
            sweep(jobs)
            if any(j["status"] == "running" for j in jobs.values()):
                return None                              # global single Factory job
            for j in sorted((j for j in jobs.values() if j["status"] in ("queued", "waiting_for_connection")), key=lambda j: j.get("at") or 0):
                try:
                    asker_access(j, write=True)
                except web.HTTPException:
                    try:
                        job_event(j, "failed", dict(error=dict(code="access_revoked", message="The requesting user no longer has write access.")), error_code="access_revoked")
                    except Exception:
                        pass
                    continue
                token = secrets.token_urlsafe(32)
                exp = now_ms() + LEASE_TTL * 1000
                job_event(j, "claimed", dict(worker_id=pres["worker_id"], lease_expires_at=exp, sdk=pres.get("sdk"), cli=pres.get("cli")),
                          lease_hash=sha(token, 64), lease_expires_at=exp, worker_id=pres["worker_id"])
                HEARTBEATS[j["id"]] = exp / 1000
                body = entry_body(_owner(j["project_id"])["owner_uid"], j["_row"])
                rc = body.get("run_context")
                conv = []
                for pj_ in sorted((x for x in jobs.values() if x.get("case_id") == j["case_id"] and x["id"] != j["id"] and x["status"] == "complete"), key=lambda x: x.get("at") or 0)[-5:]:
                    try:
                        pb = entry_body(_owner(j["project_id"])["owner_uid"], pj_["_row"])
                        fb = entry_body(_owner(j["project_id"])["owner_uid"], pj_["_final"])
                        conv += [dict(role="user", text=EI.sanitize(pb.get("question"), 2000), at=pj_.get("at")),
                                 dict(role="assistant", text=EI.sanitize((fb.get("result") or {}).get("answer"), 2000), at=pj_.get("finished_at"))]
                    except Exception:
                        pass
                q_ = EI.sanitize(body.get("question"), 2000)
                conv.append(dict(role="user", text=q_, at=j.get("at")))
                return dict(job=dict(id=j["id"], case_id=j["case_id"], project_id=j["project_id"], question=q_, attempt=1,
                                     run_context=rc, run_ref=j.get("run_id"), conversation=conv[-12:], limits=LIMITS, created_at=j.get("at"),
                                     created_at_ms=j.get("at"), lease_token=token, lease_expires_at_ms=exp),
                            lease_token=token, lease_expires_at=exp, lease_expires_at_ms=exp, lease_ttl_s=LEASE_TTL)
            return None
    out = await th(work)
    return web.json_response(out) if out else web.Response(status=204)


@W
async def w_heartbeat(request):
    jid = request.match_info["jid"]
    b = await body_of(request)

    def work():
        with STATE_LOCK:
            j = running_job(jid, b.get("lease_token"))
            HEARTBEATS[jid] = time.time() + LEASE_TTL
            steps = [s for s in (b.get("steps") or b.get("progress") or []) if isinstance(s, dict) and s.get("kind") in (None, "status")][:3]
            if steps and j.get("events", 0) < LIMITS["max_tool_calls"] + 8:
                for s in steps:
                    job_event(j, "progress", dict(tool="worker", summary=EI.sanitize(((str(s.get("tool")) + ": ") if s.get("tool") else "") + str(s.get("summary") or ""), 200), status="info"))
            return dict(ok=True, lease_expires_at=int(HEARTBEATS[jid] * 1000), lease_expires_at_ms=int(HEARTBEATS[jid] * 1000), cancel=False, cancel_requested=False)
    return web.json_response(await th(work))


def _tool_prelude(jid, b, tool):
    j = running_job(jid, b.get("lease_token"))
    used = sum(1 for d in j.get("_progress", []) if d.get("tool") not in (None, "worker"))
    if used >= LIMITS["max_tool_calls"]:
        raise err(429, "tool call budget for this investigation is used up")
    asker_access(j)
    HEARTBEATS[jid] = time.time() + LEASE_TTL
    return j


def _record_tool(j, tool, summary, ids=(), count=None, proposal=None, status="ok"):
    job_event(j, "progress", dict(tool=tool, summary=EI.sanitize(summary, 240), result_count=count, returned_ids=list(ids)[:30],
                                  proposal=proposal, status=status), tool=tool, status=status, returned_ids=list(ids)[:30] or None)


@W
async def w_tool(request):
    jid, tool = request.match_info["jid"], request.match_info["tool"]
    b = await body_of(request)
    if isinstance(b.get("args"), dict):
        b = dict(b["args"], lease_token=b.get("lease_token"))
    if tool == "validate_proposal":
        tool = "propose_correction"        # same validated dry run; the worker contract names it validate_proposal
    if tool not in ("search_help", "get_record", "read_project_state", "propose_correction"):
        raise web.HTTPNotFound()

    def work():
        j = _tool_prelude(jid, b, tool)
        uid, pid = j["by"], j["project_id"]
        if tool == "search_help":
            qtext = str(b.get("query") or "")[:500]
            flt = dict(b.get("filters") or {}) if isinstance(b.get("filters"), dict) else {}
            if flt.get("kinds") and not flt.get("kind"):
                flt["kind"] = flt.pop("kinds")
            rid = flt.pop("run_id", None) or (j.get("run_id") if b.get("use_run_context") else None)
            res = do_search(uid, pid, qtext, "project", flt, b.get("limit") or b.get("size") or 8, rid)
            ids = [h["canonical_id"] for h in res["hits"]] + [h["canonical_id"] for h in res["prior_resolutions"]]
            _record_tool(j, tool, f"Searched help and history: {qtext[:120]}", ids, len(ids))
            res.pop("run_context", None)
            return res
        if tool == "get_record":
            cid = str(b.get("canonical_id") or "")[:200]
            hits, dropped = revalidate(uid, [dict(canonical_id=cid, project_id=pid if not cid.startswith("help_doc:") else None,
                                                  visibility="public" if cid.startswith("help_doc:") else "project")], {pid}, keep_body=True)
            if not hits:
                _record_tool(j, tool, f"Record not available: {cid[:80]}", (), 0, status="not_found")
                raise err(404, "record not found or not accessible")
            h = hits[0]
            for k in ("body", "error_text"):
                if h.get(k):
                    h[k] = EI.sanitize(h[k], 6000)
            _record_tool(j, tool, f"Read {h.get('kind')}: {h.get('title', '')[:120]}", [cid], 1)
            return dict(record=h)
        if tool == "read_project_state":
            import copilot
            p = copilot.access(uid, pid)
            state, _ = copilot.project_state(uid, p)
            state = {k: v for k, v in state.items() if not k.startswith("_")}
            txt = EI.sanitize(json.dumps(state, default=str), 14000)
            _record_tool(j, tool, "Read current project state (draft, versions, runs, errors)", (), 1)
            return dict(state=json.loads(txt) if txt.endswith("}") else txt, truncated=not txt.endswith("}"))
        # propose_correction: validated by the copilot's own validator; never applied here
        import copilot
        raw = b.get("proposal") if isinstance(b.get("proposal"), dict) else {}
        accepted = [d for d in j.get("_progress", []) if d.get("tool") == "propose_correction" and d.get("status") == "ok"]
        if len(accepted) >= LIMITS["max_proposals"]:
            raise err(429, "proposal budget for this investigation is used up")
        p = copilot.access(uid, pid, write=True)
        state, avs = copilot.project_state(uid, p)
        wf = state.get("_wf")
        cat, _ = copilot.catalog(uid)
        if wf is not None and state.get("_wf_catalog"):
            cat = [dict(id=x["id"], name=x["name"], available=True) for x in state["_wf_catalog"]]
        jb = entry_body(_owner(pid)["owner_uid"], j["_row"])
        nums = copilot._numbers_in([jb.get("question") or ""])
        try:
            pr, why = copilot.build_proposal(raw, p, avs, cat, state["objects"], nums, wf)
        except Exception as ex:
            pr, why = None, f"invalid proposal ({type(ex).__name__})"
        if not pr:
            _record_tool(j, tool, f"Proposal rejected by validation: {why}", (), 0, status="rejected")
            return dict(accepted=False, ok=False, proposal=None, reason=why)
        pr["status"] = "pending"
        pr["origin"] = "factory_support"
        _record_tool(j, tool, f"Validated proposal: {pr.get('title')}", (), 1, proposal=dict(pr, _raw={k: raw.get(k) for k in ("kind", "target", "values", "sources", "rationale", "evidence")}))
        return dict(accepted=True, ok=True, reason=None, proposal={k: pr.get(k) for k in ("id", "kind", "target", "title", "values", "changes", "mode", "base_revision")})
    return web.json_response(await th(work))


def _terminal_replay(jid, key):
    jobs, _, _ = fold_jobs(support_rows())
    j = jobs.get(jid)
    if j and j["status"] in TERMINAL and j.get("idem_final") and j["idem_final"] == sha(key, 24):
        return j
    return None


def _answer_text(ans):
    """Accept a plain answer or the worker's structured {diagnosis, uncertainty, uncertainty_note, next_steps}."""
    if isinstance(ans, dict):
        parts = [str(ans.get("diagnosis") or "").strip()]
        steps = [x for x in (ans.get("next_steps") or []) if isinstance(x, dict)][:5]
        if steps:
            parts.append("Next steps:\n" + "\n".join(f"- {str(x.get('title') or '')[:120]}: {str(x.get('detail') or '')[:400]}" for x in steps))
        if ans.get("uncertainty"):
            parts.append(f"Uncertainty: {ans.get('uncertainty')}" + (f" ({str(ans.get('uncertainty_note'))[:400]})" if ans.get("uncertainty_note") else ""))
        structured = dict(diagnosis=EI.sanitize(ans.get("diagnosis"), 1200),
                          uncertainty=ans.get("uncertainty") if ans.get("uncertainty") in ("low", "medium", "high") else "high",
                          uncertainty_note=EI.sanitize(ans.get("uncertainty_note"), 400) or None,
                          next_steps=[dict(title=EI.sanitize(x.get("title"), 120), detail=EI.sanitize(x.get("detail"), 400),
                                           requires=x.get("requires") if x.get("requires") in ("user", "reviewed_change", "none") else "user") for x in steps])
        return "\n\n".join(p for p in parts if p), structured
    return str(ans or "").strip(), None


def _evidence(rt):
    """Exact provider evidence (bounded, sanitised): no thinking, no credentials."""
    rt = rt if isinstance(rt, dict) else {}
    rt = {k: v for k, v in rt.items() if k.lower() not in ("thinking", "reasoning", "api_key", "key", "token", "messages", "prompt")}
    txt = EI.sanitize(json.dumps(rt, default=str), 4000)
    try:
        return json.loads(txt)
    except Exception:
        return {"truncated": txt}


@W
async def w_finish(request):
    jid = request.match_info["jid"]
    b = await body_of(request)

    def work():
        with STATE_LOCK:
            key = str(b.get("idempotency_key") or "")[:80] or ("lease:" + sha(b.get("lease_token") or "", 32))
            rep = _terminal_replay(jid, key)
            if rep:
                return dict(ok=True, idempotent=True, replayed=True, job=job_view(rep["by"], rep))
            j = running_job(jid, b.get("lease_token"))
            res = b.get("result") if isinstance(b.get("result"), dict) else {}
            rt = res.get("runtime") if isinstance(res.get("runtime"), dict) else res.get("provider") if isinstance(res.get("provider"), dict) else {}
            if not (rt.get("provider") == "factory" or str(rt.get("name") or "").startswith("factory")):
                raise err(422, "provider evidence must identify Factory (runtime.provider='factory' or provider.name='factory-droid-sdk'); no non-Factory fallback may be recorded as a Factory job")
            answer, structured = _answer_text(res.get("answer"))
            answer = EI.sanitize(answer, LIMITS["max_answer_chars"])
            if not answer:
                raise err(422, "result.answer required")
            uid, pid = j["by"], j["project_id"]
            owner = _owner(pid)["owner_uid"]
            seen, validated = set(), {}
            for d in j.get("_progress", []):
                seen.update(d.get("returned_ids") or [])
                if d.get("tool") == "propose_correction" and d.get("status") == "ok":
                    try:
                        pb = entry_body(owner, d)
                        if pb.get("proposal"):
                            validated[pb["proposal"]["id"]] = pb["proposal"]
                    except Exception:
                        pass
            raw_c = res.get("citations") if res.get("citations") is not None else res.get("citation_ids") or []
            req = []
            for c in raw_c if isinstance(raw_c, list) else []:
                cid = c.get("canonical_id") if isinstance(c, dict) else c
                if isinstance(cid, str) and cid not in req:
                    req.append(cid[:200])
            req = req[:LIMITS["max_citations"] * 2]
            cites = [c for c in req if c in seen]
            rejected = [dict(id=c, reason="not returned by this investigation's tools") for c in req if c not in seen]
            ok, _ = revalidate(uid, [dict(canonical_id=c, project_id=None if c.startswith("help_doc:") else pid,
                                          visibility="public" if c.startswith("help_doc:") else "project") for c in cites], {pid})
            okids = [h["canonical_id"] for h in ok][:LIMITS["max_citations"]]
            rejected += [dict(id=c, reason="no longer accessible") for c in cites if c not in okids]
            # proposals: only ones this job validated through the tool, re-validated now against current state
            refs = res.get("proposal_refs") if isinstance(res.get("proposal_refs"), list) else None
            if refs is None and isinstance(res.get("proposals"), list):
                refs = [x.get("id") if isinstance(x, dict) else x for x in res["proposals"]]
            if refs is None:
                refs = list(validated)
            refs = [r for r in refs if isinstance(r, str)][:LIMITS["max_proposals"] * 2]
            proposals, rejp = [], [dict(id=r, reason="not validated by this investigation") for r in refs if r not in validated]
            chosen = [validated[r] for r in refs if r in validated][:LIMITS["max_proposals"]]
            if chosen:
                import copilot
                p = copilot.access(uid, pid, write=True)
                state, avs = copilot.project_state(uid, p)
                wf = state.get("_wf")
                cat, _ = copilot.catalog(uid)
                if wf is not None and state.get("_wf_catalog"):
                    cat = [dict(id=x["id"], name=x["name"], available=True) for x in state["_wf_catalog"]]
                jb = entry_body(owner, j["_row"])
                nums = copilot._numbers_in([jb.get("question") or ""])
                for old in chosen:
                    try:
                        pr, why = copilot.build_proposal(old.get("_raw") or {}, p, avs, cat, state["objects"], nums, wf)
                    except Exception as ex:
                        pr, why = None, f"invalid ({type(ex).__name__})"
                    if pr:
                        pr.update(status="pending", origin="factory_support", validated_in_job=old["id"])
                        proposals.append(pr)
                    else:
                        rejp.append(dict(id=old["id"], title=old.get("title"), reason=f"no longer valid: {why}"))
            turn_id = None
            if proposals:
                try:
                    import copilot
                    jb = entry_body(owner, j["_row"])
                    turn = dict(id="cpt_" + uuid.uuid4().hex[:16], at=now_ms(), by=uid, message=jb.get("question") or "",
                                reply=answer[:2000], blockers=[], proposals=proposals, rejected_suggestions=[dict(kind=None, reason=x["reason"]) for x in rejp],
                                model="factory:" + str(rt.get("model") or "droid")[:60],
                                usage={k: (rt.get("usage") or {}).get(k) for k in ("input_tokens", "output_tokens")} if isinstance(rt.get("usage"), dict) else {},
                                mode="workflow" if any(x.get("mode") == "workflow" for x in proposals) else "legacy",
                                origin="factory_support", support_job_id=jid, grounded_on=dict(support_job=jid))
                    copilot.store_entry(uid, pid, "turn", turn)
                    turn_id = turn["id"]
                except Exception as ex:
                    rejp += [dict(id=x["id"], title=x.get("title"), reason=f"could not be stored for approval ({type(ex).__name__})") for x in proposals]
            result = dict(answer=answer, structured=structured, citations=okids, rejected_citations=rejected, copilot_turn_id=turn_id,
                          proposal_ids=[x["id"] for x in proposals] if turn_id else [], rejected_proposals=rejp, runtime=_evidence(rt))
            job_event(j, "complete", dict(result=result), idem=sha(key, 24))
            HEARTBEATS.pop(jid, None)
            jobs, _, _ = fold_jobs(support_rows(pid))
        request_reconcile(pid)
        return dict(ok=True, idempotent=False, replayed=False, job=job_view(uid, jobs[jid], detail=True))
    return web.json_response(await th(work))


FAIL_ALIASES = {"connection_required": "missing_key", "provider_error": "sdk_error", "timeout": "timeout", "tool_limit": "tool_error",
                "internal": "other", "cancelled": "other"}


@W
async def w_fail(request):
    jid = request.match_info["jid"]
    b = await body_of(request)
    raw = b.get("error_code") or b.get("code")
    code = raw if raw in FAIL_CODES else FAIL_ALIASES.get(raw, "other")

    def work():
        with STATE_LOCK:
            key = str(b.get("idempotency_key") or "")[:80] or ("lease:" + sha(b.get("lease_token") or "", 32))
            rep = _terminal_replay(jid, key)
            if rep:
                return dict(ok=True, idempotent=True, replayed=True, job=job_view(rep["by"], rep))
            j = running_job(jid, b.get("lease_token"))
            job_event(j, "failed", dict(error=dict(code=code, worker_code=str(raw or "")[:40] or None, message=EI.sanitize(b.get("message"), 500)),
                                        uncertain_charge=bool(b.get("uncertain_charge")), runtime=_evidence(b.get("provider") or b.get("runtime"))),
                      idem=sha(key, 24), error_code=code)
            HEARTBEATS.pop(jid, None)
            jobs, _, _ = fold_jobs(support_rows(j["project_id"]))
        return dict(ok=True, idempotent=False, replayed=False, job=job_view(j["by"], jobs[jid], detail=True))
    return web.json_response(await th(work))


@W
async def w_status(request):
    """Provider readiness without a job (alias of presence in the worker's own vocabulary)."""
    b = await body_of(request)
    st = b.get("state")

    def work():
        _note_presence(dict(worker_id=b.get("worker_id"), factory_key_present=st == "ready",
                            sdk={"name": "droid-sdk", "version": b.get("sdk_version")} if b.get("sdk_version") else b.get("sdk"),
                            cli={"version": b.get("cli_version")} if b.get("cli_version") else b.get("cli")))
        return dict(ok=True, claim_owner=CLAIM_OK)
    return web.json_response(await th(work))


# ── registration ─────────────────────────────────────────────────────────────
class _Svc:
    def __init__(self, src):
        self.src = src

    def get(self, name, default=None):
        return self.src.get(name, default) if isinstance(self.src, dict) else getattr(self.src, name, default)

    def __getattr__(self, name):
        v = self.get(name)
        if v is None:
            raise AttributeError(f"support: services must provide {name!r}")
        return v


def _take_lock():
    global _LOCKFILE, CLAIM_OK
    HOME.mkdir(parents=True, exist_ok=True)
    _LOCKFILE = open(HOME / "claim.lock", "a+")
    try:
        fcntl.flock(_LOCKFILE.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        _LOCKFILE.seek(0)
        _LOCKFILE.truncate()
        _LOCKFILE.write(str(os.getpid()))
        _LOCKFILE.flush()
        CLAIM_OK = True
    except OSError:
        CLAIM_OK = False
        log("claim lock held by another process; worker claim/finish routes return 503 here")


def register(app, services=None, background=True):
    """Add support routes. services: the server module (db, project_for, rows, pj, JSONCOLS, RUNLOGS)."""
    global S, SANDBOX_UID, STARTED_AT
    S = _Svc(services or {})
    for n in ("db", "project_for", "rows", "pj", "JSONCOLS"):
        getattr(S, n)
    try:
        from env import get_user_id
        SANDBOX_UID = get_user_id()
    except Exception:
        SANDBOX_UID = None
    STARTED_AT = time.time()
    _take_lock()
    if not (HOME / "worker.token").exists():
        (HOME / "worker.token").write_text(secrets.token_urlsafe(32))
        os.chmod(HOME / "worker.token", 0o600)
    r = app.router
    r.add_get("/api/support/status", support_status)
    r.add_get("/api/projects/{pid}/support", get_support)
    r.add_post("/api/projects/{pid}/support/search", post_search)
    r.add_post("/api/projects/{pid}/support/ask", post_ask)
    r.add_get("/api/projects/{pid}/support/jobs/{jid}", get_job)
    r.add_post("/api/projects/{pid}/support/cases/{cid}/resolve", post_resolve)
    r.add_post("/internal/support/presence", w_presence)
    r.add_post("/internal/support/status", w_status)
    r.add_post("/internal/support/claim", w_claim)
    r.add_post("/internal/support/jobs/{jid}/heartbeat", w_heartbeat)
    r.add_post("/internal/support/jobs/{jid}/tools/{tool}", w_tool)
    r.add_post("/internal/support/jobs/{jid}/finish", w_finish)
    r.add_post("/internal/support/jobs/{jid}/fail", w_fail)
    if background:
        threading.Thread(target=_reconcile_loop, daemon=True, name="support-reconcile").start()
        threading.Thread(target=_sweep_loop, daemon=True, name="support-sweep").start()
        _RECON["event"].set()
    return ["/api/support/status", "/api/projects/{pid}/support", "/api/projects/{pid}/support/search", "/api/projects/{pid}/support/ask",
            "/api/projects/{pid}/support/jobs/{jid}", "/api/projects/{pid}/support/cases/{cid}/resolve",
            "/internal/support/* (loopback worker only)"]
