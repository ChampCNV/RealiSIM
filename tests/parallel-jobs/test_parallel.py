"""Parallel job scheduler tests for the staged RealiSIM server.py (stub run functions; no DB, no Firebase, no daemon).
Run: /workspace/.venv/bin/python tests/test_parallel.py   (from features/parallel-jobs)"""
import asyncio, json, os, sys, threading, time
from pathlib import Path
HERE = Path(__file__).resolve().parent.parent
sys.path.insert(0, "/workspace/photo2grasp/backend")
sys.path.insert(0, str(HERE))                     # staged server.py / checkpoints.py shadow the live ones
import server as S                                 # noqa: E402
assert Path(S.__file__).resolve().parent == HERE, S.__file__
import checkpoints as CK                           # noqa: E402

RESULTS = {}
EVENTS = []
EV_LOCK = threading.Lock()
ACTIVE = {}
PEAK = dict(total=0, per_project={})
GATE = {"admission": None}


def ev(*a):
    with EV_LOCK:
        EVENTS.append((time.time(), *a))


class FakeCtx:
    def __init__(self, rid, pid, kind="asset", dur=0.6, fail=False):
        self.row = dict(id=rid, project_id=pid, kind=kind, status="queued", started_at=0, attempt=1)
        self.input, self.output, self.stages = {"dur": dur, "fail": fail}, {}, []
        self.cancelled, self.proc, self.uid = False, None, "u_" + pid
        self._rt, self.log, self.pushes = {}, [], []

    def runtime(self):
        return self._rt

    def write(self, t):
        self.log.append(t)

    def push(self, force=False, **kw):
        self.row.update(kw)
        self.pushes.append(kw)


def stub(ctx):
    rid, pid = ctx.row["id"], ctx.row["project_id"]
    with EV_LOCK:
        ACTIVE[rid] = pid
        PEAK["total"] = max(PEAK["total"], len(ACTIVE))
        same = sum(1 for p in ACTIVE.values() if p == pid)
        PEAK["per_project"][pid] = max(PEAK["per_project"].get(pid, 0), same)
    ev("start", rid, pid)
    try:
        time.sleep(ctx.input["dur"])
        if ctx.input.get("fail"):
            raise RuntimeError("stub failure")
        ctx.push(status="complete")
    finally:
        with EV_LOCK:
            ACTIVE.pop(rid, None)
        ev("end", rid, pid)


for name in ("run_asset", "run_rebuild", "run_scene", "run_export", "run_scene_capture", "run_detect_robot",
             "run_import_robot", "run_workcell", "run_workcell_export"):
    setattr(S, name, stub)
S._lease_taken = lambda ctx: False
S.store_run_log = lambda ctx: None
S.log = lambda *a: None
S.traceback.print_exc = lambda *a, **k: None
CK.admission = lambda kind=None: GATE["admission"]
CK.hold_state = lambda: ({"enabled": True, "reason": "test hold"} if GATE["admission"] else None)
S.database_readiness = lambda: dict(ok=True, checked_at=time.time())
S.STATE["recovery_complete"] = True
S.WORKER_CFG = dict(workers=3, kind_limits={})
for i in range(3):
    threading.Thread(target=S.worker, args=(i,), daemon=True).start()


def reset(stagger=0):
    with S.QCOND:
        assert not S.QUEUE and not S.STATE["running"], (S.QUEUE, S.STATE["running"])
        S.LIVE.clear(); S.HELD.clear()
    S.HEAVY_STAGGER_S = stagger
    S.ADMIT["last_heavy_start"] = 0
    S.WORKER_CFG["kind_limits"] = {}
    EVENTS.clear(); PEAK.update(total=0, per_project={})


def wait_idle(timeout=20):
    t = time.time() + timeout
    while time.time() < t:
        with S.QCOND:
            if not S.QUEUE and not S.STATE["running"]:
                return True
        time.sleep(0.05)
    return False


def starts():
    return [e for e in EVENTS if e[1] == "start"]


def check(name, cond, detail=None):
    RESULTS[name] = dict(ok=bool(cond), detail=detail)
    print(("PASS " if cond else "FAIL ") + name, "" if detail is None else detail)


# 1. three projects run concurrently
reset()
for p in ("pA", "pB", "pC"):
    S.enqueue(FakeCtx("r_" + p, p, dur=0.8))
time.sleep(0.4)
h = S.running_ids()
check("three_projects_concurrent_running_set", len(h) == 3 and S.STATE["busy"] and S.STATE["current"] in h, h)
wait_idle()
check("three_projects_concurrent_peak", PEAK["total"] == 3, PEAK)

# 2. same-project jobs serialize; another project proceeds alongside
reset()
S.enqueue(FakeCtx("a1", "pA", dur=0.6)); S.enqueue(FakeCtx("a2", "pA", dur=0.3)); S.enqueue(FakeCtx("b1", "pB", dur=0.3))
wait_idle()
order = [e[2] for e in starts()]
a1_end = next(e[0] for e in EVENTS if e[1] == "end" and e[2] == "a1")
a2_start = next(e[0] for e in EVENTS if e[1] == "start" and e[2] == "a2")
b1_start = next(e[0] for e in EVENTS if e[1] == "start" and e[2] == "b1")
check("same_project_serializes", PEAK["per_project"].get("pA") == 1 and a2_start >= a1_end and order.index("a1") < order.index("a2"),
      dict(order=order, peak=PEAK))
check("other_project_not_blocked_by_queue_head", b1_start < a1_end, round(a1_end - b1_start, 2))

# 3. hold stops new starts; waiting message once per run; release resumes
reset()
GATE["admission"] = dict(code="maintenance_hold", reason="test hold")
cs = [FakeCtx("h%d" % i, "pH%d" % i, dur=0.2) for i in range(2)]
for c in cs:
    S.enqueue(c)
time.sleep(1.2)
with S.QCOND:
    queued = list(S.QUEUE)
held_msgs = [sum(1 for t in c.log if "waiting: test hold" in t) for c in cs]
check("hold_blocks_new_starts", not starts() and queued == ["h0", "h1"] and not S.STATE["running"], queued)
check("held_message_once_per_run", held_msgs == [1, 1] and all(c._rt.get("held", {}).get("code") == "maintenance_hold" for c in cs), held_msgs)
GATE["admission"] = None
with S.QCOND:
    S.QCOND.notify_all()
check("hold_release_resumes", wait_idle() and len(starts()) == 2 and all("held" not in c._rt for c in cs))

# 4. health counts running/workers/queue and busy compat
reset()
GATE["admission"] = None
for i in range(4):
    S.enqueue(FakeCtx("x%d" % i, "pX%d" % i, dur=1.0))
time.sleep(0.4)


class Req:
    headers = {"X-Sender-Uid": "u_test"}
    match_info = {}
    query = {}


body = json.loads(asyncio.run(S.health(Req())).body)
check("health_counts", body["running"] == 3 and body["workers"] == 3 and body["queue"] == 1 and body["busy"] is True,
      {k: body[k] for k in ("running", "workers", "queue", "busy")})
idle_flag = S.release_idle()[0]
check("release_idle_false_while_running", idle_flag is False)
wait_idle()
body = json.loads(asyncio.run(S.health(Req())).body)
check("health_idle_after", body["running"] == 0 and body["queue"] == 0 and body["busy"] is False and S.STATE["current"] is None,
      {k: body[k] for k in ("running", "queue", "busy")})

# 5. enqueue dedupe (queued, running, live duplicate)
reset()
GATE["admission"] = dict(code="memory_pressure", reason="test")
d = FakeCtx("dup", "pD", dur=0.6)
first, second = S.enqueue(d), S.enqueue(d)
with S.QCOND:
    count = S.QUEUE.count("dup")
GATE["admission"] = None
with S.QCOND:
    S.QCOND.notify_all()
time.sleep(0.4)
running_dup = S.enqueue(FakeCtx("dup", "pD"))
check("enqueue_dedupe", first is True and second is False and count == 1 and running_dup is False, dict(count=count))
wait_idle()

# 6. delete guard: refuses while any job of the project runs; never touches the DB first
reset()
S.enqueue(FakeCtx("del1", "pDel", dur=1.0))
time.sleep(0.3)
S.project_for = lambda uid, pid, write=False: dict(owner_uid=uid, role="owner", id=pid)


class NoDb:
    def __getattr__(self, n):
        raise AssertionError("database touched: " + n)


real_db, S.db = S.db, NoDb()
Req.match_info = {"pid": "pDel"}
status = None
try:
    asyncio.run(S.delete_project(Req()))
except S.web.HTTPException as e:
    status = e.status
S.db = real_db
check("delete_guard_running_project", status == 409, status)
check("project_running_helper", S.project_running("pDel") == ["del1"] and S.project_running("other") == [])
wait_idle()

# 7. heavy-start stagger (no double admit) while light kinds start immediately
reset(stagger=0.8)
S.enqueue(FakeCtx("s1", "pS1", dur=1.5)); S.enqueue(FakeCtx("s2", "pS2", dur=0.2)); S.enqueue(FakeCtx("l1", "pL1", kind="detect_robot", dur=0.2))
wait_idle()
st = {e[2]: e[0] for e in starts()}
check("heavy_starts_staggered", st["s2"] - st["s1"] >= 0.75, round(st["s2"] - st["s1"], 2))
check("light_kind_not_staggered", st["l1"] - st["s1"] < 0.5, round(st["l1"] - st["s1"], 2))

# 8. failure in a run function: worker survives, slot freed, run marked failed
reset()
f = FakeCtx("bad", "pF", dur=0.05, fail=True)
S.enqueue(f)
wait_idle()
S.enqueue(FakeCtx("after", "pF", dur=0.05))
check("worker_survives_failure", wait_idle() and f.row["status"] == "failed" and any(e[2] == "after" for e in starts()), f.row["status"])

# 9. kind limits from config
reset()
S.WORKER_CFG["kind_limits"] = {"asset": 1}
for i in range(3):
    S.enqueue(FakeCtx("k%d" % i, "pK%d" % i, dur=0.3))
wait_idle()
check("kind_limit_respected", PEAK["total"] == 1, PEAK["total"])

# 10. config parsing and clamping
cfg_path = S.WORKERS_F
saved = cfg_path.read_text() if cfg_path.exists() else None
try:
    import tempfile
    S.WORKERS_F = Path(tempfile.mkdtemp()) / "workers.json"
    vals = {}
    for raw, want in ((None, 3), ({"workers": 9}, 6), ({"workers": 0}, 1), ({"workers": "x"}, 3), ({"workers": 4}, 4)):
        if raw is None:
            S.WORKERS_F.unlink(missing_ok=True)
        else:
            S.WORKERS_F.write_text(json.dumps(raw))
        vals[str(raw)] = S.worker_config()["workers"] == want
    os.environ["REALISIM_WORKERS"] = "2"
    vals["env"] = S.worker_config()["workers"] == 2
    os.environ.pop("REALISIM_WORKERS")
    check("worker_config_clamp", all(vals.values()), vals)
finally:
    S.WORKERS_F = cfg_path
assert (cfg_path.read_text() if cfg_path.exists() else None) == saved

ok = all(r["ok"] for r in RESULTS.values())
print("RESULT", json.dumps(dict(ok=ok, passed=sum(r["ok"] for r in RESULTS.values()), total=len(RESULTS))))
(HERE / "tests" / "results.json").write_text(json.dumps(RESULTS, indent=1, default=str))
sys.exit(0 if ok else 1)
