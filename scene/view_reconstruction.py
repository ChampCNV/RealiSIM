"""Show a COLMAP scan in MuJoCo: the colored points and the camera path.

    python scene/view_reconstruction.py outputs/scene_dense            # interactive viewer
    python scene/view_reconstruction.py outputs/scene_dense --render   # save a turntable image

The scan is in COLMAP's own units. This is the raw reconstruction, before the table and the objects
have been measured out of it.
"""
import sys
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parent.parent
MAX_POINTS = 6000


def load(work):
    import pycolmap

    rec = pycolmap.Reconstruction(str(Path(work) / "sparse"))
    points = np.array([p.xyz for p in rec.points3D.values()])
    colors = np.array([p.color for p in rec.points3D.values()], np.float64) / 255
    keep = np.linalg.norm(points - np.median(points, 0), axis=1)
    keep = keep < np.percentile(keep, 95)
    points, colors = points[keep], colors[keep]
    if len(points) > MAX_POINTS:
        pick = np.linspace(0, len(points) - 1, MAX_POINTS).astype(int)
        points, colors = points[pick], colors[pick]
    cameras = []
    for im in sorted(rec.images.values(), key=lambda i: i.name):
        center = im.projection_center()
        forward = im.cam_from_world().rotation.matrix()[2]  # viewing direction, in world
        cameras.append((center, forward))
    return points, colors, cameras


def build(points, colors, cameras):
    centre = np.median(points, 0)
    span = np.ptp(points, 0).max()
    scale = 1.0 / span  # fit the cloud in a 1 m box so the default camera framing works
    points = (points - centre) * scale
    radius = 0.004
    import mujoco

    spec = mujoco.MjSpec()
    spec.visual.global_.offwidth = spec.visual.global_.offheight = 640
    spec.worldbody.add_light(pos=[1, -1, 1.5], dir=[-1, 1, -1.2], diffuse=[0.7, 0.7, 0.7],
                             type=mujoco.mjtLightType.mjLIGHT_DIRECTIONAL)
    spec.worldbody.add_geom(type=mujoco.mjtGeom.mjGEOM_PLANE, size=[2, 2, 0.01],
                            pos=[0, 0, float(points[:, 2].min()) - 0.02], rgba=[0.92, 0.92, 0.92, 1])
    cloud = spec.worldbody.add_body(name="cloud")
    for p, c in zip(points, colors):
        cloud.add_geom(type=mujoco.mjtGeom.mjGEOM_SPHERE, size=[radius, 0, 0], pos=p.tolist(),
                       rgba=[*c, 1], contype=0, conaffinity=0)
    step = max(1, len(cameras) // 40)
    for center, forward in cameras[::step]:
        a = (center - centre) * scale
        b = a + forward / np.linalg.norm(forward) * 0.06
        spec.worldbody.add_geom(type=mujoco.mjtGeom.mjGEOM_CAPSULE, size=[0.004, 0.03, 0],
                                fromto=[*a, *b], rgba=[1, 0.75, 0.1, 1], contype=0, conaffinity=0)
    return spec.compile(), points


def render(model, out_path):
    import mujoco

    data = mujoco.MjData(model)
    mujoco.mj_forward(model, data)
    centre = np.array([0, 0, 0.15])
    cam = mujoco.MjvCamera()
    cam.lookat[:] = centre
    cam.distance = 1.6
    cam.elevation = -25
    frames = []
    with mujoco.Renderer(model, 640, 640) as renderer:
        for azimuth in (40, 130, 220, 310):
            cam.azimuth = azimuth
            renderer.update_scene(data, cam)
            frames.append(renderer.render())
    image = np.concatenate(frames, axis=1)
    from PIL import Image
    Image.fromarray(image).save(out_path)
    return out_path


def main():
    work = Path(sys.argv[1])
    model, _ = build(*load(work))
    if "--render" in sys.argv:
        print(render(model, work / "reconstruction.png"))
    else:
        import mujoco.viewer

        mujoco.viewer.launch(model)


if __name__ == "__main__":
    main()
