"""MediaPipe hand landmarks: Google's HandLandmarker, float16 model (~8 MB, ~30 ms per 1280x720 frame on a
laptop CPU, no GPU needed). Downloaded once to third_party/models/.

3D without a depth camera: MediaPipe also gives each hand's 21 points in metres around the hand's centre (its
shape, at an average hand's size). Scaled to the person's measured hand and matched to the 21 image points
(solvePnP), they give where the hand is in front of the camera. A depth image (RealSense) corrects the distance.
"""
import urllib.request
from pathlib import Path

import cv2
import numpy as np

ROOT = Path(__file__).resolve().parent.parent
MODEL = ROOT / "third_party" / "models" / "hand_landmarker.task"
MODEL_URL = ("https://storage.googleapis.com/mediapipe-models/hand_landmarker/hand_landmarker/float16/latest/"
             "hand_landmarker.task")
LINKS = [(0, 1), (1, 2), (2, 3), (3, 4), (0, 5), (5, 6), (6, 7), (7, 8), (5, 9), (9, 10), (10, 11), (11, 12),
         (9, 13), (13, 14), (14, 15), (15, 16), (13, 17), (0, 17), (17, 18), (18, 19), (19, 20)]


def ensure_model():
    if not MODEL.exists():
        MODEL.parent.mkdir(parents=True, exist_ok=True)
        print(f"downloading the MediaPipe hand model (float16, ~8 MB) to {MODEL.relative_to(ROOT)} ...")
        urllib.request.urlretrieve(MODEL_URL, MODEL)
    return MODEL


class HandDetector:
    """rgb image -> list of hands: dict(side "left"/"right", score, px (21x2 pixels), world (21x3 m))."""

    def __init__(self, num_hands=2, video=True):
        from mediapipe.tasks.python import BaseOptions, vision
        import mediapipe as mp
        self.mp = mp
        mode = vision.RunningMode.VIDEO if video else vision.RunningMode.IMAGE
        self.video, self.t_ms = video, 0
        self.landmarker = vision.HandLandmarker.create_from_options(vision.HandLandmarkerOptions(
            base_options=BaseOptions(model_asset_path=str(ensure_model())), running_mode=mode, num_hands=num_hands,
            min_hand_detection_confidence=0.5, min_tracking_confidence=0.5))

    def __call__(self, rgb):
        h, w = rgb.shape[:2]
        image = self.mp.Image(image_format=self.mp.ImageFormat.SRGB, data=np.ascontiguousarray(rgb))
        if self.video:
            self.t_ms += 33  # only needs to increase
            result = self.landmarker.detect_for_video(image, self.t_ms)
        else:
            result = self.landmarker.detect(image)
        hands = []
        for lm, world, side in zip(result.hand_landmarks, result.hand_world_landmarks, result.handedness):
            hands.append(dict(side=side[0].category_name.lower(), score=float(side[0].score),
                              px=np.array([[p.x * w, p.y * h] for p in lm]),
                              world=np.array([[p.x, p.y, p.z] for p in world])))
        return hands

    def close(self):
        self.landmarker.close()


def pick(hands, side=None):
    """The hand to use: the given side if seen, else the biggest in the image."""
    if side:
        same = [h for h in hands if h["side"] == side]
        hands = same or hands
    if not hands:
        return None
    return max(hands, key=lambda h: np.ptp(h["px"][:, 0]) * np.ptp(h["px"][:, 1]))


def palm_size(p):
    return np.mean([np.linalg.norm(p[k] - p[0]) for k in (5, 9, 13, 17)])


def camera_points(hand, template, K, depth=None, depth_scale=1.0):
    """21 points of a detected hand in the camera frame (OpenCV: x right, y down, z forward), in metres.
    template: the person's hand points (hands/<name>/hand.json) for the real size, or None for MediaPipe's own
    estimate (an average hand). depth: optional depth image aligned with the colour image (in units of
    depth_scale metres) to correct the distance."""
    obj = hand["world"] * (palm_size(np.asarray(template)) / palm_size(hand["world"]) if template is not None else 1)
    ok, rvec, tvec = cv2.solvePnP(obj.astype(np.float64), hand["px"].astype(np.float64), K, None,
                                  flags=cv2.SOLVEPNP_SQPNP)
    if not ok:
        return None
    pts = obj @ cv2.Rodrigues(rvec)[0].T + tvec.ravel()
    if depth is not None:
        pts = depth_correct(pts, hand["px"], depth, depth_scale)
    return pts


def depth_correct(pts, px, depth, depth_scale, palm=(0, 5, 9, 13, 17), surface=0.01):
    """Scale the points along the camera rays so the palm's distance matches the depth image (the depth camera
    sees the skin, ~1 cm in front of MediaPipe's points)."""
    h, w = depth.shape[:2]
    seen, model = [], []
    for k in palm:
        u, v = np.round(px[k]).astype(int)
        if 2 <= u < w - 2 and 2 <= v < h - 2:
            patch = depth[v - 2:v + 3, u - 2:u + 3].astype(float) * depth_scale
            patch = patch[patch > 0.1]
            if patch.size:
                seen.append(np.median(patch) + surface)
                model.append(pts[k, 2])
    if len(seen) < 3:
        return pts
    return pts * (np.median(seen) / np.median(model))


def draw(frame_bgr, hand, colour=(255, 200, 0)):
    p = hand["px"].astype(int)
    for a, b in LINKS:
        cv2.line(frame_bgr, tuple(p[a]), tuple(p[b]), colour, 2, cv2.LINE_AA)
    for q in p:
        cv2.circle(frame_bgr, tuple(q), 4, (0, 0, 255), -1, cv2.LINE_AA)
