"""Find the arm pose from which the wrist webcam sees the tag anywhere in the placement area.

    python sim2real/find_look_pose.py [object] [--wide]

Random search over arm poses; score = share of placement-grid positions where the tag is detected (nominal
camera, no noise); the gripper must stay well above the table and the wrist camera >= 6 cm from the arm (it got
crushed against the lower arm before); ties go to poses closer to zero.
look_pose(object, area) caches the result in assets/<object>/look_pose.json (per area), so training and the
robot use the same pose.
"""
import argparse
import json

import mujoco
import numpy as np

from lift_env import ROOT, start_pose
from tag_env import DEFAULT_AREA, LOOK_POSE, MIN_CAMERA_CLEARANCE, WIDE_AREA, TagLiftEnv, camera_clearance


def search(object_name, area, samples=12000, seed=0, verbose=True):
    rng = np.random.default_rng(seed)
    zero = start_pose(TagLiftEnv(object_name, domain_randomization=False, start=[0] * 6).model, "zero")
    env = TagLiftEnv(object_name, domain_randomization=False, start=zero, area=area)
    env.reset()
    m, d = env.model, env.data
    a = env.obj_qadr
    grid = [(x, y) for x in np.linspace(-1, 1, 5) for y in np.linspace(-1, 1, 4)]
    lo, hi = m.jnt_range[:6, 0], m.jnt_range[:6, 1]
    best = (-1, 0, None)
    for i in range(samples):
        q = zero.copy()
        q[1:4] = rng.uniform(lo[1:4], hi[1:4])
        q[4] = rng.uniform(lo[4], hi[4])
        q[5] = 1.0  # gripper open
        d.qpos[env.qadr] = q
        mujoco.mj_forward(m, d)
        if camera_clearance(m, d) < MIN_CAMERA_CLEARANCE:  # would press the wrist camera against the arm
            continue
        seen = 0
        for gx, gy in grid:
            xy = env.spawn_center + np.array([gx, gy]) * env.spawn_half
            yaw = np.arctan2(xy[0], -xy[1])
            d.qpos[a:a + 7] = [xy[0], xy[1], 0.002, np.cos(yaw / 2), 0, 0, np.sin(yaw / 2)]
            mujoco.mj_forward(m, d)
            if env.tcp()[2] < 0.12 or d.xpos[env.fixed_jaw][1] < -0.30:
                seen = -1  # too low / too far out
                break
            seen += env.detect_wrist() is not None
        score = seen / len(grid)
        dist = np.abs(q[:5] - zero[:5]).sum()
        if score > best[0] or (score == best[0] and dist < best[1]):
            best = (score, dist, q)
            if verbose:
                print(f"   sample {i}: tag seen from {100 * score:.0f}% of placements")
    return [round(float(v), 3) for v in best[2]], best[0]


def look_pose(object_name="lego_tree", area=None, samples=12000):
    """The look pose for this object and placement area (searched once, then read from the cache)."""
    area = dict(area or DEFAULT_AREA)
    if object_name == "lego_tree" and area == DEFAULT_AREA:
        return list(LOOK_POSE)  # the pose the first models were trained with
    cache = ROOT / "assets" / object_name / "look_pose.json"
    data = json.loads(cache.read_text()) if cache.exists() else {}
    key = json.dumps(area, sort_keys=True)
    if key not in data:
        print(f"finding a look pose for {object_name} (wrist camera must see the whole area)...")
        pose, score = search(object_name, area, samples)
        data[key] = dict(pose=pose, seen=score)
        cache.write_text(json.dumps(data, indent=2))
        print(f"   look pose {pose}: tag seen from {100 * score:.0f}% of the area")
    return data[key]["pose"]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("object", nargs="?", default="lego_tree")
    ap.add_argument("--wide", action="store_true", help="the wide placement area")
    args = ap.parse_args()
    print("LOOK_POSE =", look_pose(args.object, WIDE_AREA if args.wide else None))


if __name__ == "__main__":
    main()
