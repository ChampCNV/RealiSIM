"""Cylinder label for the video can, taken from product photos instead of the phone video.

The video only walks part-way around the can, so the stitched wrap is blank on the far side.
A straight product photo is an orthographic view of the cylinder: a pixel left or right of
centre is sin(angle) across the radius, which unwraps into one strip of the label. The front
photo supplies the logo. A three-view sheet supplies the claims panel and the nutrition
panel, placed on either side of the back seam.
"""
import cv2
import numpy as np
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
REFS = ROOT / "outputs/video_scene/web_refs"
# Same can as scene/video_shapes.py. Width/height of the texture matches circumference/height.
CAN_D, CAN_H = 0.0574, 0.157
PX = 6  # texture pixels per degree around the can; width is exactly one degree per PX pixels
ANG = 84  # degrees kept from one photo; past this the surface is edge-on
PANO_W = 360 * PX
PANO_H = int(round(PANO_W * CAN_H / (np.pi * CAN_D)))

# front: one sharp can. sheet: front, claims, nutrition, left to right.
SOURCES = [
    ("front", REFS / "side_4.jpg"),
    ("sheet", REFS / "sheet_xlarge.jpg" if (REFS / "sheet_xlarge.jpg").exists() else REFS / "back_1.jpg"),
]


def find_cans(bgr):
    """Upright cans on a white background, as (x0, top, x1, bottom)."""
    h, w = bgr.shape[:2]
    ink = np.max(255 - bgr, axis=2) > 10
    has = ink.any(0)
    ymin = np.argmax(ink, 0)
    ymax = h - 1 - np.argmax(ink[::-1], 0)
    span = np.where(has, ymax - ymin, 0)
    if span.max() < 40:
        return []
    cols = span > 0.72 * span.max()
    runs, start = [], None
    for i, flag in enumerate(list(cols) + [False]):
        if flag and start is None:
            start = i
        elif not flag and start is not None:
            if i - start > 20:
                runs.append((start, i))
            start = None
    cans = []
    for x0, x1 in runs:
        rows = np.flatnonzero(ink[:, x0:x1].any(1))
        if len(rows) < 30:
            continue
        top, bot = int(rows[0]), int(rows[-1])
        width, height = x1 - x0, bot - top
        if height < 40 or not 1.6 < height / max(width, 1) < 4.2:
            continue
        if x0 <= 2 or top <= 2 or x1 >= w - 2 or bot >= h - 2:
            continue  # cropped by the frame: the silhouette is not the whole can
        cans.append((x0, top, x1, bot))
    return cans


def unwrap(bgr, box):
    """One label strip, row 0 at the top of the can, columns from -ANG to +ANG degrees."""
    x0, top0, x1, bot0 = box
    x0, x1 = max(0, x0 - 4), min(bgr.shape[1], x1 + 4)
    crop = bgr[:, x0:x1]
    dark = np.max(255 - crop, axis=2)
    height, width = dark.shape
    left = np.full(height, np.nan)
    right = np.full(height, np.nan)
    for y in range(height):
        xs = np.flatnonzero(dark[y] > 8)
        if len(xs) > 5:
            left[y], right[y] = xs[0], xs[-1]
    widths = right - left
    finite = widths[np.isfinite(widths)]
    if len(finite) < 20:
        return None
    cap = np.percentile(finite, 90)
    good = np.isfinite(widths) & (widths > 0.90 * cap)
    ys = np.flatnonzero(good)
    ys = ys[(ys >= top0) & (ys <= bot0)]
    if len(ys) < 20:
        return None
    top, bot = int(ys[0]), int(ys[-1])
    known = np.flatnonzero(np.isfinite(left[top:bot + 1])) + top
    if len(known) < 10:
        return None
    rows = np.arange(top, bot + 1)
    left[rows] = np.interp(rows, known, left[known])
    right[rows] = np.interp(rows, known, right[known])
    kernel = np.ones(31) / 31
    left[rows] = np.convolve(left[rows], kernel, mode="same")
    right[rows] = np.convolve(right[rows], kernel, mode="same")

    n_cols = int(round(2 * ANG * PX))
    angs = np.linspace(-ANG, ANG, n_cols)
    xs = np.zeros((len(rows), n_cols), np.float32)
    yy = np.zeros_like(xs)
    for i, y in enumerate(rows):
        centre = 0.5 * (left[y] + right[y])
        radius = 0.5 * (right[y] - left[y])
        xs[i] = centre + radius * np.sin(np.radians(angs))
        yy[i] = y
    strip = cv2.remap(crop, xs, yy, cv2.INTER_LINEAR, borderMode=cv2.BORDER_REPLICATE)
    strip = cv2.resize(strip, (n_cols, PANO_H), interpolation=cv2.INTER_AREA)
    gray = cv2.cvtColor(strip, cv2.COLOR_BGR2GRAY)
    sharp = float(cv2.Laplacian(gray, cv2.CV_64F).var())
    return strip, sharp


def _shift(img, dy):
    out = img.copy()
    if dy > 0:
        out[:dy] = img[0]
        out[dy:] = img[:-dy]
    elif dy < 0:
        out[dy:] = img[-1]
        out[:dy] = img[-dy:]
    return out


def _paint(pano, weight, strip, yaw, dy, sharp, keep=62):
    """Paint the head-on part of a strip. `keep` is the degrees either side of centre that stay;
    closer to the silhouette the photo is grazing the can and the label bends away."""
    view = _shift(strip, dy)
    angs = -ANG + np.arange(view.shape[1]) / PX
    use = np.abs(angs) <= keep
    cols = np.mod(np.round((yaw + angs[use]) * PX).astype(int), PANO_W)
    ww = sharp * np.maximum(np.cos(np.radians(angs[use])), 0.0) ** 4
    for col, column, w in zip(cols, view[:, use].transpose(1, 0, 2), ww):
        if w > weight[col]:
            pano[:, col] = column
            weight[col] = w


def _collect():
    strips = {}
    for role, path in SOURCES:
        if not path.exists():
            print(f"  missing {path.name}", flush=True)
            continue
        bgr = cv2.imread(str(path), cv2.IMREAD_COLOR)
        if bgr is None:
            continue
        found = []
        for box in find_cans(bgr):
            got = unwrap(bgr, box)
            if got is None:
                continue
            strip, sharp = got
            found.append((strip, sharp, box[2] - box[0]))
            print(f"  {role} {path.name}: can {box[2] - box[0]} px wide, sharpness {sharp:.0f}", flush=True)
        strips[role] = found
    return strips


def panorama():
    """360 degree label, row 0 at the top of the can. None if the product photos are not on disk.

    The front photo is centred on the logo. The retailer sheet is three cans in order
    front, claims, nutrition. The angled photo shows nutrition on the left of the frame
    and the claims panel on the right, so those two panels sit next to each other on the
    back of the can, opposite the logo.
    """
    strips = _collect()
    front = strips.get("front") or []
    sheet = strips.get("sheet") or []
    if not front or len(sheet) < 3:
        print("  need the front photo and the three-view sheet", flush=True)
        return None
    pano = np.zeros((PANO_H, PANO_W, 3), np.uint8)
    weight = np.zeros(PANO_W, np.float32)
    _paint(pano, weight, front[0][0], 0, 0, front[0][1])
    print("  front at 0 deg", flush=True)

    # The angled photo is only there to order the panels: nutrition on the left of that
    # frame, claims on the right, the pair opposite the logo. The pixels come from the
    # straight-on sheet, which is sharper than the angled frame.
    nutrition, claims = sheet[2], sheet[1]
    _paint(pano, weight, nutrition[0], 128, 0, nutrition[1])
    _paint(pano, weight, claims[0], 232, 0, claims[1])
    print("  nutrition at 128 deg, claims at 232 deg", flush=True)

    gap = weight == 0
    if gap.any() and (~gap).any():
        known = np.flatnonzero(~gap)
        for col in np.flatnonzero(gap):
            nearest = known[np.argmin(np.minimum((col - known) % PANO_W, (known - col) % PANO_W))]
            if min((col - nearest) % PANO_W, (nearest - col) % PANO_W) <= 6 * PX:
                pano[:, col] = pano[:, nearest]
    # Put the logo in the middle of the texture so the wrap seam falls on the back.
    pano = np.roll(pano, PANO_W // 2, axis=1)
    print(f"  label covers {int((weight > 0).sum() / PX)}/360 degrees", flush=True)
    return pano


if __name__ == "__main__":
    image = panorama()
    if image is None:
        raise SystemExit("no product photos to unwrap")
    out = ROOT / "assets/video_celsius_can/video_celsius_can_texture.png"
    from PIL import Image
    Image.fromarray(cv2.cvtColor(image, cv2.COLOR_BGR2RGB)).save(out)
    work = ROOT / "outputs/video_scene/rebuild"
    work.mkdir(parents=True, exist_ok=True)
    cv2.imwrite(str(work / "web_label.png"), image)
    print("wrote", out)
