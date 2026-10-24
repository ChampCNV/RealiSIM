"""Pack what Colab needs to train the AprilTag policy into colab/so100_tag_train.zip.

    python colab/build_zip.py

Then follow the steps at the top of colab/colab_cell.py (no Google Drive needed).
"""
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def main():
    files = [ROOT / "sim2real" / n for n in ("lift_env.py", "tag_env.py", "train_tag.py", "find_look_pose.py")]
    files += [p for p in (ROOT / "assets" / "lego_tree").rglob("*") if p.is_file()]
    files += [p for p in (ROOT / "third_party" / "mujoco_menagerie" / "trs_so_arm100").rglob("*")
              if p.is_file() and p.suffix != ".png"]
    out = ROOT / "colab" / "so100_tag_train.zip"
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as z:
        for f in files:
            z.write(f, f.relative_to(ROOT).as_posix())
    print(f"{out}: {len(files)} files, {out.stat().st_size / 1e6:.1f} MB")


if __name__ == "__main__":
    main()
