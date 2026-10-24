"""USB camera (OpenCV) with fixed exposure, so colours don't drift while the arm moves."""
import sys

import cv2
import numpy as np

SIM_WRIST_FOVY = 71.0  # degrees: field of view of the simulated wrist camera
SIM_RENDER = 128  # the simulation renders 128x128, then shrinks to the policy's size


def policy_view(img, size, real_fovy=None):
    """Turn a real camera frame into the image the policy was trained on: a centred square showing the same
    field of view as the sim camera, area-resized 128 -> `size`."""
    h, w = img.shape[:2]
    s = min(h, w)
    if real_fovy:  # a wider real lens sees more: crop the centre so both see the same angle
        s = int(s * min(1.0, np.tan(np.radians(SIM_WRIST_FOVY / 2)) / np.tan(np.radians(real_fovy / 2))))
    img = img[(h - s) // 2:(h - s) // 2 + s, (w - s) // 2:(w - s) // 2 + s]
    img = cv2.resize(img, (SIM_RENDER, SIM_RENDER), interpolation=cv2.INTER_AREA)
    return cv2.resize(img, (size, size), interpolation=cv2.INTER_AREA)


class Camera:
    def __init__(self, index, width=640, height=480, fixed_exposure=None):
        backend = cv2.CAP_DSHOW if sys.platform == "win32" else cv2.CAP_ANY
        self.cap = cv2.VideoCapture(index, backend)
        if not self.cap.isOpened():
            raise ConnectionError(f"camera {index} not found")
        self.cap.set(cv2.CAP_PROP_FRAME_WIDTH, width)
        self.cap.set(cv2.CAP_PROP_FRAME_HEIGHT, height)
        if fixed_exposure is not None:
            self.cap.set(cv2.CAP_PROP_AUTO_EXPOSURE, 0.25)  # manual mode (DirectShow convention)
            self.cap.set(cv2.CAP_PROP_EXPOSURE, fixed_exposure)
            self.cap.set(cv2.CAP_PROP_AUTO_WB, 0)
        else:  # the camera's own automatic view (the driver keeps manual settings from earlier runs otherwise)
            self.cap.set(cv2.CAP_PROP_AUTO_EXPOSURE, 0.75)
            self.cap.set(cv2.CAP_PROP_AUTO_WB, 1)
        for _ in range(5):  # let the sensor settle
            self.cap.read()

    def read(self):
        """Latest frame as RGB."""
        ok, frame = self.cap.read()
        if not ok:
            raise IOError("camera read failed")
        return cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)

    def set_exposure(self, value):
        if value is None:
            self.cap.set(cv2.CAP_PROP_AUTO_EXPOSURE, 0.75)  # back to auto (DirectShow convention)
        else:
            self.cap.set(cv2.CAP_PROP_AUTO_EXPOSURE, 0.25)
            self.cap.set(cv2.CAP_PROP_EXPOSURE, value)

    def close(self):
        self.cap.release()


def camera_indices(cfg):
    """(wrist, environment) camera indices from robot/config.json, corrected when Windows renumbered them after
    re-plugging: a configured camera that is missing becomes the only other camera found. The environment index
    is None when none is configured. Stops with a message if it cannot tell which camera is which."""
    found = list_cameras()
    wrist = cfg["wrist_camera"]["index"]
    env = (cfg.get("env_camera") or {}).get("index")
    names = {"wrist": wrist, "environment": env}
    for name, other in (("environment", "wrist"), ("wrist", "environment")):
        if names[name] is None or names[name] in found:
            continue
        rest = [i for i in found if i != names[other]]
        if len(rest) == 1:
            print(f"{name} camera {names[name]} not found: using camera {rest[0]} (the only other one; "
                  f"robot/config.json keeps {names[name]})")
            names[name] = rest[0]
        else:
            raise SystemExit(f"{name} camera {names[name]} not found (cameras found: {found}): set the right "
                             f"index in robot/config.json ({'wrist_camera' if name == 'wrist' else 'env_camera'})")
    return names["wrist"], names["environment"]


def list_cameras(max_index=6):
    """Indices of cameras that open and return a frame."""
    found = []
    for i in range(max_index):
        cap = cv2.VideoCapture(i, cv2.CAP_DSHOW if sys.platform == "win32" else cv2.CAP_ANY)
        if cap.isOpened() and cap.read()[0]:
            found.append(i)
        cap.release()
    return found


class VideoFile:
    """A recorded video played like a camera (loops), for trying tracking without the setup."""

    def __init__(self, path):
        self.cap = cv2.VideoCapture(str(path))
        if not self.cap.isOpened():
            raise ConnectionError(f"cannot open video {path}")

    def read(self):
        ok, frame = self.cap.read()
        if not ok:
            self.cap.set(cv2.CAP_PROP_POS_FRAMES, 0)
            ok, frame = self.cap.read()
            if not ok:
                raise IOError("video read failed")
        return cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)

    def close(self):
        self.cap.release()


class RealSense:
    """Intel RealSense colour + depth (aligned to the colour image) through pyrealsense2. read() is the colour
    frame (RGB) like Camera; depth() the matching depth image (uint16, x depth_scale = metres)."""

    def __init__(self, width=1280, height=720, fps=30):
        import pyrealsense2 as rs
        self.rs, self.pipe, cfg = rs, rs.pipeline(), rs.config()
        cfg.enable_stream(rs.stream.color, width, height, rs.format.rgb8, fps)
        cfg.enable_stream(rs.stream.depth, min(width, 1280), min(height, 720), rs.format.z16, fps)
        profile = self.pipe.start(cfg)
        self.depth_scale = profile.get_device().first_depth_sensor().get_depth_scale()
        self.align = rs.align(rs.stream.color)
        self._depth = None
        for _ in range(15):  # let auto exposure settle
            self.pipe.wait_for_frames()

    def read(self):
        frames = self.align.process(self.pipe.wait_for_frames())
        self._depth = np.asanyarray(frames.get_depth_frame().get_data()).copy()
        return np.asanyarray(frames.get_color_frame().get_data()).copy()

    def depth(self):
        return self._depth

    def close(self):
        self.pipe.stop()
