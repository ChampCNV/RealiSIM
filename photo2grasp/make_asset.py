"""Videos or photos of an object -> MuJoCo-ready asset.

    python make_asset.py celsius_can inputs/can_1.mp4 inputs/can_2.mp4    # any number of videos (and/or photos)
    python make_asset.py printed_frame inputs/frame_1.jpg inputs/frame_2.jpg
    python make_asset.py celsius_can --skip-mesh                           # rebuild from the saved mesh

1. videos -> the best 4 frames, picked by fixed rules (pipeline/video_frames.py)
2. real size, mass, material, friction: from objects.json if known, else you are asked in the terminal
   (--claude: Claude identifies the object through the Anthropic API and looks the specs up online first)
3. frames -> 3D mesh (TRELLIS on Hugging Face) -> cleaned, real-size MuJoCo asset in assets/<name>/
4. AprilTags: the IDs seen in the video, or asked (which faces the robot / the RealSense); any IDs, one is enough
"""
import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT / "pipeline"))

from api.config import load_env  # noqa: E402

load_env()  # ANTHROPIC_API_KEY and HF_TOKEN from the repo .env, when the shell has not set them

from mesh_to_mjcf import mesh_to_mjcf  # noqa: E402
from object_info import object_properties  # noqa: E402
from preview import save_preview  # noqa: E402
from tag_ids import tag_setup  # noqa: E402
from video_frames import is_video, pick_frames  # noqa: E402


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("name", help="object name (key in objects.json)")
    parser.add_argument("inputs", nargs="*", help="videos and/or photos of the object")
    parser.add_argument("--views", type=int, default=4, help="photos sent to TRELLIS")
    parser.add_argument("--skip-mesh", action="store_true", help="reuse outputs/<name>/raw_mesh.obj")
    parser.add_argument("--claude", action="store_true", help="look the object up with Claude (Anthropic API key)")
    args = parser.parse_args()
    work_dir = ROOT / "outputs" / args.name

    videos = [p for p in args.inputs if is_video(p)]
    images = [Path(p) for p in args.inputs if not is_video(p)]
    boxes = [None] * len(images)
    if videos:
        print(f"[1/4] {len(videos)} video(s) -> best {args.views} frames")
        frames, frame_boxes = pick_frames(videos, work_dir, n=max(1, args.views - len(images)))
        images, boxes = images + frames, boxes + frame_boxes
    else:
        print(f"[1/4] {len(images)} photo(s) given")
    if not images and not args.skip_mesh:
        sys.exit("give at least one video or photo")

    print("[2/4] real size / mass / friction")
    props = object_properties(args.name, images or sorted((work_dir / "frames").glob("*.jpg")),
                              ROOT / "objects.json", use_claude=args.claude)

    if args.skip_mesh:
        raw_mesh = work_dir / "raw_mesh.obj"
    else:
        from trellis_mesh import trellis_mesh  # needs the Hugging Face client and rembg

        print(f"[3/4] {len(images)} photo(s) -> 3D mesh (TRELLIS on Hugging Face), texture from the photos")
        raw_mesh = trellis_mesh([str(p) for p in images], work_dir, keep_boxes=boxes)
        for cut in work_dir.glob("cutout_*.png"):  # projection already used these
            cut.unlink()

    print("[4/4] mesh -> MuJoCo asset")
    textured = work_dir / "trellis.glb"  # TRELLIS also gives colours; keep them when available
    xml_path = mesh_to_mjcf(raw_mesh, args.name, props, ROOT / "assets" / args.name,
                            textured_mesh_path=textured if textured.exists() else None)
    print("AprilTags on the object")
    if args.inputs or "tags" not in json.loads((ROOT / "objects.json").read_text()).get(args.name, {}):
        tag_setup(args.name, args.inputs, ROOT / "objects.json")
    print(f"Done: {xml_path}")
    print((xml_path.parent / "properties.json").read_text())

    print(f"Preview (visual | collision): {save_preview(xml_path, args.name)}")


if __name__ == "__main__":
    main()
