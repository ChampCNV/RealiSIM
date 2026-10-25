"""Staged live end-to-end check: real POST /workflow/identify route → real private Actor → real Claude → citation
validation → in-memory SpacetimeDB stub. No live service, no user project, no images (crop-less QA objects).
Run once with REALISIM_ITEM_EVIDENCE_CFG pointing at the staged actor config. Prints a JSON summary."""
import asyncio, hashlib, json, sys, time, types
from pathlib import Path
STAGE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(STAGE / "backend")); sys.path.insert(1, "/workspace/photo2grasp/backend")
import workflow as WF, item_evidence as IE
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

class DB:
    def __init__(self): self.p, self.m = {}, {}
    def sql(self, q):
        pid = q.split("'")[1]
        return [dict(self.p[pid])] if "FROM project" in q and pid in self.p else []
    def call(self, reducer, **k):
        if reducer == "update_project": self.p[k["id"]]["settings_json"] = k["settings_json"]
    def store_dir(self, uid, pid, kind, oid, ver, entries, deps=None):
        files = [dict(path=p, id=f"f{i}", sha256=hashlib.sha256(b).hexdigest(), size=len(b)) for i, (p, b, mt) in enumerate(entries)]
        self.m[f"man_{oid}"] = dict(kind=kind, deps=deps, files=files); return f"man_{oid}", files
    def read_bytes(self, *a): raise KeyError("no crops in this QA fixture")

OWNER = "qa_identify_owner"
db = DB()
def obj(i, label, why, mat=""):
    f = WF.empty_fields(); f["material"] = WF.field(mat, "estimated", "analysis") if mat else WF.field()
    return dict(id=f"obj_{i}", name=f"o{i}", label=label, include=True, why_primary=why, fields=f, crops=[], selected_crop_ids=[])
wfd = dict(mode="workspace", revision=3, analysis=dict(id="ana_qa", status="complete"), robot=dict(arms=[]), table=WF.empty_table(), layout={},
           objects=[obj(1, "Stanley Quencher H2.0 FlowState tumbler 40 oz", "insulated stainless tumbler with handle and straw lid", "stainless steel"),
                    obj(2, "small grey block", "plain matte rectangular object")])
db.p["prj_qa_identify"] = dict(id="prj_qa_identify", owner_uid=OWNER, name="QA identify (staged, in-memory)", archived=False, settings_json=json.dumps(dict(workflow=wfd)))
WF.S = WF._Svc(dict(db=db, project_for=lambda uid, pid, write=False: dict(db.p[pid], role="owner"), src=types.SimpleNamespace(), ROBOTS=types.SimpleNamespace(CATALOG=[]), STATE={}))
WF.SANDBOX_UID = OWNER; WF.hold_guard = lambda: None

async def main():
    app = web.Application(); IE.register(app.router, WF)
    async with TestClient(TestServer(app)) as cl:
        r = await cl.post("/api/projects/prj_qa_identify/workflow/identify", json={"max_results": 3}, headers={"X-Sender-Uid": OWNER})
        first = (r.status, await r.json())
        t0 = time.time()
        while IE.RUNNING and time.time() - t0 < 400:
            await asyncio.sleep(2)
        return first, round(time.time() - t0)

first, secs = asyncio.run(main())
_, st, wf = WF.load("prj_qa_identify")
out = dict(post=first, seconds=secs, job={k: v for k, v in (wf.get("identification") or {}).items()},
           objects=[dict(label=o["label"], identification=o.get("identification"), fields_unchanged=o["fields"] == obj(int(o["id"][-1]), "", "", "stainless steel" if o["id"] == "obj_1" else "")["fields"] or None) for o in wf["objects"]],
           manifest={k: dict(kind=v["kind"], files=[f["path"] for f in v["files"]], deps=v["deps"]) for k, v in db.m.items()}, revision=wf["revision"])
print("E2E " + json.dumps(out, default=str))
