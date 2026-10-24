"""Pack what Colab needs to train the AprilTag policy for one object into colab/so100_tag_train.zip.

    python colab/build_zip.py                                                   # Lego tree, default area
    python colab/build_zip.py --object celsius_can --area wide --init runs/tag_envcam_2250k

The training options are stored in the zip (train_args.txt), so the Colab cell needs no editing; with --init
the starting model's weights are packed too. Then follow the steps at the top of colab/colab_cell.py.
"""
import argparse
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--object", default="lego_tree")
    ap.add_argument("--area", choices=["default", "wide"], default="default")
    ap.add_argument("--init", help="run dir with train_state.pt to start from")
    ap.add_argument("--steps", type=int, help="total training steps (default: the trainer's)")
    args = ap.parse_args()

    files = [ROOT / "sim2real" / n for n in ("lift_env.py", "tag_env.py", "train_tag.py", "find_look_pose.py")]
    files += [ROOT / "objects.json"]
    files += [p for p in (ROOT / "assets" / args.object).rglob("*") if p.is_file()]
    files += [p for p in (ROOT / "third_party" / "mujoco_menagerie" / "trs_so_arm100").rglob("*")
              if p.is_file() and p.suffix != ".png"]
    train_args = f"--object {args.object} --area {args.area}"
    if args.steps:
        train_args += f" --steps {args.steps}"
    out = ROOT / "colab" / "so100_tag_train.zip"
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as z:
        for f in files:
            z.write(f, f.relative_to(ROOT).as_posix())
        if args.init:
            z.write(Path(args.init) / "train_state.pt", "init/train_state.pt")
            train_args += " --init ../init"
        z.writestr("train_args.txt", train_args)
    print(f"{out}: {len(files)} files, {out.stat().st_size / 1e6:.1f} MB | train_tag.py {train_args}")


if __name__ == "__main__":
    main()
