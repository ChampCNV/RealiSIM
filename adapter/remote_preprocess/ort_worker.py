"""RealiSIM remote preprocessing worker (runs INSIDE an ephemeral Mana compute box).

Holds ONE onnxruntime session for the job's model and serves requests from a spool directory until told to stop,
idle for too long, or past its hard wall cap. It only runs the model graph: rembg pre/post-processing stays in the
RealiSIM pipeline process. Session options mirror adapter/shim/sitecustomize.py LeanSession exactly.
"""
import hashlib, json, os, sys, time, glob, io
import numpy as np
import onnxruntime as ort

model, want_sha, threads, spool, idle_exit, max_s = sys.argv[1], sys.argv[2], int(sys.argv[3]), sys.argv[4], float(sys.argv[5]), float(sys.argv[6])
t0 = time.time()
os.makedirs(spool, exist_ok=True)
def status(**kw):
    import resource
    hwm = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss * 1024  # works where /proc lacks VmHWM (sandboxed kernels)
    d = dict(kw, pid=os.getpid(), vmhwm_bytes=hwm, uptime_s=round(time.time() - t0, 2), at=time.time())
    tmp = spool + "/status.json.tmp"; open(tmp, "w").write(json.dumps(d)); os.replace(tmp, spool + "/status.json")
h = hashlib.sha256()
with open(model, "rb") as f:
    for b in iter(lambda: f.read(1 << 20), b""): h.update(b)
if h.hexdigest() != want_sha:
    status(state="failed", reason="model sha256 mismatch"); sys.exit(3)
so = ort.SessionOptions()
so.enable_cpu_mem_arena = False
so.enable_mem_pattern = False
so.intra_op_num_threads = threads
so.inter_op_num_threads = 1
sess = ort.InferenceSession(model, so, providers=["CPUExecutionProvider"])
meta = dict(inputs=[dict(name=i.name, shape=[d if isinstance(d, int) else str(d) for d in i.shape], type=i.type) for i in sess.get_inputs()],
            outputs=[dict(name=o.name, shape=[d if isinstance(d, int) else str(d) for d in o.shape], type=o.type) for o in sess.get_outputs()],
            ort=ort.__version__, numpy=np.__version__, model_sha256=want_sha, load_s=round(time.time() - t0, 2))
open(spool + "/meta.json", "w").write(json.dumps(meta))
served, last = 0, time.time()
status(state="ready", served=0, meta=meta)
while True:
    if os.path.exists(spool + "/stop"): status(state="stopped", served=served, reason="stop requested"); break
    if time.time() - last > idle_exit: status(state="stopped", served=served, reason="idle"); break
    if time.time() - t0 > max_s: status(state="stopped", served=served, reason="wall cap"); break
    reqs = sorted(glob.glob(spool + "/*.req.npz"))
    if not reqs: time.sleep(0.05); continue
    for rq in reqs:
        rid = os.path.basename(rq)[:-len(".req.npz")]
        s = time.time()
        try:
            with np.load(rq, allow_pickle=False) as z:
                feed = {k: z[k] for k in z.files if k != "__output_names__"}
                names = [str(x) for x in z["__output_names__"]] if "__output_names__" in z.files else None
            outs = sess.run(names or None, feed)
            onames = names or [o.name for o in sess.get_outputs()]
            buf = io.BytesIO(); np.savez(buf, **{n: o for n, o in zip(onames, outs)})
            tmp = f"{spool}/{rid}.res.tmp"; open(tmp, "wb").write(buf.getvalue()); os.replace(tmp, f"{spool}/{rid}.res.npz")
        except Exception as e:
            open(f"{spool}/{rid}.err", "w").write(f"{type(e).__name__}: {e}"[:2000])
        finally:
            try: os.remove(rq)
            except FileNotFoundError: pass
        served += 1; last = time.time()
        status(state="ready", served=served, last_infer_s=round(time.time() - s, 2))
