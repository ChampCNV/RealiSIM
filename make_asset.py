"""Photo(s) of an object -> MuJoCo-ready asset.

    python make_asset.py printed_frame inputs/frame_1.jpg inputs/frame_2.jpg     # TRELLIS (Hugging Face)

The object's real size, mass and friction come from objects.json for now
(later an agent can fill these in).
"""
import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT / "pipeline"))

from mesh_to_mjcf import mesh_to_mjcf  # noqa: E402
from preview import save_preview  # noqa: E402


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("name", help="key in objects.json")
    parser.add_argument("images", nargs="*", help="one or more photos of the object")
    parser.add_argument("--skip-mesh", action="store_true", help="reuse outputs/<name>/raw_mesh.obj")
    args = parser.parse_args()

    objects = json.loads((ROOT / "objects.json").read_text())
    if args.name not in objects:
        sys.exit(f"'{args.name}' is not in objects.json. Add its height_m, mass_kg, friction and rgba first.")
    props = objects[args.name]

    work_dir = ROOT / "outputs" / args.name
    if args.skip_mesh:
        raw_mesh = work_dir / "raw_mesh.obj"
    else:
        from trellis_mesh import trellis_mesh  # needs the Hugging Face client and rembg

        print(f"[1/3] {len(args.images)} image(s) -> mesh (TRELLIS on Hugging Face), texture from the photos")
        raw_mesh = trellis_mesh(args.images, work_dir)

    print("[2/3] mesh -> MuJoCo asset")
    textured = work_dir / "trellis.glb"  # TRELLIS also gives colours; keep them when available
    xml_path = mesh_to_mjcf(raw_mesh, args.name, props, ROOT / "assets" / args.name,
                            textured_mesh_path=textured if textured.exists() else None)
    print(f"Done: {xml_path}")
    print((xml_path.parent / "properties.json").read_text())

    print("[3/3] preview")
    print(f"Preview (visual | collision): {save_preview(xml_path, args.name)}")


if __name__ == "__main__":
    main()
