#!/usr/bin/env python
"""workflow_layout: video of a robot workcell -> table-frame + robot-base-frame object layout, with evidence.

Contract: /workspace/photo2grasp/workflow-layout-contract.json   (python adapter/workflow_layout.py schema)
Runs the upstream scene pipeline (scene/reconstruct.py:reconstruct, scene/find_objects.py) with time / memory budgets,
then derives a reviewable layout. Robot base pose only ever comes from an explicit anchor; 2D (Claude) boxes only
name reconstructed clusters; unknowns are reported as blocking items rather than filled in.
"""
import argparse, fcntl, hashlib, importlib.util, json, math, os, re, subprocess, sys, time
from pathlib import Path

import numpy as np

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import workflow_layout_geom as G  # noqa: E402

ROOT = HERE.parent
DEFAULT_REPO = ROOT / "main"
LOCK_FILE = ROOT / "cache" / "workflow_layout.lock"
FORMAT = "workflow-layout-v1"
TAG_DICTS = {"tag36h11": "DICT_APRILTAG_36h11", "tag25h9": "DICT_APRILTAG_25h9", "tag16h5": "DICT_APRILTAG_16h5"}
SLUG = re.compile(r"^[a-z][a-z0-9_]{0,63}$")
ROBOT_HEIGHT_HINT_M = 0.18      # unassigned clusters taller than this are flagged as possible robots/fixtures


class Budget(Exception):
    pass


class Fail(Exception):
    def __init__(self, code, message, fields=None, stage=None, details=None):
        super().__init__(message)
        self.code, self.message, self.fields, self.stage, self.details = code, message, fields or [], stage, details


def log(*a):
    print(*a, flush=True)


def sha256_file(p, limit=None):
    h = hashlib.sha256()
    with open(p, "rb") as f:
        for b in iter(lambda: f.read(1 << 20), b""):
            h.update(b)
    return h.hexdigest()


def sha_json(o):
    return hashlib.sha256(json.dumps(o, sort_keys=True).encode()).hexdigest() if o is not None else None


def rnd(x, n=4):
    if x is None:
        return None
    if isinstance(x, (list, tuple, np.ndarray)):
        return [rnd(v, n) for v in x]
    return round(float(x), n)


def repo_commit(repo):
    try:
        return subprocess.run(["git", "-C", str(repo), "rev-parse", "HEAD"], capture_output=True, text=True, timeout=10).stdout.strip() or None
    except Exception:
        return None


# ── budgets ─────────────────────────────────────────────────────────────────────────────────
def tree_rss_mb(pid):
    import psutil
    try:
        p = psutil.Process(pid)
        procs = [p] + p.children(recursive=True)
        return sum(q.memory_info().rss for q in procs if q.is_running()) / 1048576
    except Exception:
        return 0.0


def run_limited(cmd, cwd, deadline, max_rss_mb, label, env=None):
    """Run a heavy child; kill it (and its tree) past the wall deadline or the RSS cap. Returns (code, tail, peak_mb)."""
    import psutil
    e = dict(os.environ, OMP_NUM_THREADS=os.environ.get("OMP_NUM_THREADS", "4"), PYTHONUNBUFFERED="1")
    e.update(env or {})
    p = subprocess.Popen(cmd, cwd=str(cwd), stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, env=e)
    os.set_blocking(p.stdout.fileno(), False)
    tail, peak, reason = [], 0.0, None
    while True:
        try:
            chunk = p.stdout.read()
        except Exception:
            chunk = None
        if chunk:
            for line in chunk.splitlines():
                tail.append(line)
                if len(line) < 400:
                    log(f"  [{label}] {line}")
            tail = tail[-80:]
        if p.poll() is not None:
            break
        rss = tree_rss_mb(p.pid)
        peak = max(peak, rss)
        if rss > max_rss_mb:
            reason = f"{label}: memory {rss:.0f} MB exceeded the {max_rss_mb} MB cap"
        elif time.time() > deadline:
            reason = f"{label}: exceeded the time budget"
        if reason:
            try:
                for q in psutil.Process(p.pid).children(recursive=True):
                    q.kill()
            except Exception:
                pass
            p.kill()
            p.wait()
            raise Budget(reason)
        time.sleep(0.4)
    rest = p.stdout.read() or ""
    tail += rest.splitlines()
    return p.returncode, "\n".join(tail[-80:]), peak


# ── stage 1: frames (streaming equivalent of upstream extract_frames) ─────────────────────────
def extract_frames(video, out_dir, n_frames, max_px, deadline):
    """Sharpest frame per window, as upstream reconstruct.extract_frames, but decoding one frame at a time
    (upstream holds every decoded frame in RAM: ~6 GB for 32 s of 1080p, over this sandbox's budget)."""
    import cv2
    out_dir.mkdir(parents=True, exist_ok=True)
    for f in out_dir.glob("*.jpg"):
        f.unlink()
    cap = cv2.VideoCapture(str(video))
    if not cap.isOpened():
        raise Fail("video_unreadable", "OpenCV cannot open the video", ["video"], "frames")
    total = int(cap.get(cv2.CAP_PROP_FRAME_COUNT) or 0)
    fps = float(cap.get(cv2.CAP_PROP_FPS) or 0) or 30.0
    if total < 10:
        raise Fail("video_too_short", f"video reports {total} frames", ["video"], "frames")
    windows = np.array_split(np.arange(total), min(n_frames, total))
    starts = np.array([w[0] for w in windows])
    best = {}
    kf = []
    i = 0
    cur = 0

    def flush(k):
        if k in best:
            _, img, idx, t = best.pop(k)
            name = f"{k:04d}.jpg"
            cv2.imwrite(str(out_dir / name), img, [cv2.IMWRITE_JPEG_QUALITY, 95])
            kf.append(dict(name=name, video_frame=int(idx), time_s=round(t, 4)))

    while True:
        ok, f = cap.read()
        if not ok:
            break
        t = (cap.get(cv2.CAP_PROP_POS_MSEC) or 0) / 1000.0 or i / fps
        k = int(np.searchsorted(starts, i, side="right") - 1)
        if k != cur:
            flush(cur)
            cur = k
        h, w = f.shape[:2]
        s = min(1.0, max_px / max(h, w))
        if s < 1:
            f = cv2.resize(f, (int(round(w * s)), int(round(h * s))), interpolation=cv2.INTER_AREA)
        sharp = cv2.Laplacian(cv2.cvtColor(f, cv2.COLOR_BGR2GRAY), cv2.CV_64F).var()
        if k not in best or sharp > best[k][0]:
            best[k] = (sharp, f, i, t)
        i += 1
        if i % 100 == 0 and time.time() > deadline:
            raise Budget("frame extraction exceeded the time budget")
    flush(cur)
    cap.release()
    return dict(frames=kf, decoded=i, reported_total=total, fps=fps, width=int(w), height=int(h))


def load_upstream(repo, rel, name):
    spec = importlib.util.spec_from_file_location(name, str(Path(repo) / rel))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def child_reconstruct(repo, images, work):
    """Hidden subcommand: call upstream scene/reconstruct.py:reconstruct exactly."""
    rec_mod = load_upstream(repo, "scene/reconstruct.py", "p2g_reconstruct")
    try:
        rec = rec_mod.reconstruct(Path(images), Path(work))
    except ValueError as e:      # max() of an empty dict: no model at all
        print("RECON_EMPTY " + str(e))
        sys.exit(5)
    print(f"RECON_OK registered={rec.num_reg_images()} points={rec.num_points3D()}")


# ── reconstruction access ──────────────────────────────────────────────────────────────────
class Recon:
    def __init__(self, work):
        import pycolmap
        self.rec = pycolmap.Reconstruction(str(Path(work) / "sparse"))
        self.pid = np.array(sorted(self.rec.points3D.keys()))
        self.P = np.array([self.rec.points3D[i].xyz for i in self.pid]) if len(self.pid) else np.zeros((0, 3))
        self.index = {int(p): k for k, p in enumerate(self.pid)}
        self.images = sorted(self.rec.images.values(), key=lambda im: im.name)
        self.by_name = {im.name: im for im in self.images}

    def mean_reproj(self):
        try:
            return float(self.rec.compute_mean_reprojection_error())
        except Exception:
            return None

    def project(self, im, X):
        """World points -> pixels in image im; returns (uv (N,2), in_front mask (N,))."""
        cam = self.rec.cameras[im.camera_id]
        M = np.asarray(im.cam_from_world().matrix())
        pc = np.c_[X, np.ones(len(X))] @ M.T
        front = pc[:, 2] > 1e-9
        uv = np.full((len(X), 2), np.nan)
        if front.any():
            uv[front] = np.asarray(cam.img_from_cam(pc[front]))[:, :2]
        return uv, front

    def ray(self, im, px):
        cam = self.rec.cameras[im.camera_id]
        r = np.asarray(cam.cam_ray_from_img(np.asarray(px, float)), float).reshape(-1)[:3]
        R = np.asarray(im.cam_from_world().rotation.matrix())
        dv = R.T @ r
        return np.asarray(im.projection_center(), float), dv / np.linalg.norm(dv)

    def size(self, im):
        cam = self.rec.cameras[im.camera_id]
        return int(cam.width), int(cam.height)


# ── stage 3 helpers: table plane / scale when upstream cannot (no visible floor) ─────────────
def mirror_table(R, L, W, k, fo):
    """Upstream find_objects steps 1 and 3 with a given scale k (metres per unit): table plane selection identical,
    rectangle fit identical in result but vectorised (prefix sums over a 1 cm grid). Returns scene.json-shaped dict."""
    import open3d as o3d
    rec, P = R.rec, R.P
    cams = np.array([im.projection_center() for im in rec.images.values()])
    s = np.median(np.linalg.norm(P - np.median(P, 0), axis=1))
    target = fo.look_at_point(rec)
    up = -np.mean([im.cam_from_world().rotation.matrix()[1] for im in rec.images.values()], 0)
    up /= np.linalg.norm(up)
    best, cands = None, []
    view = np.mean([im.cam_from_world().rotation.matrix()[2] for im in rec.images.values()], 0)
    view /= np.linalg.norm(view)
    # Same plane budget as upstream find_objects.py main() (n_planes=30 since cfa0398; walls and chair backs can
    # outnumber a plain tabletop). planes() accepts n_planes in every supported upstream revision.
    for n, d, inl in fo.planes(P, np.arange(len(P)), 0.008 * s, n_planes=30):
        if np.mean(cams @ n + d) < 0:
            n, d = -n, -d
        ang = float(np.degrees(np.arccos(np.clip(n @ up, -1, 1))))
        above = float(np.mean(np.sign(cams @ n + d) > 0))
        horizontal = ang < 45
        cands.append(dict(points=int(len(inl)), angle_to_image_up_deg=round(ang, 1), cameras_above_frac=round(above, 2),
                          angle_to_mean_view_dir_deg=round(float(np.degrees(np.arccos(np.clip(abs(n @ view), -1, 1)))), 1),
                          rejected=None if horizontal and np.mean(np.sign(cams @ n + d)) >= 0.9 else ("not horizontal (>45 deg from average image-up)" if not horizontal else "cameras on both sides")))
        if not horizontal or np.mean(np.sign(cams @ n + d)) < 0.9:
            continue
        dist = abs(target @ n + d)
        if best is None or dist < best[0]:
            best = (dist, n, d, inl)
    if best is None:
        raise Fail("table_not_found", "no plane passes find_objects' table test (horizontal w.r.t. the average phone 'up', all cameras above it). "
                   "Typical causes: low-texture or glossy tabletop, close-up/steep downward filming, or a fragmented reconstruction.",
                   ["video: film the whole table from ~0.5-1 m in a slow loop, phone upright, include table edges and the floor"], "table",
                   details=dict(plane_candidates=cands, hint=("The largest plane faces the cameras' average viewing direction while lying "
                                "along the phone's 'up': the video was mostly filmed looking straight down. find_objects assumes an upright phone "
                                "tilted ~30-40 deg; refilm from the side of the table.") if cands and cands[0]["angle_to_mean_view_dir_deg"] < 35
                                and cands[0]["angle_to_image_up_deg"] > 60 else None))
    _, n, d, inl = best
    foot = target - (target @ n + d) * n
    u = np.cross(n, [1, 0, 0]) if abs(n[0]) < 0.9 else np.cross(n, [0, 1, 0])
    u /= np.linalg.norm(u)
    v = np.cross(n, u)
    out = dict(plane=dict(n=n.tolist(), d=float(d), origin=foot.tolist(), u=u.tolist(), v=v.tolist()), plane_inliers=inl, s=float(s))
    if k is None:
        return out
    h = (P @ n + d) * k
    uv = np.c_[(P - foot) @ u, (P - foot) @ v] * k
    near = np.where(np.abs(h) < 0.006)[0]
    pc = o3d.geometry.PointCloud(o3d.utility.Vector3dVector(np.c_[uv[near], np.zeros(len(near))]))
    lab = np.array(pc.cluster_dbscan(eps=0.03, min_points=5))
    if len(near) == 0 or (lab >= 0).sum() == 0:
        raise Fail("table_not_found", "no connected tabletop patch at the fitted scale", ["video"], "table")
    main = lab[np.argmin(np.linalg.norm(uv[near], axis=1))]
    top = near[lab == main]
    rect = fit_rectangle(uv[top], L, W)
    out["plane"].update(table_centre=rect["centre"].tolist(), table_yaw=rect["yaw"])
    out.update(scale_m_per_unit=k, table=dict(length=L, width=W, height=None, points_inside=rect["inside"], points_total=int(len(top))),
               top_idx=top)
    return out


def fit_rectangle(q2, L, W, step=0.01):
    """Same search as upstream (yaw 0..179 deg in 1 deg steps, centre grid over [-L/2,L/2) x [-W/2,W/2) in 1 cm steps),
    maximising tabletop points inside an L x W rectangle; counts via 2D prefix sums instead of a Python triple loop."""
    best = (-1, 0.0, 0.0, 0.0)
    nx, ny = int(round(L / step)), int(round(W / step))
    cxs = -L / 2 + step * np.arange(nx)
    cys = -W / 2 + step * np.arange(ny)
    for yaw in np.radians(np.arange(0, 180, 1.0)):
        Rm = np.array([[np.cos(yaw), -np.sin(yaw)], [np.sin(yaw), np.cos(yaw)]])
        q = q2 @ Rm
        # point counts per strict-inequality window; evaluate exactly with sorted searches per axis-grid (small grids)
        inx = (np.abs(q[:, 0][None, :] - cxs[:, None]) < L / 2)          # (nx, N)
        iny = (np.abs(q[:, 1][None, :] - cys[:, None]) < W / 2)          # (ny, N)
        counts = np.rint(inx.astype(np.float32) @ iny.T.astype(np.float32)).astype(np.int64)  # (nx, ny)
        i, j = np.unravel_index(np.argmax(counts), counts.shape)
        if counts[i, j] > best[0]:
            best = (int(counts[i, j]), float(yaw), float(cxs[i]), float(cys[j]))
    _, yaw, cx, cy = best
    Rm = np.array([[np.cos(yaw), -np.sin(yaw)], [np.sin(yaw), np.cos(yaw)]])
    return dict(inside=best[0], yaw=yaw, centre=Rm @ [cx, cy])


def footprint_scale(R, tb, L, W):
    """ESTIMATE: metres/unit from the tabletop patch's minimum-area rectangle vs the entered L x W.
    Only meaningful when all four table edges are in view; a truncated patch over-estimates the scale."""
    import cv2
    import open3d as o3d
    P = R.P
    n, d = np.array(tb["plane"]["n"]), tb["plane"]["d"]
    foot, u, v = (np.array(tb["plane"][k]) for k in ("origin", "u", "v"))
    inl = tb["plane_inliers"]
    uv = np.c_[(P[inl] - foot) @ u, (P[inl] - foot) @ v]
    nn = np.median(np.linalg.norm(uv[1:] - uv[:-1], axis=1)) if len(uv) > 2 else 0.01
    pc = o3d.geometry.PointCloud(o3d.utility.Vector3dVector(np.c_[uv, np.zeros(len(uv))]))
    lab = np.array(pc.cluster_dbscan(eps=float(max(nn, 1e-6)) * 3, min_points=5))
    if (lab >= 0).sum() < 30:
        return None
    main = lab[np.argmin(np.linalg.norm(uv, axis=1) + (lab < 0) * 1e9)]
    pts = uv[lab == main].astype(np.float32)
    (cx, cy), (a, b), ang = cv2.minAreaRect(pts)
    a, b = max(a, b), min(a, b)
    if b <= 0:
        return None
    k1, k2 = max(L, W) / a, min(L, W) / b
    k = 0.5 * (k1 + k2)
    return dict(m_per_unit=float(k), axis_disagreement=float(abs(k1 - k2) / k), patch_extent_units=[float(a), float(b)], n_points=int(len(pts)))


def detect_tag(R, family, tag_id, deadline):
    """Find one AprilTag in the registered keyframes and triangulate its 4 corners in the reconstruction frame."""
    import cv2
    if family not in TAG_DICTS:
        raise Fail("bad_tag_family", f"unsupported tag family {family}", ["alignment.scale_reference.tag_family"], "scale")
    prm = cv2.aruco.DetectorParameters()
    prm.cornerRefinementMethod = cv2.aruco.CORNER_REFINE_SUBPIX      # unrefined corners bias the edge length by ~1 px
    det = cv2.aruco.ArucoDetector(cv2.aruco.getPredefinedDictionary(getattr(cv2.aruco, TAG_DICTS[family])), prm)
    obs = [[] for _ in range(4)]
    seen = []
    for im in R.images:
        if time.time() > deadline:
            raise Budget("tag detection exceeded the time budget")
        img = cv2.imread(str(R.frames_dir / im.name), cv2.IMREAD_GRAYSCALE)
        if img is None:
            continue
        corners, ids, _ = det.detectMarkers(img)
        if ids is None:
            continue
        for c, i in zip(corners, ids.reshape(-1)):
            if int(i) == int(tag_id):
                seen.append(im.name)
                for k in range(4):
                    obs[k].append(R.ray(im, c.reshape(4, 2)[k]))
    if len(seen) < 2:
        return dict(found=False, views=seen)
    pts, rms = [], []
    for k in range(4):
        p, r = G.triangulate(obs[k])
        pts.append(p); rms.append(r)
    return dict(found=True, views=seen, corners_world=np.array(pts), ray_rms_units=float(np.mean(rms)))


def recompute_clusters(R, TF, L, W, upstream_objects):
    """find_objects' clustering (same thresholds) in the metric table frame, but keeping every cluster: tall ones and
    ones outside the fitted rectangle too, because a robot or fixture is exactly such a cluster."""
    import open3d as o3d
    Q = TF.to_table(R.P)
    h = Q[:, 2]
    foot_xy = TF.to_table(TF.foot[None])[0, :2]
    rad = np.linalg.norm(Q[:, :2] - foot_xy, axis=1)
    above = np.where((h > 0.004) & (h < 0.6) & (rad < max(L, W)))[0]
    if len(above) < 10:
        return [], Q
    pc = o3d.geometry.PointCloud(o3d.utility.Vector3dVector(np.c_[Q[above, :2], h[above]]))
    lab = np.array(pc.cluster_dbscan(eps=0.015, min_points=10))
    clusters = [above[lab == l] for l in sorted(set(lab)) if l >= 0]
    merged = []
    for c in sorted(clusters, key=len, reverse=True):
        cen = np.median(Q[c, :2], 0)
        for j in range(len(merged)):
            if np.linalg.norm(np.median(Q[merged[j], :2], 0) - cen) < 0.06:
                merged[j] = np.r_[merged[j], c]
                break
        else:
            merged.append(c)
    up_ids = {o["name"]: set(o.get("point_ids") or []) for o in upstream_objects}
    out = []
    for idx in sorted(merged, key=len, reverse=True):
        if len(idx) < 40 or np.percentile(h[idx], 2) >= 0.02:
            continue                     # same standing-on-the-table rule as upstream
        fp = G.footprint(Q[idx, :2])
        ids = set(int(R.pid[i]) for i in idx)
        up = max(up_ids.items(), key=lambda kv: len(kv[1] & ids), default=(None, set()))
        upstream = up[0] if up[0] and len(up[1] & ids) >= 0.5 * min(len(up[1]), len(ids)) else None
        cx, cy = fp["center"]
        out.append(dict(idx=idx, table_xy=np.array([cx, cy]), height=float(np.percentile(h[idx], 98)), footprint=fp,
                        n_points=int(len(idx)), upstream_object=upstream,
                        on_table_rect=bool(abs(cx) < L / 2 - 0.01 and abs(cy) < W / 2 - 0.01)))
    for i, c in enumerate(out):
        c["id"] = f"cluster_{i}"
    return out, Q


def cluster_views(R, c):
    """How well the video covers a cluster: frames that see all of it, and horizontal spread of those view directions."""
    X = R.P[c["idx"]]
    centre = np.median(X, 0)
    full, dirs = [], []
    for im in R.images:
        uv, front = R.project(im, X)
        w, h = R.size(im)
        if not front.all():
            continue
        lo, hi = np.nanpercentile(uv, 2, 0), np.nanpercentile(uv, 98, 0)
        if lo[0] < 5 or lo[1] < 5 or hi[0] > w - 5 or hi[1] > h - 5:
            continue
        full.append(im.name)
        dirs.append(np.asarray(im.projection_center()) - centre)
    return full, dirs


def project_box(R, im, X):
    uv, front = R.project(im, X)
    if front.sum() < max(5, 0.5 * len(X)):
        return None
    w, h = R.size(im)
    lo, hi = np.nanpercentile(uv[front], 2, 0), np.nanpercentile(uv[front], 98, 0)
    return [lo[0] / w, lo[1] / h, hi[0] / w, hi[1] / h]


# ── layout assembly ──────────────────────────────────────────────────────────────────────────
def nearest_keyframe(R, kf, sel_box):
    if sel_box.get("keyframe"):
        im = R.by_name.get(sel_box["keyframe"])
        return im, 0.0 if im else None
    t = float(sel_box.get("time_s", -1))
    reg = [k for k in kf if k["name"] in R.by_name]
    if not reg or t < 0:
        return None, None
    k = min(reg, key=lambda k: abs(k["time_s"] - t))
    return R.by_name[k["name"]], abs(k["time_s"] - t)


def build_layout(work, L, W, H, table_source, selections, alignment, ctx):
    """Everything after COLMAP: frames + scale + clusters + names + anchors -> layout dict."""
    work = Path(work)
    blocking, warnings = [], list(ctx.get("warnings", []))
    R = Recon(work)
    R.frames_dir = work / "frames"
    kf = json.loads((work / "keyframes.json").read_text())["frames"] if (work / "keyframes.json").exists() else []
    alignment = alignment or {}
    selections = selections or {}
    fo = load_upstream(ctx["repo"], "scene/find_objects.py", "p2g_find_objects")
    recon = dict(ok=True, frames_extracted=len(kf) or None, frames_registered=len(R.images), points3d=int(len(R.P)),
                 mean_reproj_px=rnd(R.mean_reproj(), 3), source="upstream scene/reconstruct.py:reconstruct (pycolmap)", **ctx.get("recon_extra", {}))
    if len(R.images) < 10 or len(R.P) < 500:
        raise Fail("reconstruction_weak", f"only {len(R.images)} frames / {len(R.P)} points registered", ["video: film slowly, full loop, more overlap"], "reconstruct")
    if kf and len(R.images) < 0.4 * len(kf):
        warnings.append(f"only {len(R.images)}/{len(kf)} frames registered: parts of the workcell may be missing")

    # scale + table: upstream result first, then a tag, then (estimate) the table footprint
    up = json.loads((work / "scene.json").read_text()) if (work / "scene.json").exists() and ctx.get("upstream_ok") else None
    scale = dict(method="none", source="none", m_per_unit=None, rel_uncertainty_est=None, evidence={}, cross_checks=[])
    tb, tag_info = None, None
    sref = alignment.get("scale_reference")
    if sref and sref.get("method") == "tag":
        tag_info = detect_tag(R, sref.get("tag_family", "tag36h11"), sref.get("tag_id", 0), ctx["deadline"])
    if up:
        tb = dict(plane=up["plane"], scale_m_per_unit=up["scale_m_per_unit"], table=up["table"])
        k = float(up["scale_m_per_unit"])
        scale.update(method="floor_drop", source="reconstructed", m_per_unit=k,
                     evidence=dict(rule="find_objects.py: k = entered table height / (tabletop plane to parallel floor plane distance)",
                                   table_height_m=H, table_height_source=table_source))
    if tag_info and tag_info["found"]:
        tp = G.tag_pose(tag_info["corners_world"])
        k_tag = float(sref["tag_size_m"]) / tp["edge"]
        ev = dict(views=len(tag_info["views"]), edge_units=tp["edge"], edge_spread_rel=tp["edge_spread"] / tp["edge"], ray_rms_units=tag_info["ray_rms_units"])
        if scale["m_per_unit"]:
            dis = abs(k_tag - scale["m_per_unit"]) / scale["m_per_unit"]
            scale["cross_checks"].append(dict(name="tag_vs_floor_scale_rel_diff", value=rnd(dis), ok=dis < 0.05))
            if dis >= 0.05:
                warnings.append(f"tag scale and floor-drop scale disagree by {dis * 100:.1f}%; the tag (measured size) is used")
        scale.update(method="tag", source="reconstructed", m_per_unit=k_tag, evidence=ev)
    elif sref and tag_info is not None and not tag_info["found"]:
        warnings.append(f"scale reference tag {sref.get('tag_family')}#{sref.get('tag_id')} seen in {len(tag_info['views'])} registered frame(s); need at least 2")
    if scale["method"] == "tag" or not up:
        base_tb = mirror_table(R, L, W, None, fo)
        if scale["m_per_unit"] is None:
            fs = footprint_scale(R, base_tb, L, W)
            if fs:
                scale.update(method="table_footprint", source="estimated", m_per_unit=fs["m_per_unit"], evidence=fs)
                scale["cross_checks"].append(dict(name="footprint_axis_disagreement", value=rnd(fs["axis_disagreement"]), ok=fs["axis_disagreement"] < 0.1))
                blocking.append(dict(code="scale_estimated", message="Metric scale is an estimate from the tabletop outline (valid only if all four edges were filmed). "
                                     "Film the floor below the table, or put a printed AprilTag of measured size on the table.",
                                     fields=["alignment.scale_reference", "video (floor visible)"]))
        if scale["m_per_unit"] is None:
            raise Fail("scale_unavailable", "no metric scale: the floor below the table is not reconstructed and no scale tag was found",
                       ["video: include the floor under the table", "alignment.scale_reference (AprilTag + measured size)"], "scale")
        tb = mirror_table(R, L, W, scale["m_per_unit"], fo)
        tb["table"]["height"] = H
    k = scale["m_per_unit"]
    TF = G.TableFrame(tb["plane"], k, flip=bool(alignment.get("table_frame_flip")))

    # internal-consistency checks
    Qall = TF.to_table(R.P)
    near_top = np.abs(Qall[:, 2]) < 0.006
    plane_rms = float(np.sqrt(np.mean(Qall[near_top, 2] ** 2))) if near_top.any() else None
    if up:
        n = np.array(tb["plane"]["n"]); d = tb["plane"]["d"]
        hh = R.P @ n + d
        s_ = np.median(np.linalg.norm(R.P - np.median(R.P, 0), axis=1))
        floor = [(len(inl), -np.median(R.P[inl] @ n + d), inl) for fn, fd, inl in fo.planes(R.P, np.where(hh < -0.3 * s_)[0], 0.01 * s_)
                 if np.degrees(np.arccos(min(1, abs(fn @ n)))) < 8]     # same floor rule as find_objects (RANSAC: may differ slightly)
        if floor:
            cnt, drop, inl = max(floor, key=lambda t: t[0])
            frms = float(np.std((R.P[inl] @ n + d)))
            rel = math.sqrt((plane_rms or 0) ** 2 / k ** 2 + frms ** 2) / max(drop, 1e-9)
            scale["evidence"].update(floor_points=int(cnt), drop_units=float(drop), floor_plane_std_units=frms)
            scale["rel_uncertainty_est"] = rnd(rel)
    if scale["method"] == "tag":
        scale["rel_uncertainty_est"] = rnd(scale["evidence"]["edge_spread_rel"] + scale["evidence"]["ray_rms_units"] / scale["evidence"]["edge_units"])
    if scale["method"] == "table_footprint":
        scale["rel_uncertainty_est"] = rnd(max(0.05, scale["evidence"]["axis_disagreement"]))
    rect_in, rect_tot = tb["table"].get("points_inside"), tb["table"].get("points_total")
    if rect_tot:
        cov = rect_in / rect_tot
        scale["cross_checks"].append(dict(name="tabletop_points_inside_entered_rectangle", value=rnd(cov), ok=cov > 0.85))
        if cov <= 0.85:
            warnings.append(f"only {cov * 100:.0f}% of tabletop points fit the entered {L*100:.0f} x {W*100:.0f} cm table: size or scale may be wrong, "
                            "or another surface at table height was merged")

    # table corners in a keyframe so the user can tell which edge is +x (rectangle fit is 180-degree ambiguous)
    corners = TF.corners(L, W)
    cw = TF.to_world(np.array([c for _, c in corners]))
    best_im, best_n = None, -1
    mid = len(R.images) // 2
    for i, im in enumerate(R.images):
        uv, front = R.project(im, cw)
        w, h = R.size(im)
        inv = front & (uv[:, 0] >= 0) & (uv[:, 1] >= 0) & (uv[:, 0] < w) & (uv[:, 1] < h)
        score = inv.sum() - abs(i - mid) * 1e-4
        if score > best_n:
            best_im, best_n = im, score
    uv, front = R.project(best_im, cw)
    w, h = R.size(best_im)
    table_out = dict(length_m=L, width_m=W, height_m=H, source=table_source, pose_source="reconstructed" if up or scale["method"] != "table_footprint" else "estimated",
                     pose_method="upstream find_objects rectangle fit" if up else "find_objects rule (mirrored) with alternative scale",
                     flip_applied=TF.flip, rect_points_inside=rect_in, rect_points_total=rect_tot, plane_rms_m=rnd(plane_rms, 5),
                     corners=[dict(label=lab, table_xy_m=rnd(c[:2]), keyframe=best_im.name, px=rnd(uv[i], 1) if front[i] else None,
                                   px_norm=rnd([uv[i][0] / w, uv[i][1] / h]) if front[i] else None,
                                   in_view=bool(front[i] and 0 <= uv[i][0] < w and 0 <= uv[i][1] < h)) for i, (lab, c) in enumerate(corners)])
    if "table_frame_flip" not in alignment:
        blocking.append(dict(code="table_orientation_ambiguous", message="The table rectangle fit cannot tell its two long edges apart. "
                             "Check corner A(+x,+y) in the keyframe and confirm or flip (only matters for tape-measured robot coordinates).",
                             fields=["alignment.table_frame_flip"], soft=True))

    # clusters + their view coverage
    clusters, _ = recompute_clusters(R, TF, L, W, (up or {}).get("objects", []))
    for c in clusters:
        full, dirs = cluster_views(R, c)
        dxy = TF.dir_to_table(np.array(dirs))[:, :2] if dirs else np.zeros((0, 2))
        c["views_full"] = len(full)
        c["azimuth_coverage_deg"] = G.azimuth_coverage_deg(dxy)
        c["class"], c["assigned_to"], c["match_iou"] = "unassigned", None, None

    # name assignment from 2D boxes (objects + robots)
    sel_objs = [o for o in selections.get("objects", []) if SLUG.match(str(o.get("name", "")))]
    sel_robots = [r for r in selections.get("robots", []) if SLUG.match(str(r.get("name", "")))]
    bad = [o.get("name") for o in selections.get("objects", []) + selections.get("robots", []) if not SLUG.match(str(o.get("name", "")))]
    if bad:
        warnings.append(f"ignored selections with invalid names: {bad}")
    pairs = []
    for role, items in (("object", sel_objs), ("robot", sel_robots)):
        for it in items:
            if not it.get("bbox"):
                continue
            im, dt = nearest_keyframe(R, kf, it["bbox"])
            it["_im"], it["_dt"] = im, dt
            if im is None:
                continue
            for c in clusters:
                pb = project_box(R, im, R.P[c["idx"]])
                if pb:
                    pairs.append((G.iou(pb, it["bbox"]["xyxy_norm"]), role, it["name"], c["id"]))
    used_c, used_n = set(), set()
    cl_by = {c["id"]: c for c in clusters}
    for iou, role, name, cid in sorted(pairs, reverse=True):
        if iou < 0.1 or cid in used_c or (role, name) in used_n:
            continue
        used_c.add(cid); used_n.add((role, name))
        cl_by[cid].update({"class": role, "assigned_to": name, "match_iou": iou})
    for c in clusters:
        if c["class"] == "unassigned" and c["height"] > ROBOT_HEIGHT_HINT_M:
            c["class"] = "robot_candidate"
    cand = [c for c in clusters if c["class"] == "robot_candidate"]
    if cand:
        warnings.append(f"{len(cand)} tall unassigned cluster(s) ({', '.join(c['id'] for c in cand)}): possibly a robot or fixture not named in the selections")

    sigma = lambda c: (scale["rel_uncertainty_est"] or 0.05) * float(np.linalg.norm(c["table_xy"])) + 0.5 * min(c["footprint"]["extent"]) / math.sqrt(max(1, c["n_points"] / 40)) + (plane_rms or 0)
    overrides = alignment.get("object_overrides") or {}
    objects = []
    if sel_objs:
        targets = [(o["name"], o) for o in sel_objs]
    else:
        targets = [(c["upstream_object"] or c["id"], None) for c in clusters if c["class"] == "unassigned" and c["on_table_rect"]]
        if targets:
            warnings.append("no object selections given: listing upstream on-table clusters under their generic names")
    for name, sel in targets:
        c = next((c for c in clusters if c["assigned_to"] == name and c["class"] == "object"), None) if sel else \
            next((c for c in clusters if (c["upstream_object"] or c["id"]) == name), None)
        ent = dict(name=name, cluster_id=c["id"] if c else None, height_m=rnd(c["height"]) if c else None)
        if name in overrides:
            ov = overrides[name]
            ent.update(status="manual", provenance="manual", table_frame=dict(x_m=float(ov["x_m"]), y_m=float(ov["y_m"]), z_m=0.0,
                       yaw_deg=ov.get("yaw_deg"), yaw_source="manual" if ov.get("yaw_deg") is not None else "unresolved"),
                       position_uncertainty_m_est=None)
            if c:
                ent["evidence"] = dict(reconstructed_xy_m=rnd(c["table_xy"]), delta_m=rnd(math.dist(c["table_xy"], (ov["x_m"], ov["y_m"]))))
        elif c:
            if not c["on_table_rect"]:
                warnings.append(f"{name}: reconstructed centre lies outside the fitted table rectangle")
            ent.update(status="reconstructed", provenance="reconstructed",
                       table_frame=dict(x_m=float(c["table_xy"][0]), y_m=float(c["table_xy"][1]), z_m=0.0, yaw_deg=None, yaw_source="unresolved"),
                       position_uncertainty_m_est=rnd(sigma(c)),
                       evidence=dict(match_iou=rnd(c["match_iou"], 3), n_points=c["n_points"], views_full=c["views_full"],
                                     azimuth_coverage_deg=rnd(c["azimuth_coverage_deg"], 1), footprint_m=rnd(c["footprint"]["extent"]),
                                     footprint_major_axis_deg=rnd(c["footprint"]["major_deg"], 1)))
            if c["azimuth_coverage_deg"] < 90:
                warnings.append(f"{name}: filmed from only {c['azimuth_coverage_deg']:.0f} deg around; its centre may be biased towards the visible side")
        else:
            est = None
            if sel and sel.get("_im") is not None:
                x0, y0, x1, y1 = sel["bbox"]["xyxy_norm"]
                w_, h_ = R.size(sel["_im"])
                cc, dv = R.ray(sel["_im"], [0.5 * (x0 + x1) * w_, y1 * h_])
                X = G.ray_plane(cc, dv, np.array(tb["plane"]["n"]), float(tb["plane"]["d"]))
                if X is not None:
                    est = TF.to_table(X[None])[0]
            ent.update(status="unmatched" if est is None else "estimated", provenance="estimated_ray_plane" if est is not None else None,
                       table_frame=dict(x_m=float(est[0]), y_m=float(est[1]), z_m=0.0, yaw_deg=None, yaw_source="unresolved") if est is not None else None,
                       position_uncertainty_m_est=None,
                       evidence=dict(note="2D box bottom-centre ray intersected with the table plane: an estimate, not a reconstruction",
                                     keyframe=sel["_im"].name if sel and sel.get("_im") is not None else None,
                                     keyframe_time_gap_s=rnd(sel.get("_dt"), 3) if sel else None))
            blocking.append(dict(code="object_unmatched", message=f"{name}: no reconstructed cluster matches its box; position is "
                                 + ("an estimate" if est is not None else "unknown") + ". Confirm or enter it manually.",
                                 fields=[f"alignment.object_overrides.{name}", f"selections.objects[{name}].bbox"]))
        objects.append(ent)

    # robots: explicit anchors only
    anchors = alignment.get("robots") or {}
    robots = []
    for r in sel_robots:
        name = r["name"]
        c = next((c for c in clusters if c["assigned_to"] == name and c["class"] == "robot"), None)
        a = anchors.get(name)
        if r.get("decision") == "exclude":          # listed and matched to its cluster, but not composed: no anchor required
            robots.append(dict(name=name, library_id=r.get("library_id"), cluster_id=c["id"] if c else None,
                               cluster_xy_m=rnd(c["table_xy"]) if c else None, cluster_height_m=rnd(c["height"]) if c else None,
                               anchor=dict(method="none", source="none"), base_in_table=None, table_in_base=None, status="excluded"))
            continue
        ent = dict(name=name, library_id=r.get("library_id"), cluster_id=c["id"] if c else None,
                   cluster_xy_m=rnd(c["table_xy"]) if c else None, cluster_height_m=rnd(c["height"]) if c else None,
                   anchor=dict(method="none", source="none"), base_in_table=None, table_in_base=None, status="needs_alignment")
        T = None
        if a and a.get("method") == "table_measured":
            T = G.se2(float(a["x_m"]), float(a["y_m"]), float(a.get("yaw_deg", 0)))
            ent["anchor"] = dict(method="table_measured", source="measured", note="tape-measured base origin in the table frame (after flip)")
            if "table_frame_flip" not in alignment:
                warnings.append(f"{name}: tape-measured base given but table orientation not confirmed (alignment.table_frame_flip)")
        elif a and a.get("method") == "tag":
            ti = detect_tag(R, a.get("tag_family", "tag36h11"), a.get("tag_id", 0), ctx["deadline"])
            if ti["found"]:
                cq = TF.to_table(ti["corners_world"])
                tp = G.tag_pose(cq)
                sz = float(a["tag_size_m"])
                ev = dict(views=len(ti["views"]), tag_edge_m_reconstructed=rnd(tp["edge"]), tag_edge_m_measured=sz, tilt_deg=rnd(tp["tilt_deg"], 2))
                if abs(tp["edge"] - sz) / sz > 0.05:
                    warnings.append(f"{name}: anchor tag reconstructs at {tp['edge']*100:.1f} cm vs measured {sz*100:.1f} cm (scale error)")
                if tp["tilt_deg"] > 10:
                    warnings.append(f"{name}: anchor tag is tilted {tp['tilt_deg']:.0f} deg from the table plane; planar anchor assumed")
                T = G.base_from_tag(tp["center"][:2], tp["yaw_deg"], a["tag_in_base"])
                ent["anchor"] = dict(method="tag", source="reconstructed", evidence=ev)
            else:
                blocking.append(dict(code="anchor_tag_not_found", message=f"{name}: anchor tag seen in {len(ti['views'])} registered frame(s), need 2+",
                                     fields=[f"alignment.robots.{name}"]))
        elif a and a.get("method") == "cluster":
            cc = cl_by.get(a.get("cluster_id")) or c
            if cc is None or a.get("yaw_deg") is None:
                blocking.append(dict(code="anchor_incomplete", message=f"{name}: cluster anchor needs an existing cluster_id and a supplied yaw_deg",
                                     fields=[f"alignment.robots.{name}.cluster_id", f"alignment.robots.{name}.yaw_deg"]))
            else:
                T = G.se2(float(cc["table_xy"][0]), float(cc["table_xy"][1]), float(a["yaw_deg"]))
                ent["anchor"] = dict(method="cluster", source="estimated", cluster_id=cc["id"],
                                     note="base position = footprint centre of the reconstructed robot cluster (biased by the arm pose); yaw supplied by the user")
                blocking.append(dict(code="anchor_estimated", message=f"{name}: base position is estimated from the robot's point cluster "
                                     "(the arm pose biases it by centimetres). Confirm it, or use a tag / tape-measured anchor.", fields=[f"alignment.robots.{name}"]))
        if T is not None:
            bx, by, byaw = G.se2_params(T)
            tx, ty, tyaw = G.se2_params(G.se2_inv(T))
            ent.update(base_in_table=dict(x_m=rnd(bx), y_m=rnd(by), yaw_deg=rnd(byaw, 3)),
                       table_in_base=dict(x_m=rnd(tx), y_m=rnd(ty), yaw_deg=rnd(tyaw, 3)), status="anchored", _T=T)
            if abs(bx) > L / 2 + 0.05 or abs(by) > W / 2 + 0.05:
                warnings.append(f"{name}: anchored base is off the table top ({bx:+.2f}, {by:+.2f}) m from its centre")
        else:
            if not any(b.get("fields", [""])[0].startswith(f"alignment.robots.{name}") for b in blocking):
                blocking.append(dict(code="no_robot_anchor", message=f"{name}: robot base pose is not identified. Give a measured anchor: a flat AprilTag "
                                     "at a measured offset from the base, tape-measured base coordinates on the table, or confirm the robot cluster and its yaw.",
                                     fields=[f"alignment.robots.{name}"]))
        robots.append(ent)
    if not sel_robots:
        blocking.append(dict(code="robot_not_declared", message="No robot named in the selections; robot-relative poses cannot be produced.",
                             fields=["selections.robots"]))
    anchored = [r for r in robots if r["status"] == "anchored"]
    composed = [r for r in robots if r["status"] != "excluded"]
    primary = alignment.get("primary_robot") or (composed[0]["name"] if len(composed) == 1 else None)
    if len(composed) > 1 and not alignment.get("primary_robot"):
        blocking.append(dict(code="multiple_robots_no_primary", message=f"{len(composed)} robots declared ({', '.join(r['name'] for r in composed)}); "
                             "choose the primary robot: its base is the scene origin. Every arm is still composed.",
                             fields=["alignment.primary_robot"]))
    for o in objects:
        o["robot_frame"] = {}
        if o.get("table_frame"):
            for r in anchored:
                p = G.in_frame(r["_T"], [[o["table_frame"]["x_m"], o["table_frame"]["y_m"]]])[0]
                yb = None if o["table_frame"]["yaw_deg"] is None else G.wrap_deg(o["table_frame"]["yaw_deg"] - r["base_in_table"]["yaw_deg"])
                o["robot_frame"][r["name"]] = dict(x_m=rnd(p[0]), y_m=rnd(p[1]), yaw_deg=rnd(yb, 2), distance_m=rnd(float(np.linalg.norm(p))))
        tfm = o.get("table_frame")
        if tfm:
            o["table_frame"] = {k: (rnd(v) if isinstance(v, float) else v) for k, v in tfm.items()}

    settings_patch = None
    prim = next((r for r in anchored if r["name"] == primary), None)
    if prim and scale["source"] == "reconstructed":
        pl = {}
        for o in objects:
            rf = o["robot_frame"].get(prim["name"])
            if rf and o["status"] in ("reconstructed", "manual"):
                pl[o["name"]] = dict(x_m=rf["x_m"], y_m=rf["y_m"], yaw_deg=rf["yaw_deg"] if rf["yaw_deg"] is not None else 0.0,
                                     source="manual" if o["status"] == "manual" else "reconstructed")
        settings_patch = dict(workcell=dict(table=dict(length_m=L, width_m=W, height_m=H, source=table_source,
                                                       note=f"pose from video layout ({scale['method']} scale)"), placements=pl),
                              table_pose=prim["table_in_base"], robot=prim["name"])
        others = {}
        for r in anchored:                          # every other anchored arm's base in the primary base frame
            if r["name"] != prim["name"]:
                bx, by, byaw = G.se2_params(G.se2_inv(prim["_T"]) @ r["_T"])
                others[r["name"]] = dict(x_m=rnd(bx), y_m=rnd(by), yaw_deg=rnd(byaw, 3), source=r["anchor"]["source"])
        if others:
            settings_patch["robots_in_primary"] = others
    for r in robots:
        r.pop("_T", None)

    hard = [b for b in blocking if not b.get("soft")]
    codes = {b["code"] for b in hard}
    if codes & {"no_robot_anchor", "robot_not_declared", "multiple_robots_no_primary", "anchor_tag_not_found", "anchor_incomplete"}:
        status = "needs_alignment"
    elif hard:
        status = "needs_review"
    else:
        status = "complete"
    cams = []
    for im in R.images[:: max(1, len(R.images) // 30)]:
        c = TF.to_table(np.asarray(im.projection_center())[None])[0]
        vd = TF.dir_to_table(np.asarray(im.cam_from_world().rotation.matrix())[2][None])[0]
        cams.append(dict(keyframe=im.name, position_m=rnd(c, 3), view_dir=rnd(vd, 3)))
    reg = set(R.by_name)
    return dict(
        format=FORMAT, status=status, blocking=blocking, warnings=warnings, inputs=ctx.get("inputs", {}), reconstruction=recon,
        scale={**scale, "m_per_unit": rnd(scale["m_per_unit"], 6), "evidence": {k: (rnd(v, 5) if isinstance(v, float) else v) for k, v in scale["evidence"].items()}},
        table=table_out, keyframes=[dict(k, registered=k["name"] in reg) for k in kf], cameras_table_frame=cams,
        clusters=[dict(id=c["id"], upstream_object=c["upstream_object"], table_xy_m=rnd(c["table_xy"]), height_m=rnd(c["height"]),
                       footprint_m=rnd(c["footprint"]["extent"]), major_axis_deg=rnd(c["footprint"]["major_deg"], 1), n_points=c["n_points"],
                       on_table_rect=c["on_table_rect"], views_full=c["views_full"], azimuth_coverage_deg=rnd(c["azimuth_coverage_deg"], 1),
                       **{"class": c["class"]}, assigned_to=c["assigned_to"], match_iou=rnd(c["match_iou"], 3)) for c in clusters],
        objects=objects, robots=robots, primary_robot=primary, settings_patch=settings_patch,
        frames=dict(table="origin table-top centre, x along length, y along width, z up; metres",
                    robot_base="per anchored robot: base origin, robot model axes; placements in settings_patch use the primary robot"))


# ── CLI ─────────────────────────────────────────────────────────────────────────────────────
def parse_table(s):
    try:
        v = [float(x) for x in s.split(",")]
        assert len(v) == 3 and 20 <= v[0] <= 400 and 20 <= v[1] <= 400 and 20 <= v[2] <= 150
    except Exception:
        raise SystemExit("--table-cm must be L,W,H in cm (20..400, 20..400, 20..150)")
    return v


def read_json(p):
    return json.loads(Path(p).read_text()) if p else None


def run_upstream_find(ctx, work, tcm, deadline, max_rss):
    code, tail, peak = run_limited([ctx["py"], "-u", "scene/find_objects.py", str(work), "--table", *[str(v) for v in tcm]],
                                   ctx["repo"], deadline, max_rss, "find_objects")
    ctx["peaks"]["find_objects"] = round(peak)
    if code == 0 and (work / "scene.json").exists():
        return True, None
    if "max() arg is an empty sequence" in tail or "max() iterable argument is empty" in tail:
        return False, "floor_not_found"
    if "'NoneType' object is not subscriptable" in tail or "cannot unpack non-iterable NoneType" in tail:
        return False, "table_not_found"
    return False, "find_objects_error: " + tail.strip().splitlines()[-1][:200] if tail.strip() else "find_objects_error"


def main():
    if len(sys.argv) > 1 and sys.argv[1] == "_reconstruct":
        child_reconstruct(sys.argv[2], sys.argv[3], sys.argv[4]); return
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("cmd", choices=["run", "align", "schema"])
    ap.add_argument("--video"); ap.add_argument("--work"); ap.add_argument("--table-cm"); ap.add_argument("--out")
    ap.add_argument("--table-source", default="measured", choices=["measured", "supplied", "estimated"])
    ap.add_argument("--repo", default=str(DEFAULT_REPO))
    ap.add_argument("--selections"); ap.add_argument("--alignment")
    ap.add_argument("--frames", type=int, default=150); ap.add_argument("--max-image-px", type=int, default=1920)
    ap.add_argument("--max-seconds", type=float, default=1800); ap.add_argument("--max-rss-mb", type=int, default=3500)
    ap.add_argument("--force", action="store_true")
    a = ap.parse_args()
    if a.cmd == "schema":
        print((ROOT / "workflow-layout-contract.json").read_text()); return
    if not (a.work and a.table_cm and a.out) or (a.cmd == "run" and not a.video):
        ap.print_usage(); sys.exit(64)
    t0 = time.time()
    deadline = t0 + a.max_seconds
    work, out = Path(a.work).resolve(), Path(a.out).resolve()
    work.mkdir(parents=True, exist_ok=True)
    tcm = parse_table(a.table_cm)
    L, W, H = (v / 100 for v in tcm)
    try:
        sel, ali = read_json(a.selections), read_json(a.alignment)
    except Exception as e:
        print(f"bad selections/alignment JSON: {e}"); sys.exit(64)
    repo = Path(a.repo).resolve()
    ctx = dict(repo=repo, py=str(ROOT / ".venv/bin/python"), deadline=deadline, peaks={}, warnings=[], recon_extra={})
    prev = read_json(work / "layout_inputs.json") if (work / "layout_inputs.json").exists() else {}
    inputs = dict(table_cm=tcm, table_source=a.table_source, repo_commit=repo_commit(repo), selections_sha256=sha_json(sel), alignment_sha256=sha_json(ali),
                  frames=a.frames, max_image_px=a.max_image_px, video_sha256=prev.get("video_sha256"))
    LOCK_FILE.parent.mkdir(parents=True, exist_ok=True)
    lockf = open(LOCK_FILE, "w")
    result = None
    try:
        log("waiting for the heavy-work lock…")
        while True:
            try:
                fcntl.flock(lockf, fcntl.LOCK_EX | fcntl.LOCK_NB); break
            except BlockingIOError:
                if time.time() > deadline:
                    raise Budget("timed out waiting for another layout run to finish")
                time.sleep(1)
        if a.cmd == "run":
            inputs["video_sha256"] = sha256_file(a.video)
            same = (not a.force and prev.get("video_sha256") == inputs["video_sha256"] and prev.get("frames") == a.frames
                    and prev.get("max_image_px") == a.max_image_px and (work / "sparse" / "points3D.bin").exists())
            if same:
                log("reusing existing reconstruction (same video and frame settings)")
                ctx["recon_extra"]["reused"] = True
            else:
                for f in ("scene.json",):
                    (work / f).unlink(missing_ok=True)
                log("stage frames: streaming extraction")
                fr = extract_frames(Path(a.video), work / "frames", a.frames, a.max_image_px, deadline)
                (work / "keyframes.json").write_text(json.dumps(fr))
                log(f"  {len(fr['frames'])} frames from {fr['decoded']} decoded ({fr['width']}x{fr['height']} after cap)")
                log("stage reconstruct: upstream scene/reconstruct.py:reconstruct")
                ts = time.time()
                import shutil
                shutil.rmtree(work / "sparse", ignore_errors=True)
                code, tail, peak = run_limited([ctx["py"], "-u", str(Path(__file__).resolve()), "_reconstruct", str(repo), str(work / "frames"), str(work)],
                                               repo, deadline, a.max_rss_mb, "reconstruct")
                ctx["peaks"]["reconstruct"] = round(peak)
                ctx["recon_extra"].update(seconds=round(time.time() - ts, 1), peak_rss_mb=round(peak))
                if code != 0 or not (work / "sparse" / "points3D.bin").exists():
                    raise Fail("reconstruction_failed", "COLMAP registered no usable model: " + (tail.strip().splitlines()[-1][:200] if tail.strip() else f"exit {code}"),
                               ["video: film slowly in a full loop with overlap and texture"], "reconstruct")
            (work / "layout_inputs.json").write_text(json.dumps(inputs))
        else:
            if not (work / "sparse" / "points3D.bin").exists():
                raise Fail("no_reconstruction", "align needs a work dir from a successful run", ["run first"], "align")
            inputs["video_sha256"] = prev.get("video_sha256")
            ctx["recon_extra"]["reused"] = True
        # upstream table + scale (re-run when the table size changed: scale depends on the height)
        up_ok = (work / "scene.json").exists() and prev.get("table_cm") == tcm and prev.get("upstream_ok")
        if not up_ok:
            (work / "scene.json").unlink(missing_ok=True)
            log("stage table: upstream scene/find_objects.py")
            up_ok, why = run_upstream_find(ctx, work, tcm, deadline, a.max_rss_mb)
            if not up_ok:
                ctx["warnings"].append(f"upstream find_objects did not produce a layout ({why}); trying the scale reference / footprint path")
        ctx["upstream_ok"] = bool(up_ok)
        inputs["upstream_ok"] = bool(up_ok)
        (work / "layout_inputs.json").write_text(json.dumps(inputs))
        ctx["inputs"] = {k: v for k, v in inputs.items() if k not in ("frames", "max_image_px", "upstream_ok")}
        log("stage layout")
        result = build_layout(work, L, W, H, a.table_source, sel, ali, ctx)
        code = 0
    except Fail as e:
        result = dict(format=FORMAT, status="failed", blocking=[dict(code=e.code, message=e.message, fields=e.fields, stage=e.stage, details=e.details)],
                      warnings=ctx["warnings"], inputs=inputs, objects=[], robots=[], settings_patch=None)
        code = 2
    except Budget as e:
        result = dict(format=FORMAT, status="failed", blocking=[dict(code="budget_exceeded", message=str(e),
                      fields=["--max-seconds", "--max-rss-mb", "--frames", "--max-image-px"])], warnings=ctx["warnings"], inputs=inputs,
                      objects=[], robots=[], settings_patch=None)
        code = 3
    finally:
        try:
            fcntl.flock(lockf, fcntl.LOCK_UN)
        except Exception:
            pass
    if result.get("status") == "failed" and "reconstruction" not in result and (work / "sparse" / "points3D.bin").exists():
        try:
            Rr = Recon(work)
            kfs = json.loads((work / "keyframes.json").read_text())["frames"] if (work / "keyframes.json").exists() else []
            result["reconstruction"] = dict(ok=True, frames_extracted=len(kfs), frames_registered=len(Rr.images), points3d=int(len(Rr.P)),
                                            mean_reproj_px=rnd(Rr.mean_reproj(), 3), **ctx.get("recon_extra", {}))
        except Exception as ex:
            result["reconstruction"] = dict(ok=False, error=str(ex)[:200])
    result["runtime"] = dict(seconds=round(time.time() - t0, 1), peak_rss_mb=ctx["peaks"], adapter="adapter/workflow_layout.py", contract=FORMAT)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(result, indent=1))
    log("WORKFLOW_LAYOUT_RESULT " + json.dumps(dict(status=result["status"], out=str(out), n_objects=len(result.get("objects", [])),
                                                     n_robots=len(result.get("robots", [])), blocking_codes=[b["code"] for b in result["blocking"]])))
    sys.exit(code)


if __name__ == "__main__":
    main()
