"""RealiSIM optional robot-model helpers (repo Python environment: mujoco, cv2).

    python robot_tool.py stage  --id LIBRARY_ID --out DIR      # verify allowlist/commit/license, copy referenced files to DIR
    python robot_tool.py frames --src VIDEO_OR_IMAGE --n 4 --out DIR --max 1024

Prints one `ROBOT_RESULT {json}` line.
"""
import argparse, json, shutil, sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "backend"))
import robots  # noqa: E402


def stage(a):
    out = Path(a.out)
    shutil.rmtree(out, ignore_errors=True)
    folder, facts = robots.verify_source(a.id)
    files, total = robots.referenced_files(folder)
    for rel in files:
        dst = out / rel
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(folder / rel, dst)
    import mujoco
    m = mujoco.MjModel.from_xml_path(str(out / "scene.xml"))   # the staged subset compiles on its own
    return dict(ok=True, facts=facts, files=[str(f) for f in files], bytes=total, nq=m.nq, nu=m.nu)


def frames(a):
    import cv2
    src, out = Path(a.src), Path(a.out)
    out.mkdir(parents=True, exist_ok=True)
    imgs = []
    if src.suffix.lower() in (".jpg", ".jpeg", ".png", ".webp"):
        im = cv2.imread(str(src))
        if im is not None:
            imgs.append(im)
    else:
        cap = cv2.VideoCapture(str(src))
        n = int(cap.get(cv2.CAP_PROP_FRAME_COUNT)) or 1
        for k in range(a.n):
            cap.set(cv2.CAP_PROP_POS_FRAMES, int((k + 0.5) * n / a.n))
            ok, im = cap.read()
            if ok and im is not None:
                imgs.append(im)
    if not imgs:
        return dict(ok=False, error="no frame could be decoded")
    paths = []
    for i, im in enumerate(imgs):
        s = min(1.0, a.max / max(im.shape[:2]))
        im = cv2.resize(im, None, fx=s, fy=s, interpolation=cv2.INTER_AREA) if s < 1 else im
        p = out / f"frame_{i}.jpg"
        cv2.imwrite(str(p), im, [cv2.IMWRITE_JPEG_QUALITY, 85])
        paths.append(str(p))
    return dict(ok=True, frames=paths)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("cmd", choices=["stage", "frames"])
    ap.add_argument("--id", default=""); ap.add_argument("--out", required=True)
    ap.add_argument("--src", default=""); ap.add_argument("--n", type=int, default=4); ap.add_argument("--max", type=int, default=1024)
    a = ap.parse_args()
    try:
        res = stage(a) if a.cmd == "stage" else frames(a)
    except Exception as e:
        res = dict(ok=False, error=str(e)[:600])
    print("ROBOT_RESULT " + json.dumps(res))
    sys.exit(0 if res.get("ok") else 2)


if __name__ == "__main__":
    main()
