"""Scene step 3: for each object found on the table, crop its best views from the video frames.

    python scene/object_views.py outputs/scene

A view is good when the object is large in the frame, fully inside it, not hidden behind another object,
and the frame is sharp. Picks views spread around the object (different camera directions).
"""
import json
import sys
from pathlib import Path

import cv2
import numpy as np
import pycolmap

N_VIEWS = 4


def project(rec, im, pts):
    cam = rec.cameras[im.camera_id]
    pc = np.array([im.cam_from_world() * p for p in pts])
    in_front = pc[:, 2] > 0
    return cam.img_from_cam(pc[in_front]), pc[in_front, 2].mean() if in_front.any() else np.inf, in_front.all()


def main(work):
    work = Path(work)
    scene = json.loads((work / "scene.json").read_text())
    rec = pycolmap.Reconstruction(str(work / "sparse"))
    objects = [np.array([rec.points3D[i].xyz for i in o["point_ids"]]) for o in scene["objects"]]
    images = sorted(rec.images.values(), key=lambda im: im.name)
    for k, (obj, pts) in enumerate(zip(scene["objects"], objects)):
        centre = np.median(pts, 0)
        cands = []
        for im in images:
            cam = rec.cameras[im.camera_id]
            uv, depth, all_front = project(rec, im, pts)
            if not all_front or len(uv) == 0:
                continue
            lo, hi = np.percentile(uv, 2, 0), np.percentile(uv, 98, 0)
            if lo[0] < 5 or lo[1] < 5 or hi[0] > cam.width - 5 or hi[1] > cam.height - 5:
                continue  # object cut off by the frame edge
            box = np.r_[lo, hi]
            # hidden behind another object? its box overlaps ours and it is closer to the camera
            blocked = False
            for j, other in enumerate(objects):
                if j == k:
                    continue
                ouv, odepth, ofront = project(rec, im, other)
                if len(ouv) == 0:
                    continue
                olo, ohi = np.percentile(ouv, 2, 0), np.percentile(ouv, 98, 0)
                overlap = np.prod(np.clip(np.minimum(hi, ohi) - np.maximum(lo, olo), 0, None)) / np.prod(hi - lo)
                blocked |= overlap > 0.15 and odepth < depth
            if blocked:
                continue
            img = cv2.imread(str(work / "frames" / im.name))
            sharp = cv2.Laplacian(cv2.cvtColor(img, cv2.COLOR_BGR2GRAY), cv2.CV_64F).var()
            direction = im.projection_center() - centre
            cands.append(dict(name=im.name, box=box, size=float(np.prod(hi - lo)), sharp=sharp,
                              dir=direction / np.linalg.norm(direction)))
        if not cands:
            print(f"{obj['name']}: no clear view found")
            continue
        # greedy: biggest+sharpest first, then views from directions at least 25 deg away from chosen ones
        for c in cands:
            c["score"] = c["size"] * min(c["sharp"], 500)
        cands.sort(key=lambda c: -c["score"])
        chosen = []
        for c in cands:
            if all(np.degrees(np.arccos(np.clip(c["dir"] @ ch["dir"], -1, 1))) > 25 for ch in chosen):
                chosen.append(c)
            if len(chosen) == N_VIEWS:
                break
        out = work / "objects" / obj["name"]
        out.mkdir(parents=True, exist_ok=True)
        tiles = []
        for i, c in enumerate(chosen):
            img = cv2.imread(str(work / "frames" / c["name"]))
            lo, hi = c["box"][:2], c["box"][2:]
            pad = 0.35 * (hi - lo).max()
            x0, y0 = np.maximum(0, (lo - pad).astype(int))
            x1, y1 = np.minimum([img.shape[1], img.shape[0]], (hi + pad).astype(int))
            crop = img[y0:y1, x0:x1]
            cv2.imwrite(str(out / f"view_{i}.jpg"), crop)
            # where the object itself is inside this crop (slightly enlarged), for the cut-out step
            m = 0.1 * (hi - lo)
            box = [float(lo[0] - m[0] - x0), float(lo[1] - m[1] - y0), float(hi[0] + m[0] - x0), float(hi[1] + m[1] - y0)]
            (out / f"view_{i}.json").write_text(json.dumps(dict(object_box=box)))
            tiles.append(cv2.resize(crop, (200, int(200 * crop.shape[0] / crop.shape[1]))))
        h = max(t.shape[0] for t in tiles)
        sheet = np.hstack([cv2.copyMakeBorder(t, 0, h - t.shape[0], 0, 4, cv2.BORDER_CONSTANT) for t in tiles])
        cv2.imwrite(str(out / "views.jpg"), sheet)
        print(f"{obj['name']}: {len(cands)} usable frames, chose {len(chosen)} -> {out}")


if __name__ == "__main__":
    main(sys.argv[1])
