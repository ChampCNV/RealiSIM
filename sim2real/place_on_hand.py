"""Put the held object on a person's open palm: scripted, no learning (inverse kinematics in the SO-100 model).

After a successful pick-up the gripper holds the object. plan() makes the joint targets for:
  lift   straight up until the object's bottom is ABOVE m higher than the palm (no sweeping into the hand)
  above  the object's bottom ABOVE m over the palm centre, the object turned upright
  down   the bottom LOWER m over the palm
  open   same pose, gripper open: the object stands on the palm
  up     the open gripper RETREAT m straight up, clear of the object
Each waypoint is checked in the model: inverse-kinematics error, wrist camera clearance, tip above the table.
The simulator (run_sim) and the real robot (robot/run_tag_policy.py --to-hand) use the same plan.
"""
import mujoco
import numpy as np

from lift_env import JAW_OPEN
from tag_env import MIN_MOVE_CLEARANCE, pose_clearance, ruler

ABOVE, LOWER, RETREAT = 0.04, 0.012, 0.06  # LOWER: let go 1.2 cm up (the bottom is only roughly known)
MAX_IK_ERROR, MAX_TILT = 0.012, 20.0  # m (the palm is ~8 cm wide), deg (the held object's lean)


def _pinv(J, damping=1e-4):
    return J.T @ np.linalg.inv(J @ J.T + damping * np.eye(len(J)))


def ik(env, q0, tip_goal, up=None, iters=1000):
    """Arm joints (jaw unchanged) that put the gripper tip at tip_goal and, as far as the remaining joints allow,
    keep the gripper-frame direction `up` pointing straight up (the held object stays upright).
    Returns (q, position error m, tilt deg). Damped least squares in the policy's model, position first."""
    m, d = env.model, env.check_data
    dofs = m.jnt_dofadr[[m.joint(j).id for j in ("Rotation", "Pitch", "Elbow", "Wrist_Pitch", "Wrist_Roll")]]
    lo, hi = env.ctrl_low[:5], env.ctrl_high[:5]
    q = np.array(q0, float)
    jacp, jacr = np.zeros((3, m.nv)), np.zeros((3, m.nv))
    err = tilt = 0.0
    for _ in range(iters):
        d.qpos[env.qadr] = q
        mujoco.mj_kinematics(m, d)
        mujoco.mj_comPos(m, d)
        tip = d.geom_xpos[env.tip_geoms].mean(0)
        e = np.asarray(tip_goal) - tip
        err = np.linalg.norm(e)
        mujoco.mj_jac(m, d, jacp, jacr, tip, env.fixed_jaw)
        Jp, Jr = jacp[:, dofs], jacr[:, dofs]
        Jp_pinv = _pinv(Jp)
        dq = Jp_pinv @ e
        if up is not None:  # in the position task's null space: turn the object's axis toward vertical
            axis = d.xmat[env.fixed_jaw].reshape(3, 3) @ up
            tilt = np.degrees(np.arccos(np.clip(axis[2], -1, 1)))
            N = np.eye(5) - Jp_pinv @ Jp
            dq += 0.5 * N @ (Jr.T @ np.cross(axis, [0, 0, 1.0]))  # small steps: never fights the position
        if err < 5e-4 and tilt < 1.0:
            break
        q[:5] = np.clip(q[:5] + np.clip(dq, -0.08, 0.08), lo, hi)
    return q, err, tilt


def state_of(env, q):
    """(tip position, gripper rotation) for joint angles q, in the model."""
    m, d = env.model, env.check_data
    d.qpos[env.qadr] = q
    mujoco.mj_kinematics(m, d)
    return d.geom_xpos[env.tip_geoms].mean(0), d.xmat[env.fixed_jaw].reshape(3, 3).copy()


def plan(env, q_hold, palm_top, bottom_offset, object_up=(0.0, 0.0, 1.0)):
    """Waypoints [(name, joint targets)] from the holding pose q_hold to the object standing on the palm.
    bottom_offset: the held object's bottom minus the gripper tip (world, m); object_up: the held object's axis
    (world; in the gripper it may lean). Raises ValueError if the palm is out of reach or a pose is unsafe."""
    tip0, R0 = state_of(env, q_hold)
    up = R0.T @ np.asarray(object_up, float)  # the object's axis in the gripper's frame: turned to vertical
    palm_top = np.asarray(palm_top, float)
    out, q = [], np.array(q_hold, float)
    bottom = tip0 + bottom_offset
    if bottom[2] < palm_top[2] + ABOVE:
        q, err, _ = ik(env, q, tip0 + [0, 0, palm_top[2] + ABOVE - bottom[2]])
        if err > MAX_IK_ERROR:
            raise ValueError("cannot lift the object above the palm's height (palm too high)")
        out.append(("lift", q.copy()))
    for name, rise in (("above", ABOVE), ("down", LOWER)):
        q, err, tilt = ik(env, q, palm_top + [0, 0, rise] - bottom_offset, up)
        if err > MAX_IK_ERROR or tilt > MAX_TILT:
            f, s, h = ruler(palm_top)
            raise ValueError(f"palm out of reach (front {f * 100:.0f} cm, side {s * 100:+.0f} cm, height "
                             f"{h * 100:.0f} cm): {err * 100:.1f} cm off, object leaning {tilt:.0f} deg")
        out.append((name, q.copy()))
    opened = q.copy()
    opened[5] = JAW_OPEN
    out.append(("open", opened))
    tip, _ = state_of(env, opened)
    retreat = ik(env, opened, tip + [0, 0, RETREAT], up)[0]
    out.append(("up", retreat))
    for name, target in out:
        if pose_clearance(env.model, env.check_data, target) < MIN_MOVE_CLEARANCE:
            raise ValueError(f"'{name}' would bring the wrist camera too close to the arm")
        if state_of(env, target)[0][2] < 0.005:
            raise ValueError(f"'{name}' would put the gripper into the table")
    return out


def run_sim(env, waypoints, on_step=None, speed=0.06):
    """Drive the simulated arm through the waypoints (smooth, at most `speed` rad per 0.1 s per joint)."""
    m, d = env.model, env.data
    for _, goal in waypoints:
        start = env.target.copy()
        n = max(5, int(np.abs(goal - start).max() / speed) + 1)
        for s in np.linspace(0, 1, n + 1)[1:]:
            env.target = start + (goal - start) * (0.5 - 0.5 * np.cos(np.pi * s))
            d.ctrl[:] = env.target
            for _ in range(env.n_substeps):
                mujoco.mj_step(m, d)
            if on_step:
                on_step()
        for _ in range(3):  # let it settle
            for _ in range(env.n_substeps):
                mujoco.mj_step(m, d)
            if on_step:
                on_step()


def on_palm(env):
    """Is the object standing upright on the hand, let go? (dict with the details)"""
    d = env.data
    top, normal = env.hand.palm_top()
    pos = d.xpos[env.obj_body]
    tilt = np.degrees(np.arccos(np.clip(d.xmat[env.obj_body][8], -1, 1)))
    grasped, _ = env._contacts()
    off = np.linalg.norm((pos - top)[:2])
    on_hand = any((c.geom1 in env.obj_geoms and c.geom2 in env.hand.geoms) or
                  (c.geom2 in env.obj_geoms and c.geom1 in env.hand.geoms) for c in d.contact[:d.ncon])
    ok = bool(on_hand and tilt < 20 and not grasped)
    return dict(ok=ok, off_centre_cm=round(off * 100, 1), tilt_deg=round(float(tilt), 1), held=bool(grasped))
