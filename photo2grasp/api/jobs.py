"""Video of a table -> MuJoCo scene, as background jobs. Each job lives in outputs/jobs/<id>/:

    input.mp4, frames/, sparse/        COLMAP reconstruction
    scene.json                         table pose, metric scale, objects found on the table
    objects/<key>/view_*.jpg           best video crops of each object
    objects/<key>/web/candidate_*.jpg  product photos found online (the picked ones are listed in job.json)
    info.json, layout.json             what build_scene.py reads and writes
    scene_view_*.png                   renders of the finished scene
    job.json, log.txt                  state and console output

Jobs run one at a time (TRELLIS and COLMAP each want the whole GPU / CPU). The state is saved after every
stage, so a failed or paused job can be rerun from any stage and a restarted server picks up where it was.
"""
import contextlib
import json
import os
import queue
import shutil
import sys
import threading
import time
import traceback
import uuid

from .config import JOBS_DIR, ROOT

if sys.platform.startswith("linux") and not os.environ.get("DISPLAY"):
    os.environ.setdefault("MUJOCO_GL", "egl")  # headless server: offscreen rendering
sys.path[:0] = [str(ROOT / "scene"), str(ROOT / "pipeline")]

STAGES = ["reconstruct", "find_objects", "object_views", "identify", "reference_images", "physics", "build", "render"]
REVIEW_AFTER = "physics"  # jobs created with review=True pause here so identities and physics can be fixed

_queue = queue.Queue()
_lock = threading.Lock()


def job_dir(job_id):
    return JOBS_DIR / job_id


def load(job_id):
    path = job_dir(job_id) / "job.json"
    if not job_id.isalnum() or not path.exists():
        return None
    with _lock:
        return json.loads(path.read_text())


def save(job):
    job["updated"] = time.time()
    path = job_dir(job["id"]) / "job.json"
    with _lock:
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(job, indent=2))
        tmp.replace(path)


def all_jobs():
    if not JOBS_DIR.exists():
        return []
    jobs = [load(p.name) for p in JOBS_DIR.iterdir() if (p / "job.json").exists()]
    return sorted(jobs, key=lambda j: j["created"], reverse=True)


def create(table_cm, review, video_suffix=".mp4"):
    job_id = uuid.uuid4().hex[:12]
    job_dir(job_id).mkdir(parents=True)
    job = dict(id=job_id, status="created", stage=None, next_stage="reconstruct", error=None, created=time.time(),
               video=f"input{video_suffix}", table_cm=list(table_cm), review=review, reuse_meshes=False,
               objects={})
    save(job)
    return job


def submit(job_id, from_stage, reuse_meshes=False):
    job = load(job_id)
    job.update(status="queued", next_stage=from_stage, error=None, reuse_meshes=reuse_meshes)
    save(job)
    _queue.put(job_id)
    return job


def start():
    """Starts the worker and requeues jobs that were waiting or running when the server stopped."""
    threading.Thread(target=_worker, daemon=True).start()
    for job in reversed(all_jobs()):
        if job["status"] in ("queued", "running"):
            submit(job["id"], job["next_stage"], job.get("reuse_meshes", False))


def included(job):
    return [(key, obj) for key, obj in job["objects"].items() if obj.get("include")]


# ----------------------------------------------------------------------------------------------------- runner

class _Tee:
    def __init__(self, *streams):
        self.streams = streams

    def write(self, text):
        for s in self.streams:
            s.write(text)
            s.flush()
        return len(text)

    def flush(self):
        for s in self.streams:
            s.flush()

    def isatty(self):
        return False


def _worker():
    while True:
        job_id = _queue.get()
        try:
            _run(job_id)
        except Exception:
            traceback.print_exc()


def _run(job_id):
    job = load(job_id)
    if job is None or job["status"] != "queued":
        return
    work = job_dir(job_id)
    first = STAGES.index(job["next_stage"])
    with open(work / "log.txt", "a") as log, contextlib.redirect_stdout(_Tee(log, sys.__stdout__)):
        job["status"] = "running"
        save(job)
        for i in range(first, len(STAGES)):
            stage = STAGES[i]
            job.update(stage=stage, next_stage=stage)
            save(job)
            print(f"\n== [{job_id}] {stage}", flush=True)
            started = time.time()
            try:
                STAGE_FUNCS[stage](job, work)
            except Exception as exc:
                traceback.print_exc(file=sys.stdout)
                job.update(status="failed", error=f"{stage}: {exc}")
                save(job)
                return
            print(f"== {stage} done in {time.time() - started:.0f} s", flush=True)
            job["next_stage"] = STAGES[i + 1] if i + 1 < len(STAGES) else None
            save(job)
            if stage == REVIEW_AFTER and job["review"] and i < len(STAGES) - 1:
                job.update(status="review", stage=None)
                save(job)
                print("== paused for review: PATCH the objects, then POST /jobs/{id}/run?from_stage=build")
                return
        job.update(status="done", stage=None)
        save(job)


# ----------------------------------------------------------------------------------------------------- stages

def _views(work, obj):
    """[(path, object_box)] for one object's video crops."""
    return [(work / v, json.loads((work / v).with_suffix(".json").read_text())["object_box"]) for v in obj["views"]]


def stage_reconstruct(job, work):
    from reconstruct import extract_frames, reconstruct

    if (work / "frames").exists():
        shutil.rmtree(work / "frames")
    frames = extract_frames(work / job["video"], work / "frames")
    rec = reconstruct(frames, work)
    job["reconstruction"] = dict(frames=len(list(frames.glob("*.jpg"))), registered=rec.num_reg_images(),
                                 points=rec.num_points3D())
    print(f"registered {rec.num_reg_images()} frames, {rec.num_points3D()} 3D points")


def stage_find_objects(job, work):
    from find_objects import find_objects

    L, W, H = (x / 100 for x in job["table_cm"])
    job["table"] = dict(length=L, width=W, height=H)
    try:
        scene = find_objects(work, job["table_cm"])
    except RuntimeError as exc:
        scene = None
        print(f"scan failed ({exc})")
    inside = scene["table"]["points_inside"] if scene else 0
    total = scene["table"]["points_total"] if scene else 0
    usable = scene and scene["objects"] and inside >= 0.4 * max(total, 1)
    if usable:
        job["measured"] = True
        job["objects"] = {o["name"]: dict(x=o["x"], y=o["y"], height_m=o["height"], include=True, edited=[])
                          for o in scene["objects"]}
        return
    # A plain tablecloth has almost no points, so the scan cannot place anything. The frames still show the
    # objects; later stages find them there and line them up along the near edge of the table.
    job["measured"] = False
    job["objects"] = {}
    job["placement"] = ("unmeasured: the scan had no usable table, so the objects are lined up along the "
                        "near edge instead of where they stood")
    print(f"scan unusable ({0 if not scene else len(scene['objects'])} objects, {inside}/{total} tabletop "
          "points inside the table); taking the objects from the frames")


def _frames_fallback(job, work):
    """Crops of each object from a spread of sharp frames, for a video whose scan could not place them."""
    import cv2
    import numpy as np

    from . import gemini

    frames = sorted((work / "frames").glob("*.jpg"))
    picks = []
    for group in np.array_split(np.array(frames, dtype=object), 5):
        if len(group) == 0:
            continue
        picks.append(max(group, key=lambda p: cv2.Laplacian(
            cv2.imread(str(p), cv2.IMREAD_GRAYSCALE), cv2.CV_64F).var()))
    found = gemini.objects_in_frames(picks)
    if not found.objects:
        raise RuntimeError("no objects identified in the frames")
    n = len(found.objects)
    xs = np.linspace(-(n - 1) * 0.09, (n - 1) * 0.09, n)
    # The arm is bolted 4 cm in from the edge and its folded rest pose reaches about 15 cm forward.
    # 24 cm in front of that mount is still inside the 30 cm reach, and the arm does not knock the object over.
    y = job["table"]["width"] / 2 - 0.28
    for i, item in enumerate(found.objects):
        key = f"object_{i}"
        out = work / "objects" / key
        out.mkdir(parents=True, exist_ok=True)
        views = []
        for b in item.boxes:
            if not 0 <= b.frame < len(picks):
                continue
            img = cv2.imread(str(picks[b.frame]))
            h, w = img.shape[:2]
            x0, y0, x1, y1 = (float(np.clip(v, 0, 1)) for v in b.box)
            x0, x1, y0, y1 = min(x0, x1), max(x0, x1), min(y0, y1), max(y0, y1)
            if (x1 - x0) * (y1 - y0) < 0.005:
                continue
            px0, py0, px1, py1 = x0 * w, y0 * h, x1 * w, y1 * h
            pad = 0.35 * max(px1 - px0, py1 - py0)
            cx0, cy0 = int(max(0, px0 - pad)), int(max(0, py0 - pad))
            cx1, cy1 = int(min(w, px1 + pad)), int(min(h, py1 + pad))
            cv2.imwrite(str(out / f"view_{len(views)}.jpg"), img[cy0:cy1, cx0:cx1])
            m = 0.1 * max(px1 - px0, py1 - py0)
            box = [px0 - m - cx0, py0 - m - cy0, px1 + m - cx0, py1 + m - cy0]
            (out / f"view_{len(views)}.json").write_text(json.dumps(dict(object_box=box)))
            views.append(str((out / f"view_{len(views)}.jpg").relative_to(work)))
        if not views:
            print(f"{item.label}: no usable box, left out")
            continue
        job["objects"][key] = dict(x=float(xs[i]), y=float(y), height_m=None, include=True, edited=[], views=views)
        print(f"{key}: {item.label} in {len(views)} frame(s)")
    if not job["objects"]:
        raise RuntimeError("objects were named but none had a usable box")


def stage_object_views(job, work):
    if not job.get("measured", True):
        _frames_fallback(job, work)
        return
    import object_views

    object_views.main(work)
    for key, obj in job["objects"].items():
        obj["views"] = [str(v.relative_to(work)) for v in sorted((work / "objects" / key).glob("view_*.jpg"))]
        if not obj["views"]:
            obj.update(include=False, note="no clear view of it in the video")


def stage_identify(job, work):
    from . import gemini

    for key, obj in included(job):
        if "identity" in obj["edited"]:
            continue
        ident = gemini.identify(_views(work, obj))
        obj["identity"] = ident.model_dump()
        print(f"{key}: {ident.label} (confidence {ident.confidence:.2f}) -> search '{ident.search_query}'")
        save(job)


def stage_reference_images(job, work):
    from . import gemini
    from .image_search import download, search_images, serpapi_key

    if not serpapi_key():
        print("SERPAPI_API_KEY not set: no web photos, textures come from the video only")
        for _, obj in included(job):
            obj["reference_images"] = []
        return
    for key, obj in included(job):
        web = work / "objects" / key / "web"
        shutil.rmtree(web, ignore_errors=True)
        obj["reference_images"] = []
        try:
            candidates = download(search_images(obj["identity"]["search_query"]), web)
            if not candidates:
                print(f"{key}: no usable images found")
                continue
            picks = gemini.pick_references(_views(work, obj), [p for p, _ in candidates],
                                           gemini.Identity(**obj["identity"]))
        except Exception as exc:  # web photos are a bonus: carry on with the video crops alone
            print(f"{key}: reference image search failed ({exc})")
            continue
        obj["reference_images"] = [dict(path=str(candidates[i][0].relative_to(work)), url=candidates[i][1])
                                   for i in picks]
        print(f"{key}: {len(candidates)} candidates, kept {len(picks)}")
        save(job)


def stage_physics(job, work):
    from . import gemini

    for key, obj in included(job):
        if "physics" in obj["edited"]:
            continue
        obj["physics"] = gemini.physics(gemini.Identity(**obj["identity"]), obj.get("height_m"))
        if obj.get("height_m") is None:
            obj["height_m"] = obj["physics"]["size_m"][2]
        p = obj["physics"]
        print(f"{key}: {[round(s * 100, 1) for s in p['size_m']]} cm, {p['mass_kg']} kg, "
              f"friction {p['friction'][0]}, {p['collision']} | {p['source']}")
        save(job)


def _assign_asset_names(job):
    """Asset folder per object: its slug, unless another job or object already owns that folder."""
    taken = set()
    for _, obj in included(job):
        base = obj["identity"]["slug"]
        if obj.get("asset_base") == base and obj["asset"] not in taken:
            name = obj["asset"]  # rerun of this job: overwrite its own asset
        else:
            name, n = base, 1
            while name in taken or (ROOT / "assets" / name).exists():
                name = f"{base}_{job['id'][:6]}" + (f"_{n}" if n > 1 else "")
                n += 1
        obj.update(asset=name, asset_base=base)
        taken.add(name)


def _repaint(mesh_dir, photos, boxes):
    """Keep a mesh that was already generated, and repaint it from the latest video crops and web photos."""
    from cutout import cut_out
    from texture_projection import project_photos

    raw_glb = mesh_dir / "trellis_raw.glb"
    if not raw_glb.exists():
        return
    cutouts = []
    for i, (photo, box) in enumerate(zip(photos, boxes)):
        cut_path = mesh_dir / f"cutout_{i}.png"
        if not cut_path.exists():
            cut_out(photo, box).save(cut_path)
        cutouts.append(cut_path)
    project_photos(raw_glb, cutouts, mesh_dir / "trellis.glb")


def _build_unmeasured(job, work):
    """TRELLIS + MJCF + a layout, for a video whose scan could not place the objects."""
    import cv2
    import numpy as np
    from mesh_to_mjcf import mesh_to_mjcf
    from trellis_mesh import trellis_mesh

    frame = cv2.imread(str(next((work / "frames").glob("*.jpg"))))
    centre = frame[frame.shape[0] // 3: 2 * frame.shape[0] // 3, frame.shape[1] // 3: 2 * frame.shape[1] // 3]
    rgba = (np.median(centre.reshape(-1, 3), 0)[::-1] / 255).round(3).tolist() + [1.0]
    layout = dict(table=dict(**job["table"], rgba=rgba), objects=[])
    for key, obj in included(job):
        views = [work / v for v in obj["views"]]
        refs = [work / r["path"] for r in obj.get("reference_images", [])]
        mesh_dir = work / "objects" / key
        boxes = [json.loads(v.with_suffix(".json").read_text())["object_box"] for v in views]
        photos = views + refs
        if job.get("reuse_meshes") and (mesh_dir / "raw_mesh.obj").exists():
            print(f"{obj['asset']}: reusing existing mesh, repainting from {len(photos)} photos")
            raw = mesh_dir / "raw_mesh.obj"
            _repaint(mesh_dir, photos, boxes + [None] * len(refs))
        else:
            print(f"{obj['asset']}: {len(views)} views + {len(refs)} reference photos -> TRELLIS")
            raw = trellis_mesh(photos, mesh_dir, keep_boxes=boxes + [None] * len(refs))
        props = dict(obj["physics"])
        xml = mesh_to_mjcf(raw, obj["asset"], props, ROOT / "assets" / obj["asset"],
                           textured_mesh_path=mesh_dir / "trellis.glb")
        layout["objects"].append(dict(name=obj["asset"], x=obj["x"], y=obj["y"]))
        print(f"   -> {xml}")
    (work / "layout.json").write_text(json.dumps(layout, indent=2))
    return layout


def stage_build(job, work):
    import build_scene
    from preview import save_preview

    _assign_asset_names(job)
    save(job)
    info = {key: dict(name=obj["asset"], **obj["physics"],
                      reference_images=[r["path"] for r in obj.get("reference_images", [])])
            for key, obj in included(job)}
    (work / "info.json").write_text(json.dumps(info, indent=2))
    if not job.get("measured", True):
        job["layout"] = _build_unmeasured(job, work)
    else:
        job["layout"] = build_scene.main(work, reuse=job.get("reuse_meshes", False))
    for key, obj in included(job):
        xml = ROOT / "assets" / obj["asset"] / f"{obj['asset']}.xml"
        obj["properties"] = json.loads((xml.parent / "properties.json").read_text())
        obj["preview"] = str(save_preview(xml, obj["asset"], work / "objects" / key / "preview.png").relative_to(work))


def stage_render(job, work):
    import view_scene

    images, rest = view_scene.render(work)
    job["renders"] = [str(p.relative_to(work)) for p in images]
    job["rest_positions"] = rest


STAGE_FUNCS = {s: globals()[f"stage_{s}"] for s in STAGES}
