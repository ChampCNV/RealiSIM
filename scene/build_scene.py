"""Scene step 4: object views -> meshes (TRELLIS) -> MuJoCo assets, plus the table, in one layout file.

    python scene/build_scene.py outputs/scene [--reuse]     (--reuse: keep meshes already generated)

Reads scene.json (from find_objects.py) and info.json (mass/material per object: the part a person or an
agent supplies). Object height comes from the scan, so no ruler is needed for objects.
"""
import json
import sys
from pathlib import Path

import numpy as np
import pycolmap

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "pipeline"))
from mesh_to_mjcf import mesh_to_mjcf  # noqa: E402
from trellis_mesh import trellis_mesh  # noqa: E402


def main(work, reuse=False):
    work = Path(work)
    scene = json.loads((work / "scene.json").read_text())
    info = json.loads((work / "info.json").read_text())
    rec = pycolmap.Reconstruction(str(work / "sparse"))
    plane = scene["plane"]
    n, d = np.array(plane["n"]), plane["d"]

    # table colour = median colour of the tabletop points
    P = np.array([p.xyz for p in rec.points3D.values()])
    C = np.array([p.color for p in rec.points3D.values()])
    top = np.abs(P @ n + d) * scene["scale_m_per_unit"] < 0.006
    table_rgb = (np.median(C[top], 0) / 255).round(3).tolist()

    layout = dict(table=dict(**scene["table"], rgba=table_rgb + [1.0]), objects=[])

    # Optional: a real table model from a photo of the same table (e.g. its product listing), made by the
    # image-to-3D model and sized to the measured table. Without it, the scene uses a plain box table.
    if "table" in info:
        tinfo = info["table"]
        name = tinfo["name"]
        mesh_dir = work / "table_mesh"
        if reuse and (mesh_dir / "trellis.glb").exists():
            raw = mesh_dir / "raw_mesh.obj"
        else:
            raw = trellis_mesh([work / tinfo["image"]], mesh_dir)
        t = scene["table"]
        props = dict(source=tinfo.get("source", ""), size_m=[t["length"], t["width"], t["height"]], static=True,
                     level_top=True, friction=tinfo.get("friction", [0.5, 0.005, 0.0001]))
        mesh_to_mjcf(raw, name, props, ROOT / "assets" / name, textured_mesh_path=mesh_dir / "trellis.glb")
        layout["table"]["asset"] = name
        print(f"table: {name} from {tinfo['image']} (TRELLIS, textured)")
    for obj in scene["objects"]:
        if obj["name"] not in info:
            print(f"{obj['name']}: no entry in info.json -> left out of the scene")
            continue
        meta = info[obj["name"]]
        name = meta["name"]
        views = sorted((work / "objects" / obj["name"]).glob("view_*.jpg"))
        mesh_dir = work / "objects" / obj["name"]
        if reuse and (mesh_dir / "raw_mesh.obj").exists():
            print(f"{name}: reusing existing mesh")
            raw = mesh_dir / "raw_mesh.obj"
        else:
            print(f"{name}: {len(views)} views -> TRELLIS")
            boxes = [json.loads(v.with_suffix(".json").read_text())["object_box"] for v in views]
            raw = trellis_mesh(views, mesh_dir, keep_boxes=boxes)
        props = {k: v for k, v in meta.items() if k != "name"}
        props["height_m"] = obj["height"]  # measured by the scan
        xml = mesh_to_mjcf(raw, name, props, ROOT / "assets" / name, textured_mesh_path=mesh_dir / "trellis.glb")
        size = json.loads((xml.parent / "properties.json").read_text())["size_m"]
        print(f"   -> {xml}  size {np.round(np.array(size) * 100, 1)} cm")
        layout["objects"].append(dict(name=name, x=obj["x"], y=obj["y"]))
    (work / "layout.json").write_text(json.dumps(layout, indent=2))
    print(f"layout written: {work / 'layout.json'}")


if __name__ == "__main__":
    main(sys.argv[1], reuse="--reuse" in sys.argv)
