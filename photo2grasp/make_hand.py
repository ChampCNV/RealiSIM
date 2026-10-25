"""Make a person's hand for the simulation (not an object: no 3D model, no TRELLIS, no tags).

    python make_hand.py my_hand inputs/hand.mp4 [more videos / photos] [--length 18.5] [--side right]
    python make_hand.py my_hand --webcam 0

Film the open hand (fingers straight, a little spread), palm or back toward the camera, turning it a bit.
MediaPipe finds the hand's 21 points in every frame; the hand's shape is the median over the frames where it is
open. Its size: wrist crease to the tip of the middle finger (--length in cm, else asked; MediaPipe's own estimate
is the default). Saves hands/<name>/hand.json, landmarks.jpg (the best frame with the points drawn) and
preview.png (the sim hand). Next: python photo2grasp.py check <object> --hand <name>
"""
import argparse
import json
import sys
from pathlib import Path

import cv2
import numpy as np

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT / "pipeline"))
sys.path.insert(0, str(ROOT / "sim2real"))
from hand_landmarks import HandDetector, draw, pick  # noqa: E402
from hand_sim import FINGERS, HANDS, canonical  # noqa: E402

IMAGE_TYPES = {".jpg", ".jpeg", ".png", ".bmp", ".webp"}
FRAMES_PER_VIDEO = 60
MIN_FRAMES = 3


def openness(world):
    """How straight the four long fingers are (1 = straight): knuckle-to-tip distance / bone lengths."""
    out = []
    for f in FINGERS[1:]:
        bones = sum(np.linalg.norm(world[b] - world[a]) for a, b in zip(f[:-1], f[1:]))
        out.append(np.linalg.norm(world[f[-1]] - world[f[0]]) / bones)
    return min(out)


def frames_from(paths):
    """BGR frames: every photo, and up to FRAMES_PER_VIDEO evenly spread frames of every video."""
    for p in map(Path, paths):
        if p.suffix.lower() in IMAGE_TYPES:
            img = cv2.imread(str(p))
            if img is None:
                raise SystemExit(f"cannot read {p}")
            yield img
            continue
        cap = cv2.VideoCapture(str(p))
        n = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))
        if not cap.isOpened() or n <= 0:
            raise SystemExit(f"cannot read {p} (photo: {', '.join(sorted(IMAGE_TYPES))}; or a video)")
        for i in np.linspace(0, n - 1, min(n, FRAMES_PER_VIDEO)).astype(int):
            cap.set(cv2.CAP_PROP_POS_FRAMES, int(i))
            ok, frame = cap.read()
            if ok:
                yield frame
        cap.release()


def from_files(paths, side):
    detector = HandDetector(num_hands=2, video=False)
    found = []
    for frame in frames_from(paths):
        hand = pick(detector(cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)), side)
        if hand is not None:
            found.append((hand, frame))
    detector.close()
    return found


def from_webcam(index, side, wanted=40):
    """Live view; SPACE records `wanted` frames with the open hand; q ends."""
    cap = cv2.VideoCapture(index, cv2.CAP_DSHOW if sys.platform == "win32" else cv2.CAP_ANY)
    if not cap.isOpened():
        raise SystemExit(f"camera {index} not found")
    detector, found, recording = HandDetector(num_hands=2, video=True), [], False
    print("hold the open hand to the camera (fingers straight), SPACE = record, turn it a little; q = done")
    while True:
        ok, frame = cap.read()
        if not ok:
            break
        hand = pick(detector(cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)), side)
        view = frame.copy()
        if hand is not None:
            draw(view, hand)
            if recording and openness(hand["world"]) > 0.85:
                found.append((hand, frame))
        recording = recording and len(found) < wanted
        msg = f"recording {len(found)}/{wanted}" if recording else f"{len(found)} frames | SPACE record, q done"
        cv2.putText(view, msg, (14, 40), cv2.FONT_HERSHEY_SIMPLEX, 1.0, (0, 255, 255), 2, cv2.LINE_AA)
        cv2.imshow("make hand (q = done)", view)
        key = cv2.waitKey(1) & 0xFF
        if key == ord(" "):
            recording = True
        elif key == ord("q") or (not recording and len(found) >= wanted):
            break
    cap.release()
    detector.close()
    cv2.destroyAllWindows()
    return found


def preview(hand, path):
    """Picture of the sim hand, palm up, seen from above at an angle."""
    import mujoco
    from hand_sim import SimHand, add_hand
    spec = mujoco.MjSpec()
    spec.worldbody.add_light(pos=[0, 0, 1], dir=[0, 0, -1], diffuse=[0.8, 0.8, 0.8])
    spec.worldbody.add_geom(type=mujoco.mjtGeom.mjGEOM_PLANE, size=[0.4, 0.4, 0.01], rgba=[0.3, 0.3, 0.35, 1],
                            contype=0, conaffinity=0)
    add_hand(spec, hand)
    m = spec.compile()
    d = mujoco.MjData(m)
    sim = SimHand(m, hand)
    sim.set(d, sim.palm_up([0, 0, 0.08], np.pi / 2))
    mujoco.mj_forward(m, d)
    cam = mujoco.MjvCamera()
    cam.lookat[:], cam.distance, cam.azimuth, cam.elevation = [0, 0, 0.06], 0.45, 120, -45
    with mujoco.Renderer(m, 480, 640) as r:
        r.update_scene(d, cam)
        cv2.imwrite(str(path), cv2.cvtColor(r.render(), cv2.COLOR_RGB2BGR))


def make_hand(name, inputs=(), webcam=None, length_cm=None, side=None):
    found = from_webcam(webcam, side) if webcam is not None else from_files(inputs, side)
    if not found:
        raise SystemExit("no hand found: show the whole hand, well lit, fingers visible")
    sides = [h["side"] for h, _ in found]
    if side is None:
        side = max(set(sides), key=sides.count)
        answer = input(f"this is a {side.upper()} hand? [Y/n] (or type left/right): ").strip().lower()
        if answer in ("left", "right"):
            side = answer
        elif answer == "n":
            side = "left" if side == "right" else "right"
    found = [(h, f) for h, f in found if h["side"] == side] or found
    open_ = [(h, f) for h, f in found if openness(h["world"]) > 0.85]
    if len(open_) < MIN_FRAMES:
        raise SystemExit(f"only {len(open_)} frame(s) with the hand open (need {MIN_FRAMES}): keep the fingers "
                         f"straight and the whole hand in view")
    points = np.median([canonical(h["world"], side) for h, _ in open_], axis=0)
    estimate = np.linalg.norm(points[12] - points[0]) * 100
    while length_cm is None:
        answer = input(f"hand length, wrist crease to middle fingertip, in cm [ENTER = MediaPipe's estimate "
                       f"{estimate:.1f}]: ").strip().lower().replace("cm", "")
        try:
            length_cm = float(answer) if answer else estimate
        except ValueError:
            print("   a number in cm, e.g. 18.5")
    if not 10 <= length_cm <= 25:
        raise SystemExit(f"{length_cm:.1f} cm is not a hand length (adults: about 16-21 cm)")
    points *= length_cm / estimate
    out = HANDS / name
    out.mkdir(parents=True, exist_ok=True)
    hand = dict(name=name, side=side, length_m=round(length_cm / 100, 4), points=points.round(5).tolist(),
                frames_used=len(open_), sources=[str(p) for p in inputs] or [f"webcam {webcam}"])
    (out / "hand.json").write_text(json.dumps(hand, indent=2))
    best_hand, best_frame = max(open_, key=lambda hf: openness(hf[0]["world"]))
    shown = best_frame.copy()
    draw(shown, best_hand)
    cv2.imwrite(str(out / "landmarks.jpg"), shown)
    preview(hand, out / "preview.png")
    palm = np.linalg.norm(points[5] - points[17]) * 100
    print(f"hand '{name}': {side}, {length_cm:.1f} cm long, knuckles {palm:.1f} cm across, from {len(open_)} open-hand "
          f"frames -> {out.relative_to(ROOT)}/hand.json (+ landmarks.jpg, preview.png)")
    return hand


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("name")
    ap.add_argument("inputs", nargs="*", help="videos / photos of the open hand")
    ap.add_argument("--webcam", type=int, help="record from this camera instead")
    ap.add_argument("--length", type=float, help="cm, wrist crease to middle fingertip")
    ap.add_argument("--side", choices=["left", "right"])
    args = ap.parse_args()
    if not args.inputs and args.webcam is None:
        raise SystemExit("give videos/photos of the hand, or --webcam <index>")
    make_hand(args.name, [str(Path(p).resolve()) for p in args.inputs], args.webcam, args.length, args.side)


if __name__ == "__main__":
    main()
