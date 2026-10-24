"""Pick the best photos of an object from one or more videos (fixed rules, no AI, same result every time).

    frames = pick_frames(["a.mp4", "b.mp4"], out_dir, n=4)

1. candidates: the sharpest frame in each of `candidates` equal time slices over all videos
2. keep a candidate only if the cut-out object is whole (not cut by the image border), big enough, and one
   piece (not several things)
3. choose `n` of them that look as different as possible (different sides of the object): farthest-point
   sampling on a small colour + shape signature of the cut-out object
"""
from pathlib import Path

import cv2
import numpy as np

from cutout import cut_out

VIDEO_EXT = {".mp4", ".mov", ".avi", ".mkv", ".m4v", ".webm"}


def is_video(path):
    return Path(path).suffix.lower() in VIDEO_EXT


def sharpness(img):
    small = cv2.resize(img, None, fx=480 / max(img.shape[:2]), fy=480 / max(img.shape[:2]))
    return cv2.Laplacian(cv2.cvtColor(small, cv2.COLOR_BGR2GRAY), cv2.CV_64F).var()


def read_candidates(videos, candidates):
    """Sharpest frame of each time slice, over all videos together."""
    frames = []  # (video index, frame index, sharpness)
    for v, path in enumerate(videos):
        cap = cv2.VideoCapture(str(path))
        i = 0
        while True:
            ok, img = cap.read()
            if not ok:
                break
            frames.append((v, i, sharpness(img)))
            i += 1
        cap.release()
    if not frames:
        raise ValueError(f"no frames could be read from {videos}")
    picks = []
    for k in range(candidates):
        seg = frames[k * len(frames) // candidates:(k + 1) * len(frames) // candidates]
        if seg:
            picks.append(max(seg, key=lambda f: f[2]))
    return picks


def grab(path, index):
    cap = cv2.VideoCapture(str(path))
    cap.set(cv2.CAP_PROP_POS_FRAMES, index)
    ok, img = cap.read()
    cap.release()
    return img


def check_cutout(rgba):
    """(ok, reason, object area share, signature, box) for one cut-out frame. Judges the main (largest) cut-out
    piece only: small things around it (a phone, tape) are ignored, and `box` keeps only the main object
    when the frame is cut out again for the 3D model."""
    alpha = rgba[:, :, 3] > 0
    n, labels, stats, _ = cv2.connectedComponentsWithStats(alpha.astype(np.uint8))
    if n < 2:
        return False, "object not found", 0.0, None, None
    sizes = stats[1:, cv2.CC_STAT_AREA]
    main = 1 + int(np.argmax(sizes))
    if (np.sort(sizes)[-2] if len(sizes) > 1 else 0) > 0.3 * sizes.max():
        return False, "two big things cut out (which is the object?)", alpha.mean(), None, None
    alpha = labels == main
    rgba = rgba.copy()
    rgba[:, :, 3] = np.where(alpha, 255, 0)
    area = alpha.mean()
    if area < 0.02:
        return False, "object too small", area, None, None
    if area > 0.7:
        return False, "cut-out is most of the frame (background kept?)", area, None, None
    border = np.concatenate([alpha[:3].ravel(), alpha[-3:].ravel(), alpha[:, :3].ravel(), alpha[:, -3:].ravel()])
    if border.mean() > 0.002:
        return False, "object cut off by the image border", area, None, None
    # signature: colours (HSV histogram) + coarse shape/texture of the object crop
    ys, xs = np.nonzero(alpha)
    crop = rgba[ys.min():ys.max() + 1, xs.min():xs.max() + 1]
    rgb, mask = cv2.cvtColor(np.ascontiguousarray(crop[:, :, :3]), cv2.COLOR_RGB2BGR), crop[:, :, 3]
    hsv = cv2.cvtColor(rgb, cv2.COLOR_BGR2HSV)
    hist = cv2.calcHist([hsv], [0, 1], mask, [12, 4], [0, 180, 0, 256]).ravel()
    hist /= hist.sum() + 1e-9
    gray = cv2.resize(cv2.cvtColor(rgb, cv2.COLOR_BGR2GRAY), (16, 16), interpolation=cv2.INTER_AREA)
    gray = (gray - gray.mean()) / (gray.std() + 1e-9) / 16
    box = (int(xs.min()), int(ys.min()), int(xs.max()), int(ys.max()))
    return True, "ok", area, np.concatenate([hist * 4, gray.ravel()]), box


def pick_frames(videos, out_dir, n=4, candidates=24):
    """Saves the chosen frames to out_dir/frames and a contact sheet; returns (frame paths, keep boxes)."""
    out_dir = Path(out_dir)
    cand_dir = out_dir / "candidates"
    cand_dir.mkdir(parents=True, exist_ok=True)
    good = []
    for v, i, sharp in read_candidates(videos, candidates):
        img = grab(videos[v], i)
        path = cand_dir / f"v{v}_f{i:05d}.jpg"
        cv2.imwrite(str(path), img)
        ok, reason, area, sig, box = check_cutout(np.asarray(cut_out(path)))
        print(f"   video {v + 1} frame {i:5d}: sharpness {sharp:6.0f}, object {100 * area:4.1f}% -> {reason}")
        if ok:
            good.append(dict(path=path, sharp=sharp, area=area, sig=sig, box=box))
    if not good:
        raise ValueError("no usable frame: film the whole object, slowly, filling a good part of the picture")
    # keep only frames of the MAIN object (the one in most frames): another object that happens to be the
    # biggest thing in a few frames (a tape roll next to a can) has different colours. Colours (the histogram
    # part of the signature) stay similar for one object from all sides; the frame most like all others is it.
    hist = np.array([g["sig"][:48] for g in good])
    dist = np.linalg.norm(hist[:, None] - hist[None], axis=2)
    main = int(np.argmin(dist.sum(1)))
    limit = 2.0 * np.median(dist[main]) + 1e-6
    for g, d in zip(good, dist[main]):
        if d > limit:
            print(f"   {g['path'].name}: a different object (colours differ) -> skipped")
    good = [g for g, d in zip(good, dist[main]) if d <= limit]
    # start with the clearest frame (sharp and large), then always add the one most unlike those chosen
    score = lambda g: np.log(g["sharp"] + 1) + 4 * g["area"]  # noqa: E731
    chosen = [max(good, key=score)]
    while len(chosen) < min(n, len(good)):
        rest = [g for g in good if g not in chosen]
        chosen.append(max(rest, key=lambda g: min(np.linalg.norm(g["sig"] - c["sig"]) for c in chosen)))
    frame_dir = out_dir / "frames"
    frame_dir.mkdir(exist_ok=True)
    paths = []
    for k, g in enumerate(chosen):
        p = frame_dir / f"view_{k}.jpg"
        p.write_bytes(g["path"].read_bytes())
        paths.append(p)
    sheet = np.hstack([cv2.resize(cv2.imread(str(p)), (240, 320)) for p in paths])
    cv2.imwrite(str(out_dir / "chosen_frames.jpg"), sheet)
    for p in cand_dir.iterdir():  # candidates were only needed to choose
        p.unlink()
    cand_dir.rmdir()
    print(f"   chose {len(paths)} of {len(good)} usable frames -> {out_dir / 'chosen_frames.jpg'}")
    return paths, [g["box"] for g in chosen]
