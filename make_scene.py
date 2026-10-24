"""Put a generated object in front of the SO-100 arm, check it behaves, and open the viewer.

    python make_scene.py tomato_soup_can            # drop test + viewer
    python make_scene.py tomato_soup_can --no-view  # drop test only
"""
import argparse
from pathlib import Path

import mujoco
import numpy as np

ROOT = Path(__file__).resolve().parent
ARM_SCENE = ROOT / "third_party" / "mujoco_menagerie" / "trs_so_arm100" / "scene.xml"
OBJECT_POS = [0.0, -0.25, 0.0]  # in front of the arm (it faces -y), within reach


def build_scene(name):
    scene = mujoco.MjSpec.from_file(str(ARM_SCENE))
    obj = mujoco.MjSpec.from_file(str(ROOT / "assets" / name / f"{name}.xml"))
    frame = scene.worldbody.add_frame(pos=OBJECT_POS)
    frame.attach_body(obj.body(name), "", "")
    # The arm's keyframes only cover its 6 joints; insert the object's free-joint pose
    # (position + quaternion) wherever MuJoCo placed that joint in qpos.
    arm = mujoco.MjModel.from_xml_path(str(ARM_SCENE))
    for key in list(scene.keys):
        scene.delete(key)
    adr = scene.compile().jnt_qposadr[-1]  # the object's free joint is the last joint added
    for i in range(arm.nkey):
        qpos = list(arm.key_qpos[i])
        qpos[adr:adr] = OBJECT_POS + [1, 0, 0, 0]
        scene.add_key(name=arm.key(i).name, qpos=qpos, ctrl=list(arm.key_ctrl[i]))
    return scene.compile()


def drop_test(model, name, seconds=2.0):
    """Lift the object 5 cm, let it fall, and check it lands upright and stays put."""
    data = mujoco.MjData(model)
    mujoco.mj_resetDataKeyframe(model, data, model.key("rest").id)  # arm folded out of the way
    body = model.body(name)
    qadr = model.jnt_qposadr[body.jntadr[0]]
    data.qpos[qadr + 2] += 0.05
    mujoco.mj_forward(model, data)
    while data.time < seconds:
        mujoco.mj_step(model, data)
    pos = data.xpos[body.id]
    up = data.xmat[body.id].reshape(3, 3)[:, 2]
    tilt = np.degrees(np.arccos(np.clip(up[2], -1, 1)))
    print(f"mass {model.body_mass[body.id]:.3f} kg | rest pos {np.round(pos, 3)} m | tilt {tilt:.1f} deg | "
          f"speed {np.linalg.norm(data.qvel[qadr:qadr + 3]):.4f} m/s")
    return data


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("name")
    parser.add_argument("--no-view", action="store_true")
    args = parser.parse_args()

    model = build_scene(args.name)
    drop_test(model, args.name)
    if not args.no_view:
        import mujoco.viewer
        mujoco.viewer.launch(model)


if __name__ == "__main__":
    main()
