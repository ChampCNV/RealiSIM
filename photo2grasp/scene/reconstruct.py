"""Scene step 1: video of a table with objects -> camera poses + 3D point cloud (COLMAP).

    python scene/reconstruct.py inputs/scene/1.mp4 outputs/scene
"""
import sys
from pathlib import Path

import cv2
import numpy as np
import pycolmap


def extract_frames(video, out_dir, n_frames=150):
    """Evenly spaced frames, skipping the blurriest ones in each window."""
    out_dir.mkdir(parents=True, exist_ok=True)
    cap = cv2.VideoCapture(str(video))
    total = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))
    frames = []
    while True:
        ok, f = cap.read()
        if not ok:
            break
        frames.append(f)
    windows = np.array_split(np.arange(total), n_frames)
    for k, win in enumerate(windows):
        if len(win) == 0:
            continue
        sharp = [cv2.Laplacian(cv2.cvtColor(frames[i], cv2.COLOR_BGR2GRAY), cv2.CV_64F).var() for i in win]
        cv2.imwrite(str(out_dir / f"{k:04d}.jpg"), frames[win[int(np.argmax(sharp))]], [cv2.IMWRITE_JPEG_QUALITY, 95])
    return out_dir


def reconstruct(image_dir, work_dir):
    db = work_dir / "database.db"
    if db.exists():
        db.unlink()
    pycolmap.extract_features(db, image_dir, camera_mode=pycolmap.CameraMode.SINGLE)  # one phone camera
    pycolmap.match_sequential(db)  # video frames: match neighbours, plus loop closure
    maps = pycolmap.incremental_mapping(db, image_dir, work_dir / "sparse")
    best = max(maps.values(), key=lambda r: r.num_reg_images())
    best.write(work_dir / "sparse")
    return best


if __name__ == "__main__":
    video, out = Path(sys.argv[1]), Path(sys.argv[2])
    out.mkdir(parents=True, exist_ok=True)
    frames = extract_frames(video, out / "frames")
    rec = reconstruct(frames, out)
    print(f"registered {rec.num_reg_images()} / {len(list(frames.glob('*.jpg')))} frames, "
          f"{rec.num_points3D()} 3D points")
