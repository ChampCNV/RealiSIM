"""Scene step 5: layout.json -> MuJoCo scene (floor, table, scanned objects, SO-100 on the table edge).

    python scene/view_scene.py outputs/scene            # open the interactive viewer
    python scene/view_scene.py outputs/scene --render   # save screenshots instead
"""
import json
import sys
from pathlib import Path

import mujoco
import mujoco.viewer
import numpy as np

ROOT = Path(__file__).resolve().parent.parent
ARM_SCENE = ROOT / "third_party" / "mujoco_menagerie" / "trs_so_arm100" / "scene.xml"
TOP_THICKNESS = 0.02
LEG_RADIUS = 0.012


REACH = 0.30  # SO-100 comfortable reach from its base (m)


def mount_candidates(L, W):
    """Robot mounts along the table edges: (x, y, yaw) with the arm facing into the table."""
    m = 0.04
    out = []
    for x in (0, -L / 4, L / 4):
        out += [(x, -W / 2 + m, np.pi), (x, W / 2 - m, 0.0)]
    for y in (0, -W / 4, W / 4):
        out += [(-L / 2 + m, y, -np.pi / 2), (L / 2 - m, y, np.pi / 2)]
    return out


def settle(model, steps=500):
    data = mujoco.MjData(model)
    mujoco.mj_resetData(model, data)
    arm = mujoco.MjModel.from_xml_path(str(ARM_SCENE))
    data.qpos[:6] = arm.key_qpos[arm.key("rest").id]  # arm folded at rest
    data.ctrl[:] = data.qpos[:6]
    mujoco.mj_forward(model, data)
    free = [b for b in range(model.nbody) if model.body_jntnum[b]
            and model.jnt_type[model.body_jntadr[b]] == mujoco.mjtJoint.mjJNT_FREE]
    start = data.xpos[free].copy()
    for _ in range(steps):
        mujoco.mj_step(model, data)
    moved = np.linalg.norm(data.xpos[free] - start, axis=1).max() if free else 0.0
    return data, free, moved


def build(work, mount=None):
    """Builds the scene. Without `mount`, picks the first table-edge mount where every object is within
    reach and nothing gets disturbed when the arm starts in its rest pose."""
    layout = json.loads((Path(work) / "layout.json").read_text())
    if mount is None:
        t = layout["table"]
        objs = np.array([[o["x"], o["y"]] for o in layout["objects"]])
        for cand in sorted(mount_candidates(t["length"], t["width"]),
                           key=lambda c: np.linalg.norm(objs - c[:2], axis=1).max()):
            if np.linalg.norm(objs - cand[:2], axis=1).max() > REACH:
                continue
            model = build(work, cand)
            _, _, moved = settle(model)
            if moved < 0.005:
                print(f"robot mount: ({cand[0]*100:.0f}, {cand[1]*100:.0f}) cm from table centre, "
                      f"facing {np.degrees(cand[2]):.0f} deg")
                return model
        raise RuntimeError("no table-edge mount with all objects in reach and a clear start")
    t = layout["table"]
    L, W, H = t["length"], t["width"], t["height"]
    spec = mujoco.MjSpec.from_file(str(ARM_SCENE))
    for key in list(spec.keys):
        spec.delete(key)

    if "asset" in t:  # table model generated from a photo of the real table, fixed in place
        tab = mujoco.MjSpec.from_file(str(ROOT / "assets" / t["asset"] / f"{t['asset']}.xml"))
        spec.worldbody.add_frame(pos=[0, 0, 0]).attach_body(tab.body(t["asset"]), "", "")
    else:  # fallback: plain box top + 4 legs, coloured like the scanned tabletop
        table = spec.worldbody.add_body(name="table")
        table.add_geom(type=mujoco.mjtGeom.mjGEOM_BOX, size=[L / 2, W / 2, TOP_THICKNESS / 2],
                       pos=[0, 0, H - TOP_THICKNESS / 2], rgba=t["rgba"], friction=[0.5, 0.005, 0.0001])
        for sx in (-1, 1):
            for sy in (-1, 1):
                table.add_geom(type=mujoco.mjtGeom.mjGEOM_CYLINDER, size=[LEG_RADIUS, (H - TOP_THICKNESS) / 2, 0],
                               pos=[sx * (L / 2 - 0.03), sy * (W / 2 - 0.03), (H - TOP_THICKNESS) / 2],
                               rgba=t["rgba"])

    # SO-100 bolted to the table edge; its arm points along its own -y, so rotate it to face the table
    x, y, yaw = mount
    base = spec.body("Base")
    base.pos = [x, y, H]
    base.quat = [np.cos(yaw / 2), 0, 0, np.sin(yaw / 2)]

    for obj in layout["objects"]:
        o = mujoco.MjSpec.from_file(str(ROOT / "assets" / obj["name"] / f"{obj['name']}.xml"))
        spec.worldbody.add_frame(pos=[obj["x"], obj["y"], H + 0.001]).attach_body(o.body(obj["name"]), "", "")
    return spec.compile()


def main():
    work = sys.argv[1]
    model = build(work)
    data, free, _ = settle(model)

    if "--render" in sys.argv:
        import cv2
        model.vis.global_.offwidth, model.vis.global_.offheight = 1280, 720
        r = mujoco.Renderer(model, 720, 1280)
        out = Path(work)
        for i, (az, el, dist) in enumerate([(135, -25, 1.0), (90, -40, 0.75), (180, -15, 1.4)]):
            cam = mujoco.MjvCamera()
            cam.lookat[:] = [0, 0, 0.7]
            cam.distance, cam.azimuth, cam.elevation = dist, az, el
            r.update_scene(data, cam)
            cv2.imwrite(str(out / f"scene_view_{i + 1}.png"), cv2.cvtColor(r.render(), cv2.COLOR_RGB2BGR))
        for b in free:
            print(f"{model.body(b).name}: resting at {np.round(data.xpos[b] * 100, 1)} cm")
    else:
        mujoco.viewer.launch(model, data)


if __name__ == "__main__":
    main()
