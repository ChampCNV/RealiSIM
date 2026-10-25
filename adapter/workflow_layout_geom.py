"""workflow_layout_geom: pure-numpy coordinate transforms for the layout adapter (unit tested in workflow_layout_test.py).

Frames
  world  : COLMAP reconstruction frame (arbitrary scale, arbitrary orientation)
  table  : origin = table-top centre, x along LENGTH, y along WIDTH, z = top-plane normal towards the cameras, metres
  base   : robot base frame, planar pose (x, y, yaw) in the table frame; z up, level with the table top
"""
import math
import numpy as np


class TableFrame:
    """world <-> table frame built exactly from find_objects.py's scene.json ('plane' + 'scale_m_per_unit').

    Upstream: uv = [(P-foot)@u, (P-foot)@v] * k ; R = rot(yaw) ; table point p = R.T @ (uv - table_centre) ;
    height h = (P@n + d) * k. flip=True rotates the table frame 180 deg about z (x->-x, y->-y)."""

    def __init__(self, plane, k, flip=False):
        self.n = np.asarray(plane["n"], float)
        self.d = float(plane["d"])
        self.foot = np.asarray(plane["origin"], float)
        self.u = np.asarray(plane["u"], float)
        self.v = np.asarray(plane["v"], float)
        self.tc = np.asarray(plane["table_centre"], float)
        yaw = float(plane["table_yaw"])
        self.R = np.array([[math.cos(yaw), -math.sin(yaw)], [math.sin(yaw), math.cos(yaw)]])
        self.k = float(k)
        self.flip = bool(flip)

    def to_table(self, P):
        P = np.atleast_2d(np.asarray(P, float))
        uv = np.c_[(P - self.foot) @ self.u, (P - self.foot) @ self.v] * self.k
        xy = (uv - self.tc) @ self.R          # == (R.T @ (uv - tc).T).T
        z = (P @ self.n + self.d) * self.k
        if self.flip:
            xy = -xy
        return np.c_[xy, z]

    def dir_to_table(self, D):
        """Rotate world direction vectors into the table frame (no translation, no scale)."""
        D = np.atleast_2d(np.asarray(D, float))
        uv = np.c_[D @ self.u, D @ self.v]
        xy = uv @ self.R
        if self.flip:
            xy = -xy
        return np.c_[xy, D @ self.n]

    def to_world(self, Q):
        Q = np.atleast_2d(np.asarray(Q, float))
        xy = -Q[:, :2] if self.flip else Q[:, :2]
        uv = xy @ self.R.T + self.tc
        # foot lies on the plane (n.foot + d == 0) and u, v, n are orthonormal
        return self.foot + (uv[:, :1] * self.u + uv[:, 1:2] * self.v + Q[:, 2:3] * self.n) / self.k

    def corners(self, L, W):
        """Table-top corners in the table frame, labelled by sign of (x, y)."""
        out = []
        for lab, sx, sy in (("A(+x,+y)", 1, 1), ("B(-x,+y)", -1, 1), ("C(-x,-y)", -1, -1), ("D(+x,-y)", 1, -1)):
            out.append((lab, np.array([sx * L / 2, sy * W / 2, 0.0])))
        return out


# ── planar rigid transforms (SE2), yaw in degrees ────────────────────────────────────────────
def se2(x, y, yaw_deg):
    r = math.radians(yaw_deg)
    return np.array([[math.cos(r), -math.sin(r), x], [math.sin(r), math.cos(r), y], [0.0, 0.0, 1.0]])


def se2_inv(T):
    R, t = T[:2, :2], T[:2, 2]
    out = np.eye(3)
    out[:2, :2] = R.T
    out[:2, 2] = -R.T @ t
    return out


def se2_params(T):
    return float(T[0, 2]), float(T[1, 2]), wrap_deg(math.degrees(math.atan2(T[1, 0], T[0, 0])))


def wrap_deg(a):
    return (a + 180.0) % 360.0 - 180.0


def in_frame(T_table_frame, xy_table):
    """Express table-frame points in a frame whose pose in the table frame is T_table_frame."""
    P = np.atleast_2d(np.asarray(xy_table, float))
    return (se2_inv(T_table_frame) @ np.c_[P, np.ones(len(P))].T).T[:, :2]


# ── multi-view geometry ──────────────────────────────────────────────────────────────────────
def triangulate(rays):
    """Least-squares point closest to rays [(origin, unit_dir), ...]. Returns (point, rms_distance_to_rays)."""
    A, b = np.zeros((3, 3)), np.zeros(3)
    for c, dv in rays:
        dv = dv / np.linalg.norm(dv)
        M = np.eye(3) - np.outer(dv, dv)
        A += M
        b += M @ c
    p = np.linalg.solve(A, b)
    res = [np.linalg.norm((np.eye(3) - np.outer(dv / np.linalg.norm(dv), dv / np.linalg.norm(dv))) @ (p - c)) for c, dv in rays]
    return p, float(np.sqrt(np.mean(np.square(res))))


def ray_plane(c, dv, n, d):
    """Intersection of ray c + t*dv (t>0) with plane n.X + d = 0, or None."""
    den = float(dv @ n)
    if abs(den) < 1e-9:
        return None
    t = -(float(c @ n) + d) / den
    return None if t <= 0 else c + t * dv


def tag_pose(corners_table):
    """Pose of a square tag from its 4 corners (table frame, order = AprilTag/ArUco TL, TR, BR, BL as printed).
    x = TL->TR, y = BL->TL, z = x cross y (out of the printed face). Returns dict(center, x, y, z, edge_m, tilt_deg, yaw_deg)."""
    c = np.asarray(corners_table, float)
    x = (c[1] - c[0]) + (c[2] - c[3])
    y = (c[0] - c[3]) + (c[1] - c[2])
    x /= np.linalg.norm(x)
    y = y - (y @ x) * x
    y /= np.linalg.norm(y)
    z = np.cross(x, y)
    edges = [np.linalg.norm(c[i] - c[(i + 1) % 4]) for i in range(4)]
    return dict(center=c.mean(0), x=x, y=y, z=z, edge=float(np.mean(edges)), edge_spread=float(np.std(edges)),
                tilt_deg=float(np.degrees(np.arccos(np.clip(z[2], -1, 1)))), yaw_deg=float(np.degrees(math.atan2(x[1], x[0]))))


def base_from_tag(tag_xy, tag_yaw_deg, tag_in_base):
    """Robot base pose in the table frame from the tag's table pose and the measured tag pose in the base frame."""
    T_table_tag = se2(tag_xy[0], tag_xy[1], tag_yaw_deg)
    T_base_tag = se2(tag_in_base["x_m"], tag_in_base["y_m"], tag_in_base.get("yaw_deg", 0.0))
    return T_table_tag @ se2_inv(T_base_tag)


# ── 2D helpers ───────────────────────────────────────────────────────────────────────────────
def iou(a, b):
    x0, y0 = max(a[0], b[0]), max(a[1], b[1])
    x1, y1 = min(a[2], b[2]), min(a[3], b[3])
    inter = max(0.0, x1 - x0) * max(0.0, y1 - y0)
    ua = (a[2] - a[0]) * (a[3] - a[1]) + (b[2] - b[0]) * (b[3] - b[1]) - inter
    return inter / ua if ua > 0 else 0.0


def azimuth_coverage_deg(dirs_xy):
    """360 minus the largest angular gap between horizontal viewing directions (0 if fewer than 2)."""
    if len(dirs_xy) < 2:
        return 0.0
    a = np.sort(np.degrees(np.arctan2(np.asarray(dirs_xy)[:, 1], np.asarray(dirs_xy)[:, 0])) % 360)
    gaps = np.diff(np.r_[a, a[0] + 360])
    return float(360 - gaps.max())


def footprint(xy):
    """PCA footprint: extents along the principal axes (m) and major-axis angle (deg, 180-deg ambiguous).
    Uses 5th..95th percentiles so outlier points do not inflate the size."""
    xy = np.asarray(xy, float)
    c = np.median(xy, 0)
    if len(xy) < 3:
        return dict(center=c, extent=[0.0, 0.0], major_deg=None, elongation=1.0)
    w, V = np.linalg.eigh(np.cov((xy - c).T))
    major = V[:, 1]
    q = (xy - c) @ V[:, ::-1]
    ext = np.percentile(q, 95, 0) - np.percentile(q, 5, 0)
    el = float(ext[0] / max(ext[1], 1e-6))
    ang = float(np.degrees(math.atan2(major[1], major[0]))) % 180
    return dict(center=c, extent=[float(ext[0]), float(ext[1])], major_deg=ang if el > 1.3 else None, elongation=el)
