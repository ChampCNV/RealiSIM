"""Focused tests for the RealiSIM visitor ledger and Users metric (staged copy; no SpacetimeDB, Firebase or network)."""
import asyncio, json, os, sys, tempfile
from pathlib import Path
HERE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(HERE / "backend"))
import public_beta as PB
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

OWNER = "OwnerUid0000000000000000000a"
results = []
def check(name, cond, detail=""):
    results.append(dict(name=name, ok=bool(cond), detail=str(detail)[:300]))
    print(("PASS " if cond else "FAIL ") + name, detail if not cond else "")

class FakeDB:
    def __init__(self, projects): self.projects = projects
    def sql(self, q):
        if q.startswith("SELECT id, owner_uid, archived, settings_json, created_at FROM project"): return self.projects
        if "FROM upload" in q: return [dict(project_id=p["id"], status="ready") for p in self.projects]
        return []
class FakeS:
    def __init__(self, projects): self.db = FakeDB(projects)
    def project_for(self, *a): return None

def fresh_ledger():
    d = tempfile.mkdtemp(prefix="vis-test-")
    PB.VISITORS_FILE = Path(d) / "data" / "visitors.json"       # also proves the dir is created
    PB._VIS["rows"] = None
    return PB.VISITORS_FILE

def setup(projects, cfg_extra=None):
    PB.S = FakeS(projects); PB.OWNER_UID = OWNER
    cfg = PB._sanitize_config({"enabled": True, **(cfg_extra or {})})
    PB.config = lambda fresh=False: cfg
    PB._METRICS.update(value=None, at=0)

def proj(pid, owner, **st):
    return dict(id=pid, owner_uid=owner, archived=False, settings_json=json.dumps(st), created_at=0)

# 1. hashing / file / throttling (pure functions)
f = fresh_ledger()
t0 = 1_800_000_000.0
r1 = PB.record_visit("guestA_raw_uid_123", opens=1, now=t0)
r2 = PB.record_visit("guestA_raw_uid_123", opens=1, now=t0 + 60)             # within 30 min -> ignored
r3 = PB.record_visit("guestA_raw_uid_123", opens=1, now=t0 + 31 * 60)        # after 30 min -> counted
check("dir created + atomic file", f.exists() and not f.with_name(f.name + ".tmp").exists())
raw = f.read_text()
check("no raw uid in ledger file", "guestA_raw_uid_123" not in raw and PB.vid("guestA_raw_uid_123") in raw)
row = json.loads(raw)["visitors"][PB.vid("guestA_raw_uid_123")]
check("open throttle 30 min", (r1["opens"], r2["opens"], r3["opens"]) == (1, 0, 1) and row["opens"] == 2, (r1, r2, r3, row))
check("first/last seen", row["first_seen"] == int(t0 * 1000) and row["last_seen"] == int((t0 + 31 * 60) * 1000))
c1 = PB.record_visit("guestA_raw_uid_123", clicks=900, now=t0 + 32 * 60)
check("clicks clamped per request", c1["clicks"] == 500, c1)
tot = 500
for i in range(45):
    tot += PB.record_visit("guestA_raw_uid_123", clicks=500, now=t0 + 33 * 60)["clicks"]
check("daily click cap 20000", tot == 20000, tot)
nd = PB.record_visit("guestA_raw_uid_123", clicks=10, now=t0 + 86400 * 2)
check("cap resets next UTC day", nd["clicks"] == 10, nd)
PB._VIS["rows"] = None                                       # reload from disk
check("ledger reload persists", PB.visitor_rows()[PB.vid("guestA_raw_uid_123")]["clicks"] == 20010)

# 2. metrics: dedupe owner+visitor, exclusion, clicks not users
f = fresh_ledger()
setup([proj("p1", OWNER), proj("p2", "builderB"), proj("p3", "qa_tester"), proj("p4", "excludedC"), proj("p5", PB.SYSTEM_UID)],
      {"exclude_uids": ["excludedC", "excludedD"], "team_uids": ["teamT"]})
PB.record_visit(OWNER, opens=1, clicks=3)           # owner is also a project owner -> deduped
PB.record_visit("builderB", opens=1, clicks=7)      # project owner + visitor -> deduped
PB.record_visit("guestG1", opens=1)                 # new anonymous guest
PB.record_visit("guestG2", clicks=40)               # clicks only: still one identity, clicks don't add users
PB.record_visit("teamT", opens=1)
PB.record_visit("qa_someone", opens=1, clicks=99)   # QA prefix -> excluded
PB.record_visit("excludedD", opens=1, clicks=99)    # configured exclusion -> excluded
PB.record_visit(PB.SYSTEM_UID, opens=1)             # system -> excluded (handler also rejects it)
m = PB.compute_metrics()
u = m["users"]["distinct"]
check("users = distinct owners ∪ visitors (deduped)", u["all"] == 5, u)          # OWNER, builderB, guestG1, guestG2, teamT
check("segments", u == {"all": 5, "external": 3, "owner_team": 2}, u)
check("visitors distinct excludes QA/excluded/system", m["visitors"]["distinct"]["all"] == 5 and m["excluded"]["visitor_identities"] == 3, (m["visitors"], m["excluded"]))
check("interactions = sum clicks (excl. QA)", m["interactions"]["clicks"]["all"] == 50, m["interactions"])
check("builders unchanged", m["builders"]["with_any_project"]["all"] == 2, m["builders"])
check("UI path users.distinct.all numeric", isinstance(m["users"]["distinct"]["all"], int))
PB.record_visit("guestG2", clicks=500)
m2 = PB.compute_metrics()
check("clicks never count as users", m2["users"]["distinct"]["all"] == 5 and m2["interactions"]["clicks"]["all"] == 550)
check("no raw uids in public metrics", not any(x in json.dumps(m2) for x in ("guestG1", "builderB", OWNER, "qa_someone", "excludedD")))

# 3. HTTP: auth reject, validation, guard path for an external visitor
async def http():
    fresh_ledger()
    app = web.Application(middlewares=[PB.guard])
    app.router.add_post("/api/visit", PB.visit)
    async with TestClient(TestServer(app)) as c:
        ext = {"X-Forwarded-For": "1.2.3.4", "X-Forwarded-Host": "h", "X-Forwarded-Prefix": "/p"}
        r = await c.post("/api/visit", json={"opens": 1}, headers=ext)
        check("reject missing X-Sender-Uid (external)", r.status == 401, r.status)
        r = await c.post("/api/visit", json={"opens": 1})
        check("reject missing X-Sender-Uid (loopback)", r.status == 401, r.status)
        r = await c.post("/api/visit", json={"opens": 1}, headers={**ext, "X-Sender-Uid": "bad uid!"})
        check("reject invalid uid", r.status in (401, 403), r.status)
        r = await c.post("/api/visit", json={"opens": 1, "clicks": 5}, headers={**ext, "X-Sender-Uid": "anonGuest1"})
        j = await r.json()
        check("external visitor accepted (no beta caps)", r.status == 200 and j["opens"] == 1 and j["clicks"] == 5, (r.status, j))
        r = await c.post("/api/visit", json={"opens": 1}, headers={**ext, "X-Sender-Uid": "anonGuest1"})
        check("open throttled over HTTP", (await r.json())["opens"] == 0)
        r = await c.post("/api/visit", json={"clicks": -1}, headers={**ext, "X-Sender-Uid": "anonGuest1"})
        check("reject negative clicks", r.status == 422, r.status)
        r = await c.post("/api/visit", json={"opens": 2}, headers={**ext, "X-Sender-Uid": "anonGuest1"})
        j = await r.json()
        check("opens clamped to 1 (or throttled)", r.status == 200 and j["opens"] in (0, 1), (r.status, j))
        r = await c.post("/api/visit", json={"clicks": True}, headers={**ext, "X-Sender-Uid": "anonGuest1"})
        check("reject boolean clicks", r.status == 422, r.status)
        r = await c.post("/api/visit", data="nope", headers={**ext, "X-Sender-Uid": "anonGuest1"})
        check("reject non-JSON", r.status == 400, r.status)
        r = await c.post("/api/visit", json={"opens": 1}, headers={**ext, "X-Sender-Uid": PB.SYSTEM_UID})
        check("reject system uid", r.status in (401, 403), r.status)
    check("HTTP wrote no raw uid", "anonGuest1" not in PB.VISITORS_FILE.read_text())
setup([], {"enabled": False})   # beta closed must not block analytics
asyncio.run(http())

out = dict(passed=sum(r["ok"] for r in results), failed=sum(not r["ok"] for r in results), results=results)
print(json.dumps({k: out[k] for k in ("passed", "failed")}))
Path(os.environ.get("VISIT_TEST_OUT", "/tmp/test_visits.json")).write_text(json.dumps(out, indent=2))
sys.exit(1 if out["failed"] else 0)
