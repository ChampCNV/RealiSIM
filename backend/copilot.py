"""Setup copilot for the robotics workspace: a bounded, typed Claude assistant.

Grounded in the project's real state (uploads, asset versions with accepted/rejected measurements, latest runs and
their logs, robot catalog, workcell table/placements). Claude only *proposes* structured changes; a person approves
each one through a separate endpoint, which re-validates it, rejects it if the targeted values changed since it was
proposed, and applies it through the server's own endpoints over loopback (so backend validation applies).

Storage: SpacetimeDB artifact manifests of kind 'copilot', one immutable verified JSON file per entry (turn,
decision, audit). Nothing is updated in place and nothing is written to Firebase.


    import copilot; copilot.register(app, services)   # services: dict or the server module (never imported here)
"""
import asyncio, json, math, re, threading, time, traceback, uuid
from collections import defaultdict, deque
from pathlib import Path

import requests
from aiohttp import web

MODEL = "claude-sonnet-5"
MAX_MSG = 2000
TURN_LIMIT = (40, 3600)       # turns per user per hour

TABLE_FIELDS = {"length_m": ("Table length", 0.2, 4.0), "width_m": ("Table width", 0.2, 4.0), "height_m": ("Table height", 0.01, 1e6)}
PLACE_FIELDS = {"x_m": ("x", -3.0, 3.0), "y_m": ("y", -3.0, 3.0), "yaw_deg": ("yaw", -180.0, 180.0)}
OBJ_FIELDS = {"length_cm": ("Length", 0.5, 200), "width_cm": ("Width", 0.5, 200), "height_cm": ("Height", 0.5, 200),
              "mass_g": ("Mass", 0.5, 5000), "friction": ("Friction", 0.05, 2.0), "material": ("Material", None, None)}
UNITS = {"length_m": "m", "width_m": "m", "height_m": "m", "x_m": "m", "y_m": "m", "yaw_deg": "°",
         "length_cm": "cm", "width_cm": "cm", "height_cm": "cm", "mass_g": "g", "friction": "μ", "material": ""}
SRC_IN = ("user_measured", "user_stated", "project_data", "estimated")

S = None                      # services accessor, set by register()
SANDBOX_UID = None
_CACHE = {}                   # file id -> decoded entry (entries are immutable)
_LOCKS = defaultdict(threading.Lock)
_RATE = defaultdict(deque)


class _Svc:
    def __init__(self, src):
        self.src = src

    def get(self, name, default=None):
        if isinstance(self.src, dict):
            return self.src.get(name, default)
        return getattr(self.src, name, default)

    def need(self, name):
        v = self.get(name)
        if v is None:
            raise RuntimeError(f"copilot.register: services must provide {name!r}")
        return v


def now_ms():
    return time.time() * 1000


def new_id(prefix):
    return f"{prefix}_{uuid.uuid4().hex[:16]}"


def q(s):
    return "'" + str(s).replace("'", "''") + "'"


def err(status, msg):
    cls = {400: web.HTTPBadRequest, 403: web.HTTPForbidden, 404: web.HTTPNotFound, 409: web.HTTPConflict,
           422: web.HTTPUnprocessableEntity, 429: web.HTTPTooManyRequests,
           502: web.HTTPBadGateway, 503: web.HTTPServiceUnavailable}.get(status, web.HTTPBadRequest)
    return cls(text=json.dumps({"error": msg}), content_type="application/json")


def uid_of(request):
    uid = request.headers.get("X-Sender-Uid")
    if not uid:
        raise err(403, "sign in required")
    return uid


def rate(uid, kind, limit):
    n, win = limit
    dq = _RATE[(uid, kind)]
    t = time.time()
    while dq and dq[0] < t - win:
        dq.popleft()
    if len(dq) >= n:
        raise err(429, f"copilot limit reached ({n} {kind} requests per hour); try again later")
    dq.append(t)


def H(fn):
    async def h(request):
        try:
            return await fn(request)
        except web.HTTPException:
            raise
        except Exception as e:
            msg = str(e)
            if type(e).__name__ == "StdbError":
                raise err(403 if "access" in msg else 409, msg.replace("The module instance encountered a fatal error: ", ""))
            traceback.print_exc()
            raise err(502 if isinstance(e, requests.RequestException) else 400, f"{type(e).__name__}: {msg[:300]}")
    return h


def th(fn, *a, **k):
    def safe():
        try:
            return fn(*a, **k)
        except StopIteration as e:          # would otherwise hang the awaiting future
            raise RuntimeError("internal lookup failed") from e
    return asyncio.to_thread(safe)


def redact(s):
    s = re.sub(r"eyJ[\w-]{8,}\.[\w-]{8,}\.[\w-]{8,}", "[redacted-token]", str(s or ""))
    s = re.sub(r"(?i)(bearer|token|api[_-]?key|authorization)([\"'=:\s]+)[\w.\-]{12,}", r"\1\2[redacted]", s)
    return re.sub(r"hf_[A-Za-z0-9]{20,}|sk_car_[A-Za-z0-9_]{10,}", "[redacted]", s)


def jdump(v, n):
    t = json.dumps(v, separators=(",", ":"), default=str)
    return t if len(t) <= n else t[:n] + "…"


# ── access ────────────────────────────────────────────────────────────────────
def access(uid, pid, write=False):
    p = S.need("project_for")(uid, pid, write)          # raises 404 for no access (server convention)
    st = p.get("settings_json")
    p["settings_json"] = st if isinstance(st, dict) else (json.loads(st) if isinstance(st, str) and st[:1] == "{" else {})
    billed_ok = p["owner_uid"] == SANDBOX_UID or uid == SANDBOX_UID
    p["_can_write"] = write or _has_write(uid, p)
    p["_can_model"] = p["_can_write"] and billed_ok
    return p


def _has_write(uid, p):
    if p["owner_uid"] == uid or p.get("role") == "editor":
        return True
    return False


# ── loopback to the server's own endpoints ───────────────────────────────────
def port():
    v = S.get("PORT")
    if v:
        return int(v)
    return int((Path(__file__).resolve().parent / "port").read_text().strip())


def loop(method, path, uid, body=None, timeout=60):
    r = requests.request(method, f"http://127.0.0.1:{port()}{path}", headers={"X-Sender-Uid": uid, "Content-Type": "application/json"},
                         data=json.dumps(body) if body is not None else None, timeout=timeout)
    try:
        j = r.json()
    except Exception:
        j = {"error": r.text[:300]}
    return r.status_code, j


# ── storage: immutable copilot entries in SpacetimeDB ─────────────────────────
def store_entry(uid, pid, etype, entry):
    db = S.need("db")
    data = json.dumps(entry, separators=(",", ":"), default=str).encode()
    idx = {k: entry.get(k) for k in ("id", "turn_id", "proposal_id", "action", "at", "by") if entry.get(k) is not None}
    idx["type"] = etype
    mid, files = db.store_dir(uid, pid, "copilot", entry["id"], etype, [(f"copilot/{etype}/{entry['id']}.json", data, "application/json")], deps=idx)
    _CACHE[files[0]["id"]] = entry
    return mid


def load_entries(uid, pid, limit=160):
    db = S.need("db")
    rows = db.sql(f"SELECT id, kind, version, status, files_json, created_at FROM artifact_manifest WHERE project_id = {q(pid)}")
    rows = [r for r in rows if r.get("kind") == "copilot" and r.get("status") == "verified"]
    rows.sort(key=lambda r: float(r.get("created_at") or 0))
    out = []
    for r in rows[-limit:]:
        try:
            files = json.loads(r["files_json"]) if isinstance(r["files_json"], str) else r["files_json"]
            fid = files[0]["id"]
            if fid not in _CACHE:
                _, data = db.read_bytes(uid, pid, fid)
                _CACHE[fid] = json.loads(data.decode())
            e = json.loads(json.dumps(_CACHE[fid]))
            e["_type"] = r["version"]
            out.append(e)
        except Exception as ex:
            print("copilot: unreadable entry", r.get("id"), ex, flush=True)
    return out


def thread_state(entries):
    turns = [e for e in entries if e["_type"] == "turn"]
    decisions = {}
    hints = []
    for e in entries:
        if e["_type"] == "decision":
            decisions.setdefault(e["proposal_id"], e)          # first decision is final (approvals are serialised)
    for t in turns:
        for p in t.get("proposals", []):
            d = decisions.get(p["id"])
            p["status"] = d["action"] if d else "pending"
            if d:
                p["decision"] = {k: d.get(k) for k in ("id", "action", "by", "at", "error", "after", "next")}
                if d["action"] == "applied" and p["kind"] == "robot_hint":
                    hints.append(dict(library_id=p["values"]["library_id"], name=p.get("title"), proposal_id=p["id"], approved_at=d["at"]))
    return turns, decisions, (hints[-1] if hints else None)


def find_proposal(turns, prop_id):
    for t in turns:
        for p in t.get("proposals", []):
            if p["id"] == prop_id:
                return t, p
    return None, None


# ── project state used for grounding and validation ──────────────────────────
def catalog(uid):
    code, j = 0, {}
    try:
        code, j = loop("GET", "/api/robots", uid, timeout=20)
    except Exception:
        pass
    if code == 200 and isinstance(j.get("catalog"), list):
        return [dict(id=r.get("library_id") or r.get("id"), name=r.get("name"), maker=r.get("maker"), dof=r.get("dof"),
                     gripper=r.get("gripper"), available=True, note=(r.get("note") or "")[:160]) for r in j["catalog"]], "robot library"
    prof = S.get("ROBOT_PROFILES") or []
    return [dict(id=p["id"], name=p["name"], available=bool(p.get("available")), note=p.get("note", "")[:160]) for p in prof], "legacy profiles"


def project_state(uid, p):
    rows = S.need("rows")
    pid = p["id"]
    st = p["settings_json"]
    wc = st.get("workcell") if isinstance(st.get("workcell"), dict) else {}
    newest = lambda xs: sorted(xs, key=lambda r: -(r.get("created_at") or 0))
    ups = newest(rows("upload", pid))
    avs = newest(rows("asset_version", pid))
    svs = newest(rows("scene_version", pid))
    runs = newest(rows("pipeline_run", pid))
    exps = newest(rows("gym_export", pid))
    sims = newest(rows("simulation_run", pid))
    objects = []
    for a in avs:
        if a["status"] in ("accepted", "needs_review") and a["object_name"] not in objects:
            objects.append(a["object_name"])
    for k in (wc.get("placements") or {}):
        if k not in objects:
            objects.append(k)
    latest = runs[0] if runs else None
    log = ""
    if latest:
        live = (S.get("LIVE") or {}).get(latest["id"])
        if live is not None and getattr(live, "text", None):
            log = live.text
        else:
            rl = S.get("RUNLOGS")
            f = Path(rl) / f"{latest['id']}.log" if rl else None
            log = f.read_text(errors="replace") if f and f.exists() else (latest.get("log_tail") or "")
        log = redact("\n".join([l for l in log.splitlines() if l.strip()][-40:]))[-2500:]
    meas = lambda m: {k: {"value": (v or {}).get("value"), "source": (v or {}).get("source")} for k, v in (m or {}).items() if isinstance(v, dict)}
    wcx = {}
    try:
        c, j = loop("GET", f"/api/projects/{pid}/workcell", uid, timeout=20)
        if c == 200:
            wcx = j
    except Exception:
        pass
    run_brief = lambda r: dict(id=r["id"], kind=r["kind"], status=r["status"], stage=r.get("stage"), error=redact(r.get("error"))[:400],
                               at=r.get("created_at"), stages={s.get("key"): s.get("status") for s in (r.get("stages_json") or []) if isinstance(s, dict)})
    state = dict(
        project=dict(id=pid, name=p["name"], legacy_robot_profile=p.get("robot_profile")),
        robot=dict(confirmed=st.get("robot") or None),
        workcell=dict(table=wc.get("table"), placements=wc.get("placements") or {}),
        objects=objects,
        uploads=[dict(filename=u["filename"], mode=u["capture_mode"], status=u["status"], error=u.get("error") or None,
                      duration_s=(u.get("meta_json") or {}).get("duration") if isinstance(u.get("meta_json"), dict) else None) for u in ups[:10]],
        asset_versions=[dict(id=a["id"], object=a["object_name"], version=a["version"], status=a["status"], measurements=meas(a.get("measurements_json")),
                             review=(a.get("review_json") or {}).get("action") if isinstance(a.get("review_json"), dict) else None,
                             review_note=((a.get("review_json") or {}).get("note") or None) if isinstance(a.get("review_json"), dict) else None) for a in avs[:8]],
        scene_versions=[dict(id=s["id"], version=s["version"], status=s["status"], validation=jdump(s.get("validation_json"), 300)) for s in svs[:3]],
        simulations=[dict(result=s.get("result"), metrics=jdump(s.get("metrics_json"), 300), at=s.get("created_at")) for s in sims[:3]],
        runs=[run_brief(r) for r in runs[:4]],
        latest_run_log=log,
        exports=[dict(status=e["status"], package_version=e.get("package_version"), env_id=e.get("env_id")) for e in exps[:2]],
        workcell_runs=dict(detections=[jdump((r.get("output_json") or {}), 600) for r in (wcx.get("detections") or [])[:1]],
                           imports=[run_brief(r) for r in (wcx.get("imports") or [])[:2]],
                           workcells=[dict(run_brief(r), warnings=((r.get("output_json") or {}).get("workcell") or {}).get("warnings")) for r in (wcx.get("workcells") or [])[:2]]),
    )
    wf = wf_of(p)
    if wf is not None:
        view = wf_view(uid, pid)
        if view:
            wf = view["workflow"]
        state["workflow"] = wf_brief(wf, view)
        state.pop("workcell", None)            # the draft is the source of truth for these inputs
        state["objects"] = [o.get("name") for o in wf.get("objects") or [] if o.get("include") and o.get("name")]
        state["_wf"] = wf
        state["_wf_catalog"] = (view or {}).get("catalog")
    return state, avs


# ── guided workflow drafts (project.settings_json.workflow, owned by workflow.py) ──────────────
WF_PLACE_FIELDS = {"x_m": ("x (table frame)", -3.0, 3.0), "y_m": ("y (table frame)", -3.0, 3.0), "yaw_deg": ("yaw", -180.0, 180.0)}
WF_RULES = """MODE: guided workflow draft. The project's inputs live in the draft shown under `workflow` (revision {rev}); edit proposals change that draft only, and nothing is built until the user presses Build in the app.
- table: values {{length_m,width_m,height_m}} in metres (draft table fields).
- placement: target = an included workflow object name; values {{x_m,y_m,yaw_deg}} in the TABLE frame (origin at the table-top centre, x along the length, y along the width). These manual overrides only take effect when the robot base position on the table is set in the layout step; say so if it is not.
- robot_hint: values {{library_id}} from the catalog; this selects the robot model in the draft (it is imported and checked when the user builds).
- object_measurements: target = workflow object name; values among {{length_cm,width_cm,height_cm,mass_g,friction,material}}.
Never propose build, analysis, layout runs, approval or export; tell the user which button to press instead. Asset review and rebuild of old asset versions are not available from here."""


def wf_of(p):
    w = (p.get("settings_json") or {}).get("workflow")
    return w if isinstance(w, dict) else None


def wf_view(uid, pid):
    """The workflow service's own view (draft + readiness + catalog); None if the endpoint is not available."""
    try:
        code, j = loop("GET", f"/api/projects/{pid}/workflow", uid, timeout=20)
    except Exception:
        return None
    return j if code == 200 and isinstance(j.get("workflow"), dict) else None


def fv(f):
    v = (f or {}).get("value") if isinstance(f, dict) else f
    return None if v in (None, "") else v


def wf_obj(wf, target):
    return next((o for o in wf.get("objects") or [] if target in (o.get("id"), o.get("name"))), None)


def wf_overrides(wf):
    return ((wf.get("layout") or {}).get("alignment") or {}).get("object_overrides") or {}


def _arm_model(wf, a):
    """Simulated model for one arm, with workflow.arm_library's precedence (per-arm user choice, then the user's
    global choice, then the arm's reviewed hint, then the global selection). Looked up at call time because this
    module never imports the server or workflow modules."""
    import sys
    W = sys.modules.get("workflow")
    if W is not None and hasattr(W, "arm_library"):
        return W.arm_library(wf, a)[0]
    rb = wf.get("robot") or {}
    if a.get("library_origin") == "user" and a.get("library_id"):
        return a["library_id"]
    if rb.get("selected_origin") == "user" and rb.get("selected_library_id"):
        return rb["selected_library_id"]
    return a.get("library_id") or rb.get("selected_library_id")


def wf_brief(wf, view):
    fld = lambda f: {k: (f or {}).get(k) for k in ("value", "source", "origin", "confidence", "note") if (f or {}).get(k) not in (None, "")}
    rb = wf.get("robot") or {}
    an = wf.get("analysis") or {}
    b = wf.get("build") or {}
    lay = wf.get("layout") or {}
    al = lay.get("alignment") or {}
    return dict(
        revision=int(wf.get("revision") or 0), mode=wf.get("mode"),
        objects=[dict(name=o.get("name"), label=o.get("label"), include=o.get("include"), exclusion_reason=o.get("exclusion_reason") or None,
                      origin=o.get("origin"), fields={k: fld(v) for k, v in (o.get("fields") or {}).items()},
                      coverage={k: (o.get("coverage") or {}).get(k) for k in ("adequate", "note", "frames_visible")},
                      inputs=len(o.get("selected_crop_ids") or []) + len(o.get("extra_upload_ids") or []),
                      build=(o.get("build") or {}).get("status")) for o in (wf.get("objects") or [])[:12]],
        table={k: fld((wf.get("table") or {}).get(k)) for k in TABLE_FIELDS},
        robot=dict(selected_library_id=rb.get("selected_library_id"), selected_origin=rb.get("selected_origin"), suggestion=rb.get("suggestion"),
                   multi_arm=True,   # every arm marked use is composed, simulated and exported (no one-arm limit)
                   arms=[dict(name=a.get("name"), label=a.get("label"), decision=a.get("decision"), status=a.get("status"), library_id=a.get("library_id"),
                              library_origin=a.get("library_origin"), simulated_model=_arm_model(wf, a) if a.get("decision") == "use" else None,
                              candidates=[(x.get("library_id"), x.get("confidence")) for x in (a.get("candidates") or [])[:3]]) for a in (rb.get("arms") or [])[:24]],
                   arms_total=len(rb.get("arms") or [])),
        layout=dict(status=lay.get("status"), robot_base_on_table=bool(al.get("robots")), manual_placements=wf_overrides(wf)),
        analysis=dict(status=an.get("status"), error=redact(an.get("error"))[:300] or None, summary=(an.get("summary") or "")[:400] or None,
                      excluded=(an.get("excluded") or [])[:6]) if an else None,
        build=dict(phase=b.get("phase"), error=redact(b.get("error"))[:300] or None, action=b.get("action"),
                   steps=[(s.get("label"), s.get("status"), (s.get("error") or s.get("detail") or "")[:160]) for s in (b.get("steps") or [])]) if b else None,
        readiness=(view or {}).get("readiness"),
    )


# ── proposal validation (used at creation and again at approval) ─────────────
def _num(v):
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return f if math.isfinite(f) else None


def _numbers_in(texts):
    out = []
    for t in texts:
        for m in re.findall(r"-?\d+(?:[.,]\d+)?", t or ""):
            try:
                out.append(float(m.replace(",", ".")))
            except ValueError:
                pass
    return out


def _stated(v, unit, nums):
    """True if the value (in its stored unit) appears among numbers the user actually wrote."""
    cands = [v]
    if unit == "m":
        cands += [v * 100, v * 1000]
    elif unit == "cm":
        cands += [v * 10, v / 100]
    elif unit == "g":
        cands += [v / 1000]
    return any(abs(n - c) <= max(1e-6, 0.005 * abs(c)) for n in nums for c in cands)


def _to_source(kind, claimed, ok_stated):
    """-> (basis, note). basis is the provenance shown to the user: measured | supplied | estimated."""
    if claimed in ("user_measured", "user_stated") and not ok_stated:
        return "estimated", "downgraded: the number is not in your own words"
    return {"user_measured": "measured", "user_stated": "supplied", "project_data": "supplied"}.get(claimed, "estimated"), None


def current_values(p, avs, kind, target, wf=None):
    if wf is not None:
        if kind == "table":
            return {k: fv((wf.get("table") or {}).get(k)) for k in TABLE_FIELDS}
        if kind == "placement":
            o = wf_overrides(wf).get(target) or {}
            return {k: fv(o.get(k)) for k in WF_PLACE_FIELDS}
        if kind == "robot_hint":
            return {"library_id": (wf.get("robot") or {}).get("selected_library_id")}
        if kind == "object_measurements":
            o = wf_obj(wf, target)
            return {k: fv((o.get("fields") or {}).get(k)) for k in OBJ_FIELDS} if o else None
        return None
    st = p["settings_json"]
    wc = st.get("workcell") if isinstance(st.get("workcell"), dict) else {}
    if kind == "table":
        t = wc.get("table") or {}
        return {k: _num(t.get(k)) for k in TABLE_FIELDS}
    if kind == "placement":
        pl = (wc.get("placements") or {}).get(target) or {}
        return {k: _num(pl.get(k)) for k in PLACE_FIELDS}
    if kind == "robot_hint":
        r = st.get("robot") or {}
        return {"library_id": r.get("library_id")}
    if kind == "object_measurements":
        a = next((a for a in avs if a["id"] == target), None)
        if not a:
            return None
        m = a.get("measurements_json") or {}
        out = {k: (m.get(k) or {}).get("value") for k in OBJ_FIELDS}
        out["_status"] = a["status"]
        return out
    return None


def build_proposal(raw, p, avs, cat, objects, user_nums, wf=None):
    kind = raw.get("kind")
    target = raw.get("target")
    if wf is not None:
        pr, why = _build_proposal(raw, p, avs, cat, objects, user_nums, wf)
        if pr:
            pr.update(mode="workflow", base_revision=int(wf.get("revision") or 0))
        return pr, why
    return _build_proposal(raw, p, avs, cat, objects, user_nums, None)


def _build_proposal(raw, p, avs, cat, objects, user_nums, wf):
    kind = raw.get("kind")
    target = raw.get("target")
    vals = raw.get("values") or {}
    srcs = raw.get("sources") or {}
    if not isinstance(vals, dict) or not vals:
        return None, "no values"
    changes, values = [], {}
    if kind == "table":
        spec = TABLE_FIELDS
        target = None
    elif kind == "placement":
        spec = WF_PLACE_FIELDS if wf is not None else PLACE_FIELDS
        if target not in objects:
            return None, f"unknown object {target!r}"
    elif kind == "robot_hint":
        lid = str(vals.get("library_id") or "")
        hit = next((c for c in cat if c["id"] == lid), None)
        if not hit or not hit.get("available"):
            return None, f"robot {lid!r} is not in the available catalog"
        cur = current_values(p, avs, kind, None, wf)
        if cur["library_id"] == lid:
            return None, "that robot is already the " + ("selected" if wf is not None else "confirmed") + " model"
        return dict(id=new_id("prp"), kind=kind, target=None, title=(f"Select {hit['name']}" if wf is not None else f"Use {hit['name']}"), values={"library_id": lid},
                    changes=[dict(field="library_id", label="Robot model (hint)", unit="", current=cur["library_id"], proposed=lid, basis="supplied", source="supplied")],
                    base=cur, rationale=str(raw.get("rationale") or "")[:600], evidence=[str(x)[:240] for x in (raw.get("evidence") or [])][:4]), None
    elif kind == "object_measurements" and wf is not None:
        spec = OBJ_FIELDS
        o = wf_obj(wf, target)
        if not o:
            return None, f"unknown workflow object {target!r}"
        target = o["id"]
    elif kind == "object_measurements":
        spec = OBJ_FIELDS
        a = next((a for a in avs if a["id"] == target), None)
        if not a:
            return None, "unknown asset version"
        if a["status"] != "needs_review":
            return None, f"{a['object_name']} v{a['version']} is {a['status']}; measurements can only be revised while it awaits review"
    else:
        return None, f"kind {kind!r} is not editable"
    cur = current_values(p, avs, kind, target, wf)
    for k, v in vals.items():
        if k not in spec:
            return None, f"field {k!r} is not editable"
        label, lo, hi = spec[k]
        if k == "material":
            v = str(v).strip()[:60]
            if not v:
                return None, "empty material"
            src, note = _to_source(kind, srcs.get(k), True)
        else:
            v = _num(v)
            if v is None or not lo <= v <= hi:
                return None, f"{label} must be between {lo} and {hi} {UNITS[k]}"
            v = round(v, 4)
            src, note = _to_source(kind, srcs.get(k), _stated(v, UNITS[k], user_nums))
        if cur.get(k) is not None and str(cur.get(k)) == str(v):
            continue
        values[k] = v
        changes.append(dict(field=k, label=label, unit=UNITS[k], current=cur.get(k), proposed=v, basis=src,
                            source="copilot" if kind == "placement" else src, **({"note": note} if note else {})))
    if not changes:
        return None, "no change from current values"
    if kind == "table":
        title = "Set table dimensions"
    elif kind == "placement":
        title = f"Place {target}"
    elif wf is not None:
        o = wf_obj(wf, target)
        title = f"Set {o.get('label') or o.get('name')} measurements"
    else:
        a = next((a for a in avs if a["id"] == target), None)
        title = f"Revise {a['object_name'] if a else 'object'} measurements"
    return dict(id=new_id("prp"), kind=kind, target=target, title=title, values=values, changes=changes,
                base={k: cur.get(k) for k in list(values) + (["_status"] if kind == "object_measurements" and wf is None else [])},
                rationale=str(raw.get("rationale") or "")[:600], evidence=[str(x)[:240] for x in (raw.get("evidence") or [])][:4]), None


# ── Claude ────────────────────────────────────────────────────────────────────
SYSTEM = """You are the setup copilot inside a robotics workspace that turns phone video of a robot workcell into a MuJoCo simulation and a Gymnasium package. You help the person on site get their project to a valid scene.

You receive the project's current state as JSON (uploads, asset versions with measurement sources, runs with stage status and the latest log, robot catalog, confirmed robot, workcell table and object placements) and the recent conversation. Work only from that state and from what the user writes.

What you do:
- Explain plainly what blocks progress, citing the evidence (run stage, error line, missing value).
- Ask for missing physical measurements (table length/width/height, object size, mass, friction). One or two questions at a time.
- Propose structured changes the user can approve. Allowed kinds only:
  table: values {length_m,width_m,height_m} in METRES (convert cm the user says).
  placement: target = object name from `objects`; values {x_m,y_m,yaw_deg} in the robot base frame (metres, degrees). The robot's forward direction is robot.confirmed.stats.front ([fx, fy]) when a robot is confirmed; if it is not known, say so and label the direction-dependent values estimated.
  robot_hint: values {library_id} from the catalog. This is only a hint: the user must still import and confirm the model.
  object_measurements: target = an asset version id whose status is needs_review; values among {length_cm,width_cm,height_cm,mass_g,friction,material}.
- For every value give a source: user_measured (the user said they measured it, and wrote the number), user_stated (the user gave the number), project_data (copied from the state), estimated (your guess). Never label a guess as user_stated or user_measured. Estimates are allowed only when clearly useful and stay labelled estimated.

What you never do: approve or apply anything yourself, claim a change was made, mark any validation, review, scene or export as passed, invent robot models outside the catalog, give commands for real hardware, or suggest running code. If you lack information, say what is missing.

Style: concise and concrete, at most about 110 words in `reply`. Refer to objects and runs by name, never by internal id; the proposals are shown as cards, so do not repeat their values at length. Respond only by calling the `respond` tool."""

TOOL = {
    "name": "respond",
    "description": "Your complete answer to the user for this turn.",
    "input_schema": {
        "type": "object",
        "properties": {
            "reply": {"type": "string", "description": "Answer shown in the app (plain text, may use short line breaks)."},
            "blockers": {"type": "array", "maxItems": 4, "items": {"type": "object", "properties": {
                "title": {"type": "string"}, "detail": {"type": "string"}}, "required": ["title"]}},
            "proposals": {"type": "array", "maxItems": 4, "items": {"type": "object", "properties": {
                "kind": {"type": "string", "enum": ["table", "placement", "robot_hint", "object_measurements"]},
                "target": {"type": "string"},
                "values": {"type": "object"},
                "sources": {"type": "object", "description": "field -> user_measured|user_stated|project_data|estimated"},
                "rationale": {"type": "string"},
                "evidence": {"type": "array", "items": {"type": "string"}, "maxItems": 4}},
                "required": ["kind", "values", "sources", "rationale"]}},
        },
        "required": ["reply"],
    },
}


def claude(state, history, message):
    import mana_router
    an = mana_router.provider("anthropic")
    wf = state.get("workflow")
    rules = WF_RULES.format(rev=wf["revision"]) if wf else "MODE: legacy project settings (no guided workflow draft)."
    ctx = rules + "\n\nPROJECT STATE (JSON):\n" + jdump({k: v for k, v in state.items() if not k.startswith("_")}, 14000)
    msgs = []
    for h in history:
        msgs.append({"role": "user", "content": h["message"]})
        msgs.append({"role": "assistant", "content": h["reply"] + ("\n[proposals: " + "; ".join(f"{p['title']} ({p.get('status', 'pending')})" for p in h.get("proposals", [])) + "]" if h.get("proposals") else "")})
    msgs.append({"role": "user", "content": [{"type": "text", "text": ctx}, {"type": "text", "text": "USER: " + message}]})
    body = {"model": MODEL, "max_tokens": 1400, "system": SYSTEM, "tools": [TOOL], "tool_choice": {"type": "tool", "name": "respond"}, "messages": msgs}
    last = None
    for attempt in range(3):
        r = requests.post(an.url + "/v1/messages", headers=an.headers, json=body, allow_redirects=False, timeout=120)
        if r.status_code in (429, 500, 502, 503, 529) and attempt < 2:
            last = r
            time.sleep(1.5 * (attempt + 1))
            continue
        if r.status_code == 402:
            raise err(502, "Mana credits are exhausted, so Claude cannot answer right now")
        if r.status_code != 200:
            raise err(502, f"Claude request failed (HTTP {r.status_code}): {r.text[:200]}")
        j = r.json()
        tu = next((c for c in j.get("content", []) if c.get("type") == "tool_use" and c.get("name") == "respond"), None)
        if not tu:
            raise err(502, "Claude returned no structured answer")
        return tu["input"], j.get("usage") or {}
    raise err(502, f"Claude is unavailable (HTTP {last.status_code if last is not None else '?'}); try again")


# ── handlers ──────────────────────────────────────────────────────────────────
def view_turn(t):
    return {k: v for k, v in t.items() if not k.startswith("_")}


@H
async def get_copilot(request):
    uid = uid_of(request)
    pid = request.match_info["pid"]

    def work():
        p = access(uid, pid)
        state, avs = project_state(uid, p)
        cat, cat_src = catalog(uid)
        turns, _, hint = thread_state(load_entries(uid, pid))
        latest = state["runs"][0] if state["runs"] else None
        wf = state.get("_wf")
        if wf is not None:
            wb = state["workflow"]
            workcell = dict(table={k: (wb["table"].get(k) or {}).get("value") for k in TABLE_FIELDS} if any(wb["table"].values()) else None,
                            placements=wf_overrides(wf), frame="table")
            robot = dict(confirmed=state["robot"]["confirmed"], hint=None, selected=(wf.get("robot") or {}).get("selected_library_id"))
            wfs = dict(revision=wb["revision"], mode=wb["mode"], build_phase=(wb.get("build") or {}).get("phase"),
                       analysis_status=(wb.get("analysis") or {}).get("status"),
                       blockers=len(((wb.get("readiness") or {}).get("blockers")) or []))
        else:
            workcell, robot, wfs = state["workcell"], dict(confirmed=state["robot"]["confirmed"], hint=hint), None
        return dict(model=MODEL, can_write=p["_can_write"], can_use_model=bool(p["_can_model"] or __import__("public_beta").claude_available(request, uid, p)["available"]), mode="workflow" if wf is not None else "legacy",
                    workflow=wfs, workcell=workcell, robot=robot,
                    catalog=[dict(id=c["id"], name=c["name"], available=c.get("available")) for c in cat], catalog_source=cat_src,
                    objects=state["objects"], turns=[view_turn(t) for t in turns[-40:]],
                    grounding=dict(uploads=len(state["uploads"]), asset_versions=len(state["asset_versions"]),
                                   latest_run=latest and {k: latest[k] for k in ("kind", "status", "stage", "error")}))
    return web.json_response(await th(work))


@H
async def post_turn(request):
    uid = uid_of(request)
    pid = request.match_info["pid"]
    try:
        b = await request.json()
    except Exception:
        raise err(422, "JSON body required")
    msg = str(b.get("message") or "").strip()
    if not msg or len(msg) > MAX_MSG:
        raise err(422, f"message must be 1-{MAX_MSG} characters")

    def work():
        p = access(uid, pid, write=True)
        import public_beta
        if not (p["_can_model"] or public_beta.claude_allowed(request)):
            raise err(403, "The setup assistant is unavailable for this account or its usage allowance is exhausted.")
        rate(uid, "turn", TURN_LIMIT)
        state, avs = project_state(uid, p)
        wf = state.get("_wf")
        cat, _ = catalog(uid)
        if wf is not None and state.get("_wf_catalog"):
            cat = [dict(id=x["id"], name=x["name"], available=True) for x in state["_wf_catalog"]]
        state["robot_catalog"] = cat
        turns, _, hint = thread_state(load_entries(uid, pid))
        if wf is None:
            state["robot"]["approved_hint"] = hint
        hist = turns[-5:]
        public_beta.mark_dispatched(request)
        out, usage = claude(state, hist, msg)
        user_nums = _numbers_in([msg] + [t["message"] for t in hist[-3:]])
        props, dropped = [], []
        for raw in (out.get("proposals") or [])[:4]:
            try:
                pr, why = build_proposal(raw if isinstance(raw, dict) else {}, p, avs, cat, state["objects"], user_nums, wf)
            except Exception as ex:
                pr, why = None, f"invalid suggestion ({type(ex).__name__})"
            if pr:
                pr["status"] = "pending"
                props.append(pr)
            else:
                dropped.append(dict(kind=(raw or {}).get("kind"), reason=why))
        names = {a["id"]: f"{a['object_name']} v{a['version']}" for a in avs}
        names.update({o["id"]: o.get("label") or o.get("name") for o in (wf or {}).get("objects") or []})
        reply = re.sub(r"\(?\b((?:av|obj)_[0-9a-f]{12,16})\b\)?", lambda m: names.get(m.group(1), "that asset"), str(out.get("reply") or ""))
        out["reply"] = re.sub(r"\s+\b(?:run|prp|cpt|man|arm|ana|bld)_[0-9a-f]{12,16}\b", "", reply)
        turn = dict(id=new_id("cpt"), at=now_ms(), by=uid, message=msg,
                    reply=str(out.get("reply") or "")[:2000],
                    blockers=[dict(title=str(x.get("title", ""))[:120], detail=str(x.get("detail", ""))[:300]) for x in (out.get("blockers") or [])[:4] if isinstance(x, dict)],
                    proposals=props, rejected_suggestions=dropped, model=MODEL,
                    usage={k: usage.get(k) for k in ("input_tokens", "output_tokens")},
                    mode="workflow" if wf is not None else "legacy",
                    grounded_on=dict(uploads=len(state["uploads"]), asset_versions=len(state["asset_versions"]), runs=len(state["runs"]),
                                     workflow_revision=int(wf.get("revision") or 0) if wf is not None else None))
        store_entry(uid, pid, "turn", turn)
        return dict(turn=turn)
    return web.json_response(await th(work))


def _apply(uid, p, prop, turn):
    """Apply one validated proposal through the server's own endpoints. Returns (after, next)."""
    pid = p["id"]
    k = prop["kind"]
    if prop.get("mode") == "workflow":
        return _apply_workflow(uid, pid, prop, turn, p["_wf_fresh"], p["_wf_rev"])
    if k in ("table", "placement"):
        wc = p["settings_json"].get("workcell") if isinstance(p["settings_json"].get("workcell"), dict) else {}
        wc = json.loads(json.dumps(wc))
        if k == "table":
            t = dict(wc.get("table") or {})
            t.update(prop["values"])
            srcs = [c.get("basis", c["source"]) for c in prop["changes"]]
            if len(prop["values"]) < len(TABLE_FIELDS) and (wc.get("table") or {}).get("source"):
                srcs.append((wc.get("table") or {}).get("source"))      # untouched fields keep their earlier provenance
            rank = ["measured", "supplied", "estimated", "unknown"]
            t["source"] = max(srcs, key=lambda s: rank.index(s) if s in rank else 3)
            if not all(t.get(f) is not None for f in TABLE_FIELDS):
                missing = [f for f in TABLE_FIELDS if t.get(f) is None]
                raise err(422, "the table needs all three dimensions before it can be saved; missing " + ", ".join(missing))
            t["note"] = (f"Copilot proposal {prop['id']} approved; " + ", ".join(f"{c['field']} {c.get('basis', c['source'])}" for c in prop["changes"]))[:300]
            wc["table"] = t
        else:
            pl = dict(wc.get("placements") or {})
            cur = dict(pl.get(prop["target"]) or {})
            cur.update(prop["values"])
            for f in PLACE_FIELDS:
                cur.setdefault(f, 0.0)
            cur["source"] = "copilot"
            pl[prop["target"]] = cur
            wc["placements"] = pl
        code, j = loop("PATCH", f"/api/projects/{pid}", uid, {"settings": {"workcell": wc}})
        if code != 200:
            raise err(422 if code in (400, 422) else 409, "the workspace refused the change: " + str(j.get("error"))[:300])
        return {"workcell": wc}, None
    if k == "robot_hint":
        return {"robot_hint": prop["values"]["library_id"]}, {"kind": "import_robot", "library_id": prop["values"]["library_id"]}
    if k == "object_measurements":
        avs = S.need("rows")("asset_version", pid)
        a = next((a for a in avs if a["id"] == prop["target"]), None)
        meas = json.loads(json.dumps(a.get("measurements_json") or {}))
        for c in prop["changes"]:
            note = f"copilot proposal {prop['id']} ({c['source']})"
            meas[c["field"]] = {"value": str(c["proposed"]), "source": c.get("basis", c["source"]), "note": note}
        code, j = loop("POST", f"/api/projects/{pid}/assets/{a['id']}/review", uid,
                       {"action": "revise", "measurements": meas, "tags": (a.get("params_json") or {}).get("tags") if isinstance(a.get("params_json"), dict) else None,
                        "estimates_reviewed": False, "note": f"revised via copilot proposal {prop['id']}"})
        if code != 200:
            raise err(422 if code in (400, 422) else 409, "the workspace refused the revision: " + str(j.get("error"))[:300])
        return {"measurements": meas, "rebuild_run_id": j.get("run_id")}, None
    raise err(422, "unknown proposal kind")


def _apply_workflow(uid, pid, prop, turn, wf, rev):
    """Edit the guided-workflow draft through its own PUT endpoint (base_revision guarded). Never touches settings.workcell."""
    k = prop["kind"]
    note = f"copilot proposal {prop['id']}"
    body = {"base_revision": rev}
    if k == "table":
        body["table"] = {c["field"]: {"value": c["proposed"], "source": c.get("basis", c["source"]), "note": note} for c in prop["changes"]}
    elif k == "placement":
        cur = {n: {f: v.get(f) for f in WF_PLACE_FIELDS} for n, v in wf_overrides(wf).items()}   # PUT replaces the whole override set
        t = dict(cur.get(prop["target"]) or {})
        t.update(prop["values"])
        t.setdefault("yaw_deg", 0.0)
        if t.get("x_m") is None or t.get("y_m") is None:
            raise err(422, "a placement needs both x and y in the table frame")
        cur[prop["target"]] = t
        body["layout"] = {"placements": cur}
    elif k == "robot_hint":
        body["robot"] = {"selected_library_id": prop["values"]["library_id"]}
    elif k == "object_measurements":
        objs = [{"id": o["id"]} for o in wf.get("objects") or []]       # full list required; unchanged entries keep everything
        tgt = next((o for o in objs if o["id"] == prop["target"]), None)
        if not tgt:
            raise err(409, "that object is no longer in the draft")
        tgt["fields"] = {c["field"]: {"value": c["proposed"], "source": c.get("basis", c["source"]), "note": note} for c in prop["changes"]}
        body["objects"] = objs
    else:
        raise err(422, "unsupported in the guided workflow")
    code, j = loop("PUT", f"/api/projects/{pid}/workflow", uid, body)
    if code == 409:
        raise err(409, "The draft changed after this was proposed; nothing was applied. Ask again for a fresh proposal.")
    if code == 404 and "project" not in str(j.get("error", "")):
        raise err(503, "the workflow service is not available yet; nothing was applied")
    if code != 200:
        raise err(422 if code in (400, 422) else 409, "the workspace refused the change: " + str(j.get("error"))[:300])
    nw = j.get("workflow") or {}
    after = {"revision": j.get("revision"), "values": current_values(None, [], k, prop.get("target"), nw)}
    return after, None


@H
async def decide(request):
    uid = uid_of(request)
    pid, prop_id, action = request.match_info["pid"], request.match_info["prop"], request.match_info["action"]
    try:
        b = await request.json() if request.can_read_body else {}
    except Exception:
        b = {}

    def work():
        p = access(uid, pid, write=True)
        with _LOCKS[pid]:
            turns, decisions, _ = thread_state(load_entries(uid, pid))
            turn, prop = find_proposal(turns, prop_id)
            if not prop:
                raise err(404, "proposal not found")
            if prop_id in decisions:
                raise err(409, f"this proposal was already {decisions[prop_id]['action']}")
            dec = dict(id=new_id("cpd"), proposal_id=prop_id, turn_id=turn["id"], by=uid, at=now_ms(), kind=prop["kind"])
            if action == "reject":
                dec.update(action="rejected", note=str(b.get("note") or "")[:300])
                store_entry(uid, pid, "decision", dec)
                return dict(decision=dec)
            if b.get("confirm") is not True:
                raise err(422, "approval needs explicit confirmation (confirm: true)")
            if any(c.get("basis", c["source"]) == "estimated" for c in prop["changes"]) and b.get("acknowledge_estimates") is not True:
                raise err(422, "this change contains estimated values; acknowledge that they stay labelled estimated")
            p = access(uid, pid, write=True)                                # fresh read inside the lock
            avs = S.need("rows")("asset_version", pid)
            wf = wf_of(p)
            reason = None
            if wf is not None:
                view = wf_view(uid, pid)
                wf = view["workflow"] if view else wf
                p["_wf_fresh"] = wf
                if prop.get("mode") != "workflow":
                    reason = "This project now uses the guided workflow; this older suggestion targets legacy settings, so nothing was written."
                else:
                    # Every draft PUT bumps the revision by one. Revisions produced by this copilot's own approved edits are
                    # known from their decisions, so approving sibling cards stays possible; any other edit makes it stale.
                    mine = {int(d["after"]["revision"]) for d in decisions.values()
                            if d.get("action") == "applied" and isinstance(d.get("after"), dict) and d["after"].get("revision")}
                    base, now_rev = int(prop.get("base_revision") or -1), int(wf.get("revision") or 0)
                    if now_rev < base or any(r not in mine for r in range(base + 1, now_rev + 1)):
                        reason = "The draft changed after this was proposed (another edit or an analysis); nothing was applied. Ask again for a fresh proposal."
                    p["_wf_rev"] = now_rev
            elif prop.get("mode") == "workflow":
                reason = "The guided workflow draft is no longer present; nothing was applied."
            cur = current_values(p, avs, prop["kind"], prop.get("target"), wf if prop.get("mode") == "workflow" else None) if not reason else None
            if not reason and (cur is None or any(str(cur.get(k)) != str(v) for k, v in (prop.get("base") or {}).items())):
                reason = "The targeted values changed after this was proposed; nothing was applied. Ask again for a fresh proposal."
            if reason:
                dec.update(action="stale", before=prop.get("base"), after=cur, error=reason)
                store_entry(uid, pid, "decision", dec)
                raise err(409, reason)
            try:
                after, nxt = _apply(uid, p, prop, turn)
            except web.HTTPException as e:
                msg = json.loads(e.text).get("error") if e.text else str(e)
                dec.update(action="stale" if e.status == 409 else "failed", before=cur, error=msg)
                store_entry(uid, pid, "decision", dec)
                raise
            dec.update(action="applied", before=cur, after=after, next=nxt)
            store_entry(uid, pid, "decision", dec)
            store_entry(uid, pid, "audit", dict(id=new_id("cpa"), at=dec["at"], by=uid, proposal_id=prop_id, action="applied",
                                                kind=prop["kind"], target=prop.get("target"), changes=prop["changes"], before=cur))
            return dict(decision=dec, next=nxt)
    if action not in ("approve", "reject"):
        raise err(404, "unknown action")
    return web.json_response(await th(work))


def register(app, services=None):
    """Add the copilot routes to the server's aiohttp app. `services` is the server module or a dict."""
    global S, SANDBOX_UID
    S = _Svc(services or {})
    for n in ("db", "project_for", "rows"):
        S.need(n)
    try:
        from env import get_user_id
        SANDBOX_UID = get_user_id()
    except Exception:
        SANDBOX_UID = None
    r = app.router
    r.add_get("/api/projects/{pid}/copilot", get_copilot)
    r.add_post("/api/projects/{pid}/copilot/turns", post_turn)
    r.add_post("/api/projects/{pid}/copilot/proposals/{prop}/{action}", decide)
    return ["/api/projects/{pid}/copilot", "/api/projects/{pid}/copilot/turns",
            "/api/projects/{pid}/copilot/proposals/{prop}/{approve|reject}"]
