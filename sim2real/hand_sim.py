"""A person's hand in the simulation: a stick hand (palm plate, 16 finger bones, forearm) with that person's
proportions, measured from photos/video with MediaPipe (make_hand.py -> hands/<name>/hand.json).

It is not an object: every part is a mocap body, moved by code (live tracking, or a pose chosen for training),
so objects can stand on the palm and the arm bumps into it, but nothing pushes it around.

Hand points are MediaPipe's 21: 0 wrist, thumb 1-4, index 5-8, middle 9-12, ring 13-16, pinky 17-20 (base -> tip).
Hand frame: origin = palm centre, y = toward the fingers, z = out of the palm, x = y cross z.
"""
import json

import mujoco
import numpy as np

from lift_env import ROOT

HANDS = ROOT / "hands"
FINGERS = [[1, 2, 3, 4], [5, 6, 7, 8], [9, 10, 11, 12], [13, 14, 15, 16], [17, 18, 19, 20]]
BONES = [(0, 1)] + [(f[i], f[i + 1]) for f in FINGERS for i in range(3)]
BONE_FINGER = [0] + [k for k in range(5) for _ in range(3)]
PALM = [0, 1, 5, 9, 13, 17]  # points that move together with the palm
KNUCKLES = [5, 9, 13, 17]
FINGER_RADIUS = [0.011, 0.0095, 0.0095, 0.009, 0.008]  # m, for a REFERENCE_LENGTH hand; scaled with the hand
REFERENCE_LENGTH = 0.185  # wrist crease to middle fingertip of an average adult hand
PALM_HALF_THICKNESS = 0.012
FOREARM_LENGTH, FOREARM_RADIUS, FOREARM_DROP = 0.25, 0.032, 0.35  # drop: slope down behind the wrist
SKIN = [0.87, 0.68, 0.56, 1]


def hand_frame(p, side):
    """(origin, R) of the hand frame for 21 points p (R's columns = x, y, z axes)."""
    p = np.asarray(p, float)
    origin = 0.5 * (p[0] + p[KNUCKLES].mean(0))
    y = p[9] - p[0]
    y /= np.linalg.norm(y)
    n = np.cross(p[5] - p[0], p[17] - p[0]) * (1 if side == "right" else -1)  # out of the palm
    z = n - (n @ y) * y
    z /= np.linalg.norm(z)
    return origin, np.stack([np.cross(y, z), y, z], 1)


def canonical(p, side):
    """The 21 points in their own hand frame."""
    origin, R = hand_frame(p, side)
    return (np.asarray(p, float) - origin) @ R


def load_hand(name):
    path = HANDS / name / "hand.json"
    if not path.exists():
        made = sorted(q.parent.name for q in HANDS.glob("*/hand.json")) if HANDS.exists() else []
        raise SystemExit(f"no hand '{name}' (hands: {made or 'none'}; make one: python photo2grasp.py hand {name} "
                         f"<video>)")
    return json.loads(path.read_text())


def palm_box(T):
    """Palm plate in the hand frame from the template points: (centre, half sizes)."""
    k = T[KNUCKLES]
    x0, x1 = k[:, 0].min(), k[:, 0].max()
    y0, y1 = T[0, 1], k[:, 1].mean()
    scale = np.linalg.norm(T[12] - T[0]) / REFERENCE_LENGTH
    return (np.array([(x0 + x1) / 2, (y0 + y1) / 2, 0.0]),
            np.array([(x1 - x0) / 2 + 0.01 * scale, (y1 - y0) / 2 + 0.005, PALM_HALF_THICKNESS * scale]))


def add_hand(spec, hand, prefix="hand"):
    """Add the stick hand to a scene spec (before compiling): mocap bodies, skin coloured, colliding."""
    T = np.array(hand["points"])
    scale = np.linalg.norm(T[12] - T[0]) / REFERENCE_LENGTH
    look = dict(contype=1, conaffinity=1, group=2, rgba=SKIN)
    _, half = palm_box(T)
    spec.worldbody.add_body(name=f"{prefix}_palm", mocap=True).add_geom(
        name=f"{prefix}_palm", type=mujoco.mjtGeom.mjGEOM_BOX, size=half, **look)
    for i, (a, b) in enumerate(BONES):
        spec.worldbody.add_body(name=f"{prefix}_bone{i}", mocap=True).add_geom(
            name=f"{prefix}_bone{i}", type=mujoco.mjtGeom.mjGEOM_CAPSULE,
            size=[FINGER_RADIUS[BONE_FINGER[i]] * scale, np.linalg.norm(T[b] - T[a]) / 2, 0], **look)
    spec.worldbody.add_body(name=f"{prefix}_forearm", mocap=True).add_geom(
        name=f"{prefix}_forearm", type=mujoco.mjtGeom.mjGEOM_CAPSULE,
        size=[FOREARM_RADIUS * scale, FOREARM_LENGTH / 2, 0], **look)


def _quat_z_to(v):
    q = np.zeros(4)
    mujoco.mju_quatZ2Vec(q, np.asarray(v, float))
    return q


def _quat_of(R):
    q = np.zeros(4)
    mujoco.mju_mat2Quat(q, np.ascontiguousarray(R).ravel())
    return q


def kabsch(A, B):
    """Rotation R and shift t that best map points A onto B (no mirroring)."""
    ca, cb = A.mean(0), B.mean(0)
    U, _, Vt = np.linalg.svd((A - ca).T @ (B - cb))
    D = np.diag([1, 1, np.sign(np.linalg.det(Vt.T @ U.T))])
    R = Vt.T @ D @ U.T
    return R, cb - R @ ca


class SimHand:
    """Moves the stick hand of a compiled model."""

    def __init__(self, model, hand, prefix="hand"):
        self.hand, self.side = hand, hand["side"]
        self.T = np.array(hand["points"])
        self.box_centre, self.box_half = palm_box(self.T)
        self.lengths = [np.linalg.norm(self.T[b] - self.T[a]) for a, b in BONES]
        self.scale = np.linalg.norm(self.T[12] - self.T[0]) / REFERENCE_LENGTH
        body = lambda n: model.body(f"{prefix}_{n}")  # noqa: E731
        self.palm = model.body_mocapid[body("palm").id]
        self.bones = [model.body_mocapid[body(f"bone{i}").id] for i in range(len(BONES))]
        self.forearm = model.body_mocapid[body("forearm").id]
        self.bodies = {body(n).id for n in ["palm", "forearm"] + [f"bone{i}" for i in range(len(BONES))]}
        self.geoms = np.where(np.isin(model.geom_bodyid, list(self.bodies)))[0]
        self.points = None

    def fit(self, observed):
        """Observed 21 points (noisy, any size) -> the same pose with this hand's own proportions: palm fitted as
        one rigid piece, each finger bone pointing where the observed one points, with its own length."""
        O = np.asarray(observed, float)
        R, t = kabsch(self.T[PALM], O[PALM])
        out = self.T @ R.T + t
        for f in FINGERS:
            for a, b in zip(f[:-1], f[1:]):
                d = O[b] - O[a]
                n = np.linalg.norm(d)
                bone = np.linalg.norm(self.T[b] - self.T[a])
                out[b] = out[a] + (d / n * bone if n > 1e-6 else R @ (self.T[b] - self.T[a]))
        return out

    def pose(self, origin, R):
        """21 points of the template hand placed with its hand frame at origin, rotated by R."""
        return self.T @ np.asarray(R).T + origin

    def flat(self):
        """This hand held flat, as when offering it: knuckles where measured, fingers straight in the palm's
        plane along their knuckle directions, thumb straight out to the side; bone lengths as measured."""
        T = self.T.copy()
        T[PALM, 2] = 0.0
        for f in FINGERS[1:]:
            d = T[f[0]] - T[0]
            d /= np.linalg.norm(d)
            for a, b in zip(f[:-1], f[1:]):
                T[b] = T[a] + d * np.linalg.norm(self.T[b] - self.T[a])
        index = T[5] - T[0]
        index /= np.linalg.norm(index)
        side = np.sign(T[1, 0] - T[9, 0]) or 1.0  # thumb side of the hand (x)
        d = _rot(2, -side * np.radians(50)) @ index  # turned 50 deg from the index toward the thumb side
        for a, b in zip(FINGERS[0][:-1], FINGERS[0][1:]):
            T[b] = T[a] + d * np.linalg.norm(self.T[b] - self.T[a])
        return T

    def palm_up(self, centre, heading, tilt=(0.0, 0.0)):
        """Flat hand, palm up, palm centre at `centre`, fingers pointing along `heading` (rad, from +x), then
        tilted by (about the finger axis, about the across axis) rad."""
        y = np.array([np.cos(heading), np.sin(heading), 0.0])
        z = np.array([0.0, 0.0, 1.0])
        R = np.stack([np.cross(y, z), y, z], 1)
        roll, pitch = tilt
        R = R @ _rot(1, roll) @ _rot(0, pitch)
        return self.flat() @ R.T + (np.asarray(centre, float) - R @ self.box_centre)

    def set(self, data, points):
        """Move the sim hand to these 21 points (use fit() first for tracked points)."""
        p = np.asarray(points, float)
        self.points = p
        origin, R = hand_frame(p, self.side)
        data.mocap_pos[self.palm] = origin + R @ self.box_centre
        data.mocap_quat[self.palm] = _quat_of(R)
        for i, (a, b) in enumerate(BONES):
            data.mocap_pos[self.bones[i]] = (p[a] + p[b]) / 2
            data.mocap_quat[self.bones[i]] = _quat_z_to(p[b] - p[a])
        d = -R[:, 1] - FOREARM_DROP * np.array([0, 0, 1.0])
        d /= np.linalg.norm(d)
        # starts just behind the wrist, its top no higher than the palm's surface (the wrist is thinner than the
        # forearm's round end would make it)
        r = FOREARM_RADIUS * self.scale
        start = p[0] - R[:, 2] * max(0.0, r - self.box_half[2]) + d * r
        data.mocap_pos[self.forearm] = start + d * FOREARM_LENGTH / 2
        data.mocap_quat[self.forearm] = _quat_z_to(d)

    def park(self, data):
        """Out of the way (far off the table)."""
        self.set(data, self.palm_up([2.0, 2.0, 0.1], 0.0))

    def palm_top(self, points=None):
        """Centre of the palm's surface and the palm's normal, for the current (or given) points."""
        p = self.points if points is None else np.asarray(points, float)
        origin, R = hand_frame(p, self.side)
        return origin + R @ self.box_centre + R[:, 2] * self.box_half[2], R[:, 2]


def _rot(axis, angle):
    c, s = np.cos(angle), np.sin(angle)
    R = np.eye(3)
    i, j = [(1, 2), (0, 2), (0, 1)][axis]
    R[i, i], R[i, j], R[j, i], R[j, j] = c, -s, s, c
    return R
