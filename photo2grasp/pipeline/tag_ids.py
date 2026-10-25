"""Which AprilTags (36h11) are on an object: found in its videos/photos, confirmed or typed by you.

    tags = tag_setup("celsius_can", ["can.mp4"], "objects.json")   # -> {"front": 0 or None, "back": 5 or None}

"front" faces the robot (seen by the wrist camera; the gripper touches it to calibrate), "back" faces the
environment camera (RealSense). Any IDs work, and one tag is enough. Saved as objects.json -> <name> -> tags.
"""
import json
from pathlib import Path

import cv2

from video_frames import is_video

_detector = cv2.aruco.ArucoDetector(cv2.aruco.getPredefinedDictionary(cv2.aruco.DICT_APRILTAG_36h11),
                                    cv2.aruco.DetectorParameters())


def find_tag_ids(inputs, every=5):
    """{tag id: frames it was seen in} over all videos (every `every`-th frame) and photos."""
    seen = {}

    def scan(img):
        _, ids, _ = _detector.detectMarkers(cv2.cvtColor(img, cv2.COLOR_BGR2GRAY))
        for i in ([] if ids is None else set(ids.ravel().tolist())):
            seen[i] = seen.get(i, 0) + 1

    for path in inputs:
        if is_video(path):
            cap, k = cv2.VideoCapture(str(path)), 0
            while True:
                ok, img = cap.read()
                if not ok:
                    break
                if k % every == 0:
                    scan(img)
                k += 1
            cap.release()
        else:
            img = cv2.imread(str(path))
            if img is not None:
                scan(img)
    min_hits = 2 if any(is_video(p) for p in inputs) else 1  # in videos, ignore a one-frame misread
    return {i: n for i, n in seen.items() if n >= min_hits}


def _ask_id(question, default):
    answer = input(f"   {question} [{'none' if default is None else default}]: ").strip().lower()
    if not answer:
        return default
    return None if answer in ("none", "-", "n", "no") else int(answer)


def tag_setup(name, inputs, objects_path):
    """Find the object's tags, ask which way they face (or ask for the IDs), save and return them."""
    objects = json.loads(Path(objects_path).read_text())
    found = sorted(find_tag_ids(inputs), key=int) if inputs else []
    print(f"   AprilTags seen in the video: {found or 'none'}")
    if len(found) >= 2:
        front = _ask_id(f"which ID faces the ROBOT? (of {found})", found[0])
        back = _ask_id("which ID faces the environment camera (RealSense)?",
                       next((i for i in found if i != front), None))
    elif len(found) == 1:
        side = input(f"   tag {found[0]} faces the [r]obot, the [c]amera (RealSense), or [b]oth (the same ID glued "
                     "on both sides)? [c]: ").strip().lower()
        if side.startswith("b"):  # each camera only sees its own side, so one ID on both sides works
            front = back = found[0]
        elif side.startswith("r"):
            front, back = found[0], _ask_id("ID of a tag facing the RealSense, if any", None)
        else:
            front, back = _ask_id("ID of a tag facing the robot, if any", None), found[0]
    else:
        print("   no tag in the video (filmed before gluing?): type the IDs you will glue on")
        front = _ask_id("ID facing the ROBOT (wrist camera, touched to calibrate)", 0)
        back = _ask_id("ID facing the environment camera (RealSense)", 1)
    if front is None and back is None:
        raise SystemExit("the object needs at least one tag")
    objects.setdefault(name, {})["tags"] = dict(front=front, back=back)
    Path(objects_path).write_text(json.dumps(objects, indent=2))
    print(f"   tags saved: front (robot side) {front}, back (camera side) {back}")
    return objects[name]["tags"]
