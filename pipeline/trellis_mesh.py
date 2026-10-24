"""Step 1 (alternative): photo(s) -> 3D mesh with TRELLIS, run on the free Hugging Face Space.

Takes 1 or more photos of the same object from any angles. Needs internet.
Set the HF_TOKEN environment variable (free Hugging Face account) for more daily GPU time.
"""
import os
import shutil
from pathlib import Path

import numpy as np
import trimesh
from gradio_client import Client, handle_file

from cutout import cut_out

SPACE = "trellis-community/TRELLIS"


def trellis_mesh(image_paths, out_dir, seed=0, keep_boxes=None):
    """Returns the path of a Z-up mesh (.obj) built from one or more photos."""
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    # generous timeout: the 5 s default fails photo uploads on slow (e.g. event) networks
    client = Client(SPACE, hf_token=os.environ.get("HF_TOKEN"), verbose=False, httpx_kwargs={"timeout": 120})
    client.predict(api_name="/start_session")

    # Send our own cut-outs: the Space's background removal kept the table, which TRELLIS then modelled.
    # It keeps an existing transparent background and only crops/centers the object.
    images = []
    for i, p in enumerate(image_paths):
        cut_path = out_dir / f"cutout_{i}.png"
        cut_out(p, keep_boxes[i] if keep_boxes else None).save(cut_path)
        images.append(client.predict(handle_file(str(cut_path)), api_name="/preprocess_image"))
    _, glb_path, _ = client.predict(
        image=handle_file(images[0]),
        multiimages=[{"image": handle_file(p), "caption": None} for p in images] if len(images) > 1 else [],
        seed=seed,
        ss_guidance_strength=7.5,
        ss_sampling_steps=12,
        slat_guidance_strength=3.0,
        slat_sampling_steps=12,
        multiimage_algo="stochastic",
        mesh_simplify=0.95,
        texture_size=1024,
        api_name="/generate_and_extract_glb",
    )
    shutil.copy(glb_path, out_dir / "trellis.glb")

    # glTF files are Y-up; MuJoCo is Z-up.
    mesh = trimesh.load(out_dir / "trellis.glb", force="mesh")
    mesh.merge_vertices(merge_tex=True, merge_norm=True)  # the GLB is split along texture seams
    mesh.apply_transform(trimesh.transformations.rotation_matrix(np.pi / 2, [1, 0, 0]))
    out_path = out_dir / "raw_mesh.obj"
    mesh.export(out_path)
    return out_path
