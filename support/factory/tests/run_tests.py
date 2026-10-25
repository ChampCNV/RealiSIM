"""RealiSIM Factory worker tests. Uses the real droid-sdk 0.5.0 and pinned droid CLI 0.249.0; no Factory key is
available, so live model turns are expected to fail as connection_required (asserted, never mocked as success)."""
import asyncio, json, os, socket, subprocess, sys, tempfile, time, secrets
sys.path.insert(0, "/workspace/photo2grasp/backend")
import droid_support as ds
from droid_sdk import (Autonomy, InteractionHandlers, Runtime, Session, SessionConfig, PermissionRequest, PermissionOption,
                       ToolConfirmationOutcome, ToolUseBlock)
from droid_sdk.permissions import McpToolAction, ExecuteAction, CreateFile, EditAction
from droid_sdk.mcp import create_sdk_mcp_server

PY = "/workspace/photo2grasp/factory/.venv/bin/python"
FAKE = "/workspace/photo2grasp/factory/tests/fake_support_api.py"
WORKER = "/workspace/photo2grasp/support-worker.py"
RESULTS = []

def check(name, ok, **ev):
    RESULTS.append({"test": name, "pass": bool(ok), **ev}); print(("PASS " if ok else "FAIL ") + name, json.dumps(ev, default=str)[:600], flush=True)

def free_port():
    s = socket.socket(); s.bind(("127.0.0.1", 0)); p = s.getsockname()[1]; s.close(); return p

class Fake:
    def __init__(self, scenario="normal"):
        self.port, self.token = free_port(), secrets.token_hex(16)
        self.log = tempfile.mktemp(prefix="fakeapi-", suffix=".jsonl")
        self.p = subprocess.Popen([PY, FAKE, "--port", str(self.port), "--log", self.log, "--token", self.token, "--scenario", scenario])
        for _ in range(50):
            try: socket.create_connection(("127.0.0.1", self.port), 0.2).close(); break
            except OSError: time.sleep(0.1)
        self.base = f"http://127.0.0.1:{self.port}/internal/support"
        self.token_file = tempfile.mktemp(prefix="fake-worker-token-"); open(self.token_file, "w").write(self.token); os.chmod(self.token_file, 0o600)
    def api(self): return ds.WorkerAPI(base=self.base, token=self.token)
    def entries(self):
        return [json.loads(l) for l in open(self.log)] if os.path.exists(self.log) else []
    def close(self): self.p.terminate(); self.p.wait(5)

def mk(cls, **kw):
    tu = ToolUseBlock(id="tu1", name=kw.pop("_name", "x"), input={})
    return cls(tool_use=tu, **kw)

def preq(actions):
    return PermissionRequest(actions=actions, options=[PermissionOption(label="Proceed", value=ToolConfirmationOutcome.PROCEED_ONCE),
                                                        PermissionOption(label="Always", value=ToolConfirmationOutcome.PROCEED_ALWAYS),
                                                        PermissionOption(label="Cancel", value=ToolConfirmationOutcome.CANCEL)])

def t_permissions():
    st = ds.JobState(None, {"id": "j", "lease_token": "l"})
    h = ds.make_permission_handler(st)
    cases = {
        "own_mcp_tool": (mk(McpToolAction, tool_name="realisim___search_help", impact_level="low", server_name="realisim", actual_tool_name="search_help"), "proceed_once"),
        "other_mcp_server": (mk(McpToolAction, tool_name="evil___x", impact_level="low", server_name="evil", actual_tool_name="search_help"), "cancel"),
        "unknown_tool_same_server": (mk(McpToolAction, tool_name="realisim___apply", impact_level="high", server_name="realisim", actual_tool_name="apply"), "cancel"),
        "execute": (mk(ExecuteAction, full_command="rm -rf /", command="rm"), "cancel"),
        "create_file": (mk(CreateFile, file_path="/tmp/x", file_name="x", content="y"), "cancel"),
        "edit": (mk(EditAction, file_path="/tmp/x", file_name="x"), "cancel"),
    }
    got = {k: h(preq([a])).selected_option.value for k, (a, _) in cases.items()}
    mixed = h(preq([cases["own_mcp_tool"][0], cases["execute"][0]])).selected_option.value
    ok = all(got[k] == v for k, (_, v) in cases.items()) and mixed == "cancel"
    check("permission_handler_deny_all_except_own_mcp_tools", ok, outcomes=got, mixed_batch=mixed, denied=st.denied)

async def t_sdk_tool_allowlist():
    """Actual Droid session with the production config: only the four MCP tools are allowed."""
    st = ds.JobState(None, {"id": "j", "lease_token": "l"})
    server = create_sdk_mcp_server(ds.SERVER_NAME, ds.build_tools(st))
    home = tempfile.mkdtemp(prefix="realisim-droid-test-"); os.makedirs(home + "/work")
    cfg = SessionConfig(autonomy=Autonomy.OFF, mcp_servers=[server], restrict_tools=set(ds.TOOL_IDS), disabled_tools=set(ds.NATIVE_TOOLS),
                        disable_builtin_skills=True, system_prompt=ds.SYSTEM_PROMPT)
    rt = Runtime(executable=str(ds.DROID_WRAPPER), env={"HOME": home, "TMPDIR": home})
    try:
        async with Session(cwd=home + "/work", config=cfg, runtime=rt, interactions=InteractionHandlers(on_permission=ds.make_permission_handler(st))) as s:
            tools = await s.list_tools()
            allowed = sorted(t.id for t in tools if t.allowed)
            listed = len(tools)
            mcp = await s.list_mcp_servers()
            settings = s.settings
            sid = s.id
        ok = allowed == sorted(list(ds.TOOL_IDS) + [ds.SCHEMA_LOADER])
        check("sdk_session_tool_allowlist", ok, note="ToolSearch (deferred-schema loader) remains allowed by the CLI; all native action/file/shell/web/automation/connector tools are not allowed", allowed=allowed, tools_listed=listed, mcp=[(x.name, str(x.status)) for x in mcp.servers],
              session_id=sid, model=settings.model, autonomy=str(settings.autonomy), restrict=sorted(settings.restrict_tools or []))
    finally:
        import shutil; ds._reap(home); shutil.rmtree(home, ignore_errors=True)

async def t_mcp_auth_and_tools(fake):
    import httpx
    from mcp import ClientSession
    from mcp.client.streamable_http import streamablehttp_client
    api = fake.api()
    api.presence("test-worker", True)
    job = api.claim("test-worker")
    st = ds.JobState(api, job); st.max_calls = 9
    server = create_sdk_mcp_server(ds.SERVER_NAME, ds.build_tools(st))
    cfg = await server.start()
    try:
        url = cfg.url; hdrs = {h.name: h.value for h in cfg.headers}
        async with httpx.AsyncClient(timeout=10) as hc:
            r = await hc.post(url, json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"}, headers={"Accept": "application/json, text/event-stream"})
            bad = await hc.post(url, json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"}, headers={"Accept": "application/json, text/event-stream", "Authorization": "Bearer wrong"})
        check("mcp_server_rejects_unauthenticated", r.status_code == 401 and bad.status_code == 401, no_token=r.status_code, wrong_token=bad.status_code, loopback=url.startswith("http://127.0.0.1"))
        async with streamablehttp_client(url, headers=hdrs) as (rd, wr, _):
            async with ClientSession(rd, wr) as cs:
                await cs.initialize()
                names = sorted(t.name for t in (await cs.list_tools()).tools)
                check("mcp_server_exposes_exactly_four_tools", names == sorted(ds.TOOL_NAMES), names=names)
                s1 = await cs.call_tool("search_help", {"query": "table_not_found"})
                txt = s1.content[0].text
                check("tool_output_wrapped_untrusted_and_cited", "UNTRUSTED DATA" in txt and "help_doc:README.md#workcell-capture@9fa727d54c1d" in st.citations
                      and "resolution:sres_1" in st.citations, citations=list(st.citations))
                g = await cs.call_tool("get_record", {"canonical_id": "pipeline_run:run_test_1"})
                gt = g.content[0].text
                check("tool_output_redacted", "/workspace" not in gt and "abcdefghijkl" not in gt, sample=gt[-160:])
                g404 = await cs.call_tool("get_record", {"canonical_id": "pipeline_run:run_other_user"})
                check("out_of_scope_record_is_error_not_citation", g404.isError and "pipeline_run:run_other_user" not in st.citations)
                inj = await cs.call_tool("get_record", {"canonical_id": "../../etc/passwd; rm"})
                check("invalid_record_id_rejected_locally", inj.isError)
                ps = await cs.call_tool("read_project_state", {})
                check("project_state_is_grounding_not_citation", not ps.isError and "workflow:prj_test#build" not in st.citations)
                sf = await cs.call_tool("search_help", {"query": "layout", "kinds": ["help_doc"], "stages": ["layout"], "error_classes": ["bad value!"]})
                bad_prop = await cs.call_tool("propose_correction", {"kind": "table", "values": {"length_m": 1.5}, "sources": {"length_m": "estimated"}, "rationale": "looks longer"})
                good_prop = await cs.call_tool("propose_correction", {"kind": "table", "values": {"width_m": 0.8}, "sources": {"width_m": "user_stated"}, "rationale": "user said 80 cm"})
                badkind = await cs.call_tool("propose_correction", {"kind": "shell", "values": {"x": 1}, "sources": {}, "rationale": "x"})
                check("proposal_validation_is_backend_owned", "accepted\":false" in bad_prop.content[0].text and "proposal_ref" in good_prop.content[0].text and badkind.isError and list(st.proposals) == ["p1"],
                      estimated_over_supplied=bad_prop.content[0].text[-200:], refs=list(st.proposals))
                calls_before = len(st.calls); st.max_calls = calls_before      # budget now exactly consumed
                over = await cs.call_tool("search_help", {"query": "again"})
                check("tool_budget_enforced", over.isError and "budget" in over.content[0].text.lower(), calls=calls_before, max=st.max_calls)
    finally:
        await server.close()
    tool_posts = [e for e in fake.entries() if "/tools/" in e["path"]]
    filt = [e["body"].get("filters") for e in tool_posts if e["path"].endswith("/search_help") and e["body"].get("filters")]
    check("tool_calls_use_backend_routes_lease_only_no_uid", tool_posts and all(e["token_ok"] and not e["forwarded"] and not e["sender_uid"] and "lease_token" in e["body"]
          and not ({"uid", "user_id", "owner_uid", "project_id"} & set(e["body"])) for e in tool_posts), posts=len(tool_posts),
          routes=sorted({e["path"].split("/tools/")[1] for e in tool_posts}), search_filters=filt)

def run_worker(fake, *extra, key=None, timeout=300):
    env = {"PATH": "/usr/bin:/bin", "LANG": "C.UTF-8", "HOME": "/tmp", "SOME_OTHER_SECRET": "should-not-leak-xyz"}
    if key: env["FACTORY_API_KEY"] = key
    return subprocess.run([PY, WORKER, "--api-base", fake.base, "--token-file", fake.token_file, *extra], env=env, capture_output=True, text=True, timeout=timeout)

def t_worker_scenarios():
    fake = Fake()
    r = run_worker(fake, "--presence-only")
    pr = [e for e in fake.entries() if e["path"] == "/presence"]
    check("no_key_presence_reports_missing_key_without_claiming", r.returncode == 0 and pr and pr[-1]["body"]["factory_key_present"] is False
          and pr[-1]["body"]["sdk"] == {"name": "droid-sdk", "version": "0.5.0"} and pr[-1]["body"]["cli"] == {"version": "0.249.0"}
          and not any(e["path"] == "/claim" for e in fake.entries()), stdout=r.stdout.strip()[-300:])
    r = run_worker(fake)
    check("no_key_normal_run_does_not_claim", r.returncode == 0 and not any(e["path"] == "/claim" for e in fake.entries()))
    t0 = time.time()
    r = run_worker(fake, "--allow-unauthenticated-probe", "--max-jobs", "1")
    ents = fake.entries(); claims = [e for e in ents if e["path"] == "/claim"]; fails = [e for e in ents if e["path"].endswith("/fail")]
    check("probe_claim_refused_409_when_backend_sees_no_key", claims and not fails and "claim_refused" in r.stdout, stdout=r.stdout.strip()[-200:])
    fake.close()

    # backend believes a key is present (presence true) but the SDK has none: proves the real SDK path fails cleanly
    fake = Fake()
    api = fake.api(); api.presence("probe", True)
    job = api.claim("probe")
    import asyncio as _a, concurrent.futures as _cf
    with _cf.ThreadPoolExecutor(1) as ex:
        kind, det = ex.submit(_a.run, ds.investigate(api, job, None, worker_id="probe")).result()
    ents = fake.entries(); fails = [e for e in ents if e["path"].endswith("/fail")]
    fb = fails[-1]["body"] if fails else {}
    check("real_sdk_without_key_fails_cleanly_missing_key", kind == "failed" and fb.get("error_code") == "missing_key" and fb.get("uncertain_charge") is False
          and fb.get("idempotency_key") and fb.get("runtime", {}).get("session_id") and det["provider"].get("home_removed") is True
          and not any(e["path"].endswith("/finish") for e in ents),
          fail_body={k: fb.get(k) for k in ("error_code", "message", "uncertain_charge")}, runtime=fb.get("runtime"), wall_s=round(time.time() - t0, 1))
    r2 = api.fail(job["id"], job["lease_token"], "missing_key", "repeat", {}, False)
    check("fail_is_idempotent_with_same_key", r2.get("idempotent") is True)
    fake.close()

    fake = Fake()
    bogus = "fk-INVALIDtestkey" + secrets.token_hex(12)
    r = run_worker(fake, "--max-jobs", "1", key=bogus)
    ents = fake.entries(); fails = [e for e in ents if e["path"].endswith("/fail")]
    blob = json.dumps(ents) + r.stdout + r.stderr
    check("real_sdk_invalid_key_fails_auth_and_key_never_leaks", bool(fails) and fails[-1]["body"]["error_code"] == "auth" and bogus not in blob and "should-not-leak-xyz" not in blob,
          code=fails[-1]["body"]["error_code"] if fails else None, message=(fails[-1]["body"]["message"] if fails else r.stdout[-400:]),
          uncertain_charge=fails[-1]["body"].get("uncertain_charge") if fails else None, stderr=r.stderr[-300:])
    fake.close()

    fake = Fake("lease_lost")
    r = run_worker(fake, "--max-jobs", "1", key="fk-INVALIDtestkey" + secrets.token_hex(12))
    ents = fake.entries()
    check("lease_lost_worker_stops_without_writing_result", not any(e["path"].endswith(("/finish", "/fail")) for e in ents) and '"lease_lost"' in r.stdout,
          stdout=r.stdout.strip()[-300:])
    fake.close()

    fake = Fake()
    import fcntl
    lk = open("/tmp/realisim-support-worker.lock", "w"); fcntl.flock(lk, fcntl.LOCK_EX)
    r = run_worker(fake, "--presence-only")
    fcntl.flock(lk, fcntl.LOCK_UN); lk.close()
    check("overlapping_run_refused_by_local_lock", r.returncode == 3 and not fake.entries(), stdout=r.stdout.strip())
    fake.close()

def t_finish_mapping():
    """Finish translation (no live model available): internal result -> backend finish body, idempotent."""
    fake = Fake(); api = fake.api(); api.presence("w", True); job = api.claim("w")
    clean, _ = ds.validate_result({"answer": {"diagnosis": "Table plane not visible.", "uncertainty": "medium", "uncertainty_note": "one clip",
                                              "next_steps": [{"title": "Re-record", "detail": "keep table edges in view", "requires": "user"}]},
                                   "citations": [{"canonical_id": "help_doc:README.md#workcell-capture@9fa727d54c1d", "kind": "help_doc", "title": "t", "snippet": "s"}],
                                   "proposals": [{"kind": "table", "values": {"width_m": 0.8}, "sources": {"width_m": "user_stated"}, "id": "cprop_1", "base_revision": 4}],
                                   "provider": {"sdk_version": "0.5.0", "cli_version": "0.249.0", "model": "m", "session_id": "s", "usage": {"input_tokens": 1},
                                                "tool_calls": [{"name": "search_help", "ok": True, "duration_ms": 5}], "result_subtype": "success", "duration_ms": 9}},
                                  {"help_doc:README.md#workcell-capture@9fa727d54c1d"})
    api.finish(job["id"], job["lease_token"], clean); again = api.finish(job["id"], job["lease_token"], clean)
    fb = [e for e in fake.entries() if e["path"].endswith("/finish")][0]["body"]
    res = fb["result"]
    ok = (res["citations"] == ["help_doc:README.md#workcell-capture@9fa727d54c1d"] and res["runtime"]["provider"] == "factory"
          and res["runtime"]["session_id"] == "s" and res["proposals"][0]["id"] == "cprop_1" and len(res["answer_text"]) <= 4000
          and fb["idempotency_key"] and again.get("idempotent") is True)
    check("finish_body_matches_backend_contract_and_idempotent", ok, keys=sorted(res), runtime_keys=sorted(res["runtime"]))
    fake.close()

def t_redaction():
    s = ds.redact("No access token available; Authorization: Bearer abcdefghijklmnop; api_key=sk-zzzzzzzzzzzzzzzz; fk-1234567890abcdef; https://x.y/z?X-Goog-Signature=abcd&ok=1 /workspace/photo2grasp/data2/p")
    ok = "token available" in s and "abcdefghijklmnop" not in s and "sk-zzz" not in s and "fk-1234" not in s and "abcd&" not in s and "/workspace" not in s
    check("redaction_keeps_prose_strips_secrets_paths", ok, out=s)

def t_validate_result():
    clean, probs = ds.validate_result({"answer": {"diagnosis": "x" * 5000 + " token=abcdefghijk /workspace/a", "uncertainty": "weird",
                                       "next_steps": [{"title": "t", "requires": "root"}] * 9},
                                       "citations": [{"canonical_id": "doc:a"}, {"canonical_id": "doc:invented"}],
                                       "proposals": [{"kind": "shell"}, {"kind": "table", "values": {}}]}, {"doc:a"})
    ok = (len(clean["answer"]["diagnosis"]) <= 1200 and clean["answer"]["uncertainty"] == "high" and len(clean["answer"]["next_steps"]) == 5
          and clean["answer"]["next_steps"][0]["requires"] == "user" and [c["canonical_id"] for c in clean["citations"]] == ["doc:a"]
          and [p["kind"] for p in clean["proposals"]] == ["table"])
    check("validate_result_bounds_and_drops_unseen_citations", ok, problems=probs)

async def main():
    t_permissions(); t_validate_result(); t_redaction()
    await t_sdk_tool_allowlist()
    fake = Fake()
    try: await t_mcp_auth_and_tools(fake)
    finally: fake.close()
    t_worker_scenarios(); t_finish_mapping()
    leftovers = subprocess.run("pgrep -af '[f]actory/bin/droid' || true", shell=True, capture_output=True, text=True).stdout.strip()
    check("no_droid_processes_left", not leftovers, leftovers=leftovers)
    import glob
    check("no_job_homes_left", not glob.glob("/tmp/realisim-droid-*"), left=glob.glob("/tmp/realisim-droid-*"))
    json.dump(RESULTS, open("/workspace/photo2grasp/factory/tests/last-results.json", "w"), indent=1, default=str)
    print("SUMMARY", sum(r["pass"] for r in RESULTS), "/", len(RESULTS))

asyncio.run(main())
