"""RealiSIM video-first pipeline (draft -> Claude analysis -> reviewed build -> preview -> approved export).

Wired once by the server:  import workflow; workflow.register(app, sys.modules[__name__])
Never imports server.py. State: project.settings_json.workflow (SpacetimeDB); evidence/provenance: SpacetimeDB manifests.
Contract: /workspace/photo2grasp/workflow-contract.json
"""
import base64, copy, hashlib, json, re, shutil, subprocess, sys, threading, time, traceback, uuid
from pathlib import Path
from aiohttp import web

ROOT = Path("/workspace/photo2grasp")
S = None
SANDBOX_UID = None
VERSION = 1
MAX_OBJECTS = 8
MAX_CROPS = 4
PROMPT_VERSION = "wf-analysis-2"
NUM_FIELDS = [("length_cm", 0.5, 200), ("width_cm", 0.5, 200), ("height_cm", 0.5, 200), ("mass_g", 0.5, 5000), ("friction", 0.05, 2.0)]
TEXT_FIELDS = ("material",)
TABLE_FIELDS = [("length_m", 0.2, 4.0), ("width_m", 0.2, 4.0), ("height_m", 0.01, 1e6)]
SOURCES = ("measured", "supplied", "estimated", "unknown")
SLUG = re.compile(r"^[a-z][a-z0-9_]{1,39}$")
URL_RE = re.compile(r"(https?://|www\.)\S+", re.I)
ACTIVE_PHASES = ("queued", "building", "accepting", "layout", "composing", "exporting")
WF_LOCK = threading.RLock()        # serialises read-modify-write of settings_json.workflow in this process
AN_SEM = threading.Semaphore(1)    # one Claude analysis at a time
LAYOUT_SEM = threading.Semaphore(1)
WAKE = threading.Event()
ACTIVE = set()
LAYOUT_THREADS = {}
TEST_UIDS = ("qa_tester",)


class _Svc:
    def __init__(self, src):
        self.src = src

    def __getattr__(self, name):
        v = self.src.get(name) if isinstance(self.src, dict) else getattr(self.src, name, None)
        if v is None:
            raise AttributeError(f"workflow: services must provide {name!r}")
        return v

    def has(self, name):
        return (self.src.get(name) if isinstance(self.src, dict) else getattr(self.src, name, None)) is not None


def now_ms():
    return int(time.time() * 1000)


def nid(prefix):
    return f"{prefix}_{uuid.uuid4().hex[:16]}"


def q(s):
    return "'" + str(s).replace("'", "''") + "'"


def log(*a):
    print(time.strftime("%H:%M:%S"), "[workflow]", *a, flush=True)


def jerr(status, msg, **extra):
    cls = {400: web.HTTPBadRequest, 403: web.HTTPForbidden, 404: web.HTTPNotFound, 409: web.HTTPConflict,
           422: web.HTTPUnprocessableEntity, 429: web.HTTPTooManyRequests, 500: web.HTTPInternalServerError,
           503: web.HTTPServiceUnavailable}.get(status, web.HTTPBadRequest)
    return cls(text=json.dumps(dict(error=msg, **extra)), content_type="application/json")


def sha_json(o):
    return hashlib.sha256(json.dumps(o, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def catalog():
    return {r["id"]: r for r in S.ROBOTS.CATALOG}


def clean_text(s, n=300):
    return URL_RE.sub("[link removed]", str(s or ""))[:n]


def slugify(s, fallback="object"):
    s = re.sub(r"[^a-z0-9]+", "_", str(s or "").lower()).strip("_")
    s = re.sub(r"^[^a-z]+", "", s)[:40]
    return s if SLUG.match(s) else (fallback if SLUG.match(fallback) else "object")


# ── draft model ────────────────────────────────────────────────────────────────
def field(value="", source="unknown", origin="default", **kw):
    f = dict(value=value, source=source, origin=origin, updated_at=now_ms())
    f.update({k: v for k, v in kw.items() if v is not None})
    return f


def empty_fields():
    d = {k: field() for k, _, _ in NUM_FIELDS}
    d.update({k: field() for k in TEXT_FIELDS})
    return d


def empty_table():
    return dict({k: field() for k, _, _ in TABLE_FIELDS}, note="")


DEFAULT_SIM_ROBOT = "robotstudio_so101"

def default_robot_if_absent(wf):
    if wf.get("mode") != "workspace":
        return wf
    rb = wf.setdefault("robot", {})
    if not rb.get("arms") and not rb.get("selected_library_id"):
        rb.update(selected_library_id=DEFAULT_SIM_ROBOT, selected_origin="assumed",
                  assumption="No robot was identified. SO-101 is inserted as the user-configured simulation default, not a detected physical robot.")
    return wf

def arm_library(wf, a):
    """Model for one arm. Explicit per-arm user choice wins; otherwise a user-selected global model is inherited (the
    analysis hint stays as evidence in candidates/library_id, it is not relabelled); otherwise the reviewed per-arm
    library_id, then the global selection."""
    rb = wf.get("robot") or {}
    if a.get("library_origin") == "user" and a.get("library_id"):
        return a["library_id"], "arm_user"
    if rb.get("selected_origin") == "user" and rb.get("selected_library_id"):
        return rb["selected_library_id"], "global_user"
    if a.get("library_id"):
        return a["library_id"], "arm_" + str(a.get("library_origin") or "analysis")
    if rb.get("selected_library_id"):
        return rb["selected_library_id"], "global_" + str(rb.get("selected_origin") or "default")
    return None, "none"


def used_arms(wf):
    return [a for a in (wf.get("robot") or {}).get("arms") or [] if a.get("decision") == "use"]


def robot_instances(wf):
    """Every arm that will be composed, primary first. No used arm (none detected / all assumed) = one synthetic arm
    with the global model, which keeps the legacy single-arm path. Never truncated."""
    rb = wf.get("robot") or {}
    use = used_arms(wf)
    if not use:
        lid = rb.get("selected_library_id")
        return [dict(name="robot", arm_id=None, label=None, library_id=lid, library_resolution="global_" + str(rb.get("selected_origin") or "default"),
                     synthetic=True)] if lid else []
    al = ((wf.get("layout") or {}).get("alignment") or {})
    prim = al.get("primary_robot")
    use = sorted(use, key=lambda a: 0 if a["name"] == prim else 1)
    out = []
    for a in use:
        lid, how = arm_library(wf, a)
        out.append(dict(name=a["name"], arm_id=a["id"], label=a.get("label"), library_id=lid, library_resolution=how, synthetic=False))
    return out


def new_workflow(mode="workspace"):
    return dict(version=VERSION, revision=0, mode=mode if mode in ("workspace", "object") else "workspace", primary_upload_id=None,
                objects=[], table=empty_table(),
                robot=dict(arms=[], selected_library_id=DEFAULT_SIM_ROBOT if mode=="workspace" else None, selected_origin="assumed" if mode=="workspace" else None, assumption="SO-101 simulation default until a robot is identified or supplied.", suggestion=None, multi_arm=True),
                layout=dict(status="none", alignment=dict(table_frame_flip=False, primary_robot=None, robots={}, object_overrides={}),
                            run=None, result=None, placements={}, placements_source=None, provisional=True),
                analysis=None, analyses=[], build=None, history=[], asset_cache={}, mesh_cache={}, created_at=now_ms())


def load(pid):
    rows = S.db.sql(f"SELECT * FROM project WHERE id = {q(pid)}")
    if not rows:
        raise jerr(404, "project not found")
    p = rows[0]
    st = p.get("settings_json")
    if isinstance(st, str):
        try:
            st = json.loads(st or "{}")
        except Exception:
            st = {}
    st = st if isinstance(st, dict) else {}
    wf = st.get("workflow")
    if not isinstance(wf, dict):
        wf = None
    return p, st, wf


def save(p, st, wf):
    st["workflow"] = wf
    S.db.call("update_project", uid=p["owner_uid"], id=p["id"], name=p["name"], archived=bool(p["archived"]), settings_json=json.dumps(st))


def mutate(pid, fn, bump=False, create=True):
    """fn(wf, p, st) mutates wf in place and may return a value. bump=True increments the draft revision."""
    with WF_LOCK:
        p, st, wf = load(pid)
        if wf is None:
            if not create:
                raise jerr(409, "this project has no workflow draft yet")
            wf = new_workflow(infer_mode(p["id"]))
        default_robot_if_absent(wf)
        out = fn(wf, p, st)
        default_robot_if_absent(wf)
        if bump:
            wf["revision"] = int(wf.get("revision") or 0) + 1
        wf["updated_at"] = now_ms()
        save(p, st, wf)
        return out, wf


def infer_mode(pid):
    p, st, wf = load(pid)
    return st.get("project_mode") or (wf or {}).get("mode") or "object"


def num(v):
    if v in (None, ""):
        return ""
    try:
        f = float(v)
    except (TypeError, ValueError):
        raise jerr(422, f"not a number: {str(v)[:40]!r}")
    if f != f or abs(f) > 1e6:
        raise jerr(422, "number out of range")
    return round(f, 6)


def apply_field(old, new, key, uid, numeric=True):
    """Client edit of one field. Changed value/source/note -> origin user. Unchanged -> keep provenance."""
    old = dict(old or field())
    if not isinstance(new, dict):
        new = dict(value=new)
    val = num(new.get("value", old.get("value"))) if numeric else clean_text(new.get("value", old.get("value")), 120)
    src = str(new.get("source", old.get("source")) or "unknown")
    if src not in SOURCES:
        raise jerr(422, f"{key}.source must be one of {', '.join(SOURCES)}")
    if val == "":
        src = "unknown"
    elif src == "unknown":
        src = "supplied"
    note = clean_text(new.get("note", old.get("note") or ""), 300)
    changed = (val != old.get("value") and not (val == "" and old.get("value") in (None, ""))) or src != old.get("source") or note != (old.get("note") or "")
    if changed:
        if "note" not in new and old.get("origin") == "analysis":
            note = ""          # the analysis note described the estimate, not the user's value
        old.update(value=val, source=src, note=note, origin="user", updated_at=now_ms(), updated_by=uid)
        for k in ("confidence", "analysis_id"):
            old.pop(k, None)
    return old


def user_locked(f):
    return isinstance(f, dict) and (f.get("origin") == "user" or f.get("source") in ("measured", "supplied"))


def suggest_into(f, value, source, aid, confidence=None, note=None):
    """Analysis suggestion: always recorded; value written only when the field is not user-owned."""
    f = dict(f or field())
    if value in (None, ""):
        return f
    f["suggestion"] = dict(value=value, source=source, analysis_id=aid, confidence=confidence, note=note)
    if not user_locked(f):
        f.update(value=value, source=source, origin="analysis", analysis_id=aid, confidence=confidence, note=note or "", updated_at=now_ms())
        f.pop("updated_by", None)
    return f


def measurements(o):
    out = {}
    for k, _, _ in NUM_FIELDS:
        f = o["fields"].get(k) or {}
        out[k] = dict(value=f.get("value"), source=f.get("source", "unknown"), note=f.get("note") or "")
    m = o["fields"].get("material") or {}
    if m.get("value"):
        out["material"] = dict(value=m["value"], source=m.get("source", "unknown"), note=m.get("note") or "")
    return out


def table_m(wf):
    t = wf.get("table") or {}
    vals = {k: (t.get(k) or {}).get("value") for k, _, _ in TABLE_FIELDS}
    srcs = [(t.get(k) or {}).get("source", "unknown") for k, _, _ in TABLE_FIELDS]
    order = ["unknown", "estimated", "supplied", "measured"]
    worst = min(srcs, key=order.index) if srcs else "unknown"
    return vals, worst


def ready_uploads(pid):
    return [u for u in S.rows("upload", pid) if u.get("status") == "ready"]


def readiness(wf, pid=None, uploads=None):
    B, W = [], []
    add = lambda code, msg, fld=None: B.append(dict(code=code, field=fld, message=msg))
    inc = [o for o in wf.get("objects") or [] if o.get("include")]
    mode = wf.get("mode")
    if not inc:
        add("no_objects", "Include at least one object (run the analysis or add one).", "objects")
    if mode == "object" and len(inc) > 1:
        add("object_mode_one", "Single object mode builds exactly one object; exclude the others or switch to workspace.", "objects")
    if len(inc) > MAX_OBJECTS:
        add("too_many", f"At most {MAX_OBJECTS} objects per scene; exclude {len(inc) - MAX_OBJECTS}.", "objects")
    names = [o.get("name") for o in inc]
    builtin = set(((S.STATE.get("intro") or {}).get("assets")) or [])
    for o in inc:
        n = o.get("name") or ""
        if not SLUG.match(n):
            add("bad_name", f"{n or 'object'}: name must start with a letter; lowercase letters, digits and _ (2–40).", f"objects/{o['id']}/name")
        elif n in builtin:
            add("builtin_name", f"“{n}” is a built-in asset of the repository; rename it.", f"objects/{o['id']}/name")
        if names.count(n) > 1:
            add("dup_name", f"Two included objects are named {n}.", f"objects/{o['id']}/name")
        for p in S.measurement_problems(measurements(o)):
            add("field", f"{n}: {p}", f"objects/{o['id']}/fields/{p.split(':')[0]}")
        crops = [c for c in o.get("crops") or [] if c["id"] in (o.get("selected_crop_ids") or [])]
        if not crops and not o.get("extra_upload_ids"):
            add("no_input", f"{n}: no image input (select a crop or attach an object close-up).", f"objects/{o['id']}/selected_crop_ids")
        if not (o.get("coverage") or {}).get("adequate", True):
            W.append(f"{n}: limited capture coverage ({(o.get('coverage') or {}).get('note') or 'few views'}); the mesh may be incomplete.")
        if any((o["fields"].get(k) or {}).get("source") == "estimated" for k in [x[0] for x in NUM_FIELDS] + list(TEXT_FIELDS)):
            W.append(f"{n}: some values are estimates (needs acknowledge_estimates at build).")
    if mode == "workspace":
        rb = wf.get("robot") or {}
        arms = rb.get("arms") or []
        use = [a for a in arms if a.get("decision") == "use"]
        und = [a for a in arms if a.get("decision") in (None, "undecided")]
        sel = rb.get("selected_library_id")
        if sel and sel not in catalog():
            add("robot_unknown", "The selected robot model is not in the supported catalog.", "robot/selected_library_id")
        if not sel:
            add("robot_missing", "Choose the robot model (catalog) for the arm you want to simulate, or switch to single object mode.", "robot/selected_library_id")
        if und:
            add("arm_undecided", "Decide each detected arm: use it in the scene or exclude it: " + ", ".join(a.get("label") or a["id"] for a in und), "robot/arms")
        for a in use:
            lid, _how = arm_library(wf, a)
            if not lid:
                add("arm_model_missing", f"{a.get('label') or a['name']}: choose its robot model.", f"robot/arms/{a['id']}/library_id")
            elif lid not in catalog():
                add("arm_model_unknown", f"{a.get('label') or a['name']}: {lid} is not in the supported catalog.", f"robot/arms/{a['id']}/library_id")
        if len(use) > 1:
            al_ = ((wf.get("layout") or {}).get("alignment") or {})
            prim_ = al_.get("primary_robot")
            if prim_ and prim_ not in [a["name"] for a in use]:
                add("primary_not_used", f"The primary robot {prim_} is not marked use; choose a used arm as primary.", "layout/alignment/primary_robot")
            ap_ = arm_poses(wf, (wf.get("layout") or {}).get("result"), primary_arm(wf))
            lbl_ = {a["name"]: a.get("label") or a["name"] for a in use}
            unposed = [lbl_.get(n, n) for n, v in ap_.items() if v["provisional"]]
            W.append(f"{len(use)} arms will be simulated." + (f" Base pose relative to the primary arm is provisional for: {', '.join(unposed)} "
                     "(default slot beside the primary; measure both bases on the table or anchor them in the video)." if unposed else ""))
        if arms and not use and not und:
            add("all_arms_excluded", "Every detected arm is excluded; mark the arm to simulate as use.", "robot/arms")
        vals, _ = table_m(wf)
        for k, lo, hi in TABLE_FIELDS:
            v = vals.get(k)
            try:
                fv = float(v)
                if not lo <= fv <= hi:
                    add("table", f"table {k} {fv} outside {lo}–{hi} m", f"table/{k}")
            except (TypeError, ValueError):
                add("table", f"table {k} is required (metres)", f"table/{k}")
        if any((wf["table"].get(k) or {}).get("source") == "estimated" for k, _, _ in TABLE_FIELDS):
            W.append("table dimensions include estimates; metric layout scale depends on the table height.")
        if (wf.get("layout") or {}).get("provisional", True):
            W.append("Layout is provisional (not recovered from video yet); run Recover layout or set placements manually.")
    up_ids = {u["id"] for u in (uploads if uploads is not None else (ready_uploads(pid) if pid else []))}
    for o in inc:
        for u in o.get("extra_upload_ids") or []:
            if up_ids and u not in up_ids:
                add("upload_missing", f"{o.get('name')}: attached upload {u} is not ready.", f"objects/{o['id']}/extra_upload_ids")
    return dict(ok=not B, blockers=B, warnings=W)


# ── client edits ───────────────────────────────────────────────────────────────
def apply_put(wf, b, uid, pid):
    cat = catalog()
    if "mode" in b:
        if b["mode"] not in ("workspace", "object"):
            raise jerr(422, "mode must be workspace or object")
        wf["mode"] = b["mode"]
    ups = {u["id"]: u for u in ready_uploads(pid)}
    if "primary_upload_id" in b:
        u = b["primary_upload_id"]
        if u is not None and u not in ups:
            raise jerr(422, "primary_upload_id must be a ready upload of this project")
        wf["primary_upload_id"] = u
    if "table" in b:
        t = b["table"] or {}
        for k, _, _ in TABLE_FIELDS:
            if k in t:
                wf["table"][k] = apply_field(wf["table"].get(k), t[k], f"table.{k}", uid)
        if "note" in t:
            wf["table"]["note"] = clean_text(t["note"], 300)
    if "objects" in b:
        incoming = b["objects"]
        if not isinstance(incoming, list) or len(incoming) > 24:
            raise jerr(422, "objects must be a list (at most 24 entries; at most 8 included)")
        old = {o["id"]: o for o in wf.get("objects") or []}
        out = []
        for c in incoming:
            if not isinstance(c, dict):
                raise jerr(422, "each object must be an object")
            o = copy.deepcopy(old.get(c.get("id"))) if c.get("id") in old else dict(
                id=nid("obj"), name="", label="", include=True, exclusion_reason="", origin="user", created_by=uid,
                fields=empty_fields(), crops=[], selected_crop_ids=[], extra_upload_ids=[], coverage=dict(adequate=True, note="user-added"))
            if "name" in c:
                o["name"] = str(c["name"] or "")[:40]
            if "label" in c:
                o["label"] = clean_text(c["label"], 80)
            if "include" in c:
                if bool(c["include"]) != bool(o.get("include")):
                    o["include_user_set"] = True
                o["include"] = bool(c["include"])
            if "exclusion_reason" in c:
                o["exclusion_reason"] = clean_text(c["exclusion_reason"], 200)
            if "selected_crop_ids" in c:
                ids = {x["id"] for x in o.get("crops") or []}
                bad = [x for x in c["selected_crop_ids"] or [] if x not in ids]
                if bad:
                    raise jerr(422, f"{o.get('name') or 'object'}: unknown crop id(s) {bad[:3]}")
                o["selected_crop_ids"] = list(dict.fromkeys(c["selected_crop_ids"] or []))[:MAX_CROPS * 2]
                o["crops_user_selected"] = True
            if "extra_upload_ids" in c:
                bad = [x for x in c["extra_upload_ids"] or [] if x not in ups]
                if bad:
                    raise jerr(422, f"{o.get('name') or 'object'}: upload(s) not ready in this project: {bad[:3]}")
                o["extra_upload_ids"] = list(dict.fromkeys(c["extra_upload_ids"] or []))[:6]
            for k, v in (c.get("fields") or {}).items():
                if k in dict((x[0], 1) for x in NUM_FIELDS):
                    o["fields"][k] = apply_field(o["fields"].get(k), v, f"{o.get('name')}.{k}", uid)
                elif k in TEXT_FIELDS:
                    o["fields"][k] = apply_field(o["fields"].get(k), v, f"{o.get('name')}.{k}", uid, numeric=False)
                else:
                    raise jerr(422, f"unknown object field {k!r}")
            out.append(o)
        wf["objects"] = out
    if "robot" in b:
        rb = b["robot"] or {}
        cur = wf["robot"]
        if "selected_library_id" in rb:
            lid = rb["selected_library_id"]
            if lid is not None and lid not in cat:
                raise jerr(422, "choose a robot model from the supported catalog (GET /api/robots); URLs/files are not accepted")
            if lid != cur.get("selected_library_id"):
                cur.update(selected_library_id=lid, selected_origin="user" if lid else None, selected_at=now_ms(), selected_by=uid)
        if "arms" in rb:
            byid = {a["id"]: a for a in cur.get("arms") or []}
            for a in rb["arms"] or []:
                if a.get("id") in byid:
                    d = a.get("decision", byid[a["id"]].get("decision"))
                    if d not in ("use", "exclude", "undecided"):
                        raise jerr(422, "arm decision must be use, exclude or undecided")
                    if d != byid[a["id"]].get("decision"):
                        byid[a["id"]].update(decision=d, decision_origin="user", decided_by=uid, decided_at=now_ms())
                    if "library_id" in a:
                        if a["library_id"] is not None and a["library_id"] not in cat:
                            raise jerr(422, "arm library_id must be a catalog id")
                        byid[a["id"]].update(library_id=a["library_id"], library_origin="user")
                    if "exclusion_reason" in a:
                        byid[a["id"]]["exclusion_reason"] = clean_text(a["exclusion_reason"], 200)
                elif a.get("new"):
                    lid = a.get("library_id")
                    if lid is not None and lid not in cat:
                        raise jerr(422, "arm library_id must be a catalog id")
                    arm = dict(id=nid("arm"), name=a["name"] if isinstance(a.get("name"), str) and SLUG.match(a["name"]) else slugify(a.get("label") or "robot", "robot"), label=clean_text(a.get("label") or "Robot arm", 60),
                               candidates=[], status="user", decision=a.get("decision") if a.get("decision") in ("use", "exclude") else "undecided",
                               library_id=lid, origin="user", frame_refs=[])
                    if lid:
                        arm.update(library_origin="user")     # a model chosen while adding the arm is an explicit per-arm choice
                    cur.setdefault("arms", []).append(arm)
                else:
                    raise jerr(422, f"unknown arm id {a.get('id')!r} (send new: true to add one)")
            taken = set()
            for i, a in enumerate(cur["arms"]):        # deterministic, collision-free names (arm.name keys alignment/layout)
                n0, n, k = a["name"], a["name"], i + 1
                while n in taken:
                    n, k = f"{n0}_{k}", k + 1
                a["name"] = n
                taken.add(n)
            cur.pop("single_arm_only", None)
            cur["multi_arm"] = True
    if "layout" in b:
        lb = b["layout"] or {}
        lay = wf["layout"]
        al = lay.setdefault("alignment", {})
        a_in = lb.get("alignment") or {}
        if "table_frame_flip" in a_in:
            al["table_frame_flip"] = bool(a_in["table_frame_flip"])
        if "primary_robot" in a_in:
            al["primary_robot"] = a_in["primary_robot"] and str(a_in["primary_robot"])[:40]
        if "robots" in a_in:
            rbs = {}
            for name, v in (a_in["robots"] or {}).items():
                if not isinstance(v, dict) or v.get("method") not in ("table_measured", "tag", "cluster"):
                    raise jerr(422, f"alignment.robots.{name}: method table_measured | tag | cluster")
                if v["method"] == "table_measured":
                    rbs[name] = dict(method="table_measured", x_m=num(v.get("x_m")), y_m=num(v.get("y_m")), yaw_deg=num(v.get("yaw_deg", 0)),
                                     source="measured", by=uid, at=now_ms())
                    if "" in (rbs[name]["x_m"], rbs[name]["y_m"]) or abs(rbs[name]["x_m"]) > 3 or abs(rbs[name]["y_m"]) > 3:
                        raise jerr(422, f"alignment.robots.{name}: x_m and y_m (table frame, |v|<=3 m) are required")
                elif v["method"] == "tag":
                    t = v.get("tag_in_base") or {}
                    rbs[name] = dict(method="tag", tag_family=str(v.get("tag_family") or "tag36h11")[:12], tag_id=int(num(v.get("tag_id")) or 0),
                                     tag_size_m=num(v.get("tag_size_m")), tag_in_base=dict(x_m=num(t.get("x_m", 0)), y_m=num(t.get("y_m", 0)), yaw_deg=num(t.get("yaw_deg", 0))))
                else:
                    rbs[name] = dict(method="cluster", cluster_id=str(v.get("cluster_id") or "")[:40], yaw_deg=num(v.get("yaw_deg", 0)))
            al["robots"] = rbs
        if "object_overrides" in a_in or "placements" in lb:
            src = a_in.get("object_overrides") if "object_overrides" in a_in else lb.get("placements")
            ov = {}
            for name, v in (src or {}).items():
                x, y = num((v or {}).get("x_m")), num((v or {}).get("y_m"))
                if x == "" or y == "" or abs(x) > 3 or abs(y) > 3:
                    raise jerr(422, f"override {name}: x_m and y_m (table frame, |v|<=3 m) are required")
                yaw = num((v or {}).get("yaw_deg", 0)) or 0
                if not -180 <= yaw <= 180:
                    raise jerr(422, f"override {name}: yaw_deg -180..180")
                ov[name] = dict(x_m=x, y_m=y, yaw_deg=yaw, source="manual", by=uid, at=now_ms())
            al["object_overrides"] = ov
        lay["alignment_revision"] = now_ms()


# ── analysis ───────────────────────────────────────────────────────────────────
SAMPLE_SRC = r"""
import sys, json, cv2
src, n, out = sys.argv[1], int(sys.argv[2]), sys.argv[3]
import os; os.makedirs(out, exist_ok=True)
res = []
def save(i, idx, t, im, sharp):
    full = os.path.join(out, f"full_{i}.jpg"); cv2.imwrite(full, im, [cv2.IMWRITE_JPEG_QUALITY, 94])
    s = min(1.0, 1024 / max(im.shape[:2])); sm = cv2.resize(im, None, fx=s, fy=s, interpolation=cv2.INTER_AREA) if s < 1 else im
    small = os.path.join(out, f"frame_{i}.jpg"); cv2.imwrite(small, sm, [cv2.IMWRITE_JPEG_QUALITY, 85])
    res.append(dict(i=i, index=idx, time_s=t, w=im.shape[1], h=im.shape[0], sharpness=round(sharp, 1), full=full, small=small, sw=sm.shape[1], sh=sm.shape[0]))
if src.lower().rsplit(".", 1)[-1] in ("jpg", "jpeg", "png", "webp"):
    im = cv2.imread(src)
    if im is None: print(json.dumps(dict(ok=False, error="image could not be decoded"))); sys.exit()
    save(0, 0, 0.0, im, float(cv2.Laplacian(cv2.cvtColor(im, cv2.COLOR_BGR2GRAY), cv2.CV_64F).var()))
else:
    cap = cv2.VideoCapture(src); N = int(cap.get(cv2.CAP_PROP_FRAME_COUNT)) or 1; fps = cap.get(cv2.CAP_PROP_FPS) or 30
    for k in range(n):
        best = None
        for off in (-0.25, 0.0, 0.25):
            idx = int(min(N - 1, max(0, (k + 0.5 + off) * N / n)))
            cap.set(cv2.CAP_PROP_POS_FRAMES, idx); ok, im = cap.read()
            if not ok or im is None: continue
            g = cv2.cvtColor(cv2.resize(im, (480, int(480 * im.shape[0] / im.shape[1]))), cv2.COLOR_BGR2GRAY)
            sh = float(cv2.Laplacian(g, cv2.CV_64F).var())
            if best is None or sh > best[0]: best = (sh, idx, im)
        if best: save(len(res), best[1], round(best[1] / fps, 3), best[2], best[0])
print(json.dumps(dict(ok=bool(res), frames=res, error=None if res else "no frame could be decoded")))
"""

CROP_SRC = r"""
import sys, json, cv2
jobs = json.loads(open(sys.argv[1]).read()); out = []
for j in jobs:
    im = cv2.imread(j["full"]); H, W = im.shape[:2]
    x0, y0, x1, y1 = j["box"]; pw, ph = (x1 - x0) * 0.12, (y1 - y0) * 0.12
    X0, Y0 = max(0, int((x0 - pw) * W)), max(0, int((y0 - ph) * H)); X1, Y1 = min(W, int(round((x1 + pw) * W))), min(H, int(round((y1 + ph) * H)))
    w, h = X1 - X0, Y1 - Y0
    if w < 64 or h < 64:
        out.append(dict(ok=False, reason=f"crop too small ({w}x{h} px)", w=w, h=h)); continue
    c = im[Y0:Y1, X0:X1]; cv2.imwrite(j["out"], c, [cv2.IMWRITE_JPEG_QUALITY, 93])
    out.append(dict(ok=True, out=j["out"], w=w, h=h, px=[X0, Y0, X1, Y1]))
print(json.dumps(out))
"""

ANALYSIS_PROMPT = """You are preparing a robot workcell video for 3D reconstruction into a physics simulation. The images are frames sampled from ONE video of a real workspace, in time order; each is labelled "Frame k (t=...s)".

Identify:
1. PRIMARY FOREGROUND OBJECTS: rigid items on the work surface that a robot arm could manipulate and that are the plausible task objects (at most {max_objects}). For each, give tight normalised bounding boxes [x0,y0,x1,y1] (0..1, origin top-left) in every frame where it is clearly visible and not heavily occluded. Exclude (and list under "excluded" with a short reason): robot arms, the table, background furniture/walls, people/hands, cables, tools that are clearly not task objects, items seen in fewer than 2 frames, items too small (< ~3% of the frame width), transparent/mirror-like items that 3D reconstruction cannot handle, piles/deformable items.
2. ROBOT ARMS: list EVERY robot arm visible as a separate entry (do not merge two arms). For each, choose candidate models ONLY from this catalog (ids), or give no candidate if none fits:
{catalog}
Distinguishing SO-100 from SO-101 from video is often unreliable: say so with lower confidence rather than forcing an answer.
3. TABLE: if a work table is visible, estimate its length/width/height in metres only if there is a usable scale reference; otherwise null.

Scale: a single phone video does not measure absolute size, mass or friction. Give dimension estimates in cm ONLY when there is a scale reference (known object, robot of known size, ruler, standard paper); state the basis; otherwise null. Mass/friction are guesses from apparent material: mark them low confidence. Never claim a value was measured.

Return ONLY this JSON:
{{"objects":[{{"name":"snake_case_slug","label":"short human label","why_primary":"...","boxes":[{{"frame":k,"box":[x0,y0,x1,y1]}}],"visible_sides":["front","top",...],"coverage":"adequate|limited|poor","coverage_note":"...","dimensions_cm":{{"length":n|null,"width":n|null,"height":n|null,"confidence":"low|medium|high","basis":"..."}},"material":"...|null","mass_g":{{"value":n|null,"confidence":"low|medium"}},"friction":{{"value":n|null,"confidence":"low"}}}}],
 "excluded":[{{"label":"...","reason":"..."}}],
 "robots":[{{"name":"robot_1","label":"...","boxes":[{{"frame":k,"box":[x0,y0,x1,y1]}}],"candidates":[{{"library_id":"catalog id","confidence":"low|medium|high","reasons":["..."]}}],"unlisted_guess":"model name or null"}}],
 "table":{{"visible":true|false,"length_m":n|null,"width_m":n|null,"height_m":n|null,"confidence":"low|medium|high","basis":"..."}},
 "scale_reference":"what gives absolute scale, or none",
 "summary":"one sentence"}}"""


def claude_analyze(frames):
    import requests
    import mana_router
    cat = "\n".join(f"- {r['id']}: {r['name']} by {r['maker']}; {r['dof']} DOF; gripper {r['gripper']}; cues: {'; '.join(r['cues'])}" for r in S.ROBOTS.CATALOG)
    content = []
    for f in frames:
        content.append({"type": "text", "text": f"Frame {f['i']} (t={f['time_s']}s)"})
        content.append({"type": "image", "source": {"type": "base64", "media_type": "image/jpeg", "data": base64.b64encode(Path(f["small"]).read_bytes()).decode()}})
    content.append({"type": "text", "text": ANALYSIS_PROMPT.format(catalog=cat, max_objects=MAX_OBJECTS)})
    model = getattr(S.src, "DETECT_MODEL", None) or "claude-sonnet-5"
    an = mana_router.provider("anthropic")
    r = requests.post(an.url + "/v1/messages", headers={**an.headers, "anthropic-version": "2023-06-01"}, allow_redirects=False, timeout=300,
                      json={"model": model, "max_tokens": 8000, "messages": [{"role": "user", "content": content}]})
    if r.status_code == 402:
        raise RuntimeError("out of Mana credits for the Claude analysis")
    if r.status_code != 200:
        raise RuntimeError(f"Claude analysis request failed (HTTP {r.status_code}): {r.text[:300]}")
    j = r.json()
    text = "".join(b.get("text", "") for b in j.get("content", []) if b.get("type") == "text")
    return model, text, parse_json(text), dict(j.get("usage") or {}, stop_reason=j.get("stop_reason"))


def parse_json(text):
    t = re.sub(r"```(?:json)?", "", text or "")
    i, k = t.find("{"), t.rfind("}")
    if i < 0 or k <= i:
        return None
    blob = t[i:k + 1]
    for cand in (blob, re.sub(r",\s*([}\]])", r"\1", blob), re.sub(r"//[^\n]*", "", re.sub(r",\s*([}\]])", r"\1", blob))):
        try:
            return json.loads(cand)
        except Exception:
            pass
    return None


def _box(b):
    try:
        x0, y0, x1, y1 = [float(v) for v in b]
    except Exception:
        return None
    if max(x0, y0, x1, y1) > 1.5:      # tolerate percentages
        x0, y0, x1, y1 = [v / 100 for v in (x0, y0, x1, y1)]
    x0, y0, x1, y1 = [min(1.0, max(0.0, v)) for v in (x0, y0, x1, y1)]
    if x1 - x0 < 0.02 or y1 - y0 < 0.02 or (x1 - x0) * (y1 - y0) < 0.0015:
        return None
    return [round(x0, 4), round(y0, 4), round(x1, 4), round(y1, 4)]


def _pos(v, lo, hi):
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return round(f, 4) if lo <= f <= hi else None


def clean_analysis(raw, nframes):
    cat = catalog()
    if not isinstance(raw, dict):
        return None
    conf = lambda c: c if c in ("low", "medium", "high") else "low"
    boxes = lambda bs: [dict(frame=int(b["frame"]), box=_box(b.get("box"))) for b in (bs or [])
                        if isinstance(b, dict) and str(b.get("frame", "")).lstrip("-").isdigit() and 0 <= int(b["frame"]) < nframes and _box(b.get("box"))]
    objs, excl, seen = [], [], set()
    for o in raw.get("objects") or []:
        if not isinstance(o, dict):
            continue
        name = slugify(o.get("name") or o.get("label"), "object")
        base, i = name, 2
        while name in seen:
            name = f"{base[:36]}_{i}"
            i += 1
        bx = boxes(o.get("boxes"))
        if not bx:
            excl.append(dict(label=clean_text(o.get("label") or name, 80), reason="no usable bounding box in the sampled frames"))
            continue
        seen.add(name)
        d = o.get("dimensions_cm") or {}
        objs.append(dict(name=name, label=clean_text(o.get("label") or name, 80), why=clean_text(o.get("why_primary"), 200), boxes=bx,
                         sides=[clean_text(s, 20) for s in (o.get("visible_sides") or [])][:6],
                         coverage=o.get("coverage") if o.get("coverage") in ("adequate", "limited", "poor") else "limited",
                         coverage_note=clean_text(o.get("coverage_note"), 200),
                         dims=dict(length=_pos(d.get("length"), 0.5, 200), width=_pos(d.get("width"), 0.5, 200), height=_pos(d.get("height"), 0.5, 200),
                                   confidence=conf(d.get("confidence")), basis=clean_text(d.get("basis"), 200)),
                         material=clean_text(o.get("material"), 60) if o.get("material") else None,
                         mass_g=_pos((o.get("mass_g") or {}).get("value"), 0.5, 5000), friction=_pos((o.get("friction") or {}).get("value"), 0.05, 2.0)))
    for o in objs[MAX_OBJECTS:]:
        excl.append(dict(label=o["label"], reason=f"over the {MAX_OBJECTS}-object limit (lower priority)"))
    objs = objs[:MAX_OBJECTS]
    for e in raw.get("excluded") or []:
        if isinstance(e, dict):
            excl.append(dict(label=clean_text(e.get("label"), 80), reason=clean_text(e.get("reason"), 200)))
    robots = []
    for i, rb in enumerate(raw.get("robots") or []):
        if not isinstance(rb, dict):
            continue
        cands, cs = [], set()
        for c in rb.get("candidates") or []:
            if isinstance(c, dict) and c.get("library_id") in cat and c["library_id"] not in cs:
                cs.add(c["library_id"])
                cands.append(dict(library_id=c["library_id"], name=cat[c["library_id"]]["name"], confidence=conf(c.get("confidence")),
                                  reasons=[clean_text(x, 160) for x in (c.get("reasons") or [])][:4]))
        robots.append(dict(name=f"robot_{i + 1}", label=clean_text(rb.get("label") or f"Robot arm {i + 1}", 60), boxes=boxes(rb.get("boxes")),
                           candidates=cands[:3], unlisted_guess=clean_text(rb.get("unlisted_guess"), 80) if rb.get("unlisted_guess") else None))
    t = raw.get("table") or {}
    table = dict(visible=bool(t.get("visible")), length_m=_pos(t.get("length_m"), 0.2, 4), width_m=_pos(t.get("width_m"), 0.2, 4),
                 height_m=_pos(t.get("height_m"), 0.01, 1e6), confidence=conf(t.get("confidence")), basis=clean_text(t.get("basis"), 200))
    return dict(objects=objs, excluded=excl[:30], robots=robots[:6], table=table, scale_reference=clean_text(raw.get("scale_reference"), 200),
                summary=clean_text(raw.get("summary"), 300))


def pick_upload(pid, wf, upload_id=None):
    ups = sorted(ready_uploads(pid), key=lambda u: -(u.get("created_at") or 0))
    if upload_id:
        u = next((x for x in ups if x["id"] == upload_id), None)
    else:
        u = next((x for x in ups if x["id"] == wf.get("primary_upload_id")), None) or next(
            (x for x in ups if not ((x.get("meta_json") or {}) if isinstance(x.get("meta_json"), dict) else {}).get("derived_from")), None)
    if not u:
        raise jerr(422, "upload a video (or photo) of the workspace first; it must finish validation")
    owner = S.db.sql(f"SELECT owner_uid FROM project WHERE id = {q(pid)}")[0]["owner_uid"]
    src = ensure_source(pid, owner, u["id"])     # restores the verified copy from SpacetimeDB when the worker copy is gone
    if not src:
        raise jerr(409, f"the source file of {u['filename']} is neither on the worker nor stored in SpacetimeDB; upload it again")
    return u, src


def run_analysis(pid, aid, upid, nframes, uid):
    work = S.CACHE / "tmp" / aid
    with AN_SEM:
        try:
            _, st, wf = load(pid)
            p = S.db.sql(f"SELECT owner_uid FROM project WHERE id = {q(pid)}")[0]
            owner = p["owner_uid"]
            u, src = pick_upload(pid, wf, upid)
            setan(pid, aid, status="running", stage="frames")
            r = subprocess.run([S.PY, "-c", SAMPLE_SRC, str(src), str(nframes), str(work)], capture_output=True, text=True, timeout=600)
            fr = json.loads((r.stdout.strip().splitlines() or ["{}"])[-1] or "{}")
            if not fr.get("ok"):
                raise RuntimeError(fr.get("error") or ("frame sampling failed: " + r.stderr[-300:]))
            frames = fr["frames"]
            setan(pid, aid, stage="claude", frames_sampled=len(frames))
            model, text, raw, usage = claude_analyze(frames)
            res = clean_analysis(raw, len(frames))
            if res is None:
                log("unparseable analysis answer", json.dumps(usage), (text or "")[:3000])
                raise RuntimeError("Claude did not return a usable JSON answer; run the analysis again (no automatic retry)")
            setan(pid, aid, stage="crops")
            jobs, refs = [], []
            for o in res["objects"]:
                bx = sorted(o["boxes"], key=lambda b: -((b["box"][2] - b["box"][0]) * (b["box"][3] - b["box"][1]) * (1 + frames[b["frame"]]["sharpness"] / 500)))
                for b in bx[:MAX_CROPS]:
                    f = frames[b["frame"]]
                    out = work / "crops" / f"{o['name']}_{b['frame']}.jpg"
                    out.parent.mkdir(parents=True, exist_ok=True)
                    jobs.append(dict(full=f["full"], box=b["box"], out=str(out)))
                    refs.append((o, b, f))
            (work / "jobs.json").write_text(json.dumps(jobs))
            cr = json.loads(subprocess.run([S.PY, "-c", CROP_SRC, str(work / "jobs.json")], capture_output=True, text=True, timeout=300).stdout.strip() or "[]") if jobs else []
            entries = [(f"frames/frame_{f['i']}.jpg", Path(f["small"]).read_bytes(), "image/jpeg") for f in frames]
            crop_meta = []
            for (o, b, f), c in zip(refs, cr):
                if not c.get("ok"):
                    o.setdefault("crop_notes", []).append(f"frame {b['frame']}: {c.get('reason')}")
                    continue
                data = Path(c["out"]).read_bytes()
                path = f"crops/{o['name']}/frame_{b['frame']}.jpg"
                entries.append((path, data, "image/jpeg"))
                crop_meta.append(dict(obj=o["name"], path=path, frame=b["frame"], box=b["box"], px=dict(w=c["w"], h=c["h"], rect=c["px"]),
                                      sha256=hashlib.sha256(data).hexdigest(), frame_index=f["index"], time_s=f["time_s"], sharpness=f["sharpness"]))
            frame_meta = [dict(i=f["i"], frame_index=f["index"], time_s=f["time_s"], w=f["w"], h=f["h"], sharpness=f["sharpness"]) for f in frames]
            resp = dict(model=model, prompt_version=PROMPT_VERSION, raw_text=text, parsed=raw, cleaned=res, usage=usage, frames=frame_meta,
                        crops=crop_meta, upload_id=u["id"], upload_sha256=u.get("sha256"), upload_filename=u["filename"])
            entries.append(("response.json", json.dumps(resp).encode(), "application/json"))
            setan(pid, aid, stage="persist")
            mid, files = S.db.store_dir(owner, pid, "workflow_analysis", aid, "1", entries, deps=dict(model=model, upload_sha256=u.get("sha256"), prompt=PROMPT_VERSION))
            fmap = {f["path"]: f["id"] for f in files}
            for c in crop_meta:
                c["file_id"] = fmap[c["path"]]
            for f in frame_meta:
                f["file_id"] = fmap[f"frames/frame_{f['i']}.jpg"]
            merge_analysis(pid, aid, res, crop_meta, frame_meta, u, model, mid, usage, cached=False)
        except web.HTTPException as e:
            setan(pid, aid, status="failed", error=json.loads(e.text).get("error", str(e)), finished_at=now_ms())
        except Exception as e:
            traceback.print_exc()
            setan(pid, aid, status="failed", error=str(e)[:800], finished_at=now_ms())
        finally:
            shutil.rmtree(work, ignore_errors=True)


def setan(pid, aid, **ch):
    def f(wf, p, st):
        a = wf.get("analysis") or {}
        if a.get("id") == aid:
            a.update(ch)
            wf["analysis"] = a
            if ch.get("status") in ("failed", "complete"):
                summ = {k: a.get(k) for k in ("id", "status", "upload_id", "upload_sha256", "model", "manifest_id", "finished_at", "error", "cached", "prompt_version", "frames_requested")}
                wf["analyses"] = ([summ] + [x for x in wf.get("analyses") or [] if x.get("id") != aid])[:5]
    mutate(pid, f)


def merge_analysis(pid, aid, res, crop_meta, frame_meta, u, model, mid, usage, cached):
    def f(wf, p, st):
        objs = wf.setdefault("objects", [])
        byname = {o["name"]: o for o in objs}
        found = set()
        for a in res["objects"]:
            a = dict(a)
            orig = a["name"]
            o = byname.get(a["name"]) or next((x for x in objs if x.get("origin") == "analysis" and (x.get("label") or "").lower() == a["label"].lower()), None)
            if not o:
                builtin = set(((S.STATE.get("intro") or {}).get("assets")) or [])
                if a["name"] in builtin or any(x["name"] == a["name"] for x in objs):
                    a["name"] = (a["name"][:32] + "_capture")
                o = dict(id=nid("obj"), name=a["name"], label=a["label"], include=True, exclusion_reason="", origin="analysis",
                         fields=empty_fields(), crops=[], selected_crop_ids=[], extra_upload_ids=[], coverage={})
                objs.append(o)
            found.add(o["id"])
            o["analysis_id"] = aid
            o["why_primary"] = a["why"]
            o.pop("not_in_latest_analysis", None)
            mine = [c for c in crop_meta if c["obj"] == orig]
            o["crops"] = [dict(id="crop_" + c["sha256"][:12], file_id=c["file_id"], frame=c["frame"], frame_index=c["frame_index"], time_s=c["time_s"],
                               box=c["box"], px=c["px"], sha256=c["sha256"], upload_id=u["id"], upload_sha256=u.get("sha256"), analysis_id=aid,
                               sharpness=c["sharpness"]) for c in mine]
            ids = [c["id"] for c in o["crops"]]
            if o.get("crops_user_selected"):
                o["selected_crop_ids"] = [x for x in o.get("selected_crop_ids") or [] if x in ids] or ids
            else:
                o["selected_crop_ids"] = ids
            minpx = min([min(c["px"]["w"], c["px"]["h"]) for c in mine] or [0])
            adequate = a["coverage"] == "adequate" and len(a["boxes"]) >= 2 and minpx >= 128
            o["coverage"] = dict(frames_visible=len(a["boxes"]), frames_sampled=len(frame_meta), sides=a["sides"], model_rating=a["coverage"],
                                 min_crop_px=minpx, adequate=adequate,
                                 note="; ".join(x for x in [a["coverage_note"], *(a.get("crop_notes") or []), "" if minpx >= 128 or not mine else f"smallest crop {minpx}px"] if x))
            if o.get("origin") == "analysis" and not o.get("include_user_set"):
                o["include"] = adequate
                o["exclusion_reason"] = "" if adequate else ("limited capture coverage: " + (o["coverage"]["note"] or f"seen in {len(a['boxes'])} sampled frame(s)"))
            d = a["dims"]
            dnote = f"Claude estimate from video ({d['confidence']} confidence): {d['basis'] or 'no stated basis'}. Not a measurement."
            for k, key in (("length_cm", "length"), ("width_cm", "width"), ("height_cm", "height")):
                o["fields"][k] = suggest_into(o["fields"].get(k), d.get(key), "estimated", aid, d["confidence"], dnote)
            o["fields"]["mass_g"] = suggest_into(o["fields"].get("mass_g"), a["mass_g"], "estimated", aid, "low", "Guess from apparent material and size; video cannot measure mass.")
            o["fields"]["friction"] = suggest_into(o["fields"].get("friction"), a["friction"], "estimated", aid, "low", "Guess from apparent material; video cannot measure friction.")
            if a["material"]:
                o["fields"]["material"] = suggest_into(o["fields"].get("material"), a["material"], "estimated", aid, "medium", "Visual guess.")
        for o in objs:
            if o.get("origin") == "analysis" and o["id"] not in found:
                o["not_in_latest_analysis"] = True
        # robots: every detected arm kept as its own entry
        rb = wf["robot"]
        old = {a.get("name"): a for a in rb.get("arms") or [] if a.get("origin") == "analysis"}
        users = [a for a in rb.get("arms") or [] if a.get("origin") == "user"]
        arms = []
        fmap = {f["i"]: f for f in frame_meta}
        for r_ in res["robots"]:
            prev = old.get(r_["name"]) or {}
            arm = dict(id=prev.get("id") or nid("arm"), name=r_["name"], label=r_["label"], origin="analysis", analysis_id=aid,
                       candidates=r_["candidates"], unlisted_guess=r_["unlisted_guess"],
                       status="candidate" if r_["candidates"] else "unknown",
                       frame_refs=[dict(frame=b["frame"], frame_index=fmap[b["frame"]]["frame_index"], time_s=fmap[b["frame"]]["time_s"], box=b["box"],
                                        frame_file_id=fmap[b["frame"]].get("file_id")) for b in r_["boxes"]],
                       decision=prev.get("decision") if prev.get("decision_origin") == "user" else ("use" if len(res["robots"]) == 1 else "undecided"),
                       decision_origin=prev.get("decision_origin") or "analysis",
                       library_id=prev.get("library_id") if prev.get("library_origin") == "user" else (r_["candidates"][0]["library_id"] if r_["candidates"] else None),
                       library_origin=prev.get("library_origin") or "analysis")
            arms.append(arm)
        rb["arms"] = arms + users
        top = None
        if len(res["robots"]) == 1 and res["robots"][0]["candidates"]:
            c = res["robots"][0]["candidates"]
            if c[0]["confidence"] == "high" and not any(x["confidence"] == "high" for x in c[1:]):
                top = c[0]
        if res["robots"]:
            first = res["robots"][0]["candidates"][0] if res["robots"][0]["candidates"] else None
            rb["suggestion"] = dict(library_id=first and first["library_id"], name=first and first["name"], confidence=first and first["confidence"],
                                    arms_detected=len(res["robots"]), analysis_id=aid,
                                    reason=("one arm, one high-confidence candidate" if top else
                                            "ambiguous: confirm the model (SO-100 vs SO-101 is unreliable from video)" if first else
                                            "no catalog model matched; choose one or exclude the arm"))
        else:
            rb["suggestion"] = dict(library_id=None, arms_detected=0, analysis_id=aid, reason="no robot arm detected in the sampled frames")
        if not res["robots"] and not users and rb.get("selected_origin") != "user":
            rb.update(selected_library_id=DEFAULT_SIM_ROBOT, selected_origin="assumed", selected_at=now_ms(),
                      assumption="No robot detected in the video. SO-101 is inserted as the requested simulation default, not as a detected robot.")
        elif top and rb.get("selected_origin") != "user":
            rb.update(selected_library_id=top["library_id"], selected_origin="analysis", selected_at=now_ms(), assumption=None)
        elif res["robots"] and rb.get("selected_origin") in ("analysis", "assumed") and not top:
            rb.update(selected_library_id=None, selected_origin=None, assumption=None)
        t = res["table"]
        tnote = f"Claude estimate ({t['confidence']}): {t['basis'] or 'no basis'}. Not a measurement."
        for k in ("length_m", "width_m", "height_m"):
            wf["table"][k] = suggest_into(wf["table"].get(k), t.get(k), "estimated", aid, t["confidence"], tnote)
        if not wf.get("primary_upload_id"):
            wf["primary_upload_id"] = u["id"]
        a = wf.get("analysis") or {}
        a.update(id=aid, status="complete", stage="done", finished_at=now_ms(), model=model, manifest_id=mid, usage=usage, cached=cached,
                 upload_id=u["id"], upload_sha256=u.get("sha256"), frames=frame_meta, excluded=res["excluded"], summary=res["summary"],
                 scale_reference=res["scale_reference"], table=t, prompt_version=PROMPT_VERSION, objects_found=len(res["objects"]), arms_found=len(res["robots"]),
                 note="Dimensions, mass and friction from video are estimates; a phone video does not measure absolute scale, mass or friction.")
        wf["analysis"] = a
        summ = {k: a.get(k) for k in ("id", "status", "upload_id", "upload_sha256", "model", "manifest_id", "finished_at", "error", "cached", "prompt_version", "frames_requested")}
        wf["analyses"] = ([summ] + [x for x in wf.get("analyses") or [] if x.get("id") != aid])[:5]
    mutate(pid, f, bump=True)


def cached_analysis(pid, wf, u, nframes):
    for a in wf.get("analyses") or []:
        if a.get("status") == "complete" and a.get("upload_sha256") == u.get("sha256") and a.get("prompt_version") == PROMPT_VERSION \
                and int(a.get("frames_requested") or 0) == nframes and a.get("manifest_id"):
            return a
    return None


def replay_cached(pid, aid, prev, u, owner):
    fs = {f["path"]: f for f in S.db.manifest_files(prev["manifest_id"])}
    if "response.json" not in fs:
        return False
    _, data = S.db.read_bytes(owner, pid, fs["response.json"]["id"])
    resp = json.loads(data)
    crops = resp.get("crops") or []
    for c in crops:
        c["file_id"] = fs[c["path"]]["id"]
    frames = resp.get("frames") or []
    for f in frames:
        f["file_id"] = (fs.get(f"frames/frame_{f['i']}.jpg") or {}).get("id")
    merge_analysis(pid, aid, resp["cleaned"], crops, frames, u, resp.get("model"), prev["manifest_id"], resp.get("usage"), cached=True)
    return True


# ── capture source in SpacetimeDB (once per upload sha) ────────────────────────
def persist_source(pid, owner, u, src):
    """Store the original capture as a verified capture_source manifest, deduplicated by sha256 within the project."""
    sha = u.get("sha256") or hashlib.sha256(Path(src).read_bytes()).hexdigest()
    ref = "src_" + sha[:32]
    have = S.db.sql(f"SELECT * FROM artifact_manifest WHERE project_id = {q(pid)} AND ref_id = {q(ref)}")
    have = [m for m in have if m.get("kind") == "capture_source" and m.get("status") == "verified"]
    if have:
        m = have[0]
        f = (json.loads(m["files_json"]) if isinstance(m["files_json"], str) else m["files_json"])[0]
        return dict(manifest_id=m["id"], file_id=f["id"], sha256=f["sha256"], size=f["size"], media_type=f["media_type"], reused=True)
    data = Path(src).read_bytes()
    if hashlib.sha256(data).hexdigest() != sha:
        raise RuntimeError(f"{u['filename']}: worker copy does not match its recorded SHA-256; upload it again")
    mt = u.get("media_type") or ("video/mp4" if Path(src).suffix.lower() in (".mp4", ".m4v") else "application/octet-stream")
    mid, files = S.db.store_dir(owner, pid, "capture_source", ref, "1", [("source" + Path(src).suffix.lower(), data, mt)],
                                deps=dict(upload_id=u["id"], filename=u["filename"], sha256=sha))
    return dict(manifest_id=mid, file_id=files[0]["id"], sha256=sha, size=len(data), media_type=mt, reused=False)


def ensure_source(pid, owner, upid):
    """Worker copy of an upload; restored (hash-verified) from its capture_source manifest when missing."""
    src = next((S.UPLOADS / upid).glob("source.*"), None)
    if src:
        return src
    u = S.upload_row(pid, upid)
    if not u or not u.get("sha256"):
        return None
    ms = [m for m in S.db.sql(f"SELECT * FROM artifact_manifest WHERE project_id = {q(pid)} AND ref_id = {q('src_' + u['sha256'][:32])}")
          if m.get("kind") == "capture_source" and m.get("status") == "verified"]
    if not ms:
        return None
    files = json.loads(ms[0]["files_json"]) if isinstance(ms[0]["files_json"], str) else ms[0]["files_json"]
    meta, data = S.db.read_bytes(owner, pid, files[0]["id"])
    if meta["sha256"] != u["sha256"]:
        return None
    d = S.UPLOADS / upid
    d.mkdir(parents=True, exist_ok=True)
    p = d / Path(files[0]["path"]).name
    p.write_bytes(data)
    log("restored source of", upid, "from SpacetimeDB")
    return p


def record_source(pid, upid, info):
    def f(wf, p, st):
        wf.setdefault("sources", {})[upid] = dict(info, at=now_ms())
    mutate(pid, f)


# ── loopback (server's own endpoints keep their validation) ────────────────────
def loop(method, path, uid, body=None):
    import requests
    r = requests.request(method, f"http://127.0.0.1:{S.PORT}{path}", headers={"X-Sender-Uid": uid, "Content-Type": "application/json"},
                         data=json.dumps(body or {}), timeout=120)
    try:
        j = r.json()
    except Exception:
        j = {"error": r.text[:300]}
    if r.status_code >= 400:
        raise RuntimeError(j.get("error") or f"HTTP {r.status_code}")
    return j


def run_row(pid, rid):
    try:
        return S.one("pipeline_run", pid, rid)
    except Exception:
        return None


# ── layout (adapter/workflow_layout.py, owned by the spatial worker) ───────────
LAYOUT_PY = ROOT / "adapter" / "workflow_layout.py"


def rot(x, y, deg):
    import math
    c, s = math.cos(math.radians(deg)), math.sin(math.radians(deg))
    return x * c - y * s, x * s + y * c


def table_to_robot(base, x, y, yaw=0.0):
    dx, dy = rot(x - base["x_m"], y - base["y_m"], -base["yaw_deg"])
    return round(dx, 4), round(dy, 4), round(((yaw or 0) - base["yaw_deg"] + 180) % 360 - 180, 2)


def selections(wf):
    objs = []
    for o in wf["objects"]:
        if not o.get("include"):
            continue
        c = next((c for c in o.get("crops") or [] if c["id"] in (o.get("selected_crop_ids") or [])), None)
        e = dict(name=o["name"], source="claude" if o.get("origin") == "analysis" else "manual")
        if c:
            e["bbox"] = dict(time_s=c["time_s"], xyxy_norm=c["box"])
        objs.append(e)
    robots = []
    for a in wf["robot"].get("arms") or []:
        e = dict(name=a["name"], library_id=arm_library(wf, a)[0] if a.get("decision") == "use" else a.get("library_id"),
                 source="claude" if a.get("origin") == "analysis" else "manual", decision=a.get("decision"))
        if a.get("frame_refs"):
            e["bbox"] = dict(time_s=a["frame_refs"][0]["time_s"], xyxy_norm=a["frame_refs"][0]["box"])
        robots.append(e)
    return dict(objects=objs, robots=robots)


def primary_arm(wf):
    """Explicit alignment.primary_robot if it is a used arm, else the first used arm (works for any number of arms)."""
    use = used_arms(wf)
    prim = ((wf.get("layout") or {}).get("alignment") or {}).get("primary_robot")
    if prim and prim in [a["name"] for a in use]:
        return prim
    return use[0]["name"] if use else None


def se2_inv(p):
    x, y = rot(-p["x_m"], -p["y_m"], -p["yaw_deg"])
    return dict(x_m=x, y_m=y, yaw_deg=-p["yaw_deg"])


def se2_mul(a, b):
    x, y = rot(b["x_m"], b["y_m"], a["yaw_deg"])
    return dict(x_m=round(a["x_m"] + x, 4), y_m=round(a["y_m"] + y, 4), yaw_deg=round((a["yaw_deg"] + b["yaw_deg"] + 180) % 360 - 180, 3))


def arm_poses(wf, res, prim):
    """Base pose of every composed arm in the PRIMARY arm's base frame, with provenance. measured (both bases
    tape-measured in the table frame) > video anchors (table_in_base of both) > None (provisional default slot)."""
    inst = robot_instances(wf)
    al = (wf.get("layout") or {}).get("alignment") or {}
    rbs = al.get("robots") or {}
    vid = {r.get("name"): r for r in (res or {}).get("robots") or [] if r.get("table_in_base")} if (res or {}).get("status") in ("complete", "needs_review") else {}
    out = {}
    for r in inst:
        n = r["name"]
        if n == prim or r["synthetic"]:
            out[n] = dict(pose=dict(x_m=0.0, y_m=0.0, yaw_deg=0.0), source="origin", provisional=False, label="primary arm (world origin)")
            continue
        bp, bj = rbs.get(prim) or {}, rbs.get(n) or {}
        if bp.get("method") == "table_measured" and bj.get("method") == "table_measured":
            x, y, yaw = table_to_robot(bp, bj["x_m"], bj["y_m"], bj.get("yaw_deg", 0))
            out[n] = dict(pose=dict(x_m=x, y_m=y, yaw_deg=yaw), source="measured", provisional=False,
                          label="tape-measured base poses (table frame) converted to the primary base frame")
        elif prim in vid and n in vid:
            p = se2_mul(vid[prim]["table_in_base"], se2_inv(vid[n]["table_in_base"]))
            src = (vid[n].get("anchor") or {}).get("source") or "reconstructed"
            out[n] = dict(pose=p, source="video_" + src, provisional=src == "estimated",
                          label=f"from the video layout anchors ({src})")
        else:
            out[n] = dict(pose=None, source="default", provisional=True,
                          label="provisional default slot beside the primary arm; not measured or recovered")
    return out


def effective_layout(wf, result=None):
    """Placements (robot-base frame) + table pose for the composer, with honest provenance."""
    lay = wf["layout"]
    al = lay.get("alignment") or {}
    names = [o["name"] for o in wf["objects"] if o.get("include")]
    prim = primary_arm(wf) or al.get("primary_robot")
    res = result if result is not None else lay.get("result")
    out = dict(placements={}, table_pose=None, status="provisional_default", provisional=True, source="default",
               label="Provisional default placement: objects in a row in front of the robot. Not derived from the video.")
    out = _with_arms(wf, res, prim, out)
    return out


def _with_arms(wf, res, prim, out):
    """Wrap the single-robot layout with per-arm poses. The layout stays provisional while any arm pose is a default."""
    eff = _effective_objects(wf, res, prim, out)
    inst = robot_instances(wf)
    eff["primary_robot"] = prim or (inst[0]["name"] if inst else None)
    if len(inst) > 1:
        ap = arm_poses(wf, res, eff["primary_robot"])
        eff["robots"] = ap
        missing = [n for n, v in ap.items() if v["provisional"]]
        if missing:
            eff["provisional"] = True
            eff["label"] += f" Arm base pose provisional for: {', '.join(missing)}."
    return eff


def _effective_objects(wf, res, prim, out):
    lay = wf["layout"]
    al = lay.get("alignment") or {}
    names = [o["name"] for o in wf["objects"] if o.get("include")]
    sp = (res or {}).get("settings_patch") or {}
    pl = ((sp.get("workcell") or {}).get("placements")) or {}
    if pl and res.get("status") in ("complete", "needs_review"):
        rob = next((r for r in res.get("robots") or [] if r.get("name") == prim), None)
        out.update(placements={n: dict(v) for n, v in pl.items() if n in names}, status="video_" + res["status"], provisional=False, source="workflow_layout",
                   table_pose=(rob or {}).get("table_in_base"),
                   label=f"From the video reconstruction ({res['status']}); per-object provenance in layout.result.objects.")
        return out
    base = (al.get("robots") or {}).get(prim) if prim else None
    ov = al.get("object_overrides") or {}
    if base and base.get("method") == "table_measured" and ov:
        for n in names:
            if n in ov:
                x, y, yaw = table_to_robot(base, ov[n]["x_m"], ov[n]["y_m"], ov[n].get("yaw_deg", 0))
                out["placements"][n] = dict(x_m=x, y_m=y, yaw_deg=yaw, source="manual")
        tx, ty, tyaw = table_to_robot(base, 0.0, 0.0, 0.0)
        out.update(table_pose=dict(x_m=tx, y_m=ty, yaw_deg=tyaw), status="manual" if len(out["placements"]) == len(names) else "manual_partial",
                   provisional=len(out["placements"]) < len(names), source="manual",
                   label="Manual table-frame placements converted with the tape-measured robot base pose." +
                         ("" if len(out["placements"]) == len(names) else " Objects without an override use the provisional default row."))
    return out


def run_layout(pid, lid, mode, owner):
    work = None
    with LAYOUT_SEM:
        try:
            _, st, wf = load(pid)
            if not LAYOUT_PY.exists():
                raise RuntimeError("the spatial layout adapter (adapter/workflow_layout.py) is not installed yet; set placements manually or keep the provisional default")
            upid = wf.get("primary_upload_id")
            if not upid:
                raise RuntimeError("no primary workspace video; upload one and run the analysis")
            src = ensure_source(pid, owner, upid)
            if not src:
                raise RuntimeError("the workspace video is not on the worker and has no stored copy; upload it again")
            u = S.upload_row(pid, upid)
            vals, tsrc = table_m(wf)
            try:
                cm = ",".join(str(round(float(vals[k]) * 100, 2)) for k in ("length_m", "width_m", "height_m"))
            except (TypeError, ValueError):
                raise RuntimeError("enter the table length, width and height first (metric scale depends on the table height)")
            work = S.CACHE / "workflow_layout" / pid / (u.get("sha256") or upid)[:16]
            work.mkdir(parents=True, exist_ok=True)
            sel = selections(wf)
            al = copy.deepcopy(wf["layout"].get("alignment") or {})
            al["primary_robot"] = al.get("primary_robot") or primary_arm(wf)
            al["object_overrides"] = {k: {kk: v[kk] for kk in ("x_m", "y_m", "yaw_deg", "source") if kk in v} for k, v in (al.get("object_overrides") or {}).items()}
            (work / "selections.json").write_text(json.dumps(sel))
            (work / "alignment.json").write_text(json.dumps(al))
            out = work / f"layout_{lid}.json"
            cmd = "align" if mode == "align" or (mode == "auto" and (work / "sparse").exists()) else "run"
            args = [S.PY, str(LAYOUT_PY), cmd, "--work", str(work), "--table-cm", cm, "--out", str(out), "--table-source", tsrc if tsrc != "unknown" else "estimated",
                    "--selections", str(work / "selections.json"), "--alignment", str(work / "alignment.json")]
            if cmd == "run":
                args[3:3] = ["--video", str(src)]
            setlay(pid, lid, status="running", command=cmd)
            with S.LOCK:
                r = subprocess.run(args, capture_output=True, text=True, timeout=2400)
            if not out.exists():
                raise RuntimeError(f"layout adapter exited {r.returncode} without a result: " + (r.stderr or r.stdout)[-400:])
            res = json.loads(out.read_text())
            mid, files = S.db.store_dir(owner, pid, "workflow_layout", lid, "1", [("layout.json", out.read_bytes(), "application/json"),
                                        ("selections.json", json.dumps(sel).encode(), "application/json"), ("alignment.json", json.dumps(al).encode(), "application/json")],
                                        deps=dict(command=cmd, video_sha256=u.get("sha256"), exit=r.returncode))
            setlay(pid, lid, status="complete" if r.returncode == 0 else "failed", exit_code=r.returncode, manifest_id=mid, result=res,
                   error=None if r.returncode == 0 else "; ".join(b.get("message", "") for b in res.get("blocking") or [])[:600])
        except Exception as e:
            traceback.print_exc()
            setlay(pid, lid, status="failed", error=str(e)[:800])


def setlay(pid, lid, result=None, **ch):
    def f(wf, p, st):
        lay = wf["layout"]
        run = lay.get("run") or {}
        if run.get("id") != lid:
            return
        run.update(ch)
        if ch.get("status") in ("complete", "failed"):
            run["finished_at"] = now_ms()
            LAYOUT_THREADS.pop(pid, None)
        lay["run"] = run
        if result is not None:
            keep = {k: result.get(k) for k in ("format", "status", "blocking", "warnings", "inputs", "reconstruction", "scale", "table", "objects", "robots",
                                                "settings_patch", "composer_gap", "keyframes")}
            keep["clusters"] = (result.get("clusters") or [])[:40]
            lay["result"] = keep
        eff = effective_layout(wf)
        lay.update(placements=eff["placements"], table_pose=eff["table_pose"], status=eff["status"], provisional=eff["provisional"],
                   placements_source=eff["source"], label=eff["label"])
    mutate(pid, f)
    WAKE.set()


def start_layout(pid, mode, owner):
    lid = nid("lay")
    def f(wf, p, st):
        if (wf["layout"].get("run") or {}).get("status") == "running" and pid in LAYOUT_THREADS:
            raise jerr(409, "a layout recovery is already running for this project")
        wf["layout"]["run"] = dict(id=lid, status="queued", mode=mode, started_at=now_ms())
    mutate(pid, f)
    t = threading.Thread(target=run_layout, args=(pid, lid, mode, owner), daemon=True)
    LAYOUT_THREADS[pid] = t
    t.start()
    return lid


# ── build coordinator ──────────────────────────────────────────────────────────
STEP_LABELS = dict(prepare="Prepare reviewed inputs", robot="Import and confirm robot model", assets="Build object meshes",
                   accept="Accept reviewed assets", layout="Layout", compose="Compose preview", export="Export package")


def mk_steps(mode):
    keys = ["prepare"] + (["robot"] if mode == "workspace" else []) + ["assets", "accept"] + (["layout"] if mode == "workspace" else []) + ["compose", "export"]
    return [dict(key=k, label=STEP_LABELS[k], status="pending") for k in keys]


def _ck():
    import checkpoints
    return checkpoints


def step(b, key, status, detail=None, **kw):
    for s in b["steps"]:
        if s["key"] == key:
            _ck().apply_timing(s, status)       # started_at once, active_since_at, elapsed_ms, finished_at
            if detail is not None:
                s["detail"] = str(detail)[:300]
            s.update(kw)
            s["at"] = now_ms()


def setb(pid, bid, fn):
    def f(wf, p, st):
        b = wf.get("build")
        if not b or b.get("id") != bid:
            return None
        return fn(wf, b, p)
    return mutate(pid, f)[0]


def fail_build(pid, bid, key, msg, action):
    def f(wf, b, p):
        step(b, key, "failed", error=msg[:600])
        b.update(phase="failed", error=msg[:800], action=action, finished_at=now_ms())
        hist(wf, b)
    setb(pid, bid, f)


def hist(wf, b):
    summ = {k: b.get(k) for k in ("id", "phase", "mode", "created_at", "finished_at", "error")}
    wf["history"] = ([summ] + [x for x in wf.get("history") or [] if x.get("id") != b["id"]])[:5]


def derived_upload(pid, owner, crop):
    upid = "up_wf" + hashlib.sha256((pid + crop["sha256"]).encode()).hexdigest()[:17]
    row = S.upload_row(pid, upid)
    src = S.UPLOADS / upid / "source.jpg"
    if row and row.get("status") == "ready" and src.exists():
        return upid
    meta, data = S.db.read_bytes(owner, pid, crop["file_id"])
    if hashlib.sha256(data).hexdigest() != crop["sha256"]:
        raise RuntimeError("stored crop does not match its hash")
    src.parent.mkdir(parents=True, exist_ok=True)
    src.write_bytes(data)
    S.put_upload(owner, dict(id=upid, project_id=pid, capture_mode="object", filename=f"crop_{crop['frame_index']}_{crop['sha256'][:8]}.jpg",
                             media_type="image/jpeg", size=len(data), sha256=crop["sha256"], status="ready", chunk_size=len(data) or 1, chunks_total=1,
                             chunks_received=1, error="", meta_json=dict(kind="image", width=crop["px"]["w"], height=crop["px"]["h"], thumb_file_id=crop["file_id"],
                             derived_from=dict(upload_id=crop["upload_id"], upload_sha256=crop["upload_sha256"], frame_index=crop["frame_index"],
                                               time_s=crop["time_s"], box=crop["box"], analysis_id=crop["analysis_id"], crop_file_id=crop["file_id"]))))
    return upid


def robots_pending(imports):
    """True while any model import used by a multi-arm build is not finished. `imports` = build.robot_imports."""
    return bool(imports) and any(not (im.get("confirmed") or im.get("complete")) for im in imports.values())


def sync_arm_status(b):
    """build.robots = [{name, label, library_id, import_run_id, reused, status}] (UI contract); status follows its model import."""
    imps = b.get("robot_imports") or {}
    for a in b.get("robots") or []:
        im = imps.get(a["library_id"]) or {}
        a["reused"] = im.get("reused")
        a["status"] = "ready" if (im.get("confirmed") or im.get("complete")) else "importing"
        a["confirmed"] = a["status"] == "ready"


def tick_build(pid):
    _, st, wf = load(pid)
    b = (wf or {}).get("build")
    if not b or b.get("phase") not in ACTIVE_PHASES:
        ACTIVE.discard(pid)
        return
    bid, ph = b["id"], b["phase"]
    owner = S.db.sql(f"SELECT owner_uid FROM project WHERE id = {q(pid)}")[0]["owner_uid"]
    uid = b["created_by"]
    snap = b["inputs"]
    try:
        if ph == "queued":
            prep, objs_out = [], {}
            for o in snap["objects"]:
                ups = [derived_upload(pid, owner, c) for c in o["crops"]] + list(o.get("extra_upload_ids") or [])
                for x in ups:
                    if not S.upload_row(pid, x) or not ensure_source(pid, owner, x):
                        raise RuntimeError(f"{o['name']}: input {x} is not available on the worker; upload it again")
                shas = sorted((S.upload_row(pid, x) or {}).get("sha256") or x for x in ups)
                mesh_key = sha_json(dict(name=o["name"], inputs=shas))
                asset_key = sha_json(dict(mesh=mesh_key, m=o["measurements"]))
                prep.append((o, ups, mesh_key, asset_key))
            for upid in {c["upload_id"] for o in snap["objects"] for c in o["crops"]} | {x for o in snap["objects"] for x in o.get("extra_upload_ids") or []} | ({snap.get("primary_upload_id")} - {None}):
                src = ensure_source(pid, owner, upid)
                if src:
                    record_source(pid, upid, persist_source(pid, owner, S.upload_row(pid, upid), src))
            for o, ups, mk, ak in prep:
                av = None
                avid = (wf.get("asset_cache") or {}).get(ak)
                if avid:
                    try:
                        av = S.asset_version(pid, avid)
                    except Exception:
                        av = None
                if av and av["status"] in ("needs_review", "accepted"):
                    objs_out[o["id"]] = dict(name=o["name"], asset_version_id=av["id"], run_id=av.get("run_id"), reused="asset", mesh_key=mk, asset_key=ak)
                    continue
                parent = (wf.get("mesh_cache") or {}).get(mk)
                inp = dict(measurements=o["measurements"], tags={}, estimates_reviewed=bool(b.get("acknowledge_estimates")), started_by=uid,
                           workflow=dict(build_id=bid, object_id=o["id"]))
                if parent:
                    try:
                        S.asset_version(pid, parent)
                        rid = S.new_run(owner, pid, "rebuild", dict(inp, parent_id=parent))
                        objs_out[o["id"]] = dict(name=o["name"], run_id=rid, reused="mesh", mesh_key=mk, asset_key=ak, parent_id=parent)
                        continue
                    except Exception:
                        pass
                rid = S.new_run(owner, pid, "asset", dict(inp, upload_ids=ups, object_name=o["name"]))
                objs_out[o["id"]] = dict(name=o["name"], run_id=rid, reused="none", mesh_key=mk, asset_key=ak)
            robot, robots = None, None
            if snap["mode"] == "workspace":
                inst = snap["robot"].get("instances") or [dict(name="robot", library_id=snap["robot"]["selected_library_id"], synthetic=True)]
                lib = inst[0]["library_id"]              # primary arm's model: confirmed into settings.robot (legacy contract)
                cur = st.get("robot") or {}
                imports = {}                             # one import per unique model, shared by every arm that uses it
                runs_ = None
                for lid_ in dict.fromkeys(i_["library_id"] for i_ in inst):
                    if cur.get("library_id") == lid_ and cur.get("manifest_id"):
                        imports[lid_] = dict(library_id=lid_, reused="confirmed", import_run_id=cur.get("import_run_id"), confirmed=True)
                        continue
                    runs_ = runs_ if runs_ is not None else S.rows("pipeline_run", pid)
                    done = [r for r in runs_ if r["kind"] == "import_robot" and r["status"] == "complete"
                            and ((r.get("output_json") or {}).get("robot_import") or {}).get("library_id") == lid_]
                    if done:
                        imports[lid_] = dict(library_id=lid_, reused="import", import_run_id=max(done, key=lambda r: r.get("created_at") or 0)["id"], confirmed=False)
                    else:
                        p_ = S.project_for(owner, pid)
                        imports[lid_] = dict(library_id=lid_, reused="none", import_run_id=S.create_ext_run(uid, p_, pid, "import_robot", dict(library_id=lid_, user_confirmed_model=True)), confirmed=False)
                robot = dict(imports[lib], arm=inst[0]["name"])
                if len(inst) > 1:
                    robots = dict(imports=imports, arms=[dict(name=i_["name"], arm_id=i_.get("arm_id"), label=i_.get("label"), library_id=i_["library_id"],
                                                             library_resolution=i_.get("library_resolution"), import_run_id=imports[i_["library_id"]]["import_run_id"],
                                                             primary=k_ == 0)
                                                        for k_, i_ in enumerate(inst)])

            def f(wf, b, p):
                b["objects"], b["robot"] = objs_out, robot
                if robots:
                    b["robots"], b["robot_imports"] = robots["arms"], robots["imports"]
                    sync_arm_status(b)
                step(b, "prepare", "done", f"{len(prep)} object(s), inputs verified; sources stored in SpacetimeDB")
                step(b, "assets", "running", ", ".join(f"{v['name']}: {'reused' if v['reused'] == 'asset' else 'rebuild from stored mesh' if v['reused'] == 'mesh' else 'new mesh'}" for v in objs_out.values()))
                if robot:
                    lbl = (f"{len(robots['arms'])} arms · " + ", ".join(f"{k} ({v['reused']})" for k, v in robots["imports"].items())) if robots else f"{robot['library_id']} ({robot['reused']})"
                    step(b, "robot", "done" if robot["confirmed"] and not robots_pending(robots and robots["imports"]) else "running", lbl, run_id=robot.get("import_run_id"))
                b["phase"] = "building"
            setb(pid, bid, f)
            return
        if ph == "building":
            rb = b.get("robot")
            mrb = b.get("robot_imports")
            if mrb and robots_pending(mrb):
                upd, bad = {}, []
                for lid_, im in mrb.items():
                    if im.get("confirmed") or im.get("complete"):
                        continue
                    r = run_row(pid, im["import_run_id"])
                    if r and r["status"] == "complete":
                        upd[lid_] = True
                    elif r and r["status"] == "failed":
                        bad.append(f"{lid_}: {r.get('error')}")
                if bad:
                    return fail_build(pid, bid, "robot", "robot import failed: " + "; ".join(bad), "Check the robot model choices, then press Build again.")
                if upd:
                    def f(wf, b, p):
                        for k in upd:
                            b["robot_imports"][k].update(complete=True)
                        sync_arm_status(b)
                        if not robots_pending(b["robot_imports"]) and (b.get("robot") or {}).get("confirmed"):
                            step(b, "robot", "done", f"{len(b['robots'])} arms: " + ", ".join(b["robot_imports"]) + " imported and verified")
                    setb(pid, bid, f)
            if rb and not rb.get("confirmed"):
                r = run_row(pid, rb["import_run_id"])
                if r and r["status"] == "complete":
                    with WF_LOCK:
                        loop("POST", f"/api/projects/{pid}/robot", uid, dict(import_run_id=rb["import_run_id"], confirm=True))
                    def f(wf, b, p):
                        b["robot"].update(confirmed=True)
                        if b.get("robot_imports"):
                            b["robot_imports"].get(rb["library_id"], {}).update(complete=True)
                            sync_arm_status(b)
                        if not robots_pending(b.get("robot_imports")):
                            step(b, "robot", "done", f"{rb['library_id']} imported, verified and confirmed" if not b.get("robots") else
                                 f"{len(b['robots'])} arms: " + ", ".join(b["robot_imports"]) + " imported and verified")
                    setb(pid, bid, f)
                elif r and r["status"] == "failed":
                    return fail_build(pid, bid, "robot", f"robot import failed: {r.get('error')}", "Check the robot model choice, then press Build again.")
            pending, failed, ack_objs = [], [], []
            outs = {}
            for oid, v in b["objects"].items():
                if v.get("asset_version_id"):
                    continue
                r = run_row(pid, v["run_id"])
                if not r:
                    failed.append(f"{v['name']}: run missing")
                elif r["status"] in ("needs_review", "complete") and (r.get("output_json") or {}).get("asset_version_id"):
                    outs[oid] = r["output_json"]["asset_version_id"]
                elif r["status"] == "failed":
                    failed.append(f"{v['name']}: {r.get('error') or 'failed'}")
                    try:
                        rec = _ck().run_recovery(r, _ck().for_run(pid, r["id"]))
                        if rec["requires_ack"]:
                            ack_objs.append(dict(object_id=oid, name=v["name"], run_id=r["id"], state=rec["state"]))
                    except Exception:
                        pass
                else:
                    pending.append(v["name"])
            if outs:
                def f(wf, b, p):
                    for oid, avid in outs.items():
                        v = b["objects"][oid]
                        v["asset_version_id"] = avid
                        wf.setdefault("asset_cache", {})[v["asset_key"]] = avid
                        wf.setdefault("mesh_cache", {})[v["mesh_key"]] = v.get("parent_id") or avid
                setb(pid, bid, f)
            if failed:
                if ack_objs:
                    setb(pid, bid, lambda wf, b, p: b.__setitem__("recovery", dict(state="provider_outcome_unknown", requires_ack=True, objects=ack_objs,
                         note="A mesh request was in flight when the worker restarted; its outcome is unknown and no mesh was saved.")))
                    return fail_build(pid, bid, "assets", "; ".join(failed), "Press Build again and confirm repeating the interrupted mesh request (it may use GPU quota again); finished objects and saved meshes are reused.")
                return fail_build(pid, bid, "assets", "; ".join(failed), "Fix the inputs of the failed object(s) and press Build again; finished objects are reused (no new mesh call).")
            if not pending and (not rb or b["robot"].get("confirmed") or run_row(pid, rb["import_run_id"]) is None):
                _, _, wf2 = load(pid)
                if (wf2["build"].get("robot") or {}).get("confirmed", True) and not robots_pending(wf2["build"].get("robot_imports")):
                    setb(pid, bid, lambda wf, b, p: (step(b, "assets", "done", f"{len(b['objects'])} asset version(s) built and stored"), b.update(phase="accepting")))
            return
        if ph == "accepting":
            names = []
            for oid, v in b["objects"].items():
                av = S.asset_version(pid, v["asset_version_id"])
                if av["status"] == "needs_review":
                    loop("POST", f"/api/projects/{pid}/assets/{av['id']}/review", uid,
                         dict(action="accept", estimates_reviewed=bool(b.get("acknowledge_estimates")),
                              note="Accepted by workflow build approval: inputs reviewed before build; reconstruction inspected in the preview before export."))
                elif av["status"] != "accepted":
                    return fail_build(pid, bid, "accept", f"{v['name']}: asset version is {av['status']}", "Press Build again to rebuild it.")
                names.append(v["name"])
            nxt = "layout" if b["mode"] == "workspace" else "composing"
            setb(pid, bid, lambda wf, b, p: (step(b, "accept", "done", ", ".join(names)), b.update(phase=nxt)))
            return
        if ph == "layout":
            lay = wf["layout"]
            run = lay.get("run") or {}
            if run.get("status") in ("queued", "running") and pid in LAYOUT_THREADS:
                setb(pid, bid, lambda wf, b, p: step(b, "layout", "running", "recovering layout from the video"))
                return
            eff = effective_layout(wf)
            def f(wf, b, p):
                b["layout"] = dict(eff, layout_run_id=(wf["layout"].get("run") or {}).get("id"), result_status=(wf["layout"].get("result") or {}).get("status"))
                step(b, "layout", "done" if not eff["provisional"] else "waiting", eff["label"])
                b["phase"] = "composing"
            setb(pid, bid, f)
            return
        if ph == "composing":
            pv = b.get("preview") or {}
            if b["mode"] == "workspace":
                if not pv.get("run_id"):
                    vals, tsrc = table_m(wf)
                    table = dict(length_m=float(vals["length_m"]), width_m=float(vals["width_m"]), height_m=float(vals["height_m"]), source=tsrc,
                                 note="from the reviewed workflow draft")
                    lay = b.get("layout") or {}
                    if lay.get("table_pose"):
                        table["pose"] = lay["table_pose"]
                    pl = {n: dict(x_m=v["x_m"], y_m=v["y_m"], yaw_deg=v.get("yaw_deg") or 0,
                                  source=v.get("source") if v.get("source") in ("manual", "reconstructed", "default", "copilot", "measured", "estimated") else "estimated")
                          for n, v in (lay.get("placements") or {}).items()}
                    p_ = S.project_for(owner, pid)
                    ids = [v["asset_version_id"] for v in b["objects"].values()]
                    req = dict(asset_version_ids=ids, table=table, placements=pl)
                    if b.get("robots"):
                        poses = lay.get("robots") or {}
                        req["robots"] = [dict(name=a_["name"], label=a_.get("label"), library_id=a_["library_id"], import_run_id=a_["import_run_id"],
                                              pose=(poses.get(a_["name"]) or {}).get("pose") if i_ else None,
                                              pose_source=(poses.get(a_["name"]) or {}).get("source") or ("origin" if not i_ else "default"))
                                         for i_, a_ in enumerate(b["robots"])]
                    rid = S.create_ext_run(uid, p_, pid, "workcell", req)
                    setb(pid, bid, lambda wf, b, p: (b.__setitem__("preview", dict(kind="workcell", run_id=rid)), step(b, "compose", "running", "workcell composition + drop test", run_id=rid)))
                    return
                r = run_row(pid, pv["run_id"])
                if r and r["status"] == "complete":
                    wc = (r.get("output_json") or {}).get("workcell") or {}
                    ok = wc.get("result") == "pass"
                    def f(wf, b, p):
                        b["preview"].update(result=wc.get("result"), preview=wc.get("preview"), traj=wc.get("traj"), placements=wc.get("placements"),
                                            warnings=wc.get("warnings"), workcell_id=wc.get("id"), manifest_id=wc.get("manifest_id"))
                        if wc.get("robots"):
                            b["preview"]["robots"] = [{k: r_.get(k) for k in ("name", "label", "library_id", "base_pose", "pose_source", "end_effector_body",
                                                                              "action_slice", "robot_import_id")} for r_ in wc["robots"]]
                        if ok:
                            step(b, "compose", "done", "every object settled upright on the table")
                            b.update(phase="awaiting_approval", action="Inspect the preview, then approve the export.")
                        else:
                            step(b, "compose", "failed", "not every object settled upright", error="drop test failed")
                            b.update(phase="failed", error="the composed workcell did not pass the drop test", action="Adjust placements or table, then press Compose.", finished_at=now_ms())
                            hist(wf, b)
                    setb(pid, bid, f)
                elif r and r["status"] == "failed":
                    fail_build(pid, bid, "compose", r.get("error") or "composition failed", "Adjust placements/table, then press Compose (meshes are kept).")
                return
            # single object: the accepted asset's legacy scene run (SO-100 reference profile)
            v = next(iter(b["objects"].values()))
            av = S.asset_version(pid, v["asset_version_id"])
            svs = sorted([s for s in S.rows("scene_version", pid) if s.get("asset_version_ids") == [av["id"]]], key=lambda s: -(s.get("created_at") or 0))
            r = run_row(pid, av["run_id"])
            if svs and (not r or r["status"] in ("complete", "failed")):
                sv = svs[0]
                files = {f["path"]: f["id"] for f in S.db.manifest_files(av["manifest_id"])}
                def f(wf, b, p):
                    b["preview"] = dict(kind="scene", scene_version_id=sv["id"], result=sv["status"], asset_version_id=av["id"], asset_manifest_id=av["manifest_id"],
                                        asset_zip=dict(manifest_id=av["manifest_id"], prefix="asset/", name=av["object_name"]),
                                        preview={k[len("preview/"):]: fid for k, fid in files.items() if k.startswith("preview/")},
                                        profile_note="Legacy SO-100 reference scene (MuJoCo Menagerie trs_so_arm100) for a drop test. Not your robot.")
                    step(b, "compose", "done" if sv["status"] == "validated" else "failed", f"reference scene {sv['status']}")
                    b.update(phase="awaiting_approval", action="Download the asset ZIP, or approve the optional legacy reference Gymnasium export.")
                setb(pid, bid, f)
            elif r and r["status"] == "failed" and not svs:
                fail_build(pid, bid, "compose", r.get("error") or "scene build failed", "The asset is accepted; retry from the project runs or press Build again.")
            return
        if ph == "exporting":
            ex = b.get("export") or {}
            r = run_row(pid, ex.get("run_id"))
            if not r or r["status"] in ("queued", "running"):
                return
            if r["status"] == "failed":
                return fail_build(pid, bid, "export", r.get("error") or "export failed", "Press Approve again to retry the export.")
            out = r.get("output_json") or {}
            exd = out.get("export") or {}
            if b["mode"] == "object":
                e = next((e for e in S.rows("gym_export", pid) if e["id"] == ex.get("export_id")), {})
                exd = dict(id=e.get("id"), package_version=e.get("package_version"), env_id=e.get("env_id"), status=e.get("status"),
                           archive_file_id=e.get("archive_file_id"), manifest_id=e.get("manifest_id"))
            prov = dict(format="robotics-workspace-provenance-v1", build_id=bid, project_id=pid, created_at=now_ms(), mode=b["mode"],
                        inputs=snap, sources=wf.get("sources") or {}, analysis=wf.get("analysis") and {k: wf["analysis"].get(k) for k in ("id", "model", "manifest_id", "upload_sha256", "prompt_version", "excluded", "scale_reference")},
                        objects=b["objects"], robot=b.get("robot"), robots=b.get("robots"), robot_imports=b.get("robot_imports"), layout=b.get("layout"), preview={k: (b.get("preview") or {}).get(k) for k in ("kind", "run_id", "scene_version_id", "workcell_id", "result", "placements", "robots")},
                        export=exd, approved_by=ex.get("approved_by"), approved_at=ex.get("approved_at"),
                        honesty="Dimensions/mass/friction carry their source labels; estimated values were acknowledged, not measured. Layout provenance in layout.status.")
            mid, files = S.db.store_dir(owner, pid, "workflow_provenance", bid, "1", [("provenance.json", json.dumps(prov, indent=1).encode(), "application/json")])
            def f(wf, b, p):
                b["export"].update(status=exd.get("status"), export_id=exd.get("id"), package_version=exd.get("package_version"), env_id=exd.get("env_id"),
                                   archive_file_id=exd.get("archive_file_id"), manifest_id=exd.get("manifest_id"), provenance_manifest_id=mid, provenance_file_id=files[0]["id"])
                step(b, "export", "done", f"package {exd.get('package_version')} · provenance stored")
                b.update(phase="exported", finished_at=now_ms(), action=None)
                hist(wf, b)
            setb(pid, bid, f)
    except web.HTTPException as e:
        try:
            msg = json.loads(e.text).get("error")
        except Exception:
            msg = str(e)
        fail_build(pid, bid, {"queued": "prepare", "building": "assets", "accepting": "accept", "layout": "layout", "composing": "compose", "exporting": "export"}.get(ph, "prepare"),
                   msg, "Fix the reported input and press Build again.")
    except Exception as e:
        traceback.print_exc()
        fail_build(pid, bid, {"queued": "prepare", "building": "assets", "accepting": "accept", "layout": "layout", "composing": "compose", "exporting": "export"}.get(ph, "prepare"),
                   str(e), "Press Build again after fixing the cause; finished artifacts are reused.")


def coordinator():
    last_scan = 0
    while True:
        WAKE.wait(3)
        WAKE.clear()
        try:
            if time.time() - last_scan > 60:
                last_scan = time.time()
                for p in S.db.sql("SELECT id, settings_json FROM project"):
                    try:
                        st = json.loads(p["settings_json"] or "{}")
                    except Exception:
                        continue
                    wf = st.get("workflow") or {}
                    if (wf.get("build") or {}).get("phase") in ACTIVE_PHASES:
                        ACTIVE.add(p["id"])
            if _ck().hold_state():
                continue          # maintenance hold: builds wait (nothing new starts); they continue when it clears
            for pid in list(ACTIVE):
                tick_build(pid)
        except Exception:
            traceback.print_exc()


def _recover_once():
    """Restart recovery: builds resume (children recover via the server); analyses/layouts that were running are marked
    failed with a clear reason instead of silently re-billing the paid call."""
    for p in S.db.sql("SELECT id, settings_json FROM project"):
        try:
            wf = (json.loads(p["settings_json"] or "{}")).get("workflow") or {}
        except Exception:
            continue
        a = wf.get("analysis") or {}
        if a.get("status") in ("queued", "running"):
            setan(p["id"], a["id"], status="failed", error="the backend restarted during the analysis; run it again (not retried automatically)", finished_at=now_ms())
        lr = (wf.get("layout") or {}).get("run") or {}
        if lr.get("status") in ("queued", "running"):
            setlay(p["id"], lr["id"], status="failed", error="the backend restarted during layout recovery; press Recover layout again")
        if (wf.get("build") or {}).get("phase") in ACTIVE_PHASES:
            ACTIVE.add(p["id"])
    WAKE.set()


def recover():
    delay = 1
    while True:
        try:
            _recover_once()
            return
        except Exception as e:
            log("waiting for workflow recovery", type(e).__name__)
            time.sleep(delay)
            delay = min(15, delay * 2)


# ── HTTP ───────────────────────────────────────────────────────────────────────
def _uid(request):
    uid = request.headers.get("X-Sender-Uid")
    if not uid:
        raise jerr(403, "sign in required")
    return uid


def can_bill(request, uid, p):
    if SANDBOX_UID and (uid == SANDBOX_UID or p["owner_uid"] == SANDBOX_UID):
        return True
    if uid in TEST_UIDS and not request.headers.get("X-Forwarded-For"):
        return True
    import public_beta
    return public_beta.claude_allowed(request)


def view(pid, uid, request=None, p=None):
    p = p or S.project_for(uid, pid)
    _, st, wf = load(pid)
    stored = wf is not None
    wf = default_robot_if_absent(wf or new_workflow(infer_mode(pid)))
    ups = ready_uploads(pid)
    if wf.get("mode") == "workspace" and isinstance(wf.get("robot"), dict):
        # Response view only (the stored record is not rewritten): composition supports any number of arms.
        if "single_arm_only" in wf["robot"]:
            wf["robot"]["single_arm_only"] = False
        wf["robot"]["multi_arm"] = True
    rd = readiness(wf, pid, ups)
    b = wf.get("build") or {}
    if b.get("phase") in ACTIVE_PHASES:
        rd["warnings"].insert(0, "A build is in progress; it uses the inputs snapshot taken when Build was pressed.")
    cat = [dict(id=r["id"], name=r["name"], maker=r["maker"], dof=r["dof"], gripper=r["gripper"], note=r["note"]) for r in S.ROBOTS.CATALOG]
    uplist = [dict(id=u["id"], filename=u["filename"], capture_mode=u["capture_mode"], status=u["status"], sha256=u.get("sha256"), size=u.get("size"),
                   meta=u.get("meta_json"), derived=bool((u.get("meta_json") or {}).get("derived_from")) if isinstance(u.get("meta_json"), dict) else False,
                   source_file_id=((wf.get("sources") or {}).get(u["id"]) or {}).get("file_id")) for u in ups]
    return dict(workflow=wf, stored=stored, revision=int(wf.get("revision") or 0), can_write=p.get("role") in ("owner", "editor"),
                can_analyze=bool(request is not None and (can_bill(request, uid, p) or __import__("public_beta").claude_available(request, uid, p)["available"])), readiness=rd, catalog=cat, uploads=uplist,
                robot_confirmed=st.get("robot"), layout_adapter=LAYOUT_PY.exists(), checkpoints=checkpoint_view(pid, wf),
                robot_instances=robot_instances(wf) if wf.get("mode") == "workspace" else [],
                arm_poses=(effective_layout(wf).get("robots") or {}) if wf.get("mode") == "workspace" else {},
                limits=dict(max_objects=MAX_OBJECTS, frames=[4, 10], max_crops=MAX_CROPS),
                identification=_identification_view(wf, uid, request, p))


def _identification_view(wf, uid, request, p):
    """Product lookup availability (Apify evidence + Claude) for GET; never raises and never exposes credentials."""
    try:
        import item_evidence
        return item_evidence.view_info(wf, uid, request, p)
    except Exception as e:
        log("identification view unavailable:", type(e).__name__)
        return dict(job=wf.get("identification"), connected=None, actor_ready=False, available=False, can_run=False, can_connect=False)


def H(fn):
    async def h(request):
        try:
            return await fn(request)
        except web.HTTPException:
            raise
        except Exception as e:
            traceback.print_exc()
            msg = str(e)
            if type(e).__name__ == "StdbError":
                raise jerr(403 if "access" in msg else 409, msg)
            raise jerr(500, f"{type(e).__name__}: {msg}")
    return h


def th(fn, *a):
    import asyncio
    return asyncio.to_thread(fn, *a)


async def body(request):
    try:
        b = await request.json()
    except Exception:
        raise jerr(400, "JSON body required")
    if not isinstance(b, dict):
        raise jerr(400, "JSON object required")
    return b


def check_rev(wf, b):
    if "base_revision" not in b:
        raise jerr(422, "base_revision is required")
    if int(b["base_revision"]) != int(wf.get("revision") or 0):
        raise jerr(409, "the draft changed since you loaded it (another edit or an analysis); reload and reapply", workflow=wf, revision=int(wf.get("revision") or 0))


@H
async def create_project(request):
    uid = _uid(request)
    b = await body(request)
    mode = b.get("mode", "workspace")
    if mode not in ("workspace", "object"):
        raise jerr(422, "mode must be workspace or object")
    name = str(b.get("name") or "").strip()[:120] or ("Untitled workspace" if mode == "workspace" else "Untitled object")
    def work():
        pid = S.new_id("prj")
        wf = new_workflow(mode) if mode == "workspace" else None
        if wf is not None:
            wf["revision"] = 1
        st = dict(units="metric", description=str(b.get("description") or "")[:500], project_mode=mode,
                  legacy_profile_note="SO-100 is the legacy single-object reference profile; workspace robot selection is separate, with an assumed SO-101 fallback.")
        if wf is not None:
            st["workflow"] = wf
        S.db.call("create_project", uid=uid, id=pid, name=name, robot_profile="so100-menagerie", settings_json=json.dumps(st))
        return dict(id=pid, workflow=wf, revision=1 if wf else 0, mode=mode)
    return web.json_response(await th(work))


@H
async def get_wf(request):
    uid = _uid(request)
    pid = request.match_info["pid"]
    return web.json_response(await th(view, pid, uid, request))


@H
async def put_wf(request):
    uid = _uid(request)
    pid = request.match_info["pid"]
    b = await body(request)
    def work():
        S.project_for(uid, pid, write=True)
        def f(wf, p, st):
            check_rev(wf, b)
            apply_put(wf, b, uid, pid)
        mutate(pid, f, bump=True)
        return view(pid, uid, request)
    return web.json_response(await th(work))


@H
async def post_analysis(request):
    uid = _uid(request)
    pid = request.match_info["pid"]
    b = await body(request) if request.can_read_body else {}
    def work():
        hold_guard()
        p = S.project_for(uid, pid, write=True)
        if not can_bill(request, uid, p):
            raise jerr(403, "Claude analysis is billed to the workspace owner's Mana credits, so only the owner's projects can run it")
        nf = int(num(b.get("frames", 8)) or 8)
        if not 4 <= nf <= 10:
            raise jerr(422, "frames must be 4..10")
        _, st, wf = load(pid)
        wf = wf or new_workflow()
        if (wf.get("analysis") or {}).get("status") in ("queued", "running"):
            raise jerr(409, "an analysis is already running")
        u, src = pick_upload(pid, wf, b.get("upload_id"))
        aid = nid("ana")
        prev = None if b.get("force") else cached_analysis(pid, wf, u, nf)
        def f(wf, p_, st_):
            wf["analysis"] = dict(id=aid, status="queued", upload_id=u["id"], upload_sha256=u.get("sha256"), frames_requested=nf, started_at=now_ms(),
                                  started_by=uid, prompt_version=PROMPT_VERSION)
            if not wf.get("primary_upload_id"):
                wf["primary_upload_id"] = u["id"]
        mutate(pid, f)
        owner = p["owner_uid"]
        def go():
            try:
                record_source(pid, u["id"], persist_source(pid, owner, u, ensure_source(pid, owner, u["id"]) or src))
            except Exception as e:
                log("capture source persist failed", e)
                def fail_source(w, p_, st_):
                    if (w.get("analysis") or {}).get("id") == aid:
                        w["analysis"].update(status="failed", finished_at=now_ms(), error="The original capture could not be verified in SpacetimeDB. Retry analysis after storage is available.")
                mutate(pid, fail_source)
                return
            if prev:
                try:
                    if replay_cached(pid, aid, prev, u, owner):
                        return
                except Exception:
                    traceback.print_exc()
            run_analysis(pid, aid, u["id"], nf, uid)
        threading.Thread(target=go, daemon=True).start()
        return dict(analysis=dict(id=aid, status="queued", cached_candidate=bool(prev)))
    return web.json_response(await th(work), status=202)


@H
async def post_layout(request):
    uid = _uid(request)
    pid = request.match_info["pid"]
    b = await body(request) if request.can_read_body else {}
    def work():
        hold_guard()
        p = S.project_for(uid, pid, write=True)
        mode = b.get("mode", "auto")
        if mode not in ("auto", "run", "align"):
            raise jerr(422, "mode must be auto, run or align")
        lid = start_layout(pid, mode, p["owner_uid"])
        return dict(layout_run_id=lid)
    return web.json_response(await th(work), status=202)


def hold_guard():
    h = _ck().hold_state()
    if h:
        raise jerr(503, "RealiSIM is in a maintenance hold: new processing is paused. Your captures, inputs and saved results are kept. "
                        + (h.get("reason") or ""), code="maintenance_hold", since=h.get("at"))


def build_ack_needed(pid, cur):
    """Unknown paid outcome from the previous build's interrupted object runs that has no verified mesh checkpoint since."""
    if not cur or cur.get("phase") not in ("failed", "cancelled"):
        return None
    rec = cur.get("recovery") or {}
    objs = list(rec.get("objects") or []) if rec.get("requires_ack") else []
    if not objs:     # legacy builds (before recovery fields): inspect their failed object runs
        for oid, v in (cur.get("objects") or {}).items():
            r = run_row(pid, v.get("run_id")) if v.get("run_id") and not v.get("asset_version_id") else None
            if r and r["status"] == "failed":
                rr = _ck().run_recovery(r, _ck().for_run(pid, r["id"]))
                if rr["requires_ack"]:
                    objs.append(dict(object_id=oid, name=v.get("name"), run_id=r["id"], state=rr["state"]))
    return dict(state="provider_outcome_unknown", requires_ack=True, objects=objs) if objs else None


def checkpoint_view(pid, wf):
    """Read-only checkpoint/recovery exposure for the current build (derived from SpacetimeDB on each GET)."""
    b = wf.get("build") or {}
    out = dict(objects={}, ack=None)
    try:
        ck = _ck().project_summary(pid)
    except Exception as e:
        return dict(error=type(e).__name__, objects={}, ack=None)
    for oid, v in (b.get("objects") or {}).items():
        rid = v.get("run_id")
        r = run_row(pid, rid) if rid else None
        mine = [c for c in ck if c.get("run_id") == rid] if rid else []
        out["objects"][oid] = dict(name=v.get("name"), run_id=rid, checkpoints=mine,
                                   recovery=_ck().run_recovery(r, mine) if r else None)
    try:
        out["ack"] = build_ack_needed(pid, b)
    except Exception:
        out["ack"] = None
    h = _ck().hold_state()
    out["maintenance"] = dict(hold=bool(h), reason=(h or {}).get("reason"), since=(h or {}).get("at"))
    return out


@H
async def post_build(request):
    uid = _uid(request)
    pid = request.match_info["pid"]
    b = await body(request)
    def work():
        S.project_for(uid, pid, write=True)
        if b.get("confirm") is not True:
            raise jerr(422, "confirm: true is required (a person confirms the reviewed inputs, including the robot model)")
        hold_guard()
        def f(wf, p, st):
            check_rev(wf, b)
            cur = wf.get("build") or {}
            if cur.get("phase") in ACTIVE_PHASES:
                raise jerr(409, "a build is already running; cancel it first")
            rec = build_ack_needed(pid, cur)
            if rec and b.get("acknowledge_provider_repeat") is not True:
                raise jerr(409, "The previous build was interrupted while a paid mesh request was in flight and no mesh was saved. "
                                "Repeating it may use GPU quota again: confirm with acknowledge_provider_repeat: true.", code="ack_required", recovery=rec)
            rd = readiness(wf, pid)
            if not rd["ok"]:
                raise jerr(422, "resolve these before building: " + "; ".join(x["message"] for x in rd["blockers"][:6]), blockers=rd["blockers"])
            inc = [o for o in wf["objects"] if o.get("include")]
            est = [f"{o['name']}.{k}" for o in inc for k in [x[0] for x in NUM_FIELDS] + list(TEXT_FIELDS) if (o["fields"].get(k) or {}).get("source") == "estimated"]
            if wf["mode"] == "workspace":
                est += [f"table.{k}" for k, _, _ in TABLE_FIELDS if (wf["table"].get(k) or {}).get("source") == "estimated"]
            if est and b.get("acknowledge_estimates") is not True:
                raise jerr(422, "acknowledge_estimates: true is required; these values are estimates, not measurements: " + ", ".join(est[:12]), estimated=est)
            snap = dict(mode=wf["mode"], primary_upload_id=wf.get("primary_upload_id"), draft_revision=wf["revision"],
                        objects=[dict(id=o["id"], name=o["name"], label=o.get("label"), fields=copy.deepcopy(o["fields"]), measurements=measurements(o),
                                      crops=[c for c in o.get("crops") or [] if c["id"] in (o.get("selected_crop_ids") or [])],
                                      extra_upload_ids=list(o.get("extra_upload_ids") or []), coverage=o.get("coverage")) for o in inc],
                        excluded_objects=[dict(name=o["name"], reason=o.get("exclusion_reason")) for o in wf["objects"] if not o.get("include")],
                        table=copy.deepcopy(wf["table"]) if wf["mode"] == "workspace" else None,
                        robot=dict(selected_library_id=wf["robot"].get("selected_library_id"), selected_origin=wf["robot"].get("selected_origin"),
                                   arms=copy.deepcopy(wf["robot"].get("arms") or []), instances=robot_instances(wf),
                                   primary_robot=primary_arm(wf)) if wf["mode"] == "workspace" else None,
                        estimated=est)
            bd = dict(id=nid("bld"), phase="queued", mode=wf["mode"], created_at=now_ms(), created_by=uid, inputs=snap, steps=mk_steps(wf["mode"]),
                      acknowledge_estimates=bool(est), objects={}, robot=None, layout=None, preview=None, export=None, error=None, action=None)
            if rec:
                bd["provider_repeat_ack"] = dict(by=uid, at=now_ms(), previous_build=cur.get("id"), objects=rec.get("objects"))
            step(bd, "prepare", "running")
            if cur:
                hist(wf, cur)
            wf["build"] = bd
            return bd
        bd, _ = mutate(pid, f)
        ACTIVE.add(pid)
        WAKE.set()
        return dict(build=bd)
    return web.json_response(await th(work))


@H
async def cancel_build(request):
    uid = _uid(request)
    pid = request.match_info["pid"]
    def work():
        S.project_for(uid, pid, write=True)
        _, st, wf = load(pid)
        b = (wf or {}).get("build") or {}
        if b.get("phase") not in ACTIVE_PHASES:
            raise jerr(409, "no active build")
        rids = [v.get("run_id") for v in (b.get("objects") or {}).values() if not v.get("asset_version_id")]
        rids += [(b.get("robot") or {}).get("import_run_id"), (b.get("preview") or {}).get("run_id"), (b.get("export") or {}).get("run_id")]
        for rid in filter(None, rids):
            r = run_row(pid, rid)
            if r and r["status"] in ("queued", "running"):
                try:
                    loop("POST", f"/api/projects/{pid}/runs/{rid}/cancel", uid)
                except Exception as e:
                    log("cancel", rid, e)
        def f(wf, b, p):
            for s in b["steps"]:
                if s["status"] in ("running", "pending", "waiting"):
                    _ck().apply_timing(s, "skipped" if s["status"] == "pending" else "failed")
            b.update(phase="cancelled", finished_at=now_ms(), action="Press Build to start again; finished artifacts are reused.")
            hist(wf, b)
            return b
        return dict(build=setb(pid, b["id"], f))
    return web.json_response(await th(work))


@H
async def compose(request):
    uid = _uid(request)
    pid = request.match_info["pid"]
    def work():
        hold_guard()
        S.project_for(uid, pid, write=True)
        _, st, wf = load(pid)
        b = (wf or {}).get("build") or {}
        if b.get("mode") != "workspace" or not b.get("objects") or not all(v.get("asset_version_id") for v in b["objects"].values()):
            raise jerr(409, "compose needs a workspace build whose meshes are finished")
        if b.get("phase") in ACTIVE_PHASES:
            raise jerr(409, "the build is still running")
        if any(s["key"] == "accept" and s["status"] != "done" for s in b["steps"]):
            raise jerr(409, "assets are not accepted yet; press Build")
        def f(wf, b, p):
            b.update(phase="layout", preview=None, error=None, action=None, finished_at=None)
            for s in b["steps"]:
                if s["key"] in ("layout", "compose", "export"):
                    s.update(status="pending", error=None)
            return b
        out = setb(pid, b["id"], f)
        ACTIVE.add(pid)
        WAKE.set()
        return dict(build=out)
    return web.json_response(await th(work))


@H
async def approve(request):
    uid = _uid(request)
    pid = request.match_info["pid"]
    b_in = await body(request)
    def work():
        hold_guard()
        p = S.project_for(uid, pid, write=True)
        if b_in.get("confirm") is not True:
            raise jerr(422, "confirm: true is required (a person approves the previewed result)")
        _, st, wf = load(pid)
        b = (wf or {}).get("build") or {}
        if b.get("id") != b_in.get("build_id"):
            raise jerr(409, "build_id is not the current build")
        retry = b.get("phase") == "failed" and any(s["key"] == "export" and s["status"] == "failed" for s in b["steps"])
        if b.get("phase") != "awaiting_approval" and not retry:
            raise jerr(409, f"the build is {b.get('phase')}, not awaiting approval")
        pv = b.get("preview") or {}
        if b_in.get("preview_ref") not in (pv.get("run_id"), pv.get("scene_version_id")):
            raise jerr(409, "preview_ref does not match the preview of this build (reload)")
        if b["mode"] == "workspace" and (b.get("layout") or {}).get("provisional") and b_in.get("accept_provisional_layout") is not True:
            raise jerr(422, "the layout is a provisional default, not derived from the video: send accept_provisional_layout: true to export it anyway, or set placements and press Compose")
        prov = dict(build_id=b["id"], inputs=b["inputs"], layout=b.get("layout"), sources=wf.get("sources"), robot=b.get("robot"), robots=b.get("robots"), objects=b.get("objects"))
        if b["mode"] == "workspace":
            wr = S.one("pipeline_run", pid, pv["run_id"])
            if wr["status"] != "complete" or ((wr.get("output_json") or {}).get("workcell") or {}).get("result") != "pass":
                raise jerr(422, "the preview did not pass the drop test")
            n = 1 + sum(1 for r in S.rows("pipeline_run", pid) if r["kind"] == "workcell_export")
            slug = re.sub(r"[^a-z0-9]+", "_", str(p.get("name") or "workcell").lower()).strip("_")[:28] or "workcell"
            slug = slug if slug[0].isalpha() else "w_" + slug
            rid = S.new_run(p["owner_uid"], pid, "workcell_export", dict(workcell_run_id=wr["id"], package_version=f"1.0.{n}", env_id=f"RealiSIM/Workcell_{slug}-v{n}",
                                                                         slug=slug, project_name=p.get("name"), started_by=uid, workflow_provenance=prov))
            ex = dict(run_id=rid, kind="workcell_export")
        else:
            if pv.get("result") != "validated":
                raise jerr(422, "the reference scene did not validate; the asset ZIP is still available")
            r = loop("POST", f"/api/projects/{pid}/exports", uid, dict(scene_version_id=pv["scene_version_id"]))
            ex = dict(run_id=r["run_id"], export_id=r["id"], kind="legacy_reference_export", package_version=r["package_version"], env_id=r["env_id"],
                      note="Optional legacy reference export (SO-100 profile). The standalone asset ZIP does not need it.")
        ex.update(approved_by=uid, approved_at=now_ms(), accepted_provisional_layout=bool(b_in.get("accept_provisional_layout")))
        def f(wf, b, p_):
            b["export"] = ex
            step(b, "export", "running", ex["kind"], run_id=ex["run_id"], error=None)
            b.update(phase="exporting", error=None, action=None)
            return b
        out = setb(pid, b["id"], f)
        ACTIVE.add(pid)
        WAKE.set()
        return dict(build=out)
    return web.json_response(await th(work))


def register(app, services=None):
    """Add workflow routes and start the in-process coordinator. `services` = the server module (or a dict)."""
    global S, SANDBOX_UID
    S = _Svc(services or {})
    for n in ("db", "project_for", "rows", "one", "upload_row", "put_upload", "new_run", "create_ext_run", "asset_version", "UPLOADS", "PORT",
              "ROBOTS", "STATE", "measurement_problems", "new_id", "PY", "CACHE", "LOCK"):
        getattr(S, n)
    try:
        from env import get_user_id
        SANDBOX_UID = get_user_id()
    except Exception:
        SANDBOX_UID = None
    r = app.router
    r.add_post("/api/workflow/projects", create_project)
    r.add_get("/api/projects/{pid}/workflow", get_wf)
    r.add_put("/api/projects/{pid}/workflow", put_wf)
    r.add_post("/api/projects/{pid}/workflow/analysis", post_analysis)
    r.add_post("/api/projects/{pid}/workflow/layout", post_layout)
    r.add_post("/api/projects/{pid}/workflow/build", post_build)
    r.add_post("/api/projects/{pid}/workflow/build/cancel", cancel_build)
    r.add_post("/api/projects/{pid}/workflow/compose", compose)
    r.add_post("/api/projects/{pid}/workflow/approve", approve)
    import item_evidence                      # POST /workflow/identify: Apify web evidence + Claude matching (cited, never auto-applied)
    item_evidence.register(r, sys.modules[__name__])
    if not getattr(register, "_started", False):
        register._started = True
        threading.Thread(target=lambda: (time.sleep(2), recover()), daemon=True).start()
        threading.Thread(target=coordinator, daemon=True).start()
    return ["/api/workflow/projects", "/api/projects/{pid}/workflow", "/api/projects/{pid}/workflow/{analysis|layout|build|build/cancel|compose|approve|identify}"]
