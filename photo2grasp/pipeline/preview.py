"""Render a finished asset in MuJoCo: what the camera sees (top row) and what the gripper touches (bottom row)."""
from pathlib import Path

import mujoco
import numpy as np
from PIL import Image

VISUAL_GROUP, COLLISION_GROUP = 2, 3  # as set in mesh_to_mjcf
AZIMUTHS = (0, 90, 180, 270)  # deg: all the way round
SIZE = 400  # px per view


def save_preview(xml_path, name, out_path=None):
    xml_path = Path(xml_path)
    spec = mujoco.MjSpec.from_file(str(xml_path))
    spec.visual.global_.offwidth = spec.visual.global_.offheight = SIZE
    spec.visual.quality.shadowsize = 4096
    spec.worldbody.add_light(pos=[0.3, -0.5, 0.8], dir=[-0.3, 0.5, -0.8], diffuse=[0.6] * 3,
                             type=mujoco.mjtLightType.mjLIGHT_DIRECTIONAL)
    spec.worldbody.add_geom(type=mujoco.mjtGeom.mjGEOM_PLANE, size=[50, 50, 0.01], rgba=[0.93, 0.93, 0.93, 1])
    palette = np.array([[0.90, 0.33, 0.25], [0.25, 0.55, 0.90], [0.30, 0.75, 0.40], [0.95, 0.70, 0.20],
                        [0.60, 0.40, 0.85], [0.20, 0.75, 0.75]])
    pieces = [g for g in spec.body(name).geoms if g.group == COLLISION_GROUP]
    for i, geom in enumerate(pieces):
        geom.rgba = list(palette[i % len(palette)]) + [1.0]
    model = spec.compile()
    data = mujoco.MjData(model)
    mujoco.mj_forward(model, data)

    body = model.body(name).id
    lo = np.full(3, np.inf)
    hi = -lo
    for g in range(model.ngeom):
        if model.geom_bodyid[g] == body:
            corners = model.geom_aabb[g, :3] + np.array(np.meshgrid([-1, 1], [-1, 1], [-1, 1])).reshape(3, -1).T \
                * model.geom_aabb[g, 3:]
            world = data.geom_xpos[g] + corners @ data.geom_xmat[g].reshape(3, 3).T
            lo, hi = np.minimum(lo, world.min(0)), np.maximum(hi, world.max(0))
    cam = mujoco.MjvCamera()
    cam.lookat[:] = (lo + hi) / 2
    cam.distance = 1.5 * np.linalg.norm(hi - lo) / (2 * np.tan(np.radians(model.vis.global_.fovy / 2)))
    cam.elevation = -28

    rows = []
    with mujoco.Renderer(model, SIZE, SIZE) as renderer:
        for group in (VISUAL_GROUP, COLLISION_GROUP):
            option = mujoco.MjvOption()
            option.geomgroup[:] = 0
            option.geomgroup[0] = option.geomgroup[group] = 1
            row = []
            for azimuth in AZIMUTHS:
                cam.azimuth = azimuth
                renderer.update_scene(data, cam, option)
                row.append(renderer.render())
            rows.append(np.concatenate(row, axis=1))
    out_path = Path(out_path or xml_path.parent / "preview.png")
    Image.fromarray(np.concatenate(rows, axis=0)).save(out_path)
    return out_path
