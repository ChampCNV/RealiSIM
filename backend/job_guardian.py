#!/usr/bin/env python3
"""Per-job guardian for one RealiSIM pipeline process group (not a daemon).

Started by the API right after it launches a pipeline command in its own session (start_new_session=True). Runs in
its own session, outside the job's process group. Every 0.5 s it checks the API process and the job launcher by PID
AND kernel start time (so PID reuse is never mistaken for the original process). When the API dies or the launcher
exits, it terminates ONLY processes that are still members of that job's session/process group and were started no
earlier than the launcher, then exits. If the group id has been reused by a different session leader it kills nothing.
Processes in other sessions (for example the remote-preprocessing broker, which stops its own box) are untouched.
"""
import argparse, os, signal, time


def stat(pid):
    """(state, pgrp, session, starttime) or None."""
    try:
        raw = open(f"/proc/{pid}/stat", "rb").read().decode("utf-8", "replace")
    except OSError:
        return None
    rest = raw[raw.rindex(")") + 2:].split()
    return rest[0], int(rest[2]), int(rest[3]), int(rest[19])


def alive(pid, start):
    s = stat(pid)
    return bool(s) and s[3] == start and s[0] not in ("Z", "X")


def members(pgid, job_start, exclude):
    out = []
    for d in os.listdir("/proc"):
        if not d.isdigit():
            continue
        pid = int(d)
        if pid in exclude:
            continue
        s = stat(pid)
        if s and s[1] == pgid and s[2] == pgid and s[3] >= job_start and s[0] not in ("Z", "X"):
            out.append(pid)
    return out


def kill_group(pgid, job_start, exclude, grace=3.0):
    """Terminate remaining members of the job session. Returns the pids signalled."""
    leader = stat(pgid)
    if leader and leader[3] != job_start:
        return []                      # group id now belongs to a different session leader: never touch it
    hit = members(pgid, job_start, exclude)
    for pid in hit:
        try:
            os.kill(pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
    t0 = time.time()
    while time.time() - t0 < grace and members(pgid, job_start, exclude):
        time.sleep(0.1)
    for pid in members(pgid, job_start, exclude):
        try:
            os.kill(pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
    return hit


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--api-pid", type=int, required=True)
    ap.add_argument("--api-start", type=int, required=True)
    ap.add_argument("--job-pid", type=int, required=True)
    ap.add_argument("--job-start", type=int, required=True)
    ap.add_argument("--max-seconds", type=float, default=4 * 3600)
    ap.add_argument("--log")
    a = ap.parse_args()
    me = os.getpid()
    t0 = time.time()
    reason = None
    while reason is None:
        if not alive(a.api_pid, a.api_start):
            reason = "api-exited"
        elif not alive(a.job_pid, a.job_start):
            reason = "launcher-exited"
        elif time.time() - t0 > a.max_seconds:
            reason = "guardian-deadline"
        else:
            time.sleep(0.5)
    hit = kill_group(a.job_pid, a.job_start, {me, a.api_pid})
    if a.log:
        try:
            with open(a.log, "a") as f:
                f.write(f"{time.time():.3f} guardian job={a.job_pid} reason={reason} terminated={hit}\n")
        except OSError:
            pass


if __name__ == "__main__":
    main()
