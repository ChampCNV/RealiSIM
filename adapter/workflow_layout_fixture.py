"""LABELLED SYNTHETIC FIXTURE (not user data): a fake COLMAP reconstruction of a workcell with known ground truth,
for integration-testing workflow_layout end to end (upstream find_objects + layout + anchors + tag scale).

    .venv/bin/python adapter/workflow_layout_fixture.py OUT_WORK_DIR   -> writes sparse/, frames/, keyframes.json, truth.json
World = metric scene rotated, translated and scaled (1 unit = 2 cm), so units/frames are really exercised.
"""
import json, math, sys
from pathlib import Path
import numpy as np
import cv2
import pycolmap

rng = np.random.default_rng(3)
L, W, H = 0.80, 0.50, 0.75
K_SCALE = 0.02                     # metres per world unit
TAG = dict(family="tag36h11", id=0, size=0.06, center=(-0.17, 0.15), yaw=20.0)
TAG_IN_BASE = dict(x_m=0.12, y_m=0.0, yaw_deg=0.0)
OBJECTS = {"soda_can": dict(xy=(0.15, 0.10), r=0.033, h=0.12), "tape_roll": dict(xy=(-0.05, -0.14), r=0.05, h=0.025)}
IMG = (640, 480)
F = 520.0


def tag_frame():
    yw = math.radians(TAG["yaw"])
    ex, ey = np.array([math.cos(yw), math.sin(yw)]), np.array([-math.sin(yw), math.cos(yw)])
    return np.array(TAG["center"]), ex, ey


def truth_base():
    c, ex, ey = tag_frame()
    b = c - TAG_IN_BASE["x_m"] * ex          # tag 12 cm in front of the base along the base x axis
    return b, TAG["yaw"]


def scene_points():
    pts = []
    n_top = 9000
    pts.append(np.c_[rng.uniform(-L / 2, L / 2, n_top), rng.uniform(-W / 2, W / 2, n_top), rng.normal(0, 0.0008, n_top)])
    n_fl = 7000
    pts.append(np.c_[rng.uniform(-1.2, 1.2, n_fl), rng.uniform(-1.0, 1.0, n_fl), -H + rng.normal(0, 0.001, n_fl)])
    for o in OBJECTS.values():
        n = 1200
        a = rng.uniform(0, 2 * math.pi, n); z = rng.uniform(0, o["h"], n)
        pts.append(np.c_[o["xy"][0] + o["r"] * np.cos(a), o["xy"][1] + o["r"] * np.sin(a), z])
        m = 300
        rr = o["r"] * np.sqrt(rng.uniform(0, 1, m)); a = rng.uniform(0, 2 * math.pi, m)
        pts.append(np.c_[o["xy"][0] + rr * np.cos(a), o["xy"][1] + rr * np.sin(a), np.full(m, o["h"])])
    b, yaw = truth_base()                    # robot: base column 9x9 cm, 0.32 m tall, plus a forearm reaching forward
    n = 2500
    pts.append(np.c_[b[0] + rng.uniform(-0.045, 0.045, n), b[1] + rng.uniform(-0.045, 0.045, n), rng.uniform(0, 0.32, n)])
    yr = math.radians(yaw)
    t = rng.uniform(0, 0.10, 800)
    pts.append(np.c_[b[0] + t * math.cos(yr) + rng.normal(0, 0.01, 800), b[1] + t * math.sin(yr) + rng.normal(0, 0.01, 800), 0.30 + rng.normal(0, 0.01, 800)])
    return np.vstack(pts)


def look_at(C, target):
    z = target - C; z /= np.linalg.norm(z)
    x = np.cross(z, [0, 0, 1.0]); x /= np.linalg.norm(x)
    y = np.cross(z, x)
    return np.stack([x, y, z])          # rows: camera axes in world (metric); image y points down (toward -z world)


def main(out):
    out = Path(out); (out / "sparse").mkdir(parents=True, exist_ok=True); (out / "frames").mkdir(exist_ok=True)
    ang = math.radians(rng.uniform(0, 360))
    ax = rng.normal(size=3); ax /= np.linalg.norm(ax)
    Rw, _ = cv2.Rodrigues(ax * ang)
    tw = rng.normal(size=3) * 4
    to_w = lambda X: (np.asarray(X) @ Rw.T) / K_SCALE + tw
    rec = pycolmap.Reconstruction()
    cam = pycolmap.Camera(model="SIMPLE_PINHOLE", width=IMG[0], height=IMG[1], params=[F, IMG[0] / 2, IMG[1] / 2], camera_id=1)
    rec.add_camera_with_trivial_rig(cam)
    Kmat = np.array([[F, 0, IMG[0] / 2], [0, F, IMG[1] / 2], [0, 0, 1]])
    tag_img = cv2.aruco.generateImageMarker(cv2.aruco.getPredefinedDictionary(cv2.aruco.DICT_APRILTAG_36h11), TAG["id"], 400, borderBits=1)
    pad = 80
    tag_img = cv2.copyMakeBorder(tag_img, pad, pad, pad, pad, cv2.BORDER_CONSTANT, value=255)
    c, ex, ey = tag_frame()
    s = TAG["size"] * (400 + 2 * pad) / 400 / 2
    # tag image corners (TL, TR, BR, BL) -> table points: x = +ex to the right, image-up = +ey
    quad_m = np.array([c - s * ex + s * ey, c + s * ex + s * ey, c + s * ex - s * ey, c - s * ex - s * ey])
    src = np.float32([[0, 0], [tag_img.shape[1], 0], [tag_img.shape[1], tag_img.shape[0]], [0, tag_img.shape[0]]])
    frames = []
    for i in range(36):
        a = 2 * math.pi * i / 36
        C = np.array([0.75 * math.cos(a), 0.6 * math.sin(a), 0.5 + 0.05 * math.sin(3 * a)])
        Rc = look_at(C, np.array([0.0, 0.0, 0.0]))
        Rcw = Rc @ Rw.T
        tcw = -Rcw @ (tw + Rw @ C / K_SCALE)
        im = pycolmap.Image(name=f"{i:04d}.jpg", camera_id=1, image_id=i + 1)
        rec.add_image_with_trivial_frame(im, pycolmap.Rigid3d(pycolmap.Rotation3d(Rcw), tcw))
        # render the tag: homography table-plane -> image (metric camera)
        P3 = np.c_[quad_m, np.zeros(4)]
        pc = (P3 - C) @ Rc.T
        uv = (pc @ Kmat.T)[:, :2] / pc[:, 2:3]
        Hm = cv2.getPerspectiveTransform(src, np.float32(uv))
        canvas = np.full((IMG[1], IMG[0]), 90, np.uint8)
        warped = cv2.warpPerspective(tag_img, Hm, IMG, borderValue=0)
        mask = cv2.warpPerspective(np.full(tag_img.shape, 255, np.uint8), Hm, IMG)
        canvas[mask > 0] = warped[mask > 0]
        cv2.imwrite(str(out / "frames" / f"{i:04d}.jpg"), cv2.cvtColor(canvas, cv2.COLOR_GRAY2BGR))
        frames.append(dict(name=f"{i:04d}.jpg", video_frame=i * 10, time_s=round(i / 3.0, 4)))
    for X in to_w(scene_points()):
        rec.add_point3D(X, pycolmap.Track(), np.array([120, 120, 120], np.uint8))
    rec.write(str(out / "sparse"))
    (out / "keyframes.json").write_text(json.dumps(dict(frames=frames, decoded=360, reported_total=360, fps=30, width=IMG[0], height=IMG[1])))
    # simulated "Claude" 2D boxes in keyframe 0009.jpg for the named objects and the robot (from projected truth, slightly loose)
    i = 9; a = 2 * math.pi * i / 36
    C = np.array([0.75 * math.cos(a), 0.6 * math.sin(a), 0.5 + 0.05 * math.sin(3 * a)]); Rc = look_at(C, np.zeros(3))
    def box(xy, r, h):
        q = np.array([[xy[0] + dx, xy[1] + dy, z] for dx in (-r, r) for dy in (-r, r) for z in (0, h)])
        pc = (q - C) @ Rc.T; uv = (pc @ Kmat.T)[:, :2] / pc[:, 2:3]
        lo, hi = uv.min(0), uv.max(0)
        return [float(lo[0] / IMG[0]) - 0.01, float(lo[1] / IMG[1]) - 0.01, float(hi[0] / IMG[0]) + 0.01, float(hi[1] / IMG[1]) + 0.01]
    b, yaw = truth_base()
    sel = dict(objects=[dict(name=n, bbox=dict(keyframe="0009.jpg", xyxy_norm=box(o["xy"], o["r"], o["h"])), source="fixture") for n, o in OBJECTS.items()]
               + [dict(name="ghost_mug", bbox=dict(time_s=3.0, xyxy_norm=[0.80, 0.80, 0.86, 0.90]), source="fixture")],
               robots=[dict(name="so101_main", library_id="robotstudio_so101", bbox=dict(keyframe="0009.jpg", xyxy_norm=box(b, 0.05, 0.32)), source="fixture")])
    truth = dict(L=L, W=W, H=H, k=K_SCALE, objects={n: o["xy"] for n, o in OBJECTS.items()}, base=b.tolist(), base_yaw=yaw, tag=TAG, tag_in_base=TAG_IN_BASE,
                 note="table frame of the fixture: x along length; the upstream fit may return it rotated 180 deg")
    (out / "truth.json").write_text(json.dumps(truth, indent=1))
    (out / "selections.json").write_text(json.dumps(sel, indent=1))
    print("fixture written", out, rec.num_points3D(), "points", rec.num_reg_images(), "images")


if __name__ == "__main__":
    main(sys.argv[1])
