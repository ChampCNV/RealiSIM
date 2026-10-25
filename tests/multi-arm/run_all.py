"""Run every multi-arm staging test with the right interpreter. FULL=1 (default here) clean-installs exported packages."""
import json, os, subprocess, sys, time
from pathlib import Path
T = Path(__file__).resolve().parent
SUITES = [("/workspace/.venv/bin/python", "test_backend_multi_arm.py"), ("/workspace/.venv/bin/python", "test_build_orchestration_multi_arm.py"),
          ("/workspace/photo2grasp/.venv/bin/python", "test_adapter_multi_arm.py"), ("/workspace/.venv/bin/python", "test_server_wire_multi_arm.py")]
out = {}
for py, f in SUITES:
    t0 = time.time()
    r = subprocess.run([py, str(T / f)], capture_output=True, text=True, cwd="/tmp", timeout=2400, env=dict(os.environ, FULL=os.environ.get("FULL", "1")))
    lines = r.stdout.splitlines()
    out[f] = dict(ok=r.returncode == 0, seconds=round(time.time() - t0, 1), passed=sum(l.startswith("PASS") for l in lines),
                  failed=[l for l in lines if l.startswith("FAIL")], skipped=[l for l in lines if l.startswith("SKIP")], stderr_tail=r.stderr[-400:] if r.returncode else "")
print("RUN_ALL " + json.dumps(out))
sys.exit(0 if all(v["ok"] for v in out.values()) else 1)
