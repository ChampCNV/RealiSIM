#!/usr/bin/env python3
"""Self-test of the release gate against a SYNTHETIC upstream and a SCRATCH "live" clone (never the real live repo,
never the real mirror/state). Scenarios: benign fast-forward promoted; breaking upstream change fails and keeps the
running SHA; non-fast-forward blocked; dirty live worktree refused without reset; adapter edit after check invalidates;
busy server defers. Prints SELFTEST {json}; exit 0 only if every scenario behaves."""
import json, shutil, subprocess, sys, time
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent))
import release_gate as G

BASE = G.REL / "selftest"
shutil.rmtree(BASE, ignore_errors=True)
BASE.mkdir(parents=True)
REAL_MAIN = Path("/workspace/photo2grasp/main")
real_head_before = subprocess.run(["git", "-C", str(REAL_MAIN), "rev-parse", "HEAD"], capture_output=True, text=True).stdout.strip()


def git(d, *a, check=True):
    r = subprocess.run(["git", "-C", str(d), *a], capture_output=True, text=True)
    if check and r.returncode:
        raise RuntimeError(f"git {a}: {r.stderr}")
    return r.stdout.strip()


up_work = BASE / "upstream-work"            # where synthetic upstream commits are authored
up_bare = BASE / "upstream.git"             # the synthetic "GitHub"
live = BASE / "live"                        # stands in for /workspace/photo2grasp/main
subprocess.run(["git", "clone", "-q", "--shared", str(REAL_MAIN / ".git"), str(up_work)], check=True)
git(up_work, "checkout", "-q", "-B", "main", real_head_before)
git(up_work, "config", "user.email", "gate@selftest"); git(up_work, "config", "user.name", "gate selftest")
subprocess.run(["git", "clone", "-q", "--bare", str(up_work), str(up_bare)], check=True)
subprocess.run(["git", "clone", "-q", str(up_bare), str(live)], check=True)
(live / "third_party").symlink_to(REAL_MAIN / "third_party")
adapter_copy = BASE / "adapter"
shutil.copytree(G.ADAPTER, adapter_copy, ignore=shutil.ignore_patterns("__pycache__", "*.bak*"))

G.REPO, G.MIRROR, G.UPSTREAM_URL = live, BASE / "mirror.git", str(up_bare)
G.CANDS, G.MAINT, G.WORK, G.REPORTS = BASE / "candidates", BASE / "maint", BASE / "work", BASE / "reports"
G.REL = BASE
G.STATE_F, G.LOCK_F, G.REPORT_F, G.ADAPTER = BASE / "state.json", BASE / "gate.lock", BASE / "report.json", adapter_copy
for d in (G.CANDS, G.MAINT, G.WORK, G.REPORTS):
    d.mkdir(parents=True, exist_ok=True)
IDLE = [True]
probe = lambda: (IDLE[0], "selftest probe")


def push(msg, edit):
    edit(up_work)
    git(up_work, "commit", "-qam", msg)
    git(up_work, "push", "-q", "-f", str(up_bare), "main:main")
    return git(up_work, "rev-parse", "HEAD")


def tick():
    return G.tick(idle_probe=probe, force=True)


results = {}
G.set_mode("promote", "selftest")
st = tick()                                           # bootstrap + baseline validation of the running SHA
results["baseline"] = dict(status=st["status"], validated=st["running"]["validated"], ok=st["status"] == "up_to_date" and st["running"]["validated"])

a = push("docs only", lambda d: (d / "README.md").write_text((d / "README.md").read_text() + "\nselftest line\n"))
st = tick()
results["benign_ff_promoted"] = dict(status=st["status"], live=git(live, "rev-parse", "HEAD")[:12],
                                     ok=st["status"] == "promoted" and git(live, "rev-parse", "HEAD") == a)


def breaking(d):
    p = d / "sim2real" / "tag_env.py"
    p.write_text("from selftest_missing_module import nothing  # synthetic breaking change\n" + p.read_text())
b = push("synthetic breaking import", breaking)
st = tick()
rep = G.read_report(G.load_state()["gate"]["report"])
results["breaking_failed_kept_running"] = dict(status=st["status"], error=(st["error"] or "")[:200], running=st["running"]["short"],
                                               maintainer_task=bool(rep and rep.get("maintainer_task")),
                                               ok=st["status"] in ("failed", "blocked") and git(live, "rev-parse", "HEAD") == a and bool(rep.get("maintainer_task")))

git(up_work, "reset", "-q", "--hard", a + "~1")          # rewrite history: divergent from the running SHA a
c = push("divergent history", lambda d: (d / "README.md").write_text((d / "README.md").read_text() + "\nother line\n"))
st = tick()
results["non_ff_blocked"] = dict(status=st["status"], error=(st["error"] or "")[:160], ok=st["status"] == "blocked" and git(live, "rev-parse", "HEAD") == a)

git(up_work, "reset", "-q", "--hard", a)
d2 = push("docs again", lambda d: (d / "README.md").write_text((d / "README.md").read_text() + "\nthird line\n"))
(live / "objects.json").write_text((live / "objects.json").read_text() + "\n")      # tracked edit in the live worktree
st = tick()
results["dirty_live_refused"] = dict(status=st["status"], error=(st["error"] or "")[:160],
                                     ok=st["status"] == "blocked" and git(live, "rev-parse", "HEAD") == a and git(live, "status", "--porcelain") != "")
git(live, "checkout", "-q", "--", "objects.json")

IDLE[0] = False
st = tick()
results["busy_deferred"] = dict(status=st["status"], ok=st["status"] == "promotion_deferred" and git(live, "rev-parse", "HEAD") == a)
IDLE[0] = True

orig_tp = G.try_promote
def edit_then_promote(s, rep, idle_probe=None, guard=None):     # simulate a manual adapter edit racing the gate
    p = adapter_copy / "introspect.py"
    p.write_text(p.read_text() + "\n# manual edit during final build\n")
    return orig_tp(s, rep, idle_probe, guard)
G.try_promote = edit_then_promote
st = tick()
G.try_promote = orig_tp
results["adapter_edit_invalidates"] = dict(status=st["status"], error=(st["error"] or "")[:160],
                                           ok=st["status"] == "invalidated" and git(live, "rev-parse", "HEAD") == a)
st = tick()     # adapter changed -> revalidate running with the edited adapters, then recheck and promote d2
results["recheck_then_promote"] = dict(status=st["status"], ok=st["status"] == "promoted" and git(live, "rev-parse", "HEAD") == d2)

def needs_adapter(d):          # upstream change that legitimately requires an adapter change (like hand_sim in 9fa727d)
    (d / "sim2real" / "gate_util.py").write_text("VALUE = 1\n")
    p = d / "sim2real" / "tag_env.py"
    p.write_text("from gate_util import VALUE  # synthetic new top-level import\n" + p.read_text())
    git(d, "add", "sim2real/gate_util.py")
e = push("synthetic new vendored module", needs_adapter)
st = tick()
failed_first = st["status"] == "failed"
sdir = Path(G.stage(e))
ge = sdir / "gym_export.py"
src = ge.read_text()
anchor = "    for fname, src in vendored:\n"
assert anchor in src
src = src.replace(anchor, (
    "    if \"from gate_util import\" in tag:\n"
    "        gu = (repo / \"sim2real\" / \"gate_util.py\").read_text()\n"
    "        tag = tag.replace(\"from gate_util import\", \"from .gate_util import\")\n"
    "        vendored = [(f, tag if f == \"tag_env.py\" else s) for f, s in vendored] + [(\"gate_util.py\", gu)]\n") + anchor, 1)
ge.write_text(src)
srep = G.check_staged(e, quick=True)
refused = False
try:
    G.approve(e, "selftest", "live-adapter approval must be refused (checks failed)")
except SystemExit:
    refused = True
G.approve(e, "selftest", "staged vendoring of the new module reviewed", staged=True)
st = tick()
live_ge = (adapter_copy / "gym_export.py").read_text()
results["staged_maintainer_path"] = dict(first=failed_first, staged=srep["status"], refused_unpassed=refused, status=st["status"],
                                         ok=failed_first and srep["status"] == "needs_review" and refused and st["status"] == "promoted"
                                         and git(live, "rev-parse", "HEAD") == e and "gate_util" in live_ge
                                         and any((G.REL / "backups").glob("*/gym_export.py")))
st = tick()
results["after_staged_up_to_date"] = dict(status=st["status"], ok=st["status"] == "up_to_date")

real_head_after = subprocess.run(["git", "-C", str(REAL_MAIN), "rev-parse", "HEAD"], capture_output=True, text=True).stdout.strip()
results["real_live_repo_untouched"] = dict(ok=real_head_after == real_head_before, head=real_head_after[:12])
ok = all(v["ok"] for v in results.values())
print("SELFTEST " + json.dumps(dict(ok=ok, results=results, at=time.time())))
if ok and "--keep" not in sys.argv:
    subprocess.run(["git", f"--git-dir={G.MIRROR}", "worktree", "prune"])
    shutil.rmtree(BASE, ignore_errors=True)
sys.exit(0 if ok else 1)
