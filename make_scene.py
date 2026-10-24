"""Put generated objects in front of the SO-100 arm, check they behave, and open the viewer.

    python make_scene.py tomato_soup_can                  # one object: drop test + viewer
    python make_scene.py celsius_can lego_tree            # several objects side by side
    python make_scene.py --all                            # every object in assets/
    python make_scene.py celsius_can --no-view            # drop test only
    python make_scene.py celsius_can --hand my_hand       # the object on a person's open palm (hands/<name>)

The objects stand in a row in front of the arm (spaced by their widths); the arm is at its zero pose (middle of
every joint's range, where the real robot starts). Each object is lifted 5 cm and dropped: it should land
upright and come to rest. With --hand the person's hand (palm up, in front of the arm) is added and the object
is dropped 1 cm onto the palm: it should stand there.
"""
import argparse
import sys
from pathlib import Path

import mujoco
import numpy as np

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT / "sim2real"))
ARM_SCENE = ROOT / "third_party" / "mujoco_menagerie" / "trs_so_arm100" / "scene.xml"
ROW_Y = -0.25  # the row of objects, in front of the arm (it faces -y), within reach
GAP = 0.04  # m between neighbouring objects
PALM = (0.0, -0.28, 0.08)  # where the open hand is held: palm surface centre (m, robot frame)


def all_objects():
    return sorted(p.parent.name for p in (ROOT / "assets").glob("*/*.xml") if p.stem == p.parent.name)


def build_scene(names, hand=None):
    """SO-100 + the objects in a row [+ a person's hand]; returns (model, {name: (x, y)})."""
    scene = mujoco.MjSpec.from_file(str(ARM_SCENE))
    if hand:
        from hand_sim import add_hand
        add_hand(scene, hand)
    for key in list(scene.keys):  # keyframes would need every object's pose: poses are set in reset() instead
        scene.delete(key)
    specs = {n: mujoco.MjSpec.from_file(str(ROOT / "assets" / n / f"{n}.xml")) for n in names}
    widths = []
    for n, spec in specs.items():
        m = spec.compile()
        lo, hi = m.geom_aabb[:, :3] - m.geom_aabb[:, 3:], m.geom_aabb[:, :3] + m.geom_aabb[:, 3:]
        widths.append(float(np.max(hi[:, 0]) - np.min(lo[:, 0])) if m.ngeom else 0.05)
    xs = np.cumsum([0.0] + [(a + b) / 2 + GAP for a, b in zip(widths[:-1], widths[1:])])
    xs -= xs.mean()
    places = {}
    for (n, spec), x in zip(specs.items(), xs):
        scene.worldbody.add_frame(pos=[x, ROW_Y, 0]).attach_body(spec.body(n), "", "")
        places[n] = (float(x), ROW_Y)
    return scene.compile(), places


def reset(model, places, lift=0.0, hand=None):
    """Arm at its zero pose, every object at its spot (lifted by `lift`) [on the open palm, with a hand]."""
    data = mujoco.MjData(model)
    data.qpos[:6] = (model.jnt_range[:6, 0] + model.jnt_range[:6, 1]) / 2
    data.ctrl[:] = data.qpos[:6]
    z = 0.0
    if hand:
        from hand_sim import SimHand
        sim = SimHand(model, hand)
        x, y, h = PALM
        sim.set(data, sim.palm_up([x, y, h - sim.box_half[2]], np.arctan2(-y, -x)))
        z = sim.palm_top()[0][2]
        places = {n: PALM[:2] for n in places}
    for n, (x, y) in places.items():
        a = model.jnt_qposadr[model.body(n).jntadr[0]]
        data.qpos[a:a + 7] = [x, y, z + lift, 1, 0, 0, 0]
    mujoco.mj_forward(model, data)
    return data


def drop_test(model, places, seconds=2.0, hand=None):
    """Lift every object 5 cm (1 cm above the palm with a hand), let it fall, and check it lands upright and
    stays put."""
    data = reset(model, places, lift=0.01 if hand else 0.05, hand=hand)
    while data.time < seconds:
        mujoco.mj_step(model, data)
    for n in places:
        body = model.body(n)
        a = model.jnt_qposadr[body.jntadr[0]]
        up = data.xmat[body.id].reshape(3, 3)[:, 2]
        tilt = np.degrees(np.arccos(np.clip(up[2], -1, 1)))
        print(f"{n}: mass {model.body_mass[body.id]:.3f} kg | rest pos {np.round(data.xpos[body.id], 3)} m | "
              f"tilt {tilt:.1f} deg | speed {np.linalg.norm(data.qvel[a:a + 3]):.4f} m/s"
              + ("   <- fell over?" if tilt > 30 else "")
              + (("   <- fell off the hand" if data.xpos[body.id][2] < PALM[2] - 0.02 else "   (standing on the palm)")
                 if hand else ""))


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("names", nargs="*", help="object names (assets/<name>)")
    parser.add_argument("--all", action="store_true", help="every object in assets/")
    parser.add_argument("--no-view", action="store_true")
    parser.add_argument("--hand", help="a person's hand (hands/<name>), palm up; the object stands on it")
    args = parser.parse_args()
    names = all_objects() if args.all else args.names
    hand = None
    if args.hand:
        from hand_sim import load_hand
        hand = load_hand(args.hand)
        if len(names) > 1:
            raise SystemExit("with --hand: one object (it is put on the palm), or none")
    elif not names:
        raise SystemExit("give object names or --all")

    model, places = build_scene(names, hand)
    if names:
        drop_test(model, places, hand=hand)
    if not args.no_view:
        import mujoco.viewer
        mujoco.viewer.launch(model, reset(model, places, hand=hand))


if __name__ == "__main__":
    main()
