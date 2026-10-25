"""Scene step 2: COLMAP point cloud + real table size -> table pose, metric scale, objects on the table.

    python scene/find_objects.py outputs/scene --table 48 37 65     (length, width, height in cm)

Generic, no per-scene tuning:
  - table = the large plane just below the point all cameras look at (the video is aimed at the table)
  - scale = table height (distance from tabletop to the parallel floor plane below it)
  - table pose = where a rectangle of the real table size best covers the tabletop points
  - objects = clusters of points standing on the table; nearby clusters merged (shiny objects split up)
"""
import argparse
import json
from pathlib import Path

import numpy as np
import open3d as o3d
import pycolmap


def look_at_point(rec):
    """Least-squares point closest to all camera viewing axes."""
    A, b = np.zeros((3, 3)), np.zeros(3)
    for im in rec.images.values():
        c = im.projection_center()
        axis = im.cam_from_world().rotation.matrix()[2]  # camera z axis in world = viewing direction
        M = np.eye(3) - np.outer(axis, axis)
        A += M
        b += M @ c
    return np.linalg.solve(A, b)


def planes(P, idx, thresh, n_planes=6):
    out, rem = [], idx.copy()
    for _ in range(n_planes):
        if len(rem) < 50:
            break
        pc = o3d.geometry.PointCloud(o3d.utility.Vector3dVector(P[rem]))
        (a, b, c, d), inl = pc.segment_plane(thresh, 3, 5000)
        out.append((np.array([a, b, c]), d, rem[inl]))
        rem = np.setdiff1d(rem, rem[inl])
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("work_dir")
    ap.add_argument("--table", nargs=3, type=float, required=True, metavar=("LENGTH", "WIDTH", "HEIGHT"),
                    help="real table size in cm")
    args = ap.parse_args()
    find_objects(args.work_dir, args.table)


def find_objects(work, table_cm):
    """Writes work/scene.json from work/sparse; table_cm = real (length, width, height) in cm."""
    work = Path(work)
    L, W, H = (x / 100 for x in table_cm)

    rec = pycolmap.Reconstruction(str(work / "sparse"))
    pid = np.array(list(rec.points3D.keys()))
    P = np.array([rec.points3D[i].xyz for i in pid])
    cams = np.array([im.projection_center() for im in rec.images.values()])
    s = np.median(np.linalg.norm(P - np.median(P, 0), axis=1))  # rough scene size, for thresholds

    # 1. Table plane: a horizontal plane (phones are held upright, so the average image "up" is roughly
    #    against gravity), cameras above it, and nearest to the point all cameras look at.
    target = look_at_point(rec)
    up = -np.mean([im.cam_from_world().rotation.matrix()[1] for im in rec.images.values()], 0)
    up /= np.linalg.norm(up)
    best = None
    # Walls and chair backs usually have more points than a plain tabletop, so keep extracting planes
    # until a horizontal one shows up.
    for n, d, inl in planes(P, np.arange(len(P)), 0.008 * s, n_planes=30):
        if np.mean(cams @ n + d) < 0:
            n, d = -n, -d
        # closer to "up" than to sideways; image-up is tilted by however much the phone looked down (~30-40 deg)
        horizontal = np.degrees(np.arccos(np.clip(n @ up, -1, 1))) < 45
        if not horizontal or np.mean(np.sign(cams @ n + d)) < 0.9:
            continue
        dist = abs(target @ n + d)
        if best is None or dist < best[0]:
            best = (dist, n, d, inl)
    if best is None:
        raise RuntimeError("no horizontal table plane with the cameras above it; film the table from above")
    _, n, d, _ = best
    h = P @ n + d

    # 2. Scale: floor = biggest plane parallel to the table, below it
    floor = [(len(inl), -np.median(P[inl] @ n + d)) for fn, fd, inl in planes(P, np.where(h < -0.3 * s)[0], 0.01 * s)
             if np.degrees(np.arccos(min(1, abs(fn @ n)))) < 8]
    drop = max(floor)[1]
    k = H / drop  # metres per scene unit
    h = h * k

    # 3. Table pose: rectangle of the real size covering the most tabletop points (all objects must be on it)
    foot = target - (target @ n + d) * n
    u = np.cross(n, [1, 0, 0]) if abs(n[0]) < 0.9 else np.cross(n, [0, 1, 0])
    u /= np.linalg.norm(u)
    v = np.cross(n, u)
    uv = np.c_[(P - foot) @ u, (P - foot) @ v] * k
    # tabletop = the connected patch of plane points around where the cameras look
    # (other surfaces at a similar height, like a desk behind the table, form separate patches)
    near_plane = np.where(np.abs(h) < 0.006)[0]
    pc = o3d.geometry.PointCloud(o3d.utility.Vector3dVector(np.c_[uv[near_plane], np.zeros(len(near_plane))]))
    lab = np.array(pc.cluster_dbscan(eps=0.03, min_points=5))
    main = lab[np.argmin(np.linalg.norm(uv[near_plane], axis=1))]
    top = np.zeros(len(P), bool)
    top[near_plane[lab == main]] = True
    above = np.where((h > 0.004) & (h < 0.6) & (np.linalg.norm(uv, axis=1) < max(L, W)))[0]
    pc = o3d.geometry.PointCloud(o3d.utility.Vector3dVector(np.c_[uv[above], h[above]]))
    lab = np.array(pc.cluster_dbscan(eps=0.015, min_points=10))
    clusters = [above[lab == l] for l in set(lab) if l >= 0]
    # merge clusters whose centres are within 6 cm (one shiny object often splits into several)
    merged = []
    for c in sorted(clusters, key=len, reverse=True):
        cen = np.median(uv[c], 0)
        for m in merged:
            if np.linalg.norm(np.median(uv[m["idx"]], 0) - cen) < 0.06:
                m["idx"] = np.r_[m["idx"], c]
                break
        else:
            merged.append(dict(idx=c))
    # real objects stand on the table: their lowest points reach the tabletop. Furniture behind the table
    # that merely appears above table height floats, so it is dropped.
    objs = [m for m in merged if len(m["idx"]) >= 40 and np.percentile(h[m["idx"]], 2) < 0.02]
    best_fit = (-1, None)
    for yaw in np.radians(np.arange(0, 180, 1.0)):
        R = np.array([[np.cos(yaw), -np.sin(yaw)], [np.sin(yaw), np.cos(yaw)]])
        q = uv[top] @ R
        for cx in np.arange(-L / 2, L / 2, 0.01):
            for cy in np.arange(-W / 2, W / 2, 0.01):
                score = np.sum((np.abs(q[:, 0] - cx) < L / 2) & (np.abs(q[:, 1] - cy) < W / 2))
                if score > best_fit[0]:
                    best_fit = (score, (yaw, cx, cy))
    yaw, cx, cy = best_fit[1]
    R = np.array([[np.cos(yaw), -np.sin(yaw)], [np.sin(yaw), np.cos(yaw)]])
    table_centre = R @ [cx, cy]
    # keep only objects standing inside the table rectangle
    def on_table(m):
        p = R.T @ (np.median(uv[m["idx"]], 0) - table_centre)
        return abs(p[0]) < L / 2 - 0.01 and abs(p[1]) < W / 2 - 0.01
    objs = [m for m in objs if on_table(m)]

    out = dict(scale_m_per_unit=k, table=dict(length=L, width=W, height=H, points_inside=int(best_fit[0]),
                                              points_total=int(top.sum())),
               plane=dict(n=n.tolist(), d=float(d), origin=foot.tolist(), u=u.tolist(), v=v.tolist(),
                          table_centre=table_centre.tolist(), table_yaw=float(yaw)),
               objects=[])
    print(f"scale: 1 unit = {k * 100:.2f} cm | table: {best_fit[0]}/{top.sum()} tabletop points inside {L*100:.0f}x{W*100:.0f} cm")
    for i, m in enumerate(objs):
        idx = m["idx"]
        p = R.T @ (np.median(uv[idx], 0) - table_centre)
        height = float(np.percentile(h[idx], 98))
        out["objects"].append(dict(name=f"object_{i}", x=float(p[0]), y=float(p[1]), height=height,
                                   n_points=int(len(idx)), point_ids=pid[idx].tolist()))
        print(f"object_{i}: at ({p[0]*100:5.1f}, {p[1]*100:5.1f}) cm from table centre, height {height*100:4.1f} cm, "
              f"{len(idx)} points")
    (work / "scene.json").write_text(json.dumps(out))
    return out


if __name__ == "__main__":
    main()
