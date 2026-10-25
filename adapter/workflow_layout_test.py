"""LABELLED SYNTHETIC UNIT TESTS for workflow_layout transforms and units. Not user data, never used as output.
    /workspace/photo2grasp/.venv/bin/python adapter/workflow_layout_test.py
"""
import math, sys
from pathlib import Path
import numpy as np
sys.path.insert(0, str(Path(__file__).resolve().parent))
import workflow_layout_geom as G  # noqa: E402

rng = np.random.default_rng(7)


def random_rotation():
    q = rng.normal(size=4); q /= np.linalg.norm(q)
    w, x, y, z = q
    return np.array([[1 - 2 * (y * y + z * z), 2 * (x * y - z * w), 2 * (x * z + y * w)],
                     [2 * (x * y + z * w), 1 - 2 * (x * x + z * z), 2 * (y * z - x * w)],
                     [2 * (x * z - y * w), 2 * (y * z + x * w), 1 - 2 * (x * x + y * y)]])


def synthetic_world():
    """Metric table frame -> arbitrary COLMAP-like world: rotation Rw, translation tw, scale 1/k (units per metre)."""
    Rw, tw, k = random_rotation(), rng.normal(size=3) * 3, 0.0137      # 1 unit = 1.37 cm
    to_world = lambda Q: (np.asarray(Q) @ Rw.T) / k + tw
    n = Rw[:, 2]; d = -float(n @ tw)                                 # table plane z=0 -> n.X + d = 0
    # upstream picks its own in-plane axes u, v and a foot point; mimic with an arbitrary in-plane offset and yaw
    foot_t = np.array([0.07, -0.04, 0.0])
    foot = to_world(foot_t[None])[0]
    u = np.cross(n, [1, 0, 0]); u /= np.linalg.norm(u); v = np.cross(n, u)
    # table centre/yaw in (u,v) metric coords: express table axes in u,v
    ex_w, ey_w = Rw[:, 0], Rw[:, 1]
    yaw = math.atan2(ex_w @ v, ex_w @ u)
    tc = np.array([(tw - foot) @ u, (tw - foot) @ v]) * k
    plane = dict(n=n.tolist(), d=d, origin=foot.tolist(), u=u.tolist(), v=v.tolist(), table_centre=tc.tolist(), table_yaw=yaw)
    return to_world, plane, k, Rw


def test_table_frame_roundtrip_and_upstream_formula():
    to_world, plane, k, Rw = synthetic_world()
    Q = np.c_[rng.uniform(-0.3, 0.3, 50), rng.uniform(-0.2, 0.2, 50), rng.uniform(0, 0.2, 50)]
    TF = G.TableFrame(plane, k)
    P = to_world(Q)
    assert np.allclose(TF.to_table(P), Q, atol=1e-9), "world->table must recover metric table coordinates"
    assert np.allclose(TF.to_world(Q), P, atol=1e-9)
    # upstream formula from find_objects.py: p = R.T @ (uv - table_centre)
    yaw = plane["table_yaw"]; R = np.array([[math.cos(yaw), -math.sin(yaw)], [math.sin(yaw), math.cos(yaw)]])
    foot, u, v = (np.array(plane[x]) for x in ("origin", "u", "v"))
    uv = np.c_[(P - foot) @ u, (P - foot) @ v] * k
    p_up = np.array([R.T @ (row - plane["table_centre"]) for row in uv])
    assert np.allclose(p_up, Q[:, :2], atol=1e-9)
    TFf = G.TableFrame(plane, k, flip=True)
    assert np.allclose(TFf.to_table(P)[:, :2], -Q[:, :2]) and np.allclose(TFf.to_world(TFf.to_table(P)), P)
    dirs = TF.dir_to_table(Rw @ np.array([1.0, 0, 0]))
    assert np.allclose(dirs[0], [1, 0, 0], atol=1e-9)
    print("ok table frame: round trip, upstream formula, flip, directions (scale 1 unit = 1.37 cm)")


def test_se2_and_robot_frame():
    T = G.se2(0.25, -0.1, 90.0)                      # robot base at (25, -10) cm, facing table +y
    p = G.in_frame(T, [[0.25, 0.2]])[0]              # object 30 cm along +y of the table from the base
    assert np.allclose(p, [0.3, 0.0], atol=1e-12), p   # = 30 cm straight ahead of the robot
    Ti = G.se2_inv(T)
    assert np.allclose(T @ Ti, np.eye(3))
    x, y, yaw = G.se2_params(Ti)                      # table centre seen from the base
    assert np.allclose([x, y, yaw], [0.1, 0.25, -90.0], atol=1e-12), (x, y, yaw)
    assert G.wrap_deg(190) == -170 and G.wrap_deg(-180) == -180
    print("ok SE2: robot-frame placement, inverse (table pose in base frame), wrapping")


def test_tag_anchor():
    # flat 5 cm tag, centre (0.10, 0.05), rotated 30 deg in the table frame; tag sits 12 cm in front of the base on its x axis
    c, s, yaw = np.array([0.10, 0.05, 0.0]), 0.05, math.radians(30)
    ex, ey = np.array([math.cos(yaw), math.sin(yaw), 0]), np.array([-math.sin(yaw), math.cos(yaw), 0])
    TL, TR, BR, BL = c + (-ex + ey) * s / 2, c + (ex + ey) * s / 2, c + (ex - ey) * s / 2, c + (-ex - ey) * s / 2
    tp = G.tag_pose([TL, TR, BR, BL])
    assert abs(tp["edge"] - s) < 1e-12 and abs(tp["yaw_deg"] - 30) < 1e-9 and tp["tilt_deg"] < 1e-9 and tp["z"][2] > 0.999
    T = G.base_from_tag(tp["center"][:2], tp["yaw_deg"], dict(x_m=0.12, y_m=0.0, yaw_deg=0.0))
    bx, by, byaw = G.se2_params(T)
    exp = c[:2] - 0.12 * ex[:2]
    assert np.allclose([bx, by], exp) and abs(byaw - 30) < 1e-9
    # tag in front of the robot => robot frame coordinates of the tag centre = (0.12, 0)
    assert np.allclose(G.in_frame(T, [c[:2]])[0], [0.12, 0.0], atol=1e-12)
    print("ok tag anchor: corner order/orientation, edge length, base pose from measured tag offset")


def test_triangulate_and_rays():
    X = np.array([0.3, -0.2, 1.5])
    rays = []
    for _ in range(6):
        o = rng.normal(size=3) * 2
        dv = X - o + rng.normal(size=3) * 1e-4
        rays.append((o, dv / np.linalg.norm(dv)))
    p, rms = G.triangulate(rays)
    assert np.linalg.norm(p - X) < 1e-3 and rms < 1e-3
    hit = G.ray_plane(np.array([0, 0, 1.0]), np.array([0.6, 0, -0.8]), np.array([0, 0, 1.0]), 0.0)
    assert np.allclose(hit, [0.75, 0, 0]) and G.ray_plane(np.zeros(3), np.array([0, 0, 1.0]), np.array([0, 0, 1.0]), 1.0) is None
    print("ok triangulation and ray-plane")


def test_2d_helpers():
    assert abs(G.iou([0, 0, 1, 1], [0.5, 0, 1.5, 1]) - 1 / 3) < 1e-12 and G.iou([0, 0, 1, 1], [2, 2, 3, 3]) == 0
    a = np.radians(np.arange(0, 90, 10))
    assert abs(G.azimuth_coverage_deg(np.c_[np.cos(a), np.sin(a)]) - 80) < 1e-9
    ring = np.radians(np.arange(0, 360, 30))
    assert abs(G.azimuth_coverage_deg(np.c_[np.cos(ring), np.sin(ring)]) - 330) < 1e-9
    xy = np.c_[rng.uniform(-0.1, 0.1, 2000), rng.uniform(-0.02, 0.02, 2000)] @ np.array([[math.cos(.5), math.sin(.5)], [-math.sin(.5), math.cos(.5)]])
    fp = G.footprint(xy)
    assert abs(fp["major_deg"] - math.degrees(.5)) < 2 and abs(fp["extent"][0] - 0.18) < 0.01
    print("ok IoU, azimuth coverage, footprint axis")


def test_fit_rectangle_matches_upstream_loop():
    import workflow_layout as WL
    L, W = 0.30, 0.20
    yaw = math.radians(37)
    R = np.array([[math.cos(yaw), -math.sin(yaw)], [math.sin(yaw), math.cos(yaw)]])
    pts = (np.c_[rng.uniform(-L / 2, L / 2, 400), rng.uniform(-W / 2, W / 2, 400)] + [0.03, -0.02]) @ R.T
    pts = np.r_[pts, rng.uniform(-0.4, 0.4, (40, 2))]
    best = (-1, None)                                                  # upstream loop, verbatim logic
    for yw in np.radians(np.arange(0, 180, 1.0)):
        Rm = np.array([[np.cos(yw), -np.sin(yw)], [np.sin(yw), np.cos(yw)]])
        q = pts @ Rm
        for cx in np.arange(-L / 2, L / 2, 0.01):
            for cy in np.arange(-W / 2, W / 2, 0.01):
                sc = np.sum((np.abs(q[:, 0] - cx) < L / 2) & (np.abs(q[:, 1] - cy) < W / 2))
                if sc > best[0]:
                    best = (sc, (yw, cx, cy))
    r = WL.fit_rectangle(pts, L, W)
    assert r["inside"] == best[0], (r["inside"], best[0])
    print(f"ok vectorised rectangle fit == upstream loop count ({best[0]} pts), yaw {math.degrees(r['yaw']):.0f} deg")


if __name__ == "__main__":
    test_table_frame_roundtrip_and_upstream_formula()
    test_se2_and_robot_frame()
    test_tag_anchor()
    test_triangulate_and_rays()
    test_2d_helpers()
    test_fit_rectangle_matches_upstream_loop()
    print("ALL LAYOUT GEOMETRY TESTS PASSED (synthetic fixtures)")
