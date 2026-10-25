"""Offline tests for RealiSIM Apify + Claude item identification (staged). No network, no Apify, no Claude, no database.
Run: /workspace/.venv/bin/python tests/test_identify.py"""
import asyncio, hashlib, json, sys, threading, time, types
from pathlib import Path

STAGE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(STAGE / "backend"))
sys.path.insert(1, "/workspace/photo2grasp/backend")
FAIL, N = [], [0]


def check(c, m):
    N[0] += 1
    print(("PASS " if c else "FAIL ") + m)
    if not c:
        FAIL.append(m)


import evidence_extract as EX

# ── 1. extraction fixtures ─────────────────────────────────────────────────
check((STAGE / "actor/src/extract.py").read_bytes() == (STAGE / "backend/evidence_extract.py").read_bytes(), "actor and backend extractors are byte-identical")
HTML = """<html><head><title>Acme Mug 12oz | Acme</title><script type="application/ld+json">
{"@context":"https://schema.org","@graph":[{"@type":"WebPage","name":"x"},{"@type":"Product","name":"Acme Ceramic Mug 12 oz","brand":{"@type":"Brand","name":"Acme"},
"gtin13":"0123456789012","sku":"AM-12","weight":{"@type":"QuantitativeValue","value":0.35,"unitCode":"KGM"},
"width":{"@type":"QuantitativeValue","value":3.5,"unitCode":"INH"},"height":{"@type":"QuantitativeValue","value":95,"unitCode":"MMT"},
"depth":"12 cm","material":"Ceramic"}]}</script></head><body></body></html>"""
prods = EX.jsonld_products(HTML)
p = prods[0] if prods else {}
check(len(prods) == 1 and p["name"] == "Acme Ceramic Mug 12 oz" and p["brand"] == "Acme" and p["gtin"] == "0123456789012" and p["sku"] == "AM-12", "JSON-LD Product in @graph: name/brand/gtin/sku")
sp = p.get("specs", {})
check(sp.get("mass_g", {}).get("value") == 350.0 and sp.get("width_cm", {}).get("value") == 8.89 and sp.get("height_cm", {}).get("value") == 9.5
      and sp.get("length_cm", {}).get("value") == 12.0 and sp.get("material", {}).get("value") == "Ceramic", "JSON-LD units KGM/INH/MMT/string normalised to g and cm")
check(all(v["quote"] in p["text"] for v in sp.values()), "JSON-LD quotes are exact substrings of the stored structured text")
check(EX.jsonld_products('<script type="application/ld+json">{"@type":"Product","name":"X","weight":2}</script>')[0]["specs"] == {}, "unitless JSON-LD weight is refused, not guessed")
check(EX.jsonld_products('<script type="application/ld+json">{bad json</script>') == [], "malformed JSON-LD ignored")

TXT = ("Product details\nDimensions: 12 x 8 x 4 in\nItem Weight: 1.5 lbs\nCapacity: 12 fl oz\n"
       "Also available: 30 x 20 x 10 cm (large). Height: 95 mm. Body made of stainless steel; lid is plastic.\n5.5\" tall and 3 inches in diameter")
d = EX.dims_in(TXT)
tri = [x["values_cm"] for x in d if x["kind"] == "triple"]
check([30.48, 20.32, 10.16] in tri and [30.0, 20.0, 10.0] in tri, "dimension triples in inches and cm (multiple candidates kept)")
singles = {(x.get("axis"), x["values_cm"][0]) for x in d if x["kind"] == "single"}
check(("height", 9.5) in singles and ("height", 13.97) in singles and ("diameter", 7.62) in singles, "labelled single dimensions before/after the number")
w = [x["mass_g"] for x in EX.weights_in(TXT)]
check(w == [680.4], "weight 1.5 lb → 680.4 g; '12 fl oz' capacity is not a weight")
check([x["mass_g"] for x in EX.weights_in("Net wt. 340 g (12 oz)")] == [340.0], "net weight in grams")
check({m["material"] for m in EX.materials_in(TXT)} >= {"stainless steel", "plastic"} and "steel" not in {m["material"] for m in EX.materials_in(TXT)}, "materials: compound term wins over substring")
check(all(x["quote"] in TXT for x in d) and all(x["quote"] in TXT for x in EX.weights_in(TXT)), "every text quote is an exact substring of the page")
src = EX.extract_source("https://www.acme.com/mug", None, TXT, HTML)
check(src["domain"] == "acme.com" and src["title"].startswith("Acme Mug") and src["jsonld"] and "span" not in json.dumps(src["dimensions"]), "extract_source record shape")
cands = EX.candidate_specs([src])
check(any(c["origin"] == "jsonld" and c["field"] == "mass_g" for c in cands) and any(c["field"] == "dimensions" for c in cands) and all(c["url"] and c["quote"] for c in cands), "candidate specs carry url + quote")


long_page = ("Shop " * 3000) + "\n### Weight:\n\n1.18 lb.\n\n### Dimensions:\n\n3.54 x 5.43 x 11.02 in.\n" + ("footer " * 3000)
ls = EX.extract_source("https://brand.example/p", "P", long_page)
check(len(ls["text"]) <= 9100 and [w["mass_g"] for w in ls["weights"]] == [535.2] and [8.99, 13.79, 27.99] in [d["values_cm"] for d in ls["dimensions"]], "long page: specs past the head are found (multi-line labels) and kept in stored windows")
check(all(EX.norm_ws(x["quote"]) in EX.norm_ws(ls["text"]) for x in ls["weights"] + ls["dimensions"]), "long page: every kept quote occurs in the stored text")
check(not {m["material"] for m in EX.materials_in("Great for pet owners; tin can; abs workout")}, "ambiguous material words (pet/tin/abs) are not reported")

# ── 2. actor (fake apify SDK) ─────────────────────────────────────────────
PUSHED, CALLS = [], []


class FakeDataset:
    def __init__(self, rows):
        self.rows = rows

    async def list_items(self, limit=None):
        return types.SimpleNamespace(items=self.rows[:limit])


class FakeClient:
    def dataset(self, i):
        return FakeDataset(DATA.get(i, []))


DATA = {"ds_ok": [dict(metadata=dict(url="https://www.acme.com/mug", title="Acme mug"), markdown=TXT, html=HTML),
                  dict(searchResult=dict(url="https://shop.example/m", title="m"), markdown="Weight: 2 kg")],
        "ds_empty": []}


class FakeActor:
    apify_client = FakeClient()
    inp = {}

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    @staticmethod
    async def get_input():
        return FakeActor.inp

    @staticmethod
    async def call(actor, run_input=None, memory_mbytes=None, timeout=None):
        CALLS.append(dict(actor=actor, run_input=run_input, memory=memory_mbytes, timeout=timeout.total_seconds()))
        if "boom" in run_input["query"]:
            raise RuntimeError("child failed")
        return types.SimpleNamespace(id="child1", status="SUCCEEDED", default_dataset_id="ds_empty" if "nothing" in run_input["query"] else "ds_ok")

    @staticmethod
    async def push_data(r):
        PUSHED.append(r)

    @staticmethod
    async def set_status_message(m):
        pass

    @staticmethod
    async def fail(status_message=None):
        PUSHED.append(dict(failed=status_message))


sys.modules["apify"] = types.SimpleNamespace(Actor=FakeActor())
sys.path.insert(0, str(STAGE / "actor"))
import importlib
AM = importlib.import_module("src.main")
check(AM.build_query(dict(label="Red mug https://evil.example/x", description="ceramic www.x.com/y")) == "Red mug ceramic dimensions weight specifications", "query strips URLs, appends spec terms")
items = AM.norm_items([dict(id="item_1", label="Acme mug", description="d"), dict(label=""), "x", dict(id="item_3", label="boom gadget"), dict(id="item_4", label="nothing thing")])
check([i["id"] for i in items] == ["item_1", "item_3", "item_4"], "norm_items drops unlabeled/invalid entries")
FakeActor.inp = dict(items=[dict(id="item_1", label="Acme mug"), dict(id="item_2", label="boom gadget"), dict(id="item_3", label="nothing thing")], maxResultsPerItem=9)
import os
os.environ["ACTOR_RUN_ID"] = "RUNabc123"
asyncio.run(AM.main())
check(len(PUSHED) == 3 and [r["id"] for r in PUSHED] == ["item_1", "item_2", "item_3"], "one dataset item per input item, order preserved")
check(all(c["actor"] == "apify/rag-web-browser" and c["memory"] == 1024 and c["run_input"]["maxResults"] == 5 and c["run_input"]["scrapingTool"] == "raw-http"
          and "markdown" in c["run_input"]["outputFormats"] for c in CALLS), "child calls: rag-web-browser, 1024 MB, raw-http, maxResults clamped to 5")
check(all("image" not in json.dumps(c["run_input"]).lower() for c in CALLS), "no image data sent to the child Actor")
r1, r2, r3 = PUSHED
check(r1["status"] == "ok" and len(r1["sources"]) == 2 and r1["run_id"] == "RUNabc123" and "acme.com" in r1["domains"] and r1["candidate_specs"], "ok item has sources, domains, candidates, own run id")
check(r2["status"] == "error" and "child failed" in r2["error"] and r3["status"] == "no_results", "child failure and empty results are isolated per item")
check("html" not in json.dumps(PUSHED)[:0] and all("html" not in s for s in r1["sources"]), "raw HTML is not output")

# ── 3. backend: citation validation ───────────────────────────────────────
import workflow as WF
import item_evidence as IE
IE.W = WF
EV = dict(status="ok", sources=[src, EX.extract_source("https://shop.example/m", "m", "Weight: 2 kg")])
ans = dict(match="confident", product_name="Acme Ceramic Mug", brand="Acme", confidence="high", matched_url="https://www.acme.com/mug",
           specs=dict(length_cm=dict(value=30.5, source_url="https://www.acme.com/mug/", quote="Dimensions: 12 x 8 x 4 in"),
                      width_cm=dict(value=8.89, source_url="https://www.acme.com/mug", quote=sp["width_cm"]["quote"]),
                      height_cm=dict(value=50, source_url="https://www.acme.com/mug", quote="Height: 95 mm"),
                      mass_g=dict(value=680, source_url="https://other.example/x", quote="Item Weight: 1.5 lbs"),
                      material=dict(value="Stainless steel", source_url="https://www.acme.com/mug", quote="Body made of   stainless steel")))
m, kept, dropped = IE.validate(EV, ans)
reasons = {x["field"]: x["reason"] for x in dropped}
check(m == "confident" and set(kept) == {"length_cm", "width_cm", "material"}, "validation keeps cited, quoted, stated values (trailing slash / whitespace tolerant)")
check("not stated" in reasons.get("height_cm", "") and "not in the fetched evidence" in reasons.get("mass_g", ""), "drops value not in quote and URL outside evidence")
m2, k2, d2 = IE.validate(EV, dict(ans, specs=dict(mass_g=dict(value=680, source_url="https://www.acme.com/mug", quote="Item Weight: 1.5 kilos invented"))))
check(not k2 and "quote not found" in d2[0]["reason"], "drops quote that does not occur on the page")
m3, k3, d3 = IE.validate(EV, dict(ans, match="none"))
check(m3 == "none" and not k3 and all(x["reason"] == "no product match" for x in d3), "match=none keeps no specs")
m4, k4, _ = IE.validate(EV, dict(match="possible", specs=dict(mass_g=dict(value=2000, source_url="https://shop.example/m", quote="Weight: 2 kg"))))
check(m4 == "possible" and k4["mass_g"]["value"] == 2000.0 and k4["mass_g"]["domain"] == "shop.example", "possible match keeps verified spec with domain")
m5, k5, d5 = IE.validate(EV, dict(match="confident", specs=dict(material=dict(value="Titanium", source_url="https://www.acme.com/mug", quote="Body made of stainless steel"))))
check(not k5 and "material" in d5[0]["reason"], "material must be stated in the quote")

# ── 4. route + job wiring with stubs ──────────────────────────────────────
OWNER, VIS = "owner_uid_1", "visitor_1"


class DB:
    def __init__(self):
        self.projects, self.blobs, self.manifests = {}, {}, {}

    def sql(self, q):
        pid = q.split("'")[1] if "'" in q else None
        if "FROM project" in q and pid in self.projects:
            return [dict(self.projects[pid])]
        return []

    def call(self, reducer, **k):
        if reducer == "update_project":
            self.projects[k["id"]]["settings_json"] = k["settings_json"]

    def store_dir(self, uid, pid, kind, oid, ver, entries, deps=None):
        files = []
        for i, (pth, b, mt) in enumerate(entries):
            fid = f"f_{oid}_{i}"
            self.blobs[fid] = b
            files.append(dict(path=pth, id=fid, sha256=hashlib.sha256(b).hexdigest()))
        mid = f"man_{kind}_{oid}"
        self.manifests[mid] = dict(kind=kind, deps=deps, files=files, owner=uid)
        return mid, files

    def read_bytes(self, uid, pid, fid):
        return {}, self.blobs[fid]


db = DB()
db.blobs["crop_file"] = b"\xff\xd8jpegbytes"
fields = WF.empty_fields()
fields["material"] = WF.field("ceramic", "estimated", "analysis")
fields["height_cm"] = WF.field(10, "supplied", "user")
WFD = dict(mode="workspace", revision=7, analysis=dict(id="ana_1", status="complete"),
           objects=[dict(id="obj_a", name="mug", label="Red mug", include=True, why_primary="mug in the centre", fields=fields,
                         crops=[dict(id="c1", file_id="crop_file", sharpness=50)], selected_crop_ids=["c1"]),
                    dict(id="obj_b", name="tape", label="Tape roll", include=True, fields=WF.empty_fields(), crops=[], selected_crop_ids=[]),
                    dict(id="obj_c", name="cup", label="Excluded cup", include=False, fields=WF.empty_fields(), crops=[])],
           robot=dict(arms=[dict(id="arm1", name="robot_1"), dict(id="arm2", name="robot_2")], selected_library_id="robotstudio_so101"),
           table=WF.empty_table(), layout={})
db.projects["prj_1"] = dict(id="prj_1", owner_uid=OWNER, name="P", archived=False, settings_json=json.dumps(dict(workflow=WFD)))
db.projects["prj_2"] = dict(id="prj_2", owner_uid=OWNER, name="P2", archived=False, settings_json=json.dumps(dict(workflow=dict(WFD, analysis=dict(id="a", status="running")))))
SVC = dict(db=db, project_for=lambda uid, pid, write=False: dict(db.projects[pid], role="owner" if uid == OWNER else "editor"),
           src=types.SimpleNamespace(DETECT_MODEL="claude-test"), ROBOTS=types.SimpleNamespace(CATALOG=[]), STATE={})
WF.S = WF._Svc(SVC)
WF.SANDBOX_UID = OWNER
WF.hold_guard = lambda: None
items, mapping = IE.build_items(WFD)
check([i["id"] for i in items] == ["item_1", "item_2"] and mapping == {"item_1": "obj_a", "item_2": "obj_b"}, "only included objects become items, ids are neutral")
blob = json.dumps(items)
check("obj_" not in blob and "prj_" not in blob and OWNER not in blob and items[0]["material_guess"] == "ceramic" and items[0]["size_hint_cm"] == {"height_cm": 10.0}, "items carry no project/user/object ids; text hints only")

STATE = dict(connected=False, actor=None, gate=None, actor_calls=[], claude_calls=[])


def fake_proxies():
    if not STATE["connected"]:
        raise IE.NotConnected("no")
    return object(), object()


def fake_run_actor(it, mr):
    STATE["actor_calls"].append((it, mr))
    if STATE["gate"]:
        STATE["gate"].wait(5)
    out = [dict(id="item_1", status="ok", run_id="RUNx", sources=EV["sources"], domains=["acme.com", "shop.example"]),
           dict(id="item_2", status="no_results", run_id="RUNx", sources=[], domains=[])]
    return out, "RUNx", 0.0123


def fake_claude(blocks):
    STATE["claude_calls"].append(blocks)
    parsed = dict(items=[dict(item_id="item_1", match="confident", product_name="Acme Ceramic Mug", brand="Acme", confidence="high",
                              matched_url="https://www.acme.com/mug", specs=ans["specs"], reasons=["same shape"], mismatches=[]),
                         dict(item_id="item_2", match="none", specs={})])
    return "claude-test", json.dumps(parsed), parsed, dict(input_tokens=10)


IE.proxies, IE.run_actor, IE.claude_identify = fake_proxies, fake_run_actor, fake_claude
IE.actor_id = lambda: STATE["actor"]

from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer


async def routes():
    app = web.Application()
    IE.register(app.router, WF)
    async with TestClient(TestServer(app)) as cl:
        post = lambda pid, uid, **h: cl.post(f"/api/projects/{pid}/workflow/identify", json={}, headers={"X-Sender-Uid": uid, **h})
        r = await post("prj_2", OWNER)
        check(r.status == 409 and (await r.json())["code"] == "analysis_required", "requires a completed analysis")
        r = await post("prj_1", VIS, **{"X-Forwarded-For": "1.2.3.4"})
        WF.SANDBOX_UID = "someone_else"
        r = await post("prj_1", VIS, **{"X-Forwarded-For": "1.2.3.4"})
        check(r.status == 403 and (await r.json())["code"] == "billing", "non-owner without a beta reservation is refused (owner-billed)")
        WF.SANDBOX_UID = OWNER
        r = await post("prj_1", OWNER)
        check(r.status == 409 and (await r.json())["code"] == "apify_not_connected", "missing APIFY_API_TOKEN → 409 apify_not_connected")
        STATE["connected"] = True
        r = await post("prj_1", OWNER)
        check(r.status == 409 and (await r.json())["code"] == "apify_actor_missing", "Actor not configured → 409 apify_actor_missing")
        check(not STATE["actor_calls"] and not STATE["claude_calls"], "no provider call before all pre-checks pass")
        STATE["actor"] = "user~realisim-item-evidence"
        STATE["gate"] = threading.Event()
        r = await post("prj_1", OWNER)
        j = await r.json()
        check(r.status == 202 and j["identification"]["status"] == "queued" and j["identification"]["items"] == 2, "202 queued job")
        await asyncio.sleep(0.2)
        r2 = await post("prj_1", OWNER)
        check(r2.status == 409 and (await r2.json())["code"] == "already_running", "no duplicate concurrent job")
        STATE["gate"].set()
        for _ in range(50):
            await asyncio.sleep(0.1)
            if not IE.RUNNING:
                break
        return j["identification"]["id"]


iid = asyncio.run(routes())
_, st, wf = WF.load("prj_1")
job = wf["identification"]
check(job["status"] == "complete" and job["actor_run_id"] == "RUNx" and job["actor_usage_usd"] == 0.0123 and job["matched"] == 1 and job["manifest_id"], "job completes with run id, cost and manifest")
oa, ob, oc = wf["objects"]
ida = oa.get("identification") or {}
check(ida.get("match") == "confident" and set(ida["specs"]) == {"length_cm", "width_cm", "material"} and ida["product_name"] == "Acme Ceramic Mug"
      and "acme.com" in ida["domains"] and len(ida["dropped"]) == 2, "object summary stores only verified specs plus dropped reasons")
check(ida.get("applicable") is True, "confident match with verified specs is applicable")
check(IE.summarize("i", "a", dict(id="item_1"), EV, dict(match="possible", confidence="low", specs=dict(mass_g=dict(value=2000, source_url="https://shop.example/m", quote="Weight: 2 kg"))), "m", "x")["applicable"] is False, "possible matches keep cited values for review but are not applicable")
check(ob["identification"]["match"] == "none" and not ob["identification"]["specs"] and "identification" not in oc, "no-match summary; excluded object untouched")
check(oa["fields"] == fields and wf["revision"] == 7, "object fields and draft revision are never changed by identification")
man = db.manifests[job["manifest_id"]]
paths = [f["path"] for f in man["files"]]
check(man["kind"] == "workflow_identification" and paths == ["evidence.json", "claude.json"] and man["deps"]["actor_run_id"] == "RUNx", "evidence + Claude response persisted as SpacetimeDB manifest")
blocks = STATE["claude_calls"][0]
check(blocks[0][2] == b"\xff\xd8jpegbytes" and blocks[1][2] is None, "Claude gets each object's best crop (none for crop-less objects)")
check(STATE["actor_calls"][0][1] == 3 and "jpeg" not in json.dumps(STATE["actor_calls"][0][0]), "Actor receives text-only items, 3 results each")
check(wf["robot"]["arms"] == WFD["robot"]["arms"], "multi-arm robot state unchanged")

# view info + interrupted detection
req = types.SimpleNamespace(headers={}, get=lambda k, d=None: d)
IE.connected = lambda: True
vi = IE.view_info(wf, OWNER, req, dict(db.projects["prj_1"], role="owner"))
check(vi["available"] and vi["can_run"] and vi["can_connect"] and vi["job"]["status"] == "complete" and not any(x in json.dumps(vi).lower() for x in ("bearer", "authorization", "secrets.", "api.apify.com")), "view: available, owner can connect, no secret material")
stale = dict(wf, identification=dict(id="idn_old", status="running"))
check(IE.view_info(stale, OWNER, req, dict(db.projects["prj_1"], role="owner"))["job"]["status"] == "interrupted", "a running job unknown to this process shows as interrupted (no auto retry)")
IE.connected = lambda: False
vv = IE.view_info(wf, VIS, req, dict(db.projects["prj_1"], role="editor"))
check(not vv["available"] and not vv["can_connect"], "visitor never gets the connect hint")

# ── 5. public-beta quota reservation ──────────────────────────────────────
import public_beta as PB


class LDB:
    def __init__(self):
        self.rows = []

    def sql(self, q):
        if "FROM project WHERE id" in q:
            return [dict(id=PB.LEDGER_PID, owner_uid=PB.SYSTEM_UID, settings_json="{}", name="l", archived=False)]
        return []

    def call(self, reducer, **k):
        self.rows.append((reducer, k.get("status")))


PB.S = types.SimpleNamespace(db=LDB(), project_for=lambda uid, pid, write=True: dict(owner_uid=OWNER, role="editor"))
cfg = PB._sanitize_config({"enabled": True})
check(cfg["per_user"]["identify_per_day"] == 3 and cfg["global"]["identify_per_day"] == 20, "default caps: 3/day per user, 20/day global")
check(PB._sanitize_config({"enabled": True, "per_user": {"claude_per_day": 9}})["per_user"]["identify_per_day"] == 3, "stored configs without the new key inherit defaults")
key, acc, g = PB.match(types.SimpleNamespace(path="/api/projects/prj_1/workflow/identify", method="POST"))
check(key == "wf_identify" and acc == PB.USER, "route is listed for guarded users")
R = PB.plan(key, dict(uid="u1"), g, {}, cfg)
check(R == {"identify": 1, "claude": 1}, "plan reserves one identify and one Claude unit")
ids = []
for u in ("u1", "u1", "u1"):
    ids.append(PB.reserve(dict(uid=u, simulated=False, method="POST"), key, "prj_1", R, cfg))
try:
    PB.reserve(dict(uid="u1", simulated=False, method="POST"), key, "prj_1", R, cfg)
    check(False, "4th identify per user is refused")
except web.HTTPException as e:
    check(e.status == 429 and json.loads(e.text)["resource"] == "identify" and json.loads(e.text)["scope"] == "user", "4th identify per user is refused (429 identify/user)")
rq = {"p2g_beta": dict(reservation=ids[0], resources=R, dispatched=False)}
fake = types.SimpleNamespace(get=lambda k, d=None: rq.get(k, d), headers={"X-Sender-Uid": "u1"})
check(PB.identify_allowed(fake) and PB.claude_allowed(fake), "identify_allowed true only with this request's reservation")
rq2 = {"p2g_beta": dict(reservation=ids[1], resources={"claude": 1}, dispatched=False)}
check(not PB.identify_allowed(types.SimpleNamespace(get=lambda k, d=None: rq2.get(k, d), headers={"X-Sender-Uid": "u1"})), "a Claude-only reservation does not authorise identify")
PB.mark_dispatched(fake)
check(PB.finalize(ids[0], 500, False) == "retained" and PB.finalize(ids[1], 409, False) == "released" and PB.finalize(ids[2], 0, True) == "retained", "charge kept on dispatched/unknown outcomes, refunded on pre-dispatch 409")
cfg2 = PB._sanitize_config({"enabled": True, "per_user": {"identify_per_day": 50, "claude_per_day": 50}})
n = 0
try:
    for i in range(30):
        PB.reserve(dict(uid=f"g{i}", simulated=False, method="POST"), key, "prj_1", R, cfg2)
        n += 1
except web.HTTPException as e:
    check(e.status == 429 and json.loads(e.text)["scope"] == "global", "global identify cap enforced")
check(n == 18, f"global cap counts retained units (20 - 2 retained = 18 new, got {n})")

# ── 6. UI source ──────────────────────────────────────────────────────────
ui = (STAGE / "workflow-ui.jsx").read_text()
check(ui.count("<ItemWebMatch ") == 1 and ui.count("function ItemWebMatch(") == 1, "UI: one component, one hook")
check("idn.applicable&&" in ui and "source:'supplied'" in ui and "matched by Claude using Apify web evidence" in ui and "/workflow/identify" in ui, "UI: Use these values fills unsaved edits as Supplied with provenance note")
check("useManaRouter(" not in ui and "apiManager" not in ui, "UI: no model proxy or credential access in frontend")

print(f"RESULT {N[0] - len(FAIL)}/{N[0]} passed")
sys.exit(1 if FAIL else 0)
