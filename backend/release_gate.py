#!/usr/bin/env python3
"""RealiSIM safe upstream release gate (mechanical). Contract: /workspace/photo2grasp/upstream-release-contract.json

Replaces "fetch origin/main and git reset --hard when idle" with a staged gate:

  fetch      into a private mirror (releases/upstream.git, objects shared read-only with the live repo); the live repo
             /workspace/photo2grasp/main is never touched while checking
  check      the candidate SHA in an isolated detached worktree (releases/candidates/<sha12>) with
             adapter/release_check.py: upstream CLI + introspection, no-hand scene assembly + drop tests, legacy and
             generic package clean install + check_env + seeded reset; pins candidate SHA, tree and adapter hashes
  review     contract drift (model hash, seeded rollout, introspection, adapter-facing API, requirements, non-fast-forward)
             needs an agent review/approval pinned to (sha, adapter fingerprint); the gate never interprets semantics
  promote    only in mode "promote", only fast-forward, only with a clean live worktree whose HEAD is the last validated
             running SHA, only with the tested adapter fingerprint unchanged, only while server and workflow are idle;
             `git merge --ff-only` (never reset --hard). On any failure the running SHA stays as it is.

Serialized by an flock, bounded by timeouts, logs and report retention. Local operational state only
(releases/state.json); no project data, no SpacetimeDB, no Firebase. Starts in mode "observe".
This gate proves adapter/upstream contract compatibility with repository sample fixtures, not spatial accuracy,
physical fidelity or hardware safety.
"""
import fcntl, hashlib, importlib.util, json, os, re, shutil, subprocess, sys, threading, time, urllib.request
from contextlib import contextmanager, nullcontext
from pathlib import Path

ROOT = Path("/workspace/photo2grasp")
REPO = ROOT / "main"                                  # live repo used by the server (never reset by this module)
ADAPTER = ROOT / "adapter"
CHECKER = ADAPTER / "release_check.py"
REL = ROOT / "releases"
MIRROR = REL / "upstream.git"
CANDS = REL / "candidates"
MAINT = REL / "maint"
WORK = REL / "work"
REPORTS = REL / "reports"
STATE_F = REL / "state.json"
LOCK_F = REL / "gate.lock"
CONTRACT_F = ROOT / "upstream-release-contract.json"
REPORT_F = ROOT / "upstream-release-report.json"
UPSTREAM_URL = "https://github.com/adityabangde/photo2grasp.git"
BRANCH = "main"
MODES = ("hold", "observe", "promote")
DEFAULT_MODE = "observe"
DEBOUNCE_S = 120                 # adapter files must be unchanged this long before a check starts
CHECK_TIMEOUT_S = 45 * 60
FETCH_TIMEOUT_S = 120
KEEP_REPORTS, KEEP_CANDIDATES, KEEP_HISTORY, LOG_CAP = 30, 2, 40, 256 * 1024
PIPELINE_SCRIPTS = ("photo2grasp.py", "make_asset.py", "sim_worker.py", "gym_export.py", "workcell_export.py", "workcell.py",
                    "workflow_layout.py", "robot_tool.py", "make_scene.py")
for d in (REL, CANDS, MAINT, WORK, REPORTS):
    d.mkdir(parents=True, exist_ok=True)

_spec = importlib.util.spec_from_file_location("realisim_release_check", str(CHECKER))
_rc = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_rc)
adapter_fingerprint = _rc.adapter_fingerprint
sanitize = _rc.sanitize
_THREAD = None


def now():
    return time.time()


def sh(args, cwd=None, timeout=120):
    try:
        r = subprocess.run([str(a) for a in args], cwd=str(cwd) if cwd else None, capture_output=True, text=True, timeout=timeout)
        return r.returncode, (r.stdout or "").strip(), (r.stderr or "").strip()
    except subprocess.TimeoutExpired:
        return 124, "", f"timeout after {timeout}s"


def mgit(*a, timeout=120):
    return sh(["git", f"--git-dir={MIRROR}", *a], timeout=timeout)


def lgit(*a, timeout=120):
    return sh(["git", "-C", REPO, *a], timeout=timeout)


# ── state ──────────────────────────────────────────────────────────────────────
def load_state():
    try:
        s = json.loads(STATE_F.read_text())
    except Exception:
        s = {}
    s.setdefault("version", 1)
    s.setdefault("mode", DEFAULT_MODE)
    s.setdefault("running", {})
    s.setdefault("upstream", {})
    s.setdefault("adapter", {})
    s.setdefault("gate", dict(status="new"))
    s.setdefault("approvals", {})
    s.setdefault("maintainer", {})
    s.setdefault("history", [])
    return s


def save_state(s):
    s["history"] = s["history"][-KEEP_HISTORY:]
    tmp = STATE_F.with_suffix(".tmp")
    tmp.write_text(json.dumps(s, indent=1, default=str))
    os.replace(tmp, STATE_F)


def event(s, ev, **kw):
    s["history"].append(dict(kw, at=now(), kind=ev))


@contextmanager
def gate_lock(block=False):
    f = open(LOCK_F, "a+")
    try:
        fcntl.flock(f, fcntl.LOCK_EX | (0 if block else fcntl.LOCK_NB))
    except BlockingIOError:
        f.close()
        yield False
        return
    try:
        yield True
    finally:
        fcntl.flock(f, fcntl.LOCK_UN)
        f.close()


# ── mirror + worktrees ─────────────────────────────────────────────────────────
def ensure_mirror():
    if not (MIRROR / "HEAD").exists():
        shutil.rmtree(MIRROR, ignore_errors=True)
        code, _, err = sh(["git", "clone", "--bare", "--shared", "--quiet", str(REPO / ".git"), str(MIRROR)], timeout=300)
        if code:
            raise RuntimeError("could not create the gate mirror: " + err[-300:])
        mgit("remote", "set-url", "origin", UPSTREAM_URL)
        mgit("config", "gc.auto", "0")          # never prune objects borrowed from the live repo


def fetch(s):
    ensure_mirror()
    code, _, err = mgit("fetch", "--quiet", "--prune", "origin", f"+refs/heads/{BRANCH}:refs/remotes/origin/{BRANCH}", timeout=FETCH_TIMEOUT_S)
    _, sha, _ = mgit("rev-parse", f"refs/remotes/origin/{BRANCH}")
    s["upstream"] = dict(sha=sha if re.fullmatch(r"[0-9a-f]{40}", sha or "") else s["upstream"].get("sha"),
                         fetched_at=now(), fetch_error=sanitize(err) if code else None)
    return s["upstream"]["sha"]


def candidate_worktree(sha, base=None):
    wt = (base or CANDS) / sha[:12]
    if wt.exists():
        mgit("worktree", "remove", "--force", str(wt))
        shutil.rmtree(wt, ignore_errors=True)
    mgit("worktree", "prune")
    code, _, err = mgit("worktree", "add", "--detach", "--force", str(wt), sha, timeout=300)
    if code:
        raise RuntimeError("could not create the candidate worktree: " + err[-300:])
    tp = wt / "third_party"
    if not tp.exists() and (REPO / "third_party").exists():
        tp.symlink_to(REPO / "third_party")     # gitignored, read-only use (pinned Menagerie robot)
    return wt


def prune_candidates(keep_shas):
    keep = {k[:12] for k in keep_shas if k}
    dirs = sorted((p for p in CANDS.iterdir() if p.is_dir()), key=lambda p: p.stat().st_mtime, reverse=True)
    for i, p in enumerate(dirs):
        if p.name not in keep and i >= KEEP_CANDIDATES:
            mgit("worktree", "remove", "--force", str(p))
            shutil.rmtree(p, ignore_errors=True)
    mgit("worktree", "prune")


# ── checks + reports ───────────────────────────────────────────────────────────
def report_path(rid):
    return REPORTS / f"{rid}.json"


def read_report(rid):
    try:
        return json.loads(report_path(rid).read_text()) if rid else None
    except Exception:
        return None


def write_report(rep):
    report_path(rep["id"]).write_text(json.dumps(rep, indent=1, default=str))
    REPORT_F.write_text(json.dumps(rep, indent=1, default=str))
    files = sorted(REPORTS.glob("*.json"), key=lambda p: p.stat().st_mtime)
    for p in files[:-KEEP_REPORTS]:
        p.unlink(missing_ok=True)
        p.with_suffix(".log").unlink(missing_ok=True)


def find_report(s, sha, fp, adapter_dir=None):
    for h in reversed(s["history"]):
        if h.get("kind") == "checked" and h.get("sha") == sha and h.get("adapter") == fp and h.get("adapter_dir") == (str(adapter_dir) if adapter_dir else None):
            rep = read_report(h.get("report"))
            if rep:
                return rep
    return None


def upstream_diff(base, cand):
    if not base or base == cand:
        return dict(ff=True, commits=[], files=[], requirements_changed=False)
    ff = mgit("merge-base", "--is-ancestor", base, cand)[0] == 0
    _, log, _ = mgit("log", "--format=%h %s", f"{base}..{cand}", "-n", "30")
    _, names, _ = mgit("diff", "--name-status", base, cand)
    _, req, _ = mgit("diff", base, cand, "--", "requirements.txt")
    return dict(ff=ff, commits=log.splitlines(), files=names.splitlines()[:200], requirements_changed=bool(req.strip()),
                requirements_diff=req[-2000:])


def drift(base_rep, res):
    """Mechanical comparison with the last validated report of the running SHA. Any difference needs review."""
    if not base_rep:
        return ["no validated baseline report for the running SHA (first check)"]
    b, out = base_rep.get("check") or {}, []
    for key, label in (("model_hash_celsius_can", "compiled MuJoCo scene (celsius_can) changed"),
                       ("legacy_rollout_fingerprint", "seeded legacy rollout changed (obs/return/info keys)"),
                       ("introspect_contract_sha256", "upstream CLI / stage contract changed"),
                       ("api", "adapter-facing make_scene / sim2real API changed")):
        if b.get(key) is not None and res.get(key) != b.get(key):
            out.append(label)
    return out


def run_check(sha, adapter_dir=None, quick=False, base=None):
    adapter_dir = adapter_dir or ADAPTER
    wt = candidate_worktree(sha, base or CANDS)
    rid = time.strftime("%Y%m%dT%H%M%S") + "-" + sha[:12] + ("-staged" if Path(adapter_dir) != ADAPTER else "")
    work = WORK / rid
    args = [str(ROOT / ".venv" / "bin" / "python"), str(CHECKER), "--repo", str(wt), "--adapter-dir", str(adapter_dir),
            "--work", str(work), "--expect-sha", sha] + (["--quick"] if quick else [])
    t0 = now()
    try:
        r = subprocess.run(args, capture_output=True, text=True, timeout=CHECK_TIMEOUT_S, cwd="/tmp", env=_rc.clean_env())
        out, err, code = r.stdout or "", r.stderr or "", r.returncode
    except subprocess.TimeoutExpired:
        out, err, code = "", f"release check timed out after {CHECK_TIMEOUT_S}s", 124
    res = _rc.marker(out, "RELEASE_CHECK") or dict(passed=False, blockers=["checker crashed: " + sanitize(err)], steps={})
    log = work / "check.log"
    if log.exists():
        data = log.read_bytes()
        report_path(rid).with_suffix(".log").write_bytes(data[-LOG_CAP:])
    shutil.rmtree(work, ignore_errors=True)
    return rid, res, round(now() - t0, 1), code


def brief(err):
    lines = [l.strip() for l in str(err or "failed").splitlines() if l.strip()]
    exc = [l for l in lines if re.search(r"\b\w*(Error|Exception)\b.*:", l)]
    return (exc[-1] if exc else lines[-1] if lines else "failed")[:300]


def failure_summary(res):
    if res.get("blockers"):
        return "; ".join(res["blockers"])
    bad = [f"{k}: {brief(v.get('error'))}" for k, v in (res.get("steps") or {}).items() if not v.get("ok")]
    return sanitize("; ".join(bad)) if bad else None


def maintainer_task(s, rep):
    sha, fp = rep["sha"], rep["adapter_fingerprint"]
    key = f"{sha[:12]}:{fp[:12]}"
    staged = MAINT / sha[:12] / "adapter"
    running = s["running"].get("sha") or ""
    prompt = (
        "RealiSIM upstream release maintainer (bounded task). Product: capture -> reviewed fields -> generated assets -> "
        "scene validation -> portable export; robot/hand model additions are components, not headline features.\n"
        f"The mechanical release gate reports status '{rep['status']}' for upstream candidate {sha} (running {running[:12]}).\n"
        f"Report: {report_path(rep['id'])} (log beside it). Reasons: {json.dumps(rep.get('review_required') or [rep.get('error')])}\n"
        "Do this, in order, and nothing else:\n"
        f"1. Read the actual change: git --git-dir={MIRROR} log/diff {running[:12]}..{sha[:12]} and the candidate README "
        f"(worktree {CANDS / sha[:12]}). Decide whether it affects the RealiSIM adapter contract.\n"
        f"2. If adapters must change: python {Path(__file__)} stage {sha} (creates the isolated copy {staged}); edit ONLY files "
        "in that staged adapter copy (adapters/orchestration glue). Never edit the live adapter/, backend/server.py, "
        "workflow.py, the UI, Firebase, the live repo main, data2/ or SpacetimeDB; never restart services.\n"
        f"3. Test: python {Path(__file__)} check-staged {sha}. It runs the full checker with the staged adapter in an "
        "isolated candidate worktree and records a pinned report.\n"
        f"4. Only if the checks pass and you reviewed every substantive difference (do not approximate semantics with "
        f"code): python {Path(__file__)} approve {sha} --by <your chat id> --note '<one-paragraph rationale>' "
        "[--staged]. If the change is out of scope or unsafe (hardware, hand features as headline, new paid GPU calls, "
        "dependency risk), do not approve; write the blocker with --reject instead.\n"
        "5. Report what changed, test evidence and remaining risk. Promotion stays with the gate (fast-forward, idle, "
        "unchanged fingerprints) once the parent enables mode 'promote'. Do not claim spatial accuracy or physical safety.\n"
        "Limits: no delegation, at most 45 minutes, no paid GPU calls, no hardware, no user projects.")
    return dict(dedupe_key=key, needed=True, staged_adapter_dir=str(staged), prompt=prompt,
                trigger=dict(when="gate.status in ['needs_review','failed','blocked'] and gate.sha == upstream.sha and maintainer.last_key != dedupe_key",
                             schedule="parent: MPM interval node (>= 300 s) reading public_status()/state, or mana.ask with notify; at most one task per dedupe_key",
                             model_effort="any capable coding model; effort medium"))


def check_target(s, target, fp, kind):
    """Run (or reuse) the check of `target` with the live adapters; update gate state. Holds the gate lock."""
    running = s["running"].get("sha")
    base_rep = read_report(s["running"].get("validated_report"))
    rep = find_report(s, target, fp["sha256"])
    if not rep:
        s["gate"] = dict(status="checking", sha=target, kind=kind, started_at=now())
        save_state(s)
        rid, res, secs, code = run_check(target)
        d = upstream_diff(running, target) if kind == "candidate" else dict(ff=True, commits=[], files=[], requirements_changed=False)
        review = [] if kind == "revalidate" else drift(base_rep, res)
        if d.get("requirements_changed"):
            review.append("requirements.txt changed upstream (shared repo venv is never modified by the gate)")
        rep = dict(id=rid, contract=1, kind=kind, sha=target, running_sha=running, tree=res.get("candidate_tree"),
                   adapter_fingerprint=fp["sha256"], adapter_files=fp["files"], adapter_dir=None,
                   orchestration=orchestration_fingerprint(), check=res, upstream=d, checked_at=now(), seconds=secs,
                   review_required=review, passed=bool(res.get("passed")))
        if not res.get("passed"):
            rep["status"], rep["error"] = ("blocked" if res.get("blockers") else "failed"), failure_summary(res)
        elif kind == "candidate" and not d.get("ff"):
            rep["status"], rep["error"] = "blocked", "candidate is not a fast-forward of the running SHA (upstream history rewritten)"
        elif review:
            rep["status"] = "needs_review"
        else:
            rep["status"] = "passed"
        if rep["status"] in ("failed", "blocked", "needs_review"):
            rep["maintainer_task"] = maintainer_task(s, rep)
        write_report(rep)
        event(s, "checked", sha=target, adapter=fp["sha256"], adapter_dir=None, report=rid, status=rep["status"], kind=kind)
    if kind == "revalidate":
        if rep["passed"]:
            s["running"].update(adapter_fingerprint=fp["sha256"], validated_report=rep["id"], validated_at=rep["checked_at"],
                                tree=rep.get("tree"))
            if not s["running"].get("source") or s["running"]["source"] == "bootstrap-unvalidated":
                s["running"]["source"] = "baseline-validated"
    s["gate"] = dict(status=rep["status"], sha=target, kind=kind, report=rep["id"], checked_at=rep["checked_at"], error=rep.get("error"),
                     review_required=rep.get("review_required"))
    return rep


def orchestration_fingerprint():
    out = {}
    for p in (ROOT / "backend" / "server.py", ROOT / "backend" / "workflow.py", Path(__file__).resolve()):
        if p.exists():
            out[p.name] = hashlib.sha256(p.read_bytes()).hexdigest()
    return out


# ── idle + promotion ───────────────────────────────────────────────────────────
def pipeline_processes():
    found = []
    for d in Path("/proc").iterdir():
        if not d.name.isdigit():
            continue
        try:
            cmd = (d / "cmdline").read_bytes().split(b"\0")
        except Exception:
            continue
        txt = " ".join(c.decode(errors="replace") for c in cmd if c)
        if str(REL) in txt or "release_check.py" in txt:
            continue
        if any(Path(c.decode(errors="replace")).name in PIPELINE_SCRIPTS for c in cmd[:4] if c):
            found.append(int(d.name))
    return found


def default_idle():
    """Standalone idle probe (fail-safe): backend /health says not busy with an empty queue, and no pipeline or
    adapter process (server jobs or workflow layout/analysis threads) is running."""
    try:
        port = int((ROOT / "backend" / "port").read_text().strip())
        h = json.loads(urllib.request.urlopen(f"http://127.0.0.1:{port}/health", timeout=5).read())
    except Exception as e:
        return False, f"backend health unavailable ({type(e).__name__})"
    if h.get("busy") or h.get("queue"):
        return False, "backend job running or queued"
    procs = pipeline_processes()
    if procs:
        return False, f"{len(procs)} pipeline process(es) running"
    return True, "idle"


def apply_staged(rep):
    """Copy staged adapter files whose live content still equals the base they were staged from. Backup first."""
    base, staged = rep["staged_base_files"], Path(rep["adapter_dir"])
    live = adapter_fingerprint(ADAPTER)["files"]
    changed = {f: h for f, h in rep["adapter_files"].items() if base.get(f) != h}
    conflicts = [f for f in changed if live.get(f) != base.get(f)]
    if conflicts:
        raise RuntimeError("live adapter files changed since staging: " + ", ".join(conflicts))
    bdir = REL / "backups" / time.strftime("%Y%m%dT%H%M%S")
    bdir.mkdir(parents=True)
    for f in changed:
        src, dst = staged / f, ADAPTER / f
        if dst.exists():
            (bdir / f).parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(dst, bdir / f)
        dst.parent.mkdir(parents=True, exist_ok=True)
        tmp = dst.with_suffix(dst.suffix + ".gate-tmp")
        shutil.copy2(src, tmp)
        os.replace(tmp, dst)
    if adapter_fingerprint(ADAPTER)["sha256"] != rep["adapter_fingerprint"]:
        raise RuntimeError("adapter fingerprint after applying the staged patch differs from the tested one")
    return sorted(changed), str(bdir)


def try_promote(s, rep, idle_probe=None, guard=None):
    sha = rep["sha"]
    with (guard or nullcontext()):
        ok, why = (idle_probe() if idle_probe else default_idle())
        if isinstance(ok, tuple):
            ok, why = ok
        if not ok:
            s["gate"].update(status="promotion_deferred", error=f"waiting for idle: {why}")
            return False
        running = s["running"].get("sha")
        _, head, _ = lgit("rev-parse", "HEAD")
        _, dirty, _ = lgit("status", "--porcelain", "--untracked-files=no")
        _, up, _ = mgit("rev-parse", f"refs/remotes/origin/{BRANCH}")
        live_fp = adapter_fingerprint(ADAPTER)["sha256"]
        staged = bool(rep.get("adapter_dir"))
        problems = []
        if head != running:
            problems.append(f"live repo HEAD {head[:12]} is not the last validated running SHA {running[:12] if running else None}; not touching it")
        if dirty:
            problems.append("live repo has modified tracked files (possibly user-authored); not resetting")
        if up != sha:
            problems.append("upstream moved after the check; rechecking first")
        if not staged and live_fp != rep["adapter_fingerprint"]:
            problems.append("adapter files changed after the check; result invalidated")
        if staged and live_fp != hashlib.sha256(json.dumps(rep["staged_base_files"], sort_keys=True).encode()).hexdigest():
            problems.append("live adapters differ from the staged base; staged patch invalidated")
        if mgit("merge-base", "--is-ancestor", running, sha)[0] != 0:
            problems.append("not a fast-forward")
        if problems:
            s["gate"].update(status="invalidated" if any("changed" in p or "moved" in p for p in problems) else "blocked", error="; ".join(problems))
            event(s, "promotion_refused", sha=sha, reasons=problems)
            return False
        code, _, err = lgit("fetch", "--quiet", str(MIRROR), sha, timeout=300)
        if code or lgit("cat-file", "-e", sha + "^{commit}")[0]:
            s["gate"].update(status="blocked", error="could not bring the candidate commit into the live repo: " + sanitize(err))
            return False
        applied = None
        if staged:
            try:
                applied = apply_staged(rep)
            except Exception as e:
                s["gate"].update(status="blocked", error=sanitize(str(e)))
                return False
        code, _, err = lgit("merge", "--ff-only", "--quiet", sha, timeout=300)
        _, new_head, _ = lgit("rev-parse", "HEAD")
        _, new_tree, _ = lgit("rev-parse", "HEAD^{tree}")
        if code or new_head != sha or new_tree != rep.get("tree"):
            s["gate"].update(status="blocked", error="fast-forward failed; live repo left at " + new_head[:12] + ": " + sanitize(err))
            event(s, "promotion_failed", sha=sha, head=new_head)
            return False
        s["running"] = dict(sha=sha, tree=new_tree, validated_report=rep["id"], validated_at=rep["checked_at"], promoted_at=now(),
                            adapter_fingerprint=adapter_fingerprint(ADAPTER)["sha256"], source="promoted", previous=running,
                            applied_adapter_files=applied)
        s["gate"].update(status="promoted", error=None)
        event(s, "promoted", sha=sha, previous=running, report=rep["id"], applied=applied)
        return True


def approval_ok(s, rep):
    a = s["approvals"].get(rep["sha"])
    return bool(a) and not a.get("rejected") and a.get("adapter_fingerprint") == rep["adapter_fingerprint"] and a.get("report") == rep["id"]


# ── entry points ───────────────────────────────────────────────────────────────
def tick(idle_probe=None, guard=None, force=False):
    """One serialized gate step. Returns the public status. Safe to call every 120 s; skips if a step is running."""
    with gate_lock() as got:
        if not got:
            return public_status()
        s = load_state()
        try:
            cand = fetch(s)
            if not s["running"].get("sha"):
                _, head, _ = lgit("rev-parse", "HEAD")
                s["running"] = dict(sha=head, source="bootstrap-unvalidated", bootstrapped_at=now())
                event(s, "bootstrap", sha=head)
            _, live_head, _ = lgit("rev-parse", "HEAD")
            s["running"]["live_head_mismatch"] = live_head != s["running"]["sha"] or None
            if s["mode"] == "hold":
                s["gate"] = dict(status="held", sha=cand, checked_at=s["gate"].get("checked_at"), error=None)
                return _done(s)
            fp = adapter_fingerprint(ADAPTER)
            if s["adapter"].get("fingerprint") != fp["sha256"]:
                s["adapter"] = dict(fingerprint=fp["sha256"], first_seen_at=now())
            stable = force or now() - s["adapter"]["first_seen_at"] >= DEBOUNCE_S
            running = s["running"]["sha"]
            if s["running"].get("adapter_fingerprint") != fp["sha256"]:
                target, kind = running, "revalidate"
            elif cand and cand != running:
                target, kind = cand, "candidate"
            else:
                s["gate"] = dict(status="up_to_date", sha=running, report=s["running"].get("validated_report"),
                                 checked_at=s["running"].get("validated_at"), error=None)
                return _done(s)
            if not stable and not find_report(s, target, fp["sha256"]):
                s["gate"] = dict(s["gate"], status="pending_debounce", sha=target, kind=kind,
                                 error=f"adapter files changed {int(now() - s['adapter']['first_seen_at'])} s ago; waiting {DEBOUNCE_S} s")
                return _done(s)
            rep = check_target(s, target, fp, kind)
            if kind == "revalidate" and rep["passed"] and cand and cand != running:
                rep = check_target(s, cand, fp, "candidate")
            if kind == "revalidate" and rep["passed"] and (not cand or cand == running):
                s["gate"]["status"] = "up_to_date"
            if kind == "revalidate" and not rep["passed"]:
                s["gate"]["error"] = "current adapters fail against the running SHA: " + (rep.get("error") or "")
            appr = (s.get("approvals") or {}).get(cand) or {}
            appr_rep = read_report(appr.get("report")) if appr and not appr.get("rejected") else None
            staged_rep = latest_staged_report(s, cand)
            if appr_rep and appr_rep.get("sha") == cand and appr_rep.get("passed") and rep["status"] != "passed":
                rep = appr_rep              # promote exactly the report the maintainer approved (staged or live adapters)
            elif staged_rep and staged_rep.get("passed") and rep["status"] != "passed":
                rep = staged_rep
            if rep.get("kind") in ("candidate", "staged") and s["mode"] == "promote":
                if rep["status"] == "passed" and not rep.get("adapter_dir") or approval_ok(s, rep) and rep.get("passed") and rep["upstream"].get("ff"):
                    try_promote(s, rep, idle_probe, guard)
                elif rep["status"] == "needs_review":
                    s["gate"]["error"] = "awaiting maintainer review/approval"
            prune_candidates([s["running"].get("sha"), cand])
        except Exception as e:
            s["gate"] = dict(s["gate"], status="error", error=sanitize(f"{type(e).__name__}: {e}"))
            event(s, "error", error=s["gate"]["error"])
        return _done(s)


def _done(s):
    s["gate"]["updated_at"] = now()
    save_state(s)
    return public_status(s)


def tick_async(idle_probe=None, guard=None):
    """Non-blocking: start tick() in a daemon thread unless one is running. For the server's 120 s loop."""
    global _THREAD
    if _THREAD and _THREAD.is_alive():
        return False
    _THREAD = threading.Thread(target=tick, kwargs=dict(idle_probe=idle_probe, guard=guard), daemon=True, name="release-gate")
    _THREAD.start()
    return True


def running_sha():
    """The last validated (or bootstrapped) running SHA. Use it for per-project worktrees instead of reading main HEAD."""
    return load_state()["running"].get("sha")


def latest_staged_report(s, sha):
    for h in reversed(s["history"]):
        if h.get("kind") == "checked" and h.get("sha") == sha and h.get("adapter_dir"):
            return read_report(h.get("report"))
    return None


def stage(sha):
    dst = MAINT / sha[:12] / "adapter"
    if dst.exists():
        return str(dst)
    dst.parent.mkdir(parents=True, exist_ok=True)
    shutil.copytree(ADAPTER, dst, ignore=shutil.ignore_patterns("__pycache__", "*.bak*"))
    (dst.parent / "BASE.json").write_text(json.dumps(adapter_fingerprint(ADAPTER), indent=1))
    candidate_worktree(sha)
    return str(dst)


def check_staged(sha, quick=False):
    with gate_lock(block=True):
        s = load_state()
        sdir = MAINT / sha[:12] / "adapter"
        base = json.loads((sdir.parent / "BASE.json").read_text())
        fp = adapter_fingerprint(sdir)
        rid, res, secs, code = run_check(sha, adapter_dir=sdir, quick=quick)
        running = s["running"].get("sha")
        d = upstream_diff(running, sha)
        rep = dict(id=rid, contract=1, kind="staged", sha=sha, running_sha=running, tree=res.get("candidate_tree"),
                   adapter_fingerprint=fp["sha256"], adapter_files=fp["files"], adapter_dir=str(sdir), staged_base_files=base["files"],
                   staged_changed=sorted(f for f, h in fp["files"].items() if base["files"].get(f) != h),
                   check=res, upstream=d, checked_at=now(), seconds=secs, passed=bool(res.get("passed")),
                   review_required=["staged adapter patch: requires explicit approval"], orchestration=orchestration_fingerprint())
        rep["status"] = "needs_review" if rep["passed"] and d.get("ff") else "failed"
        rep["error"] = None if rep["passed"] else failure_summary(res)
        write_report(rep)
        event(s, "checked", sha=sha, adapter=fp["sha256"], adapter_dir=str(sdir), report=rid, status=rep["status"], kind="staged")
        save_state(s)
        return rep


def approve(sha, by, note, staged=False, reject=False):
    with gate_lock(block=True):
        s = load_state()
        rep = latest_staged_report(s, sha) if staged else find_report(s, sha, adapter_fingerprint(ADAPTER)["sha256"])
        if not rep:
            raise SystemExit("no report for this SHA and adapter state; run the check first")
        if not reject and not rep.get("passed"):
            raise SystemExit("refusing to approve: the checks did not pass")
        s["approvals"][sha] = dict(adapter_fingerprint=rep["adapter_fingerprint"], report=rep["id"], by=by, note=note[:2000], at=now(),
                                   staged=bool(staged), rejected=bool(reject))
        s["maintainer"] = dict(last_key=f"{sha[:12]}:{rep['adapter_fingerprint'][:12]}", at=now(), by=by, outcome="rejected" if reject else "approved")
        event(s, "rejected" if reject else "approved", sha=sha, by=by, report=rep["id"])
        save_state(s)
        return s["approvals"][sha]


def set_mode(mode, by):
    if mode not in MODES:
        raise SystemExit(f"mode must be one of {MODES}")
    with gate_lock(block=True):
        s = load_state()
        s.update(mode=mode, mode_set_by=by, mode_set_at=now())
        event(s, "mode", mode=mode, by=by)
        save_state(s)


def rollback(to_sha, by):
    """Explicit operator action only: move the live repo back to a previously validated running SHA with
    `git reset --keep` (refuses instead of discarding local changes)."""
    with gate_lock(block=True):
        s = load_state()
        ok = any(h.get("kind") in ("promoted", "bootstrap") and (h.get("sha") == to_sha or h.get("previous") == to_sha) for h in s["history"]) \
            or s["running"].get("previous") == to_sha
        if not ok:
            raise SystemExit("not a previously running SHA")
        ok2, why = default_idle()
        if not ok2:
            raise SystemExit("not idle: " + why)
        code, _, err = lgit("reset", "--keep", to_sha)
        if code:
            raise SystemExit("reset --keep refused: " + err[-300:])
        s["running"] = dict(sha=to_sha, source="rollback", by=by, at=now(), adapter_fingerprint=None)   # forces revalidation
        event(s, "rollback", sha=to_sha, by=by)
        save_state(s)


def maintainer_pending():
    """For the parent's scheduler: the bounded maintainer task to dispatch now, or None. Deduplicated per
    (candidate SHA, adapter fingerprint); only for upstream candidates (adapter regressions against the running SHA are
    the build team's to fix, not an upstream integration task)."""
    s = load_state()
    g = s.get("gate", {})
    # A staged adapter approval is waiting for a controlled promotion, not another agent run.
    approval = (s.get("approvals") or {}).get((s.get("upstream") or {}).get("sha"), {})
    if approval.get("staged") and not approval.get("rejected"):
        return None
    hold = Path("/workspace/photo2grasp/maintenance-hold.json")
    if hold.exists():
        try:
            if json.loads(hold.read_text()).get("enabled"):
                return None
        except (OSError, ValueError):
            return None
    if s.get("mode") == "hold" or g.get("status") not in ("needs_review", "failed", "blocked") or not g.get("report"):
        return None
    rep = read_report(g["report"])
    if not rep or rep.get("kind") not in ("candidate", "staged") or rep.get("sha") != s["upstream"].get("sha"):
        return None
    task = rep.get("maintainer_task")
    m = s["maintainer"]
    if not task:
        return None
    if m.get("last_key") == task["dedupe_key"]:
        # One bounded retry when the previous maintainer run for this exact key ended without approve/reject.
        if m.get("outcome") != "ended_without_decision" or int(m.get("attempts") or 1) >= MAX_MAINTAINER_ATTEMPTS:
            return None
    return dict(task, report=rep["id"], sha=rep["sha"], attempt=int(m.get("attempts") or 1) + 1 if m.get("last_key") == task["dedupe_key"] else 1)


MAX_MAINTAINER_ATTEMPTS = 2


def mark_maintainer_ended(dedupe_key, status):
    """The scheduler saw the dispatched maintainer chat end (complete/error/aborted) without an approve/reject decision."""
    with gate_lock(block=True):
        s = load_state()
        m = s["maintainer"]
        if m.get("last_key") != dedupe_key or m.get("outcome") != "dispatched":
            return False
        m.update(outcome="ended_without_decision", ended_status=status, ended_at=now())
        event(s, "maintainer_ended_without_decision", key=dedupe_key, status=status)
        save_state(s)
        return True


def mark_maintainer_dispatched(dedupe_key, chat_id=None, message_id=None):
    with gate_lock(block=True):
        s = load_state()
        prev = s.get("maintainer") or {}
        attempts = int(prev.get("attempts") or 1) + 1 if prev.get("last_key") == dedupe_key else 1
        s["maintainer"] = dict(last_key=dedupe_key, dispatched_at=now(), chat_id=chat_id, message_id=message_id, outcome="dispatched",
                               attempts=attempts)
        event(s, "maintainer_dispatched", key=dedupe_key, chat_id=chat_id)
        save_state(s)


def public_status(s=None):
    """Sanitized status for the UI: no filesystem paths, logs or secrets."""
    s = s or load_state()
    g, r, u = s.get("gate", {}), s.get("running", {}), s.get("upstream", {})
    err = g.get("error")
    if err:
        err = re.sub(r"/(workspace|tmp|home|root)/[^\s:;,'\"]*", "[path]", str(err))[:400]
    rep = read_report(g.get("report")) if g.get("report") else None
    return dict(mode=s.get("mode"), status=g.get("status"),
                running=dict(sha=r.get("sha"), short=(r.get("sha") or "")[:7], validated=bool(r.get("validated_report")),
                             validated_at=r.get("validated_at"), source=r.get("source"), live_head_mismatch=bool(r.get("live_head_mismatch"))),
                candidate=dict(sha=u.get("sha"), short=(u.get("sha") or "")[:7], fetched_at=u.get("fetched_at"), fetch_ok=not u.get("fetch_error")),
                checked_at=g.get("checked_at"), updated_at=g.get("updated_at"), error=err,
                review_required=list(g.get("review_required") or [])[:6],
                checks={k: bool(v.get("ok")) for k, v in ((rep or {}).get("check") or {}).get("steps", {}).items()} if rep else None,
                scope="contract compatibility with sample fixtures; not spatial accuracy or physical safety")


def main(argv):
    import argparse
    ap = argparse.ArgumentParser(description="RealiSIM upstream release gate")
    sub = ap.add_subparsers(dest="cmd", required=True)
    t = sub.add_parser("tick"); t.add_argument("--force", action="store_true")
    sub.add_parser("status"); sub.add_parser("state"); sub.add_parser("maintainer-pending")
    m = sub.add_parser("set-mode"); m.add_argument("mode", choices=MODES); m.add_argument("--by", required=True)
    st = sub.add_parser("stage"); st.add_argument("sha")
    cs = sub.add_parser("check-staged"); cs.add_argument("sha"); cs.add_argument("--quick", action="store_true")
    a = sub.add_parser("approve"); a.add_argument("sha"); a.add_argument("--by", required=True); a.add_argument("--note", required=True)
    a.add_argument("--staged", action="store_true"); a.add_argument("--reject", action="store_true")
    rb = sub.add_parser("rollback"); rb.add_argument("--to", required=True); rb.add_argument("--by", required=True)
    x = ap.parse_args(argv)
    if x.cmd == "tick":
        print(json.dumps(tick(force=x.force), indent=1, default=str))
    elif x.cmd == "status":
        print(json.dumps(public_status(), indent=1, default=str))
    elif x.cmd == "maintainer-pending":
        print(json.dumps(maintainer_pending(), indent=1, default=str))
    elif x.cmd == "state":
        print(json.dumps(load_state(), indent=1, default=str))
    elif x.cmd == "set-mode":
        set_mode(x.mode, x.by); print(json.dumps(public_status(), indent=1))
    elif x.cmd == "stage":
        print(stage(x.sha))
    elif x.cmd == "check-staged":
        r = check_staged(x.sha, x.quick); print(json.dumps(dict(status=r["status"], report=r["id"], error=r.get("error"), changed=r["staged_changed"]), indent=1))
    elif x.cmd == "approve":
        print(json.dumps(approve(x.sha, x.by, x.note, x.staged, x.reject), indent=1, default=str))
    elif x.cmd == "rollback":
        rollback(x.to, x.by); print(json.dumps(public_status(), indent=1))


if __name__ == "__main__":
    main(sys.argv[1:])
