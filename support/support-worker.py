#!/workspace/photo2grasp/factory/.venv/bin/python
"""RealiSIM support worker: claims queued troubleshooting jobs from the support backend and runs each one as a bounded
Factory Droid SDK investigation (backend/droid_support.py).

Run by an owner MPM python node (which passes FACTORY_API_KEY from get_api_keys() in THIS child's environment only),
or manually for tests. Never put the key in argv, files, daemon commands or Firebase.

  support-worker.py [--max-jobs N] [--budget-s S] [--model MODEL] [--api-base URL] [--presence-only]

Backend wire contract: /workspace/photo2grasp/support-contract.json v1 (translated in droid_support.WorkerAPI).

Exit 0 = ran normally (including "nothing queued", "not connected", or job failures that were recorded);
exit 2 = support backend unavailable; exit 3 = another worker holds the local lock.
"""
import argparse, asyncio, fcntl, json, os, socket, sys, time

sys.path.insert(0, "/workspace/photo2grasp/backend")
import droid_support as ds  # noqa: E402

LOCK = "/tmp/realisim-support-worker.lock"
KEEP_ENV = ("PATH", "LANG", "LC_ALL", "TZ", "SSL_CERT_FILE", "SSL_CERT_DIR")


def scrub_env():
    """Take the key out of the process environment (the SDK merges os.environ into the droid child) and drop
    everything not needed, so no other sandbox credential or context reaches the CLI."""
    key = os.environ.pop("FACTORY_API_KEY", None)
    for k in list(os.environ):
        if k not in KEEP_ENV:
            os.environ.pop(k, None)
    os.environ.setdefault("PATH", "/usr/bin:/bin")
    os.environ.setdefault("LANG", "C.UTF-8")
    return (key or "").strip() or None


def out(**kv):
    print(json.dumps({"at": int(time.time()), **kv}, default=str), flush=True)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--max-jobs", type=int, default=3)
    ap.add_argument("--budget-s", type=int, default=480)
    ap.add_argument("--model")
    ap.add_argument("--api-base")
    ap.add_argument("--presence-only", action="store_true")
    ap.add_argument("--token-file", help="worker capability file (default: support/worker.token); tests point this at a fixture")
    ap.add_argument("--allow-unauthenticated-probe", action="store_true",
                    help="TEST ONLY: run the real SDK without a key to prove it fails cleanly as connection_required")
    a = ap.parse_args()
    key = scrub_env()
    model = a.model or None

    lockf = open(LOCK, "w")
    try:
        fcntl.flock(lockf, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        out(event="skip", reason="another support worker is running")
        return 3

    worker_id = f"droid-{socket.gethostname()[:20]}-{os.getpid()}"
    tok = open(a.token_file).read().strip() if a.token_file else None
    api = ds.WorkerAPI(base=a.api_base, token=tok)
    cli_ok = ds.DROID_BIN.exists() and os.access(ds.DROID_WRAPPER, os.X_OK)
    state = "ready" if (key and cli_ok) else ("connection_required" if cli_ok else "error")
    detail = "" if state == "ready" else ("FACTORY_API_KEY is not available to this worker run" if cli_ok else "pinned Factory CLI missing")
    try:
        counts = api.presence(worker_id, state == "ready")
    except ds.WorkerApiError as e:
        out(event="support_unavailable", status=e.status, error=e.msg)
        return 2
    out(event="presence", state=state, detail=detail, backend=counts)
    if a.presence_only or (state != "ready" and not (a.allow_unauthenticated_probe and cli_ok)):
        return 0

    t_end = time.time() + a.budget_s
    n = 0
    while n < a.max_jobs and time.time() + ds.LIMITS["total_timeout_s"] + 20 < t_end:
        try:
            job = api.claim(worker_id, key_present=bool(key))
        except ds.WorkerApiError as e:
            out(event="claim_refused" if e.status == 409 else "claim_error", status=e.status, code=e.code, error=e.msg)
            return 0 if e.status == 409 else 2
        if not job:
            out(event="idle", jobs_run=n)
            break
        n += 1
        out(event="claimed", job_id=job["id"], project_id=job.get("project_id"), attempt=job.get("attempt"))
        kind, detail = asyncio.run(ds.investigate(api, job, key, model=model, worker_id=worker_id))
        prov = detail.get("provider", detail) if isinstance(detail, dict) else {}
        out(event=kind, job_id=job["id"], code=detail.get("code") if isinstance(detail, dict) else None,
            session_id=prov.get("session_id"), model=prov.get("model"), subtype=prov.get("result_subtype"),
            duration_ms=prov.get("duration_ms"), tool_calls=len(prov.get("tool_calls") or []), usage=prov.get("usage"),
            denied=prov.get("denied_permission_requests"),
            uncertain_charge=detail.get("uncertain_charge") if isinstance(detail, dict) else None)
    return 0


if __name__ == "__main__":
    sys.exit(main())
