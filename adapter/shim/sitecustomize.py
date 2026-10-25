
"""Photo2Grasp web backend shim (NOT part of the repo; on PYTHONPATH only for pipeline runs).

The sandbox has an 8 GB memory cap. rembg's BiRefNet session on onnxruntime grows its CPU memory arena with every
frame it cuts out (24 candidates per video), which crossed the cap. This patches onnxruntime.InferenceSession when
it is first imported: no CPU arena, no memory-pattern planning, at most 4 intra-op threads. Results are identical.
"""
import os, sys
import json as _json
_THREADS = int(os.environ.get("P2G_ORT_THREADS", "4"))

def _patch(ort):
    Orig = ort.InferenceSession
    if getattr(Orig, "_p2g_lean", False):
        return
    class LeanSession(Orig):
        _p2g_lean = True
        def __new__(cls, path_or_bytes=None, *args, **kwargs):
            # Opt-in remote execution of a pinned model (REALISIM_REMOTE_PREPROCESS=1, set by the backend per job).
            name = os.path.basename(str(path_or_bytes)) if isinstance(path_or_bytes, (str, os.PathLike)) else None
            if _REMOTE_ON and name in _REMOTE_MODELS:
                return _RemoteSession(str(path_or_bytes), name)
            return super().__new__(cls)
        def __init__(self, path_or_bytes, sess_options=None, *args, **kwargs):
            so = sess_options if sess_options is not None else ort.SessionOptions()
            try:
                so.enable_cpu_mem_arena = False
                so.enable_mem_pattern = False
                so.intra_op_num_threads = _THREADS
                so.inter_op_num_threads = 1
            except Exception:
                pass
            super().__init__(path_or_bytes, so, *args, **kwargs)
    ort.InferenceSession = LeanSession

class _Hook:
    def find_spec(self, name, path=None, target=None):
        if name != "onnxruntime":
            return None
        sys.meta_path.remove(self)
        import importlib.util
        spec = importlib.util.find_spec(name)
        if spec is None or spec.loader is None:
            return spec
        orig = spec.loader.exec_module
        def exec_module(module):
            orig(module)
            try:
                _patch(module)
            except Exception as e:
                print("[p2g shim] could not patch onnxruntime:", e, file=sys.stderr)
        spec.loader.exec_module = exec_module
        return spec

sys.meta_path.insert(0, _Hook())


# ── Hugging Face Space access through Mana symbolic proxies (set by the backend per run) ──────────────────────
# P2G_HF_PROXY = {"<host>": {"GET": {"url": ..., "headers": {...}}, "POST": {...}}}. The proxies inject the user's
# saved HF_TOKEN server-side; this process never sees the token.
import json as _json
_PX = _json.loads(os.environ.get("P2G_HF_PROXY") or "null") or {}


def _rewrite(request, httpx):
    route = _PX.get(request.url.host, {}).get(request.method)
    if not route:
        return
    base = httpx.URL(route["url"])
    request.url = base.copy_with(path=base.path.rstrip("/") + request.url.path, query=request.url.query or None)
    request.headers["Host"] = base.netloc.decode()
    for k, v in route["headers"].items():
        request.headers[k] = v


def _patch_httpx(httpx):
    if getattr(httpx, "_p2g_px", False):
        return
    sync, asyn = httpx.HTTPTransport.handle_request, httpx.AsyncHTTPTransport.handle_async_request
    def handle(self, request):
        _rewrite(request, httpx)
        return sync(self, request)
    async def handle_async(self, request):
        _rewrite(request, httpx)
        return await asyn(self, request)
    httpx.HTTPTransport.handle_request = handle
    httpx.AsyncHTTPTransport.handle_async_request = handle_async
    httpx._p2g_px = True


class _HttpxHook:
    def find_spec(self, name, path=None, target=None):
        if name != "httpx" or not _PX:
            return None
        sys.meta_path.remove(self)
        import importlib.util
        spec = importlib.util.find_spec(name)
        orig = spec.loader.exec_module
        def exec_module(module):
            orig(module)
            _patch_httpx(module)
        spec.loader.exec_module = exec_module
        return spec


if _PX:
    sys.meta_path.insert(0, _HttpxHook())


# ── Opt-in remote preprocessing (REALISIM_REMOTE_PREPROCESS=1, set by the backend for one pipeline job) ─────────
# BiRefNet-general-lite's 7x7 deformable-conv decoder is exported as dense bilinear-gather tensors (decoder_block1:
# twenty-one 784 MiB intermediates), so one CPU inference needs more than 5 GiB of private memory, which cannot coexist
# with Elasticsearch + SpacetimeDB + the browser in the sandbox's 8 GiB. With the flag set, ONLY that pinned model's
# session.run() is executed by the same onnxruntime 1.23.2, same model bytes (sha256 checked here and on the box) and
# the same session options in one ephemeral Mana compute box per job (adapter/remote_preprocess/broker.py). rembg's
# preprocessing, mask normalisation, resizing and compositing stay in this process, unchanged. No local fallback.
_REMOTE_ON = os.environ.get("REALISIM_REMOTE_PREPROCESS") == "1"
_REMOTE_MODELS = {"birefnet-general-lite.onnx": "5600024376f572a557870a5eb0afb1e5961636bef4e1e22132025467d0f03333"}
_BROKER = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "remote_preprocess", "broker.py")
_BROKER_PY = os.environ.get("REALISIM_REMOTE_BROKER_PYTHON", "/workspace/.venv/bin/python")


class _Arg:
    def __init__(self, d):
        self.name, self.shape, self.type = d["name"], d["shape"], d["type"]


class _RemoteSession:
    _p2g_remote = True

    def __init__(self, path, name):
        import hashlib, subprocess, atexit
        h = hashlib.sha256()
        with open(path, "rb") as f:
            for b in iter(lambda: f.read(1 << 20), b""):
                h.update(b)
        if h.hexdigest() != _REMOTE_MODELS[name]:
            raise RuntimeError(f"RealiSIM remote preprocessing: {name} is not the pinned model (sha256 mismatch)")
        env = {k: v for k, v in os.environ.items() if k not in ("PYTHONPATH", "P2G_HF_PROXY", "VIRTUAL_ENV", "PYTHONHOME")}
        env["PYTHONUNBUFFERED"] = "1"
        self._p = subprocess.Popen([_BROKER_PY, _BROKER, str(os.getpid())], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                   env=env, start_new_session=True)
        atexit.register(self.close)
        self._send({"model": name})
        head, _ = self._recv()
        if not head or not head.get("ok"):
            self.close()
            raise RuntimeError("RealiSIM remote preprocessing unavailable: " + str((head or {}).get("error", "broker exited")))
        meta = head["meta"]
        self._in = [_Arg(d) for d in meta["inputs"]]
        self._out = [_Arg(d) for d in meta["outputs"]]
        self.remote_meta = meta

    def _send(self, head, payload=b""):
        import struct
        hb = _json.dumps(head).encode()
        self._p.stdin.write(struct.pack(">I", len(hb)) + hb + struct.pack(">Q", len(payload)) + payload)
        self._p.stdin.flush()

    def _rd(self, n):
        b = b""
        while len(b) < n:
            c = self._p.stdout.read(n - len(b))
            if not c:
                return None
            b += c
        return b

    def _recv(self):
        import struct
        h = self._rd(4)
        if h is None:
            return None, None
        head = _json.loads(self._rd(struct.unpack(">I", h)[0]))
        n = struct.unpack(">Q", self._rd(8))[0]
        return head, (self._rd(n) if n else b"")

    def get_inputs(self):
        return self._in

    def get_outputs(self):
        return self._out

    def get_providers(self):
        return ["CPUExecutionProvider"]

    def run(self, output_names, input_feed, run_options=None):
        import io
        import numpy as np
        buf = io.BytesIO()
        arrays = dict(input_feed)
        if output_names:
            arrays["__output_names__"] = np.array(list(output_names))
        np.savez(buf, **arrays)
        try:
            self._send({"op": "run"}, buf.getvalue())
            head, data = self._recv()
        except (BrokenPipeError, OSError) as e:
            raise RuntimeError(f"RealiSIM remote preprocessing failed: broker unavailable ({e})")
        if not head or not head.get("ok"):
            raise RuntimeError("RealiSIM remote preprocessing failed: " + str((head or {}).get("error", "broker exited")))
        with np.load(io.BytesIO(data), allow_pickle=False) as z:
            names = list(output_names) if output_names else [o.name for o in self._out]
            return [z[n] for n in names]

    def close(self):
        p = getattr(self, "_p", None)
        if p and p.poll() is None:
            try:
                p.stdin.close()
            except Exception:
                pass
            try:
                p.wait(timeout=60)
            except Exception:
                pass


# ── Headless rendering for upstream previews (added for upstream cfa0398; still required at 166a8ed) ───────────────────────────────────
# make_asset.py now ends with pipeline/preview.py (an offscreen mujoco.Renderer). The backend daemon has no DISPLAY
# and no MUJOCO_GL, so MuJoCo cannot create a GL context and make_asset exits non-zero after writing the asset.
# EGL is verified to work in this sandbox. Only fills the variable when nothing is configured.
if not os.environ.get("MUJOCO_GL") and not os.environ.get("DISPLAY"):
    os.environ["MUJOCO_GL"] = "egl"


# ── Memory-bounded, result-identical inside_any for pipeline/mesh_to_mjcf.py (added for upstream cfa0398; still required at 166a8ed) ─────
# Upstream evaluates (200k surface points x every hull facet) as dense float64 temporaries: ~3 GB per call for a
# 256-section cylinder, which cannot coexist with the API, SpacetimeDB and Elastic in the 8 GiB sandbox. This
# evaluates the SAME expression over row chunks of points; each point's result depends only on its own row, so
# the booleans (and every collision_accuracy number) are unchanged. Applied only when the upstream function
# source matches the reviewed revision exactly; any other upstream version is left untouched.
_INSIDE_ANY_SHA = "f1dfbc33d0737b7bc50820504c2ba0ec6bb89ffaee62770eb1af86b3829587e3"
_INSIDE_ROWS_BYTES = 64 * 1024 * 1024


def _inside_any_chunked(points, hulls, tol):
    import numpy as np
    inside = np.zeros(len(points), bool)
    if not len(points):
        return inside
    widest = max((len(eq) for eq in hulls), default=1) or 1
    rows = max(1024, _INSIDE_ROWS_BYTES // (8 * widest))
    for s in range(0, len(points), rows):
        block = points[s:s + rows]
        acc = inside[s:s + rows]
        for eq in hulls:
            acc |= np.all(block @ eq[:, :3].T + eq[:, 3] <= tol, axis=1)
    return inside


def _patch_mesh_to_mjcf(mod):
    import hashlib, inspect
    fn = getattr(mod, "inside_any", None)
    if fn is None:
        return
    try:
        ok = hashlib.sha256(inspect.getsource(fn).strip().encode()).hexdigest() == _INSIDE_ANY_SHA
    except (OSError, TypeError):
        ok = False
    if ok:
        mod.inside_any = _inside_any_chunked
        mod._realisim_inside_any = "chunked"


class _MeshHook:
    def find_spec(self, name, path=None, target=None):
        if name != "mesh_to_mjcf":
            return None
        sys.meta_path.remove(self)
        import importlib.util
        spec = importlib.util.find_spec(name)
        if spec is None or spec.loader is None:
            return spec
        orig = spec.loader.exec_module
        def exec_module(module):
            orig(module)
            try:
                _patch_mesh_to_mjcf(module)
            except Exception as e:
                print("[p2g shim] could not bound mesh_to_mjcf.inside_any:", e, file=sys.stderr)
        spec.loader.exec_module = exec_module
        return spec


sys.meta_path.insert(0, _MeshHook())
