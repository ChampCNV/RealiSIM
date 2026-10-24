"""Find the arm pose from which the webcam sees the tag anywhere in the placement area (run once, then paste
the result into tag_env.LOOK_POSE).

    python sim2real/find_look_pose.py

Random search over arm poses; score = share of placement-grid positions where the tag is detected
(nominal camera, no noise), the gripper must stay well above the table, ties go to poses closer to zero.
"""
import mujoco
import numpy as np

from lift_env import start_pose
from tag_env import TagLiftEnv


def main(samples=12000, seed=0):
    rng = np.random.default_rng(seed)
    zero = start_pose(TagLiftEnv("lego_tree", domain_randomization=False, start=[0] * 6).model, "zero")
    env = TagLiftEnv("lego_tree", domain_randomization=False, start=zero)
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
        seen = 0
        for gx, gy in grid:
            xy = env.spawn_center + np.array([gx, gy]) * env.spawn_half
            yaw = np.arctan2(xy[0], -xy[1])
            d.qpos[a:a + 7] = [xy[0], xy[1], 0.002, np.cos(yaw / 2), 0, 0, np.sin(yaw / 2)]
            mujoco.mj_forward(m, d)
            if env.tcp()[2] < 0.12 or d.xpos[env.fixed_jaw][1] < -0.30:
                seen = -1  # too low / too far out
                break
            seen += env.detect() is not None
        score = seen / len(grid)
        dist = np.abs(q[:5] - zero[:5]).sum()
        if score > best[0] or (score == best[0] and dist < best[1]):
            best = (score, dist, q)
            print(f"sample {i}: tag seen from {100 * score:.0f}% of placements, distance from zero {dist:.2f} rad")
    print("LOOK_POSE =", [round(float(v), 3) for v in best[2]])


if __name__ == "__main__":
    main()
