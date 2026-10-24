"""The person's hand as the environment camera sees it, in the robot frame.

MediaPipe finds the hand's 21 points in the image; with the person's measured hand (hands/<name>/hand.json) and
the camera's lens they become 3D points in front of the camera (solvePnP; a RealSense depth image corrects the
distance), and the environment camera's calibration (robot/config.json) turns them into the robot's frame.
"""
import sys
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "pipeline"))
sys.path.insert(0, str(ROOT / "sim2real"))
from hand_landmarks import HandDetector, camera_points, draw, pick  # noqa: E402
from lift_env import look_at  # noqa: E402
from tag_env import ENV_CAMERA, intrinsics, vfov  # noqa: E402


def env_camera_pose(cfg, width, height):
    """(position, OpenCV rotation, K, calibrated?) of the environment camera for frames of this size: from the
    calibration, else where tag_env.ENV_CAMERA plans it (good enough to try hand tracking)."""
    ec = cfg.get("env_camera") or {}
    if "pos" in ec:
        return np.array(ec["pos"]), np.array(ec["R"]), intrinsics(ec["fovy_used"], width, height), True
    e = ENV_CAMERA
    xy = look_at(np.array(e["pos"]), np.array(e["target"]))
    R = np.stack([xy[:3], xy[3:], np.cross(xy[:3], xy[3:])], 1) @ np.diag([1, -1, -1])  # MuJoCo -> OpenCV axes
    return np.array(e["pos"], float), R, intrinsics(vfov(e["hfov"], width, height), width, height), False


class HandWatcher:
    def __init__(self, hand_model, cam_pos, cam_R, K, depth_scale=1.0):
        self.detector = HandDetector(num_hands=2, video=True)
        self.template, self.side = np.array(hand_model["points"]), hand_model["side"]
        self.pos, self.R, self.K, self.depth_scale = np.asarray(cam_pos), np.asarray(cam_R), K, depth_scale

    def __call__(self, rgb, depth=None):
        """dict(points: 21x3 robot frame, detection) for the person's hand in this frame, or None."""
        hand = pick(self.detector(rgb), self.side)
        if hand is None:
            return None
        pc = camera_points(hand, self.template, self.K, depth, self.depth_scale)
        if pc is None or np.median(pc[:, 2]) < 0.05:
            return None
        return dict(points=self.pos + pc @ self.R.T, detection=hand)

    @staticmethod
    def draw(frame_bgr, seen):
        draw(frame_bgr, seen["detection"])

    def close(self):
        self.detector.close()
