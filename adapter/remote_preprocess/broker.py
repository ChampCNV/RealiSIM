"""RealiSIM remote preprocessing broker: one ephemeral Mana compute box per pipeline job.

Started lazily by adapter/shim/sitecustomize.py (only when the backend sets REALISIM_REMOTE_PREPROCESS=1) with
/workspace/.venv/bin/python, because mana_compute lives on that interpreter's path. It talks to the pipeline
process over stdin/stdout with length-prefixed frames, and to the box only through mana_compute (the sandbox's
own session; no saved keys or tokens are read, passed or written).

Lifecycle guarantees
- One box and one ONNX session per job; every frame of the job reuses it.
- Stops the box on: stdin EOF, SIGTERM/SIGINT/SIGHUP, the pipeline process disappearing (polled every 0.5 s; it
  runs in its own session so a process-group SIGKILL of the pipeline cannot skip this), the job wall cap, or the
  per-job call cap. The remote worker also exits on idle/wall cap, and the box has TTL = cap + 5 min, idle 5 min.
- Never falls back to local inference and never changes model, input size or session options.
"""
import io, json, os, signal, struct, sys, threading, time, uuid
from pathlib import Path

HERE = Path(__file__).resolve().parent
MODEL_URL = "https://github.com/danielgatis/rembg/releases/download/v0.0.0/BiRefNet-general-bb_swin_v1_tiny-epoch_232.onnx"
PINNED = {"birefnet-general-lite.onnx": "5600024376f572a557870a5eb0afb1e5961636bef4e1e22132025467d0f03333"}
PIP = "onnxruntime==1.23.2 numpy==2.2.6"
E = os.environ.get
JOB = (E("REALISIM_REMOTE_JOB_ID") or "adhoc")[:40]
CAP_S = float(E("REALISIM_REMOTE_MAX_SECONDS", "1200"))
MAX_CALLS = int(E("REALISIM_REMOTE_MAX_CALLS", "96"))
CPU = float(E("REALISIM_REMOTE_CPU", "4"))
MEM = float(E("REALISIM_REMOTE_MEMORY_GIB", "16"))
THREADS = int(E("P2G_ORT_THREADS", "4"))
INFER_TIMEOUT = float(E("REALISIM_REMOTE_INFER_TIMEOUT", "180"))
STATUS = E("REALISIM_REMOTE_STATUS_FILE")
IMAGE = E("REALISIM_REMOTE_IMAGE") or None
SPOOL = "/root/realisim_spool"
T0 = time.time()
st = dict(state="starting", job=JOB, cap_s=CAP_S, cpu=CPU, memory_gib=MEM, calls=0, box=None, started_at=T0)
_lock = threading.Lock()
_box = None
_done = False
_cleaned = threading.Event()


def log(msg):
    sys.stderr.write(f"[RealiSIM remote preprocessing] {msg}\n"); sys.stderr.flush()


def put(_final=False, **kw):
    if _done and not _final:
        return  # cleanup owns the terminal status; an in-flight call failing because of it must not overwrite it
    st.update(kw, updated_at=time.time(), elapsed_s=round(time.time() - T0, 1))
    if STATUS:
        try:
            tmp = STATUS + ".tmp"; Path(tmp).write_text(json.dumps(st)); os.replace(tmp, STATUS)
        except Exception:
            pass


def cleanup(reason):
    global _done
    with _lock:
        if _done:
            _cleaned.wait(90)  # another thread is stopping the box; do not exit underneath it
            return
        _done = True
    box_s = None
    if _box is not None:
        try:
            ws = json.loads(_box.run(f"cat {SPOOL}/status.json", quiet=True, timeout=20)["out"])
            st.update(remote_vmhwm_bytes=ws.get("vmhwm_bytes"), remote_served=ws.get("served"))
        except Exception:
            pass
        try: _box.write(SPOOL + "/stop", "1")
        except Exception: pass
        try:
            rec = _box.stop()
            if isinstance(rec, dict) and rec.get("startedAt"):
                box_s = round((time.time() * 1000 - rec["startedAt"]) / 1000, 1)
        except Exception as e:
            log(f"box stop error: {e}")
    if box_s is None and st.get("box_started_at"):
        box_s = round(time.time() - st["box_started_at"], 1)
    put(_final=True, state=st.get("state") if st.get("state") in ("failed", "cancelled") else "stopped", stop_reason=reason, box_seconds=box_s,
        core_seconds=round((box_s or 0) * CPU, 1), box_stopped=True)
    log(f"box stopped ({reason}); box time {box_s}s, {st['calls']} inferences")
    _cleaned.set()


def die(reason, code=0):
    cleanup(reason)
    os._exit(code)


def _alive(pid):
    try:  # a zombie (killed, not yet reaped by run_proc) counts as gone
        return open(f"/proc/{pid}/stat").read().rsplit(")", 1)[1].split()[0] not in ("Z", "X")
    except (FileNotFoundError, ProcessLookupError, IndexError):
        return False


def watch_parent(ppid):
    while not _done:
        if not _alive(ppid):
            put(state="cancelled", reason="pipeline process exited"); die("pipeline process exited")
        if time.time() - T0 > CAP_S:
            put(state="failed", reason="job wall cap reached"); die("job wall cap reached")
        time.sleep(0.5)


def rd(n):
    b = b""
    while len(b) < n:
        c = sys.stdin.buffer.read(n - len(b))
        if not c:
            return None
        b += c
    return b


def recv():
    h = rd(4)
    if h is None: return None, None
    head = json.loads(rd(struct.unpack(">I", h)[0]))
    n = struct.unpack(">Q", rd(8))[0]
    return head, (rd(n) if n else b"")


def send(head, payload=b""):
    hb = json.dumps(head).encode()
    if _done:
        return
    sys.stdout.buffer.write(struct.pack(">I", len(hb)) + hb + struct.pack(">Q", len(payload)) + payload); sys.stdout.buffer.flush()


def start(model_name):
    global _box
    from mana_compute import compute, ComputeError
    want = PINNED[model_name]
    put(state="provisioning")
    t = time.time()
    _box = compute.shell(description=f"RealiSIM preprocessing (BiRefNet cutout) job {JOB}; auto-stops with the job",
                         name=f"realisim-pre-{uuid.uuid4().hex[:8]}", cpu=CPU, memory=MEM, image=IMAGE,
                         ttl=int(CAP_S + 300), idle="5m")
    put(box=_box.id, box_started_at=time.time())
    log(f"box {_box.name} started ({CPU:g} cores, {MEM:g} GiB) in {time.time() - t:.1f}s")
    q = dict(quiet=True)
    model = f"/opt/realisim/models/{model_name}"
    script = ("set -e; python -c 'import onnxruntime' 2>/dev/null || pip install -q --no-cache-dir " + PIP + " >/dev/null 2>&1; "
              f"mkdir -p /opt/realisim/models {SPOOL}; test -f {model} || python -c \"import urllib.request;"
              f"urllib.request.urlretrieve('{MODEL_URL}','{model}.part')\" && (test -f {model} || mv {model}.part {model}); "
              "python -c 'import onnxruntime,numpy;print(onnxruntime.__version__,numpy.__version__)'")
    r = _box.run(script, timeout=600, **q)
    if r["exit"] != 0 or "1.23.2 2.2.6" not in r["out"]:
        raise RuntimeError("box provisioning failed: " + (r["err"] or r["out"])[-300:])
    _box.write("/opt/realisim/ort_worker.py", (HERE / "ort_worker.py").read_text())
    _box.write("/opt/realisim/ort_wait.py", (HERE / "ort_wait.py").read_text())
    job = _box.run(f"python /opt/realisim/ort_worker.py {model} {want} {THREADS} {SPOOL} 300 {int(CAP_S)} ", background=True)
    wlog = job.get("log") if isinstance(job, dict) else None
    for i in range(240):
        if i and i % 10 == 0 and wlog:
            ex = _box.run(f"cat {wlog}.exit 2>/dev/null; tail -c 600 {wlog} 2>/dev/null", **q)["out"]
            if ex.strip()[:1].isdigit():
                raise RuntimeError("remote worker exited: " + ex.strip()[-500:])
        r = _box.run(f"cat {SPOOL}/status.json 2>/dev/null", **q)
        s = json.loads(r["out"]) if r["out"].strip().startswith("{") else {}
        if s.get("state") == "ready":
            put(state="ready", provision_s=round(time.time() - t, 1), remote=s.get("meta"))
            log(f"session ready on box in {time.time() - t:.1f}s (model sha256 verified, onnxruntime {s['meta']['ort']})")
            return s["meta"]
        if s.get("state") in ("failed", "stopped"):
            raise RuntimeError("remote worker " + s["state"] + ": " + str(s.get("reason")))
        time.sleep(0.5)
    raise RuntimeError("remote session did not become ready")


def main():
    ppid = int(sys.argv[1])
    for sig in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP):
        signal.signal(sig, lambda s, f: die(f"signal {s}"))
    threading.Thread(target=watch_parent, args=(ppid,), daemon=True).start()
    head, _ = recv()
    if head is None:
        die("no request")
    try:
        meta = start(head["model"])
        send(dict(ok=True, meta=meta, box=_box.id))
    except Exception as e:
        reason = getattr(e, "args", [str(e)])[0] if e.args else str(e)
        put(state="failed", reason=str(reason)[:500])
        send(dict(ok=False, error=f"{type(e).__name__}: {str(reason)[:400]}"))
        die("start failed")
    prev = ""
    while True:
        head, payload = recv()
        if head is None:
            time.sleep(0.3)
            if _alive(ppid):
                die("pipeline finished")
            put(state="cancelled", reason="pipeline process exited"); die("pipeline process exited")
        if st["calls"] >= MAX_CALLS:
            put(state="failed", reason="per-job inference cap reached"); send(dict(ok=False, error="per-job inference cap reached")); die("call cap")
        rid = f"r{st['calls']:04d}"
        t = time.time()
        try:
            _box.write(f"{SPOOL}/{rid}.req.tmp", payload)
            r = _box.run(f"python /opt/realisim/ort_wait.py {SPOOL} {rid} {INFER_TIMEOUT}" + (f"; rm -f {SPOOL}/{prev}.res.npz" if prev else ""),
                         timeout=INFER_TIMEOUT + 60, quiet=True)
            res = json.loads(r["out"].strip().splitlines()[0])
            if not res.get("ok"):
                raise RuntimeError(res.get("error"))
            data = _box.read(f"{SPOOL}/{rid}.res.npz", binary=True)
        except Exception as e:
            put(state="failed", reason=f"inference {rid}: {e}"[:500]); send(dict(ok=False, error=f"remote inference failed: {e}"[:500])); die("inference failed")
        prev = rid
        st["calls"] += 1
        put(state="running", last_round_trip_s=round(time.time() - t, 2), last_remote_wait_s=res.get("wait_s"))
        send(dict(ok=True, round_trip_s=round(time.time() - t, 2), wait_s=res.get("wait_s")), data)


if __name__ == "__main__":
    try:
        main()
    except BaseException as e:  # e.g. BrokenPipe after the pipeline died mid-inference: still finish cleanup
        if not isinstance(e, SystemExit):
            put(state="cancelled" if isinstance(e, (BrokenPipeError, ConnectionError)) else "failed", reason=f"{type(e).__name__}: {e}"[:300])
        die("broker exiting")
