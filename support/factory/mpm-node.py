# RealiSIM support worker (owner MPM node). Scheduled by the parent; runs the Factory Droid SDK worker for queued
# troubleshooting jobs. The Factory key exists only in this run's tool context and in the worker CHILD's environment.
import json, subprocess
from sandbox_runtime_context import get_api_keys

key = (get_api_keys() or {}).get("FACTORY_API_KEY") or ""
env = {"PATH": "/usr/bin:/bin", "LANG": "C.UTF-8", "HOME": "/tmp/realisim-support-worker-home"}
if key.strip():
    env["FACTORY_API_KEY"] = key.strip()
del key
r = subprocess.run(["/workspace/photo2grasp/factory/.venv/bin/python", "/workspace/photo2grasp/support-worker.py",
                    "--max-jobs", "3", "--budget-s", "480"], env=env, capture_output=True, text=True, timeout=560)
env.pop("FACTORY_API_KEY", None)
lines = [l for l in r.stdout.splitlines() if l.strip()]
print("\n".join(lines[-12:]))
if r.returncode == 2:
    raise SystemExit("support backend unavailable: " + (lines[-1] if lines else r.stderr[-300:]))
if r.returncode not in (0, 3):
    raise SystemExit("support worker crashed (exit %d): %s" % (r.returncode, r.stderr[-400:]))
