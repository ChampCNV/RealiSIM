"""Step 4: set up the wrist camera. Shows the live image next to the 64x64 view the policy will get.

    python robot/camera_setup.py

Keys (in the preview window):
    0-9      switch to camera with that index
    + / -    exposure brighter / darker (fixed exposure keeps colours stable while the arm moves)
    a        auto exposure (not recommended for running the policy)
    [ / ]    lens field of view estimate down / up (used to crop the same view as the 71 deg sim camera)
    s        save to config.json        q / Esc  quit
"""
import json
from pathlib import Path

import cv2
import numpy as np

from camera import Camera, list_cameras, policy_view

CONFIG = Path(__file__).parent / "config.json"


def main():
    cfg = json.loads(CONFIG.read_text())
    cc = cfg["wrist_camera"]
    found = list_cameras()
    print(f"cameras found: {found}")
    if not found:
        raise SystemExit("No camera found. Plug the wrist camera in and allow camera access for desktop apps "
                         "(Settings > Privacy & security > Camera).")
    index = cc["index"] if cc["index"] in found else found[-1]
    exposure, fovy = cc.get("fixed_exposure", -6), cc.get("vertical_fov_deg")
    cam = Camera(index, cc["width"], cc["height"], exposure)
    print(__doc__)
    while True:
        img = cam.read()
        small = policy_view(img, 64, fovy)
        left = cv2.resize(img, (480, int(480 * img.shape[0] / img.shape[1])))
        right = cv2.resize(small, (left.shape[0], left.shape[0]), interpolation=cv2.INTER_NEAREST)
        panel = np.hstack([left, right])
        text = f"camera {index} | exposure {exposure if exposure is not None else 'auto'} | fov {fovy or 'not set'}"
        cv2.putText(panel, text, (8, 22), cv2.FONT_HERSHEY_SIMPLEX, 0.55, (255, 255, 255), 1, cv2.LINE_AA)
        cv2.putText(panel, "policy view (64x64)", (left.shape[1] + 8, 22), cv2.FONT_HERSHEY_SIMPLEX, 0.55,
                    (255, 255, 255), 1, cv2.LINE_AA)
        cv2.imshow("wrist camera setup", cv2.cvtColor(panel, cv2.COLOR_RGB2BGR))
        key = cv2.waitKey(30) & 0xFF
        if key in (ord("q"), 27):
            break
        if ord("0") <= key <= ord("9") and key - ord("0") in found:
            cam.close()
            index = key - ord("0")
            cam = Camera(index, cc["width"], cc["height"], exposure)
        elif key in (ord("+"), ord("=")):
            exposure = (exposure if exposure is not None else -6) + 1
            cam.set_exposure(exposure)
        elif key == ord("-"):
            exposure = (exposure if exposure is not None else -6) - 1
            cam.set_exposure(exposure)
        elif key == ord("a"):
            exposure = None
            cam.set_exposure(None)
        elif key in (ord("["), ord("]")):
            fovy = (fovy or 71) + (-2 if key == ord("[") else 2)
        elif key == ord("s"):
            cc.update(index=index, fixed_exposure=exposure, vertical_fov_deg=fovy)
            CONFIG.write_text(json.dumps(cfg, indent=2))
            print(f"saved: camera {index}, exposure {exposure}, fov {fovy}")
    cam.close()
    cv2.destroyAllWindows()


if __name__ == "__main__":
    main()
