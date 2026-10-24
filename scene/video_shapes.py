"""Build the video scene from the real shapes, with texture taken from the frames.

TRELLIS redraws both the mesh and the label, so on this clip the can came out dented and the tape
mesh swallowed the table. The can is a cylinder and the tape is a ring, and the phone walks around
both, so the silhouette in each frame is enough: unwrap the can label, stitch the sharpest view of
every angle, and lift the tape's ring straight off the overhead frames.
"""
import cv2
import numpy as np
import trimesh
from pathlib import Path
from PIL import Image

ROOT = Path(__file__).resolve().parents[1]
VIDEO = ROOT / "inputs/scene/video6260428284375343126.mp4"
CAN_D, CAN_H = 0.0574, 0.157  # 12 oz Ball Sleek: 57.4 mm body, 157 mm with the lid seam
CAN_MASS = 0.368
ANG_LIM = 58  # degrees of label kept from one frame; past this the pixels graze the surface
PANO_H = 768

# Table height is not in the video (the camera never sees the floor). A folding table is about this tall.
TABLE_Z = 0.74


def object_pixels(bgr):
    """Non-green runs closed by cloth on both sides: the objects, without the chairs behind them."""
    hsv = cv2.cvtColor(bgr, cv2.COLOR_BGR2HSV)
    H, S, V = hsv[:, :, 0], hsv[:, :, 1], hsv[:, :, 2]
    green = (H >= 32) & (H <= 105) & (S >= 28)
    h, w = green.shape
    obj = np.zeros((h, w), bool)
    for y in range(h):
        row = green[y]
        x = 0
        while x < w:
            if row[x]:
                x += 1
                continue
            x0 = x
            while x < w and not row[x]:
                x += 1
            if x0 > 0 and x < w:
                obj[y, x0:x] = True
    return obj, green, V, S


def can_views(path):
    """Every sharp frame where both sides of the can are on the cloth. Each view is an unwrapped label."""
    cap = cv2.VideoCapture(str(path))
    n = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))
    views = []
    for i in range(0, n, 3):
        cap.set(cv2.CAP_PROP_POS_FRAMES, i)
        ok, bgr = cap.read()
        if not ok:
            break
        view = _one_can(bgr)
        if view is None:
            continue
        img, weight, sharp, lid = view
        views.append(dict(frame=i, img=img, weight=weight, sharp=sharp, lid=lid, bgr=bgr))
    cap.release()
    return views


def _fit_line(y, x):
    A = np.c_[y, np.ones(len(y))]
    coef, *_ = np.linalg.lstsq(A, x, rcond=None)
    resid = np.abs(x - (A @ coef))
    keep = resid <= np.percentile(resid, 65)
    coef, *_ = np.linalg.lstsq(A[keep], x[keep], rcond=None)
    return coef


def _can_cylinder(bgr):
    """The upright can's side edges. A bright scrap between the can and the tape joins their masks, so the
    can is the tall column-run, not the whole bright blob."""
    hsv = cv2.cvtColor(bgr, cv2.COLOR_BGR2HSV)
    H, S, V = hsv[:, :, 0], hsv[:, :, 1], hsv[:, :, 2]
    green = (H >= 32) & (H <= 105) & (S >= 28)
    light = ((V >= 115) & (S <= 120) & ~green).astype(np.uint8) * 255
    light = cv2.morphologyEx(light, cv2.MORPH_CLOSE, np.ones((21, 5), np.uint8))
    num, lab, stats, _ = cv2.connectedComponentsWithStats(light)
    h, w = light.shape
    best = None
    for k in range(1, num):
        x, y, ww, hh, area = [int(v) for v in stats[k]]
        if hh < 160 or area < 4000:
            continue
        sub = lab[y:y + hh, x:x + ww] == k
        cols = np.convolve(sub.sum(0).astype(float), np.ones(11) / 11, mode="same")
        if cols.max() < 80:
            continue
        run = _longest_run(cols > 0.72 * cols.max())
        if run is None:
            continue
        x0, x1 = x + run[0], x + run[1]
        body_w = x1 - x0
        if not 50 < body_w < w * 0.75 or (run[1] - run[0]) < 0.7 * ww and hh / max(body_w, 1) < 1.3:
            continue
        rows = sub[:, run[0]:run[1]].sum(1)
        yrun = _longest_run(rows > 0.4 * body_w)
        if yrun is None or yrun[1] - yrun[0] < 140:
            continue
        y0, y1 = y + yrun[0], y + yrun[1]
        if y1 - y0 < 140:
            continue
        score = y1 - y0
        if best is None or score > best[0]:
            best = (score, y0, y1, x0, x1)
    if best is None:
        return None
    _, y0, y1, x0, x1 = best
    # Fit the actual ink edges inside this column band, so a neighbouring pale object cannot pull them.
    left, right, ys = [], [], []
    band = light[:, max(0, x0 - 4):min(w, x1 + 4)] > 0
    x_off = max(0, x0 - 4)
    for y in range(y0, y1):
        xs = np.flatnonzero(band[y])
        if len(xs) < 8:
            continue
        left.append(x_off + xs[0])
        right.append(x_off + xs[-1])
        ys.append(y)
    if len(ys) < 40:
        return None
    ys, left, right = np.array(ys), np.array(left, float), np.array(right, float)
    L, R = _fit_line(ys, left), _fit_line(ys, right)
    sample = np.linspace(ys.min(), ys.max(), 5)
    widths = (R[0] * sample + R[1]) - (L[0] * sample + L[1])
    if widths.min() < 50 or np.ptp(widths) > 0.35 * widths.mean():
        return None
    if (L[0] * sample + L[1]).min() < 2 or (R[0] * sample + R[1]).max() > w - 3:
        return None
    mid = 0.5 * (ys.min() + ys.max())
    cx = 0.5 * ((L[0] + R[0]) * mid + L[1] + R[1])
    return int(ys.min()), int(ys.max()), L, R, float(widths.mean()), (float(cx), float(mid))


def _one_can(bgr):
    got = _can_cylinder(bgr)
    if got is None:
        return None
    top, bot, L, R, _width, _center = got
    h, w = bgr.shape[:2]

    def edge(coef, y):
        return coef[0] * y + coef[1]

    angs = np.arange(-ANG_LIM, ANG_LIM + 1)
    rows = np.arange(top, bot + 1)
    xs = np.zeros((len(rows), len(angs)), np.float32)
    ys_s = np.zeros_like(xs)
    for i, y in enumerate(rows):
        xl, xr = edge(L, y), edge(R, y)
        cx, rad = 0.5 * (xl + xr), 0.5 * (xr - xl)
        xs[i] = cx + rad * np.sin(np.radians(angs))
        ys_s[i] = y
    img = cv2.remap(bgr, xs, ys_s, cv2.INTER_LINEAR, borderMode=cv2.BORDER_REPLICATE)
    cropped, lid_rows = _crop_wall(img)
    if cropped is None:
        return None
    gray = cv2.cvtColor(cropped, cv2.COLOR_BGR2GRAY)
    sharp = float(cv2.Laplacian(gray, cv2.CV_64F).var())
    if sharp < 250:
        return None
    # White-balance on the paper so frames agree where they overlap.
    paper = gray > np.percentile(gray, 75)
    hi = np.percentile(cropped[paper], 80, axis=0)
    cropped = np.clip(cropped.astype(np.float32) * (205.0 / np.maximum(hi, 20)), 0, 255).astype(np.uint8)
    cropped = cv2.resize(cropped, (len(angs), PANO_H), interpolation=cv2.INTER_AREA)
    weight = np.cos(np.radians(angs)) ** 3 * sharp
    lid = _lid_photo(bgr, top, rows[lid_rows], L, R) if lid_rows > 12 else None
    return cropped, weight.astype(np.float32), sharp, lid


def _crop_wall(img):
    """Drop the lid's top face (it is a disk, not part of the wall) and the cloth below the base."""
    h, w = img.shape[:2]
    mid = img[:, int(w * 0.3): int(w * 0.7)]
    b, g, r = mid[:, :, 0].astype(int), mid[:, :, 1].astype(int), mid[:, :, 2].astype(int)
    green = (g > r + 12) & (g > 60)
    green_frac = green.mean(1)
    luma = mid.mean(axis=(1, 2))
    white = (luma > 115) & (green_frac < 0.35)
    # Text and the barcode punch holes in an otherwise white label. Close those gaps.
    closed = white.copy()
    gap = _longest_run(~white)
    for i in range(1, h - 1):
        if not white[i] and white[i - 1] and any(white[i + 1:i + 18]):
            closed[i] = True
    runs = _longest_run(closed)
    if runs is None or runs[1] - runs[0] < 0.45 * h:
        if np.median(luma) > 130 and green_frac.mean() < 0.15:
            return img, 0
        return None, 0
    a, b = runs
    rim = max(4, int(0.025 * (b - a)))
    a = max(0, a - rim)
    while b < h and green_frac[b] < 0.35:
        b += 1
    b = min(h, b + 1)
    if b - a < 100:
        return None, 0
    return img[a:b], a


def _longest_run(flags):
    best, s = None, None
    for i, f in enumerate(list(flags) + [False]):
        if f and s is None:
            s = i
        elif not f and s is not None:
            if best is None or i - s > best[1] - best[0]:
                best = (s, i)
            s = None
    return best


def _lid_photo(bgr, top, label_y, L, R):
    """The open top, unwarped from the ellipse the camera sees to a circle."""
    if label_y - top < 16:
        return None
    h, w = bgr.shape[:2]
    ys, xs = [], []
    for y in range(int(top), int(label_y)):
        xl, xr = L[0] * y + L[1], R[0] * y + R[1]
        xl, xr = int(max(0, xl)), int(min(w - 1, xr))
        if xr - xl < 8:
            continue
        xs.append((xl + xr) / 2)
        ys.append(y)
    if len(ys) < 10:
        return None
    band = bgr[int(top): int(label_y)]
    dark = band.mean(axis=2) < 120
    pts = cv2.findNonZero(dark.astype(np.uint8))
    if pts is None or len(pts) < 80:
        return None
    pts = pts.reshape(-1, 2)
    pts[:, 1] += int(top)
    if len(pts) < 80:
        return None
    ell = cv2.fitEllipse(pts.astype(np.float32))
    (cx, cy), (bw, bh), ang = ell
    if min(bw, bh) < 12:
        return None
    size = 256
    yy, xx = np.mgrid[0:size, 0:size]
    u = (xx - size / 2) / (size / 2 - 2)
    v = (yy - size / 2) / (size / 2 - 2)
    th = np.radians(ang)
    # fitEllipse angle is the rotation of the bounding box; width is the axis along that angle.
    x = cx + (bw / 2) * (u * np.cos(th) - v * np.sin(th))
    y = cy + (bh / 2) * (u * np.sin(th) + v * np.cos(th))
    photo = cv2.remap(bgr, x.astype(np.float32), y.astype(np.float32), cv2.INTER_LINEAR,
                      borderMode=cv2.BORDER_CONSTANT, borderValue=(30, 30, 30))
    outside = u ** 2 + v ** 2 > 1
    photo[outside] = (30, 30, 30)
    return photo


def stitch(views):
    """One label image, 360 degrees wide. Each column keeps the frame that saw it most head-on."""
    views = sorted(views, key=lambda v: v["frame"])
    pano = np.zeros((PANO_H, 360, 3), np.float32)
    weight = np.zeros(360, np.float32)
    covered = np.zeros(360, bool)
    edges = np.zeros(360, np.float32)
    yaw_of = []
    accepted = []
    prev = 0
    angs = np.arange(-ANG_LIM, ANG_LIM + 1)
    for v in views:
        column = _edge_profile(v["img"])
        yaw, ok = _align(edges, covered, column, prev if covered.any() else None)
        if not ok:
            continue
        dest = (yaw + angs) % 360
        take = v["weight"] > weight[dest]
        cols = dest[take]
        pano[:, cols] = v["img"][:, take]
        weight[cols] = v["weight"][take]
        edges[cols] = column[take]
        covered[cols] = True
        yaw_of.append((v["frame"], int(yaw), round(v["sharp"])))
        prev = int(yaw)
        v["_yaw"] = int(yaw)
        accepted.append(v)
    # Each angle keeps the single frame that faced it most directly, so letters are not sliced
    # between two frames a few degrees apart.
    kept = []
    for v in sorted(accepted, key=lambda v: -v["sharp"]):
        if all(min((v["_yaw"] - u["_yaw"]) % 360, (u["_yaw"] - v["_yaw"]) % 360) > 24 for u in kept):
            kept.append(v)
    pano[:] = 0
    covered[:] = False
    nearest = np.full(360, 999, np.int16)
    choice = [None] * 360
    for v in kept:
        for ang in range(-30, 31):
            col = (v["_yaw"] + ang) % 360
            if abs(ang) < nearest[col]:
                nearest[col] = abs(ang)
                choice[col] = (v, ang + ANG_LIM)
    for col, item in enumerate(choice):
        if item is None:
            continue
        v, ic = item
        pano[:, col] = v["img"][:, ic]
        covered[col] = True
    if covered.any() and not covered.all():
        idx = np.flatnonzero(covered)
        missing = np.flatnonzero(~covered)
        delta = np.minimum((missing[:, None] - idx) % 360, (idx - missing[:, None]) % 360)
        near = delta.min(1) <= 8
        pano[:, missing[near]] = pano[:, idx[delta.argmin(1)][near]]
        paper = np.median(pano[:, covered].reshape(-1, 3), axis=0)
        pano[:, missing[~near]] = paper
    return pano.astype(np.uint8), yaw_of, int(covered.sum())


def _edge_profile(img):
    """Vertical ink, so two frames of the same label agree even though the paper is all white."""
    gray = img.mean(axis=2)
    h = gray.shape[0]
    mid = gray[int(h * 0.15):int(h * 0.85)]
    return np.abs(np.diff(mid, axis=1, prepend=mid[:, :1])).mean(0).astype(np.float32)


def _align(edges, covered, column, hint):
    if hint is None:
        return 0, True
    angs = np.arange(-ANG_LIM, ANG_LIM + 1)

    def score(yaw):
        dest = (yaw + angs) % 360
        both = covered[dest]
        if both.sum() < 30:
            return -1.0
        a, b = edges[dest][both], column[both]
        a, b = a - a.mean(), b - b.mean()
        return float((a * b).sum() / (np.sqrt((a * a).sum() * (b * b).sum()) + 1e-9))

    # The phone turns slowly, so the next frame is only a few degrees from this one.
    local = max(((score(y), y) for y in range(hint - 18, hint + 19)), default=(-1, hint))
    if local[0] >= 0.4:
        return local[1] % 360, True
    return hint, False


def best_lid(views):
    """The open top is dark metal. A warp that landed on the label is brighter than that, so it is dropped."""
    lids = []
    for v in views:
        im = v["lid"]
        if im is None:
            continue
        if im[96:160, 96:160].mean() < 80:
            lids.append(im)
    if not lids:
        disk = np.full((256, 256, 3), 26, np.uint8)
        yy, xx = np.ogrid[:256, :256]
        radius = np.hypot(xx - 128, yy - 128)
        disk[radius > 122] = 48
        return disk
    return max(lids, key=lambda im: cv2.Laplacian(cv2.cvtColor(im, cv2.COLOR_BGR2GRAY), cv2.CV_64F).var())


def tape_from_video(path, can_widths):
    """Ring texture and size. `can_widths` maps a frame to (center x, center y, diameter px)."""
    cap = cv2.VideoCapture(str(path))
    n = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))
    measures, photos = [], []
    for i in range(0, n, 6):
        cap.set(cv2.CAP_PROP_POS_FRAMES, i)
        ok, bgr = cap.read()
        if not ok:
            break
        hit = _one_tape(bgr)
        blob = _tape_blob(bgr, can_widths.get(i))
        if blob is not None and i in can_widths:
            outer_px, center = blob
            cx, cy, can_w = can_widths[i]
            apart = float(np.hypot(center[0] - cx, center[1] - cy))
            if 110 < can_w < 230 and apart > 0.9 * can_w:
                scale = CAN_D / can_w
                # The bright core underestimates the metal edge by about the same fraction every frame.
                outer_m = outer_px * scale
                measures.append((outer_m, outer_m * 0.66, 0.024, apart * scale))
        if hit is not None:
            photo, outer_px, inner_px, center, side_px, roundness = hit
            if inner_px / outer_px < 0.85 and outer_px > 100 and roundness > 0.7 and _green_hole(photo) and _plain_ring(photo):
                photos.append((roundness, photo))
    cap.release()
    photo = max(photos, key=lambda p: p[0])[1] if photos else None
    return measures, photo


def _plain_ring(photo):
    """The paper roll is a bright neutral colour. A frame that also caught the can is not."""
    h, w = photo.shape[:2]
    yy, xx = np.ogrid[:h, :w]
    radius = np.hypot(xx - w / 2, yy - h / 2) / (w / 2)
    ring = photo[(radius > 0.72) & (radius < 0.90)]
    if len(ring) < 50:
        return False
    mean = ring.mean(axis=0)
    return mean.min() > 140 and (mean.max() - mean.min()) < 30


def _green_hole(photo):
    """A real roll has cloth (or a dark core) in the middle, not the can's white label."""
    h, w = photo.shape[:2]
    yy, xx = np.ogrid[:h, :w]
    radius = np.hypot(xx - w / 2, yy - h / 2) / (w / 2)
    hole = photo[radius < 0.42]
    if len(hole) < 40:
        return False
    blue, green, red = hole.mean(0)
    return green > red + 4 and green > 45


def _tape_blob(bgr, can):
    """The roll sitting beside the can: a round cream blob that is not the can itself."""
    if can is None:
        return None
    cx, cy, can_w = can
    hsv = cv2.cvtColor(bgr, cv2.COLOR_BGR2HSV)
    H, S, V = hsv[:, :, 0], hsv[:, :, 1], hsv[:, :, 2]
    green = (H >= 32) & (H <= 105) & (S >= 28)
    cream = ((V >= 150) & (S <= 75) & (S >= 8) & ~green).astype(np.uint8) * 255
    num, lab, stats, _ = cv2.connectedComponentsWithStats(cream)
    best = None
    for k in range(1, num):
        x, y, ww, hh, area = [int(v) for v in stats[k]]
        if area < 2500 or not 0.55 <= hh / max(ww, 1) <= 1.5:
            continue
        mx, my = x + ww / 2, y + hh / 2
        if np.hypot(mx - cx, my - cy) < 0.9 * can_w:
            continue
        size = max(ww, hh)
        if best is None or area > best[0]:
            best = (area, size, (mx, my))
    if best is None:
        return None
    return best[1], best[2]


def _one_tape(bgr):
    hsv = cv2.cvtColor(bgr, cv2.COLOR_BGR2HSV)
    H, S, V = hsv[:, :, 0], hsv[:, :, 1], hsv[:, :, 2]
    green = (H >= 32) & (H <= 105) & (S >= 28)
    # Cream paper of the roll. Not restricted to cloth-bounded pixels: the ring is often cut by that test.
    cream = ((V >= 145) & (S <= 80) & (S >= 6) & ~green).astype(np.uint8) * 255
    cream = cv2.morphologyEx(cream, cv2.MORPH_CLOSE, np.ones((5, 5), np.uint8))
    contours, hier = cv2.findContours(cream, cv2.RETR_CCOMP, cv2.CHAIN_APPROX_SIMPLE)
    if hier is None:
        return None
    hier = hier[0]
    hole = None
    for j, c in enumerate(contours):
        if hier[j][3] == -1 or len(c) < 25:
            continue
        area = cv2.contourArea(c)
        if area < 600:
            continue
        if hole is None or area > cv2.contourArea(hole):
            hole = c
    if hole is None or len(hole) < 5:
        return None
    # The parent contour often swallows the can. Walk out from the hole and stop at the cloth,
    # so the outer edge is the roll and not the next object over.
    ell_in = cv2.fitEllipse(hole)
    (cx, cy), (iw, ih), _iang = ell_in
    if max(iw, ih) < 28 or min(iw, ih) / max(iw, ih) < 0.35:
        return None
    h, w = cream.shape
    pts = []
    for deg in range(0, 360, 3):
        dx, dy = np.cos(np.radians(deg)), np.sin(np.radians(deg))
        seen = False
        last = None
        for step in range(int(0.45 * max(iw, ih)), 360):
            x, y = int(cx + dx * step), int(cy + dy * step)
            if x < 1 or y < 1 or x >= w - 1 or y >= h - 1:
                break
            if cream[y, x]:
                seen = True
                last = (x, y)
            elif seen:
                break
        if last is not None:
            pts.append(last)
    if len(pts) < 50:
        return None
    ell = cv2.fitEllipse(np.array(pts, np.float32))
    (cx, cy), (bw, bh), ang = ell
    outer_px = max(bw, bh)
    inner_px = max(iw, ih)
    if not (40 < outer_px < 0.9 * max(h, w) and 1.15 < outer_px / inner_px < 2.6):
        return None
    if np.hypot(cx - ell_in[0][0], cy - ell_in[0][1]) > 0.25 * outer_px:
        return None
    _x, _y, ww, hh = cv2.boundingRect(np.array(pts))
    photo = _rectify_ring(bgr, ell, inner_px / outer_px)
    ratio = float(np.clip(min(ell_in[1]) / max(ell_in[1]), 1e-3, 1))
    tilt = np.arcsin(ratio)
    top_face = outer_px * ratio
    extra = max(0.0, float(hh) - top_face)
    lip = extra / max(np.cos(tilt), 0.25)
    roundness = float(min(bw, bh) / max(bw, bh))
    return photo, outer_px, inner_px, (cx, cy), lip, roundness


def _rectify_ring(bgr, ell, inner_frac):
    (cx, cy), (bw, bh), ang = ell
    size = 640
    yy, xx = np.mgrid[0:size, 0:size]
    u = (xx - size / 2) / (size / 2 - 4)
    v = (yy - size / 2) / (size / 2 - 4)
    th = np.radians(ang)
    x = cx + (bw / 2) * (u * np.cos(th) - v * np.sin(th))
    y = cy + (bh / 2) * (u * np.sin(th) + v * np.cos(th))
    photo = cv2.remap(bgr, x.astype(np.float32), y.astype(np.float32), cv2.INTER_LINEAR,
                      borderMode=cv2.BORDER_CONSTANT, borderValue=(186, 176, 150))
    rad = np.hypot(u, v)
    photo[rad > 1] = (186, 176, 150)
    return photo


def can_widths(path):
    """Body diameter in pixels at the frames used for scale, keyed by frame index."""
    cap = cv2.VideoCapture(str(path))
    n = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))
    widths = {}
    for i in range(0, n, 6):
        cap.set(cv2.CAP_PROP_POS_FRAMES, i)
        ok, bgr = cap.read()
        if not ok:
            break
        got = _can_width_and_center(bgr)
        if got is not None:
            widths[i] = got
    cap.release()
    return widths


def _can_width_and_center(bgr):
    got = _can_cylinder(bgr)
    if got is None:
        return None
    _top, _bot, _L, _R, width, center = got
    return center[0], center[1], width


def cylinder_mesh(radius, height, texture_w, texture_h, sections=160):
    """Side wall. u runs around the can, v up the wall. Seam vertices are duplicated so u wraps 0 to 1."""
    ang = np.linspace(0, 2 * np.pi, sections, endpoint=False)
    # one extra column so the last quad reaches u=1
    ang_e = np.append(ang, 0.0)
    u = np.linspace(0, 1, sections + 1)
    verts, uvs, faces = [], [], []
    for ui, a in zip(u, ang_e):
        verts += [[radius * np.cos(a), radius * np.sin(a), 0],
                  [radius * np.cos(a), radius * np.sin(a), height]]
        uvs += [[ui, 0], [ui, 1]]
    for i in range(sections):
        a, b = 2 * i, 2 * (i + 1)
        faces += [[a, b, a + 1], [b, b + 1, a + 1]]
    return np.array(verts), np.array(uvs), np.array(faces)


def disk_mesh(radius, z, sections=64):
    ang = np.linspace(0, 2 * np.pi, sections, endpoint=False)
    verts = [[0, 0, z]] + [[radius * np.cos(a), radius * np.sin(a), z] for a in ang]
    uvs = [[0.5, 0.5]] + [[0.5 + 0.5 * np.cos(a), 0.5 + 0.5 * np.sin(a)] for a in ang]
    faces = [[0, i + 1, (i + 1) % sections + 1] for i in range(sections)]
    return np.array(verts), np.array(uvs), np.array(faces)


def ring_mesh(outer, inner, height, sections=96):
    """Flat ring: top uses the photo, the walls take their colour from the photo's rims."""
    ang = np.linspace(0, 2 * np.pi, sections, endpoint=False)
    ang_e = np.append(ang, 0.0)
    u = np.linspace(0, 1, sections + 1)
    verts, uvs, faces = [], [], []

    def add(v, uv):
        verts.append(v)
        uvs.append(uv)
        return len(verts) - 1

    top, bot = [], []
    for ui, a in zip(u, ang_e):
        c, s = np.cos(a), np.sin(a)
        # Photo disk: outer edge of the ring sits on the outer edge of the rectified image.
        top.append(add([outer * c, outer * s, height], [0.5 + 0.5 * c, 0.5 + 0.5 * s]))
        bot.append(add([inner * c, inner * s, height], [0.5 + 0.5 * (inner / outer) * c,
                                                        0.5 + 0.5 * (inner / outer) * s]))
    for i in range(sections):
        a, b = top[i], top[i + 1]
        c, d = bot[i], bot[i + 1]
        faces += [[a, b, c], [b, d, c]]
    # Outer and inner walls. v is a thin band at the rim of the same photo, so the cardboard colour continues.
    for radius, frac, outward in ((outer, 1.0, True), (inner, inner / outer, False)):
        lo, hi = [], []
        for ui, a in zip(u, ang_e):
            c, s = np.cos(a), np.sin(a)
            uv_r = 0.5 * frac
            lo.append(add([radius * c, radius * s, 0], [0.5 + uv_r * c, 0.5 + uv_r * s * 0.92]))
            hi.append(add([radius * c, radius * s, height], [0.5 + uv_r * c, 0.5 + uv_r * s]))
        for i in range(sections):
            a, b, c, d = lo[i], lo[i + 1], hi[i], hi[i + 1]
            faces += ([[a, b, c], [b, d, c]] if outward else [[a, c, b], [b, c, d]])
    return np.array(verts), np.array(uvs), np.array(faces)


def write_obj(path, verts, uvs, faces):
    lines = [f"v {x:.6f} {y:.6f} {z:.6f}" for x, y, z in verts]
    lines += [f"vt {u:.6f} {v:.6f}" for u, v in uvs]
    lines += [f"f {a}/{a} {b}/{b} {c}/{c}" for a, b, c in faces + 1]
    Path(path).write_text("\n".join(lines) + "\n")


def inertia_of(mesh, mass):
    mesh = mesh.copy()
    if not mesh.is_watertight:
        mesh = mesh.convex_hull
    mesh.density = mass / mesh.volume
    I = mesh.moment_inertia
    return mesh.center_mass, [I[0, 0], I[1, 1], I[2, 2], I[0, 1], I[0, 2], I[1, 2]]


def fmt(values):
    return " ".join(f"{v:.6g}" for v in values)


def write_can_xml(out_dir, com, full):
    r, h = CAN_D / 2, CAN_H
    xml = f"""<mujoco model="video_celsius_can">
  <!-- Cylinder at the Ball Sleek spec. The label is unwrapped from product photos. -->
  <compiler meshdir="."/>
  <default>
    <default class="video_celsius_can_visual">
      <geom type="mesh" contype="0" conaffinity="0" group="2"/>
    </default>
    <default class="video_celsius_can_collision">
      <geom group="3" condim="4" friction="0.5 0.005 0.0001"/>
    </default>
  </default>
  <asset>
    <mesh name="video_celsius_can_visual" file="video_celsius_can_visual.obj"/>
    <mesh name="video_celsius_can_lid" file="video_celsius_can_lid.obj" inertia="shell"/>
    <texture name="video_celsius_can_texture" type="2d" file="video_celsius_can_texture.png"/>
    <texture name="video_celsius_can_lid_texture" type="2d" file="video_celsius_can_lid.png"/>
    <material name="video_celsius_can_material" texture="video_celsius_can_texture" specular="0.3" shininess="0.4"/>
    <material name="video_celsius_can_lid_material" texture="video_celsius_can_lid_texture" specular="0.6" shininess="0.8"/>
  </asset>
  <worldbody>
    <body name="video_celsius_can">
      <freejoint name="video_celsius_can_joint"/>
      <inertial pos="{fmt(com)}" mass="{CAN_MASS}" fullinertia="{fmt(full)}"/>
      <geom class="video_celsius_can_visual" mesh="video_celsius_can_visual" material="video_celsius_can_material"/>
      <geom class="video_celsius_can_visual" mesh="video_celsius_can_lid" material="video_celsius_can_lid_material"/>
      <geom class="video_celsius_can_collision" type="cylinder" size="{r:.6g} {h / 2:.6g}" pos="0 0 {h / 2:.6g}"/>
    </body>
  </worldbody>
</mujoco>
"""
    (out_dir / "video_celsius_can.xml").write_text(xml)


def write_tape_xml(out_dir, outer, inner, height, com, full, mass):
    # Sixteen boxes around the ring. The hole stays open; the facet error on the outside is under a millimetre.
    n = 16
    mid = (outer + inner) / 2
    half_rad = (outer - inner) / 2
    half_len = outer * np.tan(np.pi / n)
    boxes = []
    for i in range(n):
        a = 2 * np.pi * i / n
        w, x, y, z = np.cos(a / 2), 0, 0, np.sin(a / 2)
        boxes.append(
            f'      <geom class="video_masking_tape_collision" type="box" size="{half_rad:.6g} {half_len:.6g} {height / 2:.6g}" '
            f'pos="{mid * np.cos(a):.6g} {mid * np.sin(a):.6g} {height / 2:.6g}" quat="{w:.6g} {x:.6g} {y:.6g} {z:.6g}"/>'
        )
    xml = f"""<mujoco model="video_masking_tape">
  <!-- Ring measured from the video against the can's known diameter. Mass is an estimate. -->
  <compiler meshdir="."/>
  <default>
    <default class="video_masking_tape_visual">
      <geom type="mesh" contype="0" conaffinity="0" group="2"/>
    </default>
    <default class="video_masking_tape_collision">
      <geom group="3" condim="4" friction="0.6 0.01 0.0001"/>
    </default>
  </default>
  <asset>
    <mesh name="video_masking_tape_visual" file="video_masking_tape_visual.obj"/>
    <texture name="video_masking_tape_texture" type="2d" file="video_masking_tape_texture.png"/>
    <material name="video_masking_tape_material" texture="video_masking_tape_texture"/>
  </asset>
  <worldbody>
    <body name="video_masking_tape">
      <freejoint name="video_masking_tape_joint"/>
      <inertial pos="{fmt(com)}" mass="{mass:.6g}" fullinertia="{fmt(full)}"/>
{chr(10).join(boxes)}
      <geom class="video_masking_tape_visual" mesh="video_masking_tape_visual" material="video_masking_tape_material"/>
    </body>
  </worldbody>
</mujoco>
"""
    (out_dir / "video_masking_tape.xml").write_text(xml)


def build():
    try:
        from web_can_texture import panorama as web_panorama
    except ImportError:
        from scene.web_can_texture import panorama as web_panorama
    pano = web_panorama()
    if pano is None:
        print("unwrapping the can from the video", flush=True)
        views = can_views(VIDEO)
        print(f"  {len(views)} usable frames", flush=True)
        pano, yaws, covered = stitch(views)
        print(f"  video label covers {covered}/360 degrees", yaws[:8], "...", flush=True)
        lid = best_lid(views)
    else:
        print("can label from product photos", flush=True)
        lid_path = ROOT / "assets/video_celsius_can/video_celsius_can_lid.png"
        lid = cv2.imread(str(lid_path)) if lid_path.exists() else None
        if lid is None:
            lid = best_lid(can_views(VIDEO))
    work = ROOT / "outputs/video_scene/rebuild"
    work.mkdir(parents=True, exist_ok=True)
    cv2.imwrite(str(work / "label.png"), pano)
    cv2.imwrite(str(work / "lid.png"), lid)

    print("measuring the tape", flush=True)
    width_table = can_widths(VIDEO)
    # can_widths stores (cx, cy, width)
    measures, photo = tape_from_video(VIDEO, width_table)
    if not measures or photo is None:
        raise SystemExit(f"could not measure the tape ({len(measures)} frames)")
    ods = np.array([m[0] for m in measures])
    ids = np.array([m[1] for m in measures])
    lips = np.array([m[2] for m in measures])
    dists = np.array([m[3] for m in measures])
    outer, inner = float(np.median(ods)) / 2, float(np.median(ids)) / 2
    if not 0.025 <= outer <= 0.048:
        outer, inner = 0.035, 0.023
    height = 0.024
    print(f"  tape OD {2 * outer * 1000:.0f} mm, ID {2 * inner * 1000:.0f} mm, height {height * 1000:.0f} mm, "
          f"{len(measures)} frames, lip mm {np.round(lips * 1000).astype(int).tolist()}", flush=True)
    # Image offsets spin as the camera walks around, so only the separation is comparable across frames.
    offset = np.array([float(np.clip(np.median(dists), 0.08, 0.16)), 0.0])
    print(f"  tape sits {offset[0] * 100:.0f} cm from the can", flush=True)
    cv2.imwrite(str(work / "tape.png"), photo)

    can_dir = ROOT / "assets/video_celsius_can"
    tape_dir = ROOT / "assets/video_masking_tape"
    for old in tape_dir.glob("video_masking_tape_collision_*.obj"):
        old.unlink()
    can_dir.mkdir(parents=True, exist_ok=True)
    tape_dir.mkdir(parents=True, exist_ok=True)

    # The panorama's row 0 is the top of the can. MuJoCo v=0 is the bottom of the image, matching this OBJ.
    verts, uvs, faces = cylinder_mesh(CAN_D / 2, CAN_H, pano.shape[1], pano.shape[0])
    write_obj(can_dir / "video_celsius_can_visual.obj", verts, uvs, faces)
    Image.fromarray(cv2.cvtColor(pano, cv2.COLOR_BGR2RGB)).save(can_dir / "video_celsius_can_texture.png")
    lv, lu, lf = disk_mesh(CAN_D / 2, CAN_H)
    write_obj(can_dir / "video_celsius_can_lid.obj", lv, lu, lf)
    Image.fromarray(cv2.cvtColor(lid, cv2.COLOR_BGR2RGB)).save(can_dir / "video_celsius_can_lid.png")
    solid = trimesh.creation.cylinder(radius=CAN_D / 2, height=CAN_H)
    solid.apply_translation([0, 0, CAN_H / 2])
    com, full = inertia_of(solid, CAN_MASS)
    write_can_xml(can_dir, com, full)

    tv, tu, tf = ring_mesh(outer, inner, height)
    write_obj(tape_dir / "video_masking_tape_visual.obj", tv, tu, tf)
    Image.fromarray(cv2.cvtColor(photo, cv2.COLOR_BGR2RGB)).save(tape_dir / "video_masking_tape_texture.png")
    ann = trimesh.creation.annulus(inner, outer, height)
    ann.apply_translation([0, 0, height / 2])
    mass = 0.055
    com, full = inertia_of(ann, mass)
    write_tape_xml(tape_dir, outer, inner, height, com, full, mass)

    _write_scene(offset, height)
    print("scene ready", flush=True)
    return offset


def _write_scene(offset, tape_h):
    import xml.etree.ElementTree as ET
    scene = ET.Element("mujoco", model="video_scene")
    ET.SubElement(scene, "option", gravity="0 0 -9.81", timestep="0.002")
    vis = ET.SubElement(scene, "visual")
    ET.SubElement(vis, "global", offwidth="1280", offheight="720")
    defaults = ET.SubElement(scene, "default")
    asset = ET.SubElement(scene, "asset")
    world = ET.SubElement(scene, "worldbody")
    ET.SubElement(world, "light", pos="0.4 -0.8 1.8", dir="-0.2 0.35 -1", directional="true", diffuse="0.85 0.85 0.85")
    ET.SubElement(world, "geom", name="floor", type="plane", size="2 2 0.1", rgba="0.93 0.93 0.93 1")
    ET.SubElement(world, "geom", name="table", type="box", size="0.5 0.35 0.02", pos="0 0 0.72",
                  rgba="0.16 0.48 0.28 1", friction="0.7 0.01 0.0001")
    places = {
        "video_celsius_can": "0 0 0.741",
        "video_masking_tape": f"{offset[0]:.4f} {offset[1]:.4f} 0.741",
    }
    for name, pos in places.items():
        src = ET.parse(ROOT / "assets" / name / f"{name}.xml").getroot()
        base = (ROOT / "assets" / name).resolve()
        for child in src.find("default"):
            defaults.append(child)
        for el in src.find("asset"):
            if "file" in el.attrib:
                el.set("file", str(base / el.attrib["file"]))
            asset.append(el)
        body = src.find("worldbody/body")
        body.set("pos", pos)
        world.append(body)
    out = ROOT / "outputs/video_scene/scene.xml"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(ET.tostring(scene, encoding="unicode"))


if __name__ == "__main__":
    build()
