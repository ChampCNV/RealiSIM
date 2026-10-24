"""Repaint the image-to-3D texture with the real photos, so fine detail such as label text stays readable.

The generator re-draws the texture from a coarse latent and smears anything small. Here every photo is
registered to the mesh (view direction from a colour match against the generated texture, scale and
offset from the silhouette), then its pixels are written into the mesh's existing UV atlas wherever that
photo sees the surface head-on. Surface no photo saw keeps the generated colours.
"""
import numpy as np
import trimesh
from PIL import Image
from scipy import ndimage
from trimesh.visual.material import PBRMaterial

SEARCH_HEIGHT = 256  # px: registration renders; large enough that label text survives as detail
MIN_MATCH = 0.35  # correlation below this means the photo does not show this object; it is skipped


def rasterize_uv(mesh, width, height):
    """Surface position and normal for every texel the mesh's UV charts cover."""
    uv = np.asarray(mesh.visual.uv, dtype=np.float64)
    tx = uv[:, 0] * width - 0.5
    ty = (1.0 - uv[:, 1]) * height - 0.5
    verts, normals = mesh.vertices, mesh.vertex_normals
    pos = np.zeros((height, width, 3), np.float32)
    nrm = np.zeros((height, width, 3), np.float32)
    covered = np.zeros((height, width), bool)
    for a, b, c in mesh.faces:
        x0, x1, x2 = tx[a], tx[b], tx[c]
        y0, y1, y2 = ty[a], ty[b], ty[c]
        den = (y1 - y2) * (x0 - x2) + (x2 - x1) * (y0 - y2)
        if abs(den) < 1e-12:
            continue
        lo_x, hi_x = max(int(np.floor(min(x0, x1, x2))), 0), min(int(np.ceil(max(x0, x1, x2))), width - 1)
        lo_y, hi_y = max(int(np.floor(min(y0, y1, y2))), 0), min(int(np.ceil(max(y0, y1, y2))), height - 1)
        if lo_x > hi_x or lo_y > hi_y:
            continue
        gx, gy = np.meshgrid(np.arange(lo_x, hi_x + 1), np.arange(lo_y, hi_y + 1))
        w0 = ((y1 - y2) * (gx - x2) + (x2 - x1) * (gy - y2)) / den
        w1 = ((y2 - y0) * (gx - x2) + (x0 - x2) * (gy - y2)) / den
        w2 = 1.0 - w0 - w1
        inside = (w0 >= -1e-4) & (w1 >= -1e-4) & (w2 >= -1e-4)
        if not inside.any():
            continue
        w = np.stack([w0[inside], w1[inside], w2[inside]], 1)
        ys, xs = gy[inside], gx[inside]
        pos[ys, xs] = w @ verts[[a, b, c]]
        nrm[ys, xs] = w @ normals[[a, b, c]]
        covered[ys, xs] = True
    nrm /= np.linalg.norm(nrm, axis=2, keepdims=True) + 1e-12
    return pos, nrm, covered


def camera(yaw, elev):
    """Orthographic camera on a sphere around the Y-up mesh: (towards camera, image right, image up)."""
    yaw, elev = np.radians(yaw), np.radians(elev)
    d = np.array([np.sin(yaw) * np.cos(elev), np.sin(elev), np.cos(yaw) * np.cos(elev)])
    right = np.cross([0.0, 1.0, 0.0], d)
    right /= np.linalg.norm(right)
    return d, right, np.cross(d, right)


def fit_to_silhouette(points, right, up, box):
    """Scale + offset that put the mesh's projected outline exactly on the photo's object box."""
    u, v = points @ right, points @ up
    x0, y0, x1, y1 = box
    s = 0.5 * ((x1 - x0) / np.ptp(u) + (y1 - y0) / np.ptp(v))
    return s, (x0 + x1) / 2 - s * (u.min() + u.max()) / 2, (y0 + y1) / 2 + s * (v.min() + v.max()) / 2


def project(points, d, right, up, s, ox, oy):
    return ox + s * (points @ right), oy - s * (points @ up), points @ d


def splat(x, y, depth, values, shape):
    """Nearest-surface point render: one value per pixel, front-most point wins."""
    h, w = shape
    xi, yi = np.round(x).astype(int), np.round(y).astype(int)
    ok = (xi >= 0) & (xi < w) & (yi >= 0) & (yi < h)
    order = np.argsort(depth[ok])  # back to front: later (closer) writes overwrite earlier ones
    img = np.zeros((h, w) + values.shape[1:], np.float32)
    hit = np.zeros((h, w), bool)
    img[yi[ok][order], xi[ok][order]] = values[ok][order]
    hit[yi[ok][order], xi[ok][order]] = True
    return img, hit


def correlation(a, b, mask):
    if mask.sum() < 50:
        return -1.0
    a, b = a[mask], b[mask]
    a, b = a - a.mean(0), b - b.mean(0)
    return float((a * b).sum() / (np.sqrt((a * a).sum() * (b * b).sum()) + 1e-9))


def detail(img):
    return img - ndimage.gaussian_filter(img, (2, 2, 0))


def register(photo, alpha, points, colors, painted):
    """Best (yaw, elevation) for one photo.

    Coarse colours are compared against the current texture everywhere. Where the view overlaps surface an
    earlier photo already painted, fine detail (text, edges) must agree too: a real overlap earns a bonus,
    a mismatched one (e.g. the back label landing on the front label) a penalty.
    """
    scale = SEARCH_HEIGHT / photo.shape[0]
    small = np.asarray(Image.fromarray(photo).resize(
        (max(1, round(photo.shape[1] * scale)), SEARCH_HEIGHT), Image.BILINEAR), np.float32)
    small_alpha = np.asarray(Image.fromarray((alpha * 255).astype(np.uint8)).resize(
        small.shape[1::-1], Image.BILINEAR), np.float32) / 255 > 0.5
    ys, xs = np.nonzero(small_alpha)
    box = (xs.min(), ys.min(), xs.max() + 1, ys.max() + 1)
    target = ndimage.gaussian_filter(small, (1.5, 1.5, 0))
    target_detail = detail(small)
    values = np.concatenate([colors, painted[:, None]], 1)

    def score(yaw, elev):
        d, right, up = camera(yaw, elev)
        s, ox, oy = fit_to_silhouette(points, right, up, box)
        x, y, depth = project(points, d, right, up, s, ox, oy)
        img, hit = splat(x, y, depth, values, small.shape[:2])
        seen = hit & small_alpha
        coarse = correlation(ndimage.gaussian_filter(img[:, :, :3], (1.5, 1.5, 0)), target, seen)
        overlap = seen & (img[:, :, 3] > 0.5)
        if overlap.sum() < 0.05 * seen.sum():
            return coarse
        fine = correlation(detail(img[:, :, :3]), target_detail, overlap)
        return coarse + 2 * (overlap.sum() / seen.sum()) * (fine - 0.3)

    best = max(((score(yaw, elev), yaw, elev) for yaw in range(0, 360, 6) for elev in (-10, 0, 10, 20, 35)))
    _, yaw0, elev0 = best
    best = max(((score(yaw, elev), yaw, elev) for yaw in np.arange(yaw0 - 6, yaw0 + 6.1, 1.5)
                for elev in np.arange(elev0 - 6, elev0 + 6.1, 3)))
    return best


def project_photos(glb_in, photo_paths, glb_out):
    """Writes glb_out: same mesh and UVs as glb_in, texture repainted from the photos (RGBA cut-outs)."""
    mesh = trimesh.load(glb_in, force="mesh")
    atlas = np.asarray(mesh.visual.material.to_simple().image.convert("RGB"), np.float32)
    h, w = atlas.shape[:2]
    pos, nrm, covered = rasterize_uv(mesh, w, h)
    texel_pos, texel_nrm = pos[covered], nrm[covered]
    texel_old = atlas[covered]

    rng = np.random.default_rng(0)
    pick = rng.choice(len(texel_pos), size=min(len(texel_pos), 300_000), replace=False)
    keep = 0.02  # how strongly the generated colour holds on where photos only graze the surface
    total = np.zeros_like(texel_old)
    weight = np.zeros(len(texel_old), np.float32)
    report = []
    for path in photo_paths:
        rgba = np.asarray(Image.open(path).convert("RGBA"))
        photo, alpha = rgba[:, :, :3], rgba[:, :, 3].astype(np.float32) / 255
        # Register against the texture painted so far: the generated colours are only a guess away from
        # the first photo, while a wrong overlap with an already-painted photo shows up as a poor match.
        current = (total[pick] + keep * texel_old[pick]) / (weight[pick] + keep)[:, None]
        painted = weight[pick] / (weight[pick] + keep)
        match, yaw, elev = register(photo, alpha, texel_pos[pick], current, painted)
        report.append(dict(photo=str(path), yaw_deg=float(yaw), elevation_deg=float(elev), match=round(match, 3)))
        if match < MIN_MATCH:
            print(f"   texture: {path} skipped (match {match:.2f}: doesn't look like this object)")
            continue

        d, right, up = camera(yaw, elev)
        mask = alpha > 0.5
        ys, xs = np.nonzero(mask)
        s, ox, oy = fit_to_silhouette(texel_pos[pick], right, up, (xs.min(), ys.min(), xs.max() + 1, ys.max() + 1))
        x, y, depth = project(texel_pos, d, right, up, s, ox, oy)

        # Depth test: only the texels at the front of each photo pixel are actually visible in it.
        xi = np.clip(np.round(x).astype(int), 0, photo.shape[1] - 1)
        yi = np.clip(np.round(y).astype(int), 0, photo.shape[0] - 1)
        front = np.full(photo.shape[:2], -np.inf, np.float32)
        np.maximum.at(front, (yi, xi), depth)
        tolerance = 0.01 * np.ptp(texel_pos, axis=0).max()
        visible = depth >= front[yi, xi] - tolerance

        # Down-weight grazing angles (stretched pixels) and the cut-out edge (background fringe).
        facing = np.clip(texel_nrm @ d, 0, 1)
        edge = ndimage.distance_transform_edt(mask)
        feather = np.clip(ndimage.map_coordinates(edge, [y, x], order=1) / 6.0, 0, 1)
        wgt = np.where(visible, facing ** 6 * feather, 0).astype(np.float32)
        wgt[facing < 0.2] = 0
        color = np.stack([ndimage.map_coordinates(photo[:, :, c].astype(np.float32), [y, x], order=1)
                          for c in range(3)], 1)
        total += wgt[:, None] * color
        weight += wgt
        print(f"   texture: {path} -> view yaw {yaw:.0f} deg, elevation {elev:.0f} deg (match {match:.2f}), "
              f"{(wgt > 0.05).mean() * 100:.0f}% of the surface")

    new = (total + keep * texel_old) / (weight + keep)[:, None]
    out = atlas.copy()
    out[covered] = new
    # Bleed chart borders outwards so texture filtering at UV seams never picks up the old colours.
    near = ndimage.distance_transform_edt(~covered, return_distances=True, return_indices=True)
    dist, (iy, ix) = near
    pad = (~covered) & (dist <= 4)
    out[pad] = out[iy[pad], ix[pad]]

    mesh.visual = trimesh.visual.TextureVisuals(
        uv=mesh.visual.uv,
        material=PBRMaterial(baseColorTexture=Image.fromarray(np.clip(out, 0, 255).astype(np.uint8)),
                             metallicFactor=0.0, roughnessFactor=1.0))
    mesh.export(glb_out)
    return report
