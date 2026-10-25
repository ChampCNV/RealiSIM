"""Robotics workspace workcell composer (runs in the repo's Python environment).

Any robot model (a MuJoCo Menagerie folder or another MJCF folder with scene.xml) + a measured table + the
reconstructed objects -> one MuJoCo scene. The robot stands at the world origin on the table top (z = 0), the floor is
moved down to the table's real height, objects rest on the table at their placements.

    python workcell.py robot    --robot-dir DIR --out OUT [--front fx,fy]
    python workcell.py bundle   --robot-dir DIR --assets A --names a,b --out OUT [--table L,W,H] [--places JSON] [--front fx,fy]
                                [--table-pose '{"x_m":..,"y_m":..,"yaw_deg":..}']   (table centre + length axis, robot-base frame)
    python workcell.py simulate (same as bundle) [--seed N] [--lift 0.05] [--seconds 3] [--x X --y Y --yaw DEG]

Multi-arm (any number of arms): replace --robot-dir with
    --robots '[{"name":"robot_1","robot_dir":DIR,"library_id":..,"label":..,"pose":null},
               {"name":"robot_2","robot_dir":DIR2,"pose":{"x_m":..,"y_m":..,"yaw_deg":..,"z_m":0},"pose_source":"measured"}]'
    (or --robots @file.json). The FIRST entry is the primary arm; its base is the world origin (robot-base frame used by
    --places and --table-pose). Other poses are in that frame; "pose": null means a provisional default slot beside the
    primary (reported as pose_source "default"). Every arm is attached with its own namespace "<name>/" (bodies, joints,
    actuators, tendons, equalities, sensors, meshes, materials, defaults). A single entry keeps the legacy unprefixed
    composition (identical model hash). No arm-count cap; explicit resource limits fail with kind "resource".

Every command prints one `WORKCELL_RESULT {json}` line. Lengths are metres.
"""
import argparse, hashlib, json, math, sys
from pathlib import Path
import numpy as np
import mujoco

sys.path.insert(0, str(Path(__file__).resolve().parent))
from sim_worker import geometry, FPS  # same browser bundle format as every other scene

PLANE = mujoco.mjtGeom.mjGEOM_PLANE


class PlacementError(ValueError):
    pass


class ResourceError(ValueError):
    pass


# Resource guards for multi-arm composition: explicit failures, never silent truncation of arms.
MAX_TOTAL_ROBOT_ACTUATORS = 512
MAX_COMPOSED_GEOMS = 60000
MAX_COMPOSED_MESH_VERTICES = 25_000_000
ARM_NAME = __import__("re").compile(r"^[A-Za-z][A-Za-z0-9_]{0,39}$")
BOX = mujoco.mjtGeom.mjGEOM_BOX


def scene_file(robot_dir):
    d = Path(robot_dir)
    if (d / "scene.xml").exists():
        return d / "scene.xml"
    c = sorted(d.glob("scene*.xml"))
    if c:
        return c[0]
    xs = sorted(p for p in d.glob("*.xml") if not p.name.startswith("mjx"))
    if not xs:
        raise SystemExit(f"no MJCF file in {d}")
    return xs[0]


def absolutize(spec):
    """Make every mesh/texture/hfield path absolute so specs from different folders can be combined."""
    base = Path(spec.modelfiledir or ".")
    for coll, sub in ((spec.meshes, spec.meshdir), (spec.textures, spec.texturedir), (getattr(spec, "hfields", []), spec.meshdir)):
        for a in coll:
            f = getattr(a, "file", "")
            if f and not Path(f).is_absolute():
                a.file = str((base / (sub or "") / f).resolve())
    spec.meshdir = ""
    spec.texturedir = ""
    return spec


def load_robot(robot_dir):
    spec = absolutize(mujoco.MjSpec.from_file(str(scene_file(robot_dir))))
    keys = list(spec.keys)
    home = next((k for k in keys if k.name == "home"), keys[0] if keys else None)
    hq = list(home.qpos) if home is not None else None
    hc = list(home.ctrl) if home is not None and len(home.ctrl) else None
    for k in keys:
        spec.delete(k)
    return spec, hq, hc


def robot_stats(m, home_q, front_override=None):
    d = mujoco.MjData(m)
    nq = m.nq
    if home_q is not None and len(home_q) == nq:
        d.qpos[:] = home_q
    mujoco.mj_forward(m, d)
    roots = [b for b in range(1, m.nbody) if m.body_parentid[b] == 0]
    root = roots[0] if roots else 0
    base = d.xpos[root].copy()
    # front: from the base towards the farthest body at the home pose
    far = max(range(1, m.nbody), key=lambda b: np.linalg.norm(d.xpos[b][:2] - base[:2]))
    v = d.xpos[far][:2] - base[:2]
    front = (v / np.linalg.norm(v)) if np.linalg.norm(v) > 0.02 else np.array([1.0, 0.0])
    ax = int(np.argmax(np.abs(front)))                 # snap to the nearest axis so the table lines up with the robot frame
    front = np.array([np.sign(front[0]), 0.0]) if ax == 0 else np.array([0.0, np.sign(front[1])])
    if front_override:
        f = np.array(front_override, dtype=float)
        front = f / np.linalg.norm(f)
    # reach: Monte Carlo over the joint ranges (horizontal distance of any body from the base)
    rng = np.random.default_rng(0)
    lo = np.where(m.jnt_limited.astype(bool), m.jnt_range[:, 0], -math.pi)
    hi = np.where(m.jnt_limited.astype(bool), m.jnt_range[:, 1], math.pi)
    reach = 0.0
    MOVING = (int(mujoco.mjtJoint.mjJNT_HINGE), int(mujoco.mjtJoint.mjJNT_SLIDE))
    hinge = [j for j in range(m.njnt) if int(m.jnt_type[j]) in MOVING]
    for i in range(4000):
        for j in hinge:   # half the samples near the joint limits / mid-range, where outstretched poses live
            d.qpos[m.jnt_qposadr[j]] = rng.uniform(lo[j], hi[j]) if i % 2 else rng.choice([lo[j], hi[j], 0.5 * (lo[j] + hi[j]), 0.0])
        mujoco.mj_kinematics(m, d)
        if m.ngeom:
            reach = max(reach, float(np.max(np.linalg.norm(d.geom_xpos[:, :2] - base[:2], axis=1)[m.geom_bodyid > 0])))
    joints = [m.joint(j).name for j in range(m.njnt) if int(m.jnt_type[j]) != int(mujoco.mjtJoint.mjJNT_FREE)]
    acts = [m.actuator(a).name for a in range(m.nu)]
    height = 0.0
    if home_q is not None and len(home_q) == nq:
        d.qpos[:] = home_q
        mujoco.mj_forward(m, d)
        height = float(np.max(d.xpos[:, 2]))
    return dict(nq=int(nq), nv=int(m.nv), nu=int(m.nu), nbody=int(m.nbody), joints=joints, actuators=acts,
                reach_m=round(reach, 3), front=[round(float(front[0]), 4), round(float(front[1]), 4)], home_height_m=round(height, 3),
                has_home=home_q is not None, timestep=float(m.opt.timestep))


def default_table(reach):
    return [round(max(0.5, reach * 1.5), 3), round(max(0.6, reach * 2.2), 3), 0.75]


def parse_table_pose(tp):
    """Optional table pose in the robot-base (world) frame: {x_m, y_m, yaw_deg} = table-top centre and the direction of
    the table LENGTH axis. None keeps the legacy rule (table centred in front of the robot, base near the back edge)."""
    if tp is None or tp == "":
        return None
    if isinstance(tp, str):
        tp = json.loads(tp)
    if not isinstance(tp, dict):
        raise PlacementError("table pose must be {x_m, y_m, yaw_deg}")
    try:
        x, y, yw = float(tp["x_m"]), float(tp["y_m"]), float(tp.get("yaw_deg", 0.0))
    except (KeyError, TypeError, ValueError):
        raise PlacementError("table pose must be {x_m, y_m, yaw_deg} with numeric values")
    if not all(math.isfinite(v) for v in (x, y, yw)) or abs(x) > 3 or abs(y) > 3:
        raise PlacementError("table pose must be finite with |x|, |y| <= 3 m")
    return dict(x_m=x, y_m=y, yaw_deg=(yw + 180.0) % 360.0 - 180.0)


def compose(robot_dir, assets, names, table=None, places=None, front_override=None, table_pose=None):
    spec, hq, hc = load_robot(robot_dir)
    m0 = spec.compile()
    st = robot_stats(m0, hq, front_override)
    nq_robot, nu = m0.nq, m0.nu
    f = np.array(st["front"])
    lat = np.array([-f[1], f[0]])
    L, W, H = table or default_table(st["reach_m"])
    t = 0.03
    back = min(0.12, L * 0.18)                         # robot base sits this far from the back edge
    tpose = parse_table_pose(table_pose)
    if tpose is None:
        c = f * (L / 2 - back)                         # legacy: table centre (world xy) in front of the robot
        yaw = math.atan2(f[1], f[0])
    else:
        c = np.array([tpose["x_m"], tpose["y_m"]])     # given: e.g. reconstructed from the workcell video
        yaw = math.radians(tpose["yaw_deg"])
    ex, ey = np.array([math.cos(yaw), math.sin(yaw)]), np.array([-math.sin(yaw), math.cos(yaw)])

    def table_xy(v):                                   # world xy -> (along length, along width) from the table centre
        return float((v - c) @ ex), float((v - c) @ ey)
    for g in spec.worldbody.geoms:                     # the real floor is below the table
        if g.type == PLANE:
            g.pos = [g.pos[0], g.pos[1], -H]
    tb = spec.worldbody.add_body(name="workcell_table", pos=[float(c[0]), float(c[1]), 0.0], quat=[math.cos(yaw / 2), 0, 0, math.sin(yaw / 2)])
    tb.add_geom(name="workcell_table_top", type=BOX, size=[L / 2, W / 2, t / 2], pos=[0, 0, -t / 2], rgba=[0.58, 0.56, 0.53, 1], friction=[0.8, 0.005, 0.0001])
    for sx in (-1, 1):
        for sy in (-1, 1):
            tb.add_geom(name=f"workcell_table_leg_{'p' if sx > 0 else 'n'}{'p' if sy > 0 else 'n'}", type=BOX,
                        size=[0.025, 0.025, max(0.01, (H - t) / 2)], pos=[sx * (L / 2 - 0.05), sy * (W / 2 - 0.05), -t - (H - t) / 2],
                        rgba=[0.32, 0.32, 0.34, 1], contype=0, conaffinity=0)
    # objects: given placements (world frame, e.g. from scene reconstruction) or a row in front of the robot
    places = dict(places or {})
    unknown = sorted(set(places) - set(names))
    if unknown:
        raise PlacementError(f"placements for objects not in this workcell: {', '.join(unknown)}")
    warnings = []
    base_clear = float(np.clip(0.12 * st["reach_m"], 0.05, 0.10))
    for n, p in places.items():
        if len(p) < 2 or not all(math.isfinite(float(v)) for v in p):
            raise PlacementError(f"{n}: placement must be finite [x_m, y_m, yaw_deg]")
        v = np.array([float(p[0]), float(p[1])])
        if tpose is None:
            along, side = float(v @ f), float(v @ lat)
            off_table = not (-back + 0.02 <= along <= L - back - 0.02 and abs(side) <= W / 2 - 0.02)
        else:
            along, side = table_xy(v)
            off_table = not (abs(along) <= L / 2 - 0.02 and abs(side) <= W / 2 - 0.02)
        if off_table:
            raise PlacementError(f"{n}: ({v[0]:.3f}, {v[1]:.3f}) m is not on the {L:.2f} x {W:.2f} m table (2 cm margin)")
        if np.linalg.norm(v) < base_clear:
            raise PlacementError(f"{n}: {np.linalg.norm(v) * 100:.1f} cm from the robot base; keep at least {base_clear * 100:.0f} cm clear")
        if np.linalg.norm(v) > st["reach_m"]:
            warnings.append(f"{n} is {np.linalg.norm(v):.2f} m from the base, beyond the estimated {st['reach_m']:.2f} m reach")
    if tpose is not None:
        ba, bs = table_xy(np.zeros(2))
        if not (abs(ba) <= L / 2 and abs(bs) <= W / 2):
            warnings.append(f"the robot base (origin) is {ba:+.3f}, {bs:+.3f} m from the table centre, outside the {L:.2f} x {W:.2f} m top")
    d0 = float(np.clip(0.5 * st["reach_m"], 0.12, L - back - 0.08))
    spacing = 0.12 if st["reach_m"] < 0.6 else 0.2
    for i, n in enumerate(names):
        if n not in places:
            off = (i - (len(names) - 1) / 2) * spacing
            p = f * d0 + lat * off
            if tpose is not None:
                a_, s_ = table_xy(p)
                if not (abs(a_) <= L / 2 - 0.02 and abs(s_) <= W / 2 - 0.02):
                    raise PlacementError(f"{n}: no placement given and the default spot in front of the robot is off the posed table; give its placement")
            places[n] = [round(float(p[0]), 4), round(float(p[1]), 4), 0.0]
        x, y = places[n][:2]
        oyaw = math.radians(places[n][2] if len(places[n]) > 2 else 0.0)
        obj = absolutize(mujoco.MjSpec.from_file(str(Path(assets) / n / f"{n}.xml")))
        fr = spec.worldbody.add_frame(pos=[x, y, 0.0], quat=[math.cos(oyaw / 2), 0, 0, math.sin(oyaw / 2)])
        fr.attach_body(obj.body(n), "", "")
    for i, a in enumerate(names):
        for b in names[i + 1:]:
            if math.dist(places[a][:2], places[b][:2]) < 0.04:
                warnings.append(f"{a} and {b} are less than 4 cm apart; they may overlap")
    m = spec.compile()
    info = dict(robot=st, table=dict(length_m=L, width_m=W, height_m=H, top_m=t, center=[round(float(c[0]), 4), round(float(c[1]), 4)], yaw_deg=round(math.degrees(yaw), 2),
                           pose_source="given" if tpose is not None else "default"),
                places={k: [float(v) for v in places[k]] for k in names}, nq_robot=int(nq_robot), nu_robot=int(nu), warnings=warnings,
                home_qpos=hq, home_ctrl=hc)
    return spec, m, info



# ── multi-arm composition ──────────────────────────────────────────────────────────────────────────────────
def parse_robots(arg):
    """--robots JSON (or @path) -> validated list of instance dicts. First entry = primary (world origin)."""
    if isinstance(arg, str):
        txt = Path(arg[1:]).read_text() if arg.startswith("@") else arg
        arg = json.loads(txt)
    if not isinstance(arg, list) or not arg:
        raise PlacementError("--robots must be a non-empty JSON list")
    out, seen = [], set()
    for i, r in enumerate(arg):
        if not isinstance(r, dict) or not r.get("robot_dir"):
            raise PlacementError(f"robot {i}: robot_dir is required")
        name = str(r.get("name") or f"robot_{i + 1}")
        if not ARM_NAME.match(name):
            raise PlacementError(f"robot {i}: name {name!r} must start with a letter (letters, digits, _; <= 40)")
        if name in seen:
            raise PlacementError(f"two robots are named {name}")
        seen.add(name)
        pose = r.get("pose")
        if pose is not None:
            try:
                pose = dict(x_m=float(pose["x_m"]), y_m=float(pose["y_m"]), yaw_deg=float(pose.get("yaw_deg", 0.0)), z_m=float(pose.get("z_m", 0.0) or 0.0))
            except (KeyError, TypeError, ValueError):
                raise PlacementError(f"{name}: pose must be {{x_m, y_m, yaw_deg[, z_m]}} with numeric values")
            if not all(math.isfinite(v) for v in pose.values()) or abs(pose["x_m"]) > 3 or abs(pose["y_m"]) > 3 or abs(pose["z_m"]) > 0.5:
                raise PlacementError(f"{name}: pose must be finite with |x|, |y| <= 3 m and |z| <= 0.5 m")
            pose["yaw_deg"] = (pose["yaw_deg"] + 180.0) % 360.0 - 180.0
        if i == 0 and pose is not None and any(abs(pose[k]) > 1e-9 for k in ("x_m", "y_m", "yaw_deg", "z_m")):
            raise PlacementError(f"{name} is the primary arm: its base is the world origin (pose must be null or zero)")
        out.append(dict(name=name, robot_dir=str(r["robot_dir"]), library_id=r.get("library_id"), label=r.get("label"),
                        pose=pose, pose_source=(str(r.get("pose_source") or "given")[:24] if pose is not None else ("origin" if i == 0 else "default"))))
    return out


def strip_robot(spec):
    """Keep only the scene decorations of a robot scene (floor, lights, skybox, options, visual); remove the robot."""
    for b in list(spec.worldbody.bodies):
        spec.delete(b)
    for coll in ("actuators", "sensors", "tendons", "equalities", "excludes", "pairs"):
        for e in list(getattr(spec, coll, [])):
            spec.delete(e)
    keep_mesh = {g.meshname for g in spec.worldbody.geoms if getattr(g, "meshname", "")}
    keep_mat = {g.material for g in spec.worldbody.geoms if g.material}
    for e in list(spec.meshes):
        if e.name not in keep_mesh:
            spec.delete(e)
    keep_tex = set()
    for mt in list(spec.materials):
        if mt.name in keep_mat:
            keep_tex |= {t for t in mt.textures if t}
        else:
            spec.delete(mt)
    for t in list(spec.textures):
        if t.type != mujoco.mjtTexture.mjTEXTURE_SKYBOX and t.name not in keep_tex:
            spec.delete(t)
    return spec


def _counts(m):
    return dict(nu=int(m.nu), njnt=int(m.njnt), ntendon=int(m.ntendon), neq=int(m.neq), nsensor=int(m.nsensor), nexclude=int(m.nexclude),
                nq=int(m.nq), nv=int(m.nv))


def subtree_bodies(m, root_ids):
    roots = set(root_ids)
    out = []
    for b in range(1, m.nbody):
        r = b
        while int(m.body_parentid[r]) != 0:
            r = int(m.body_parentid[r])
        if r in roots:
            out.append(b)
    return out


def deepest_body(m, bodies):
    depth = {0: 0}
    best, bd = None, -1
    for b in range(1, m.nbody):
        depth[b] = depth[int(m.body_parentid[b])] + 1
    for b in bodies:
        if depth[b] > bd:
            best, bd = b, depth[b]
    return m.body(best).name if best is not None else None


def arm_maps(m, arms):
    """Per-arm index maps on a compiled (or re-loaded exported) model, resolved by namespace prefix."""
    out = []
    FREE = int(mujoco.mjtJoint.mjJNT_FREE)
    robot_joints_all = [j for j in range(m.njnt) if int(m.jnt_type[j]) != FREE]
    obs_pos = {j: k for k, j in enumerate(robot_joints_all)}
    nrj = len(robot_joints_all)
    for a in arms:
        pre = a["prefix"]
        roots = [b for b in range(1, m.nbody) if int(m.body_parentid[b]) == 0 and m.body(b).name.startswith(pre)]
        bodies = subtree_bodies(m, roots)
        joints = [j for j in range(m.njnt) if m.joint(j).name.startswith(pre)]
        acts = [u for u in range(m.nu) if m.actuator(u).name.startswith(pre)]
        if len(joints) != a["standalone"]["njnt"] or len(acts) != a["standalone"]["nu"]:
            raise PlacementError(f"{a['name']}: namespaced joints/actuators ({len(joints)}/{len(acts)}) do not match the model "
                                 f"({a['standalone']['njnt']}/{a['standalone']['nu']}); unnamed elements cannot be mapped")
        if acts and acts != list(range(acts[0], acts[-1] + 1)):
            raise PlacementError(f"{a['name']}: actuators are not contiguous")
        nf = [j for j in joints if int(m.jnt_type[j]) != FREE]
        qadr = sorted({int(m.jnt_qposadr[j]) + k for j in joints for k in range({0: 7, 1: 4, 2: 1, 3: 1}[int(m.jnt_type[j])])})
        vadr = sorted({int(m.jnt_dofadr[j]) + k for j in joints for k in range({0: 6, 1: 3, 2: 1, 3: 1}[int(m.jnt_type[j])])})
        out.append(dict(name=a["name"], prefix=pre, root_bodies=[m.body(b).name for b in roots], end_effector_body=deepest_body(m, bodies),
                        joints=[m.joint(j).name for j in nf], actuators=[m.actuator(u).name for u in acts],
                        joint_ids=nf, actuator_ids=acts, qpos_indices=qadr, qvel_indices=vadr,
                        action_slice=[acts[0], acts[-1] + 1] if acts else [0, 0],
                        observation_qpos_indices=[obs_pos[j] for j in nf], observation_qvel_indices=[nrj + obs_pos[j] for j in nf]))
    return out


def compose_multi(robots, assets, names, table=None, places=None, table_pose=None):
    robots = parse_robots(robots) if not (isinstance(robots, list) and robots and "standalone" in robots[0]) else robots
    if len(robots) < 2:
        raise PlacementError("compose_multi needs two or more robots (a single robot uses the legacy composer)")
    clash = sorted({r["name"] for r in robots} & (set(names) | {"workcell_table", "world"}))
    if clash:
        raise PlacementError(f"robot names collide with object names: {', '.join(clash)}")
    # Per-model standalone compile: stats, home keyframe, element counts (computed once per unique model folder).
    by_dir = {}
    for r in robots:
        d = str(Path(r["robot_dir"]).resolve())
        if d not in by_dir:
            s, hq, hc = load_robot(d)
            m_ = s.compile()
            by_dir[d] = dict(stats=robot_stats(m_, hq), hq=hq if hq is not None else m_.qpos0.tolist(), hc=hc, counts=_counts(m_),
                             degree=bool(s.compiler.degree), scene=scene_file(d).name, qpos0=m_.qpos0.tolist())
        r.update(dir=d, standalone=by_dir[d]["counts"], stats=by_dir[d]["stats"], prefix=r["name"] + "/")
    if sum(r["standalone"]["nu"] for r in robots) > MAX_TOTAL_ROBOT_ACTUATORS:
        raise ResourceError(f"{len(robots)} arms have {sum(r['standalone']['nu'] for r in robots)} actuators in total, above the "
                            f"{MAX_TOTAL_ROBOT_ACTUATORS} actuator safety limit; no arm was dropped")
    prim = robots[0]
    host = strip_robot(load_robot(prim["dir"])[0])
    if any(by_dir[r["dir"]]["degree"] != bool(host.compiler.degree) for r in robots):
        raise PlacementError("these robot models use different angle units (degree/radian); they cannot be composed in one scene")
    st = prim["stats"]
    f = np.array(st["front"])
    lat = np.array([-f[1], f[0]])
    max_reach = max(r["stats"]["reach_m"] for r in robots)
    # provisional default slots for arms without a pose: beside the primary along its lateral axis, same facing
    spacing = round(float(np.clip(0.9 * max_reach + 0.05, 0.30, 0.9)), 3)
    slots, k = [], 1
    while len(slots) < len(robots):
        slots += [k, -k]
        k += 1
    used = 0
    for r in robots[1:]:
        if r["pose"] is None:
            off = slots[used] * spacing
            used += 1
            v = lat * off
            fi = r["stats"]["front"]           # face the same way as the primary arm (models differ in their own front axis)
            yd = math.degrees(math.atan2(f[1], f[0]) - math.atan2(fi[1], fi[0]))
            r["pose"] = dict(x_m=round(float(v[0]), 4), y_m=round(float(v[1]), 4), yaw_deg=round((yd + 180.0) % 360.0 - 180.0, 3), z_m=0.0)
            r["pose_source"] = "default"
    prim["pose"] = dict(x_m=0.0, y_m=0.0, yaw_deg=0.0, z_m=0.0)
    if table:
        L, W, H = table
    else:
        L, W, H = default_table(max_reach)
        span = max(abs(float(np.array([r["pose"]["x_m"], r["pose"]["y_m"]]) @ lat)) for r in robots)
        W = round(max(W, 2 * span + 2 * max_reach), 3)
    t = 0.03
    back = min(0.12, L * 0.18)
    tpose = parse_table_pose(table_pose)
    if tpose is None:
        c = f * (L / 2 - back)
        yaw = math.atan2(f[1], f[0])
    else:
        c = np.array([tpose["x_m"], tpose["y_m"]])
        yaw = math.radians(tpose["yaw_deg"])
    ex, ey = np.array([math.cos(yaw), math.sin(yaw)]), np.array([-math.sin(yaw), math.cos(yaw)])

    def table_xy(v):
        return float((v - c) @ ex), float((v - c) @ ey)
    warnings = []
    bases = {r["name"]: np.array([r["pose"]["x_m"], r["pose"]["y_m"]]) for r in robots}
    for i, a in enumerate(robots):
        for b in robots[i + 1:]:
            dd = float(np.linalg.norm(bases[a["name"]] - bases[b["name"]]))
            if dd < 0.08:
                raise PlacementError(f"{a['name']} and {b['name']} bases are {dd * 100:.1f} cm apart; keep at least 8 cm (they would intersect)")
            if dd < 0.2:
                warnings.append(f"{a['name']} and {b['name']} bases are {dd * 100:.0f} cm apart; their links may collide")
    for r in robots:
        ba, bs = table_xy(bases[r["name"]])
        if not (abs(ba) <= L / 2 and abs(bs) <= W / 2):
            warnings.append(f"the {r['name']} base is {ba:+.3f}, {bs:+.3f} m from the table centre, outside the {L:.2f} x {W:.2f} m top")
    for g in host.worldbody.geoms:
        if g.type == PLANE:
            g.pos = [g.pos[0], g.pos[1], -H]
    # attach every arm (in order: primary first), each under its own namespace
    for r in robots:
        s_i = load_robot(r["dir"])[0]
        p = r["pose"]
        yw = math.radians(p["yaw_deg"])
        fr = host.worldbody.add_frame(pos=[p["x_m"], p["y_m"], p["z_m"]], quat=[math.cos(yw / 2), 0, 0, math.sin(yw / 2)])
        for b in list(s_i.worldbody.bodies):
            fr.attach_body(b, r["prefix"], "")
    tb = host.worldbody.add_body(name="workcell_table", pos=[float(c[0]), float(c[1]), 0.0], quat=[math.cos(yaw / 2), 0, 0, math.sin(yaw / 2)])
    tb.add_geom(name="workcell_table_top", type=BOX, size=[L / 2, W / 2, t / 2], pos=[0, 0, -t / 2], rgba=[0.58, 0.56, 0.53, 1], friction=[0.8, 0.005, 0.0001])
    for sx in (-1, 1):
        for sy in (-1, 1):
            tb.add_geom(name=f"workcell_table_leg_{'p' if sx > 0 else 'n'}{'p' if sy > 0 else 'n'}", type=BOX,
                        size=[0.025, 0.025, max(0.01, (H - t) / 2)], pos=[sx * (L / 2 - 0.05), sy * (W / 2 - 0.05), -t - (H - t) / 2],
                        rgba=[0.32, 0.32, 0.34, 1], contype=0, conaffinity=0)
    places = dict(places or {})
    unknown = sorted(set(places) - set(names))
    if unknown:
        raise PlacementError(f"placements for objects not in this workcell: {', '.join(unknown)}")
    base_clear = float(np.clip(0.12 * min(r["stats"]["reach_m"] for r in robots), 0.05, 0.10))

    def near_base(v):
        for r in robots:
            dd = float(np.linalg.norm(v - bases[r["name"]]))
            if dd < base_clear:
                return r["name"], dd
        return None
    for n, p in places.items():
        if len(p) < 2 or not all(math.isfinite(float(v)) for v in p):
            raise PlacementError(f"{n}: placement must be finite [x_m, y_m, yaw_deg]")
        v = np.array([float(p[0]), float(p[1])])
        if tpose is None:
            al_, sd_ = table_xy(v)
            off_table = not (abs(al_) <= L / 2 - 0.02 and abs(sd_) <= W / 2 - 0.02)
        else:
            al_, sd_ = table_xy(v)
            off_table = not (abs(al_) <= L / 2 - 0.02 and abs(sd_) <= W / 2 - 0.02)
        if off_table:
            raise PlacementError(f"{n}: ({v[0]:.3f}, {v[1]:.3f}) m is not on the {L:.2f} x {W:.2f} m table (2 cm margin)")
        nb = near_base(v)
        if nb:
            raise PlacementError(f"{n}: {nb[1] * 100:.1f} cm from the {nb[0]} base; keep at least {base_clear * 100:.0f} cm clear")
        if all(np.linalg.norm(v - bases[r["name"]]) > r["stats"]["reach_m"] for r in robots):
            warnings.append(f"{n} is beyond the estimated reach of every arm")
    d0 = float(np.clip(0.5 * st["reach_m"], 0.12, L - back - 0.08))
    ospace = 0.12 if st["reach_m"] < 0.6 else 0.2
    for i, n in enumerate(names):
        if n not in places:
            p = f * d0 + lat * ((i - (len(names) - 1) / 2) * ospace)
            a_, s_ = table_xy(p)
            if not (abs(a_) <= L / 2 - 0.02 and abs(s_) <= W / 2 - 0.02):
                raise PlacementError(f"{n}: no placement given and the default spot in front of the primary arm is off the table; give its placement")
            if near_base(p):
                raise PlacementError(f"{n}: no placement given and the default spot is next to an arm base; give its placement")
            places[n] = [round(float(p[0]), 4), round(float(p[1]), 4), 0.0]
        x, y = places[n][:2]
        oyaw = math.radians(places[n][2] if len(places[n]) > 2 else 0.0)
        obj = absolutize(mujoco.MjSpec.from_file(str(Path(assets) / n / f"{n}.xml")))
        fr = host.worldbody.add_frame(pos=[x, y, 0.0], quat=[math.cos(oyaw / 2), 0, 0, math.sin(oyaw / 2)])
        fr.attach_body(obj.body(n), "", "")
    for i, a in enumerate(names):
        for b in names[i + 1:]:
            if math.dist(places[a][:2], places[b][:2]) < 0.04:
                warnings.append(f"{a} and {b} are less than 4 cm apart; they may overlap")
    m = host.compile()
    if m.ngeom > MAX_COMPOSED_GEOMS or m.nmeshvert > MAX_COMPOSED_MESH_VERTICES:
        raise ResourceError(f"the composed scene has {m.ngeom} geoms / {m.nmeshvert} mesh vertices, above the safety limit "
                            f"({MAX_COMPOSED_GEOMS} / {MAX_COMPOSED_MESH_VERTICES}); no arm was dropped")
    tot = {k: sum(r["standalone"][k] for r in robots) for k in ("nu", "ntendon", "neq", "nsensor", "nexclude")}
    tot_j = sum(r["standalone"]["njnt"] for r in robots) + len(names)
    if (m.nu, m.ntendon, m.neq, m.nsensor, m.njnt) != (tot["nu"], tot["ntendon"], tot["neq"], tot["nsensor"], tot_j) or m.nexclude < tot["nexclude"]:
        raise PlacementError("the composed scene does not contain every element of every arm "
                             f"(nu {m.nu}/{tot['nu']}, joints {m.njnt}/{tot_j}, tendons {m.ntendon}/{tot['ntendon']}, equalities {m.neq}/{tot['neq']}, sensors {m.nsensor}/{tot['nsensor']})")
    maps = arm_maps(m, robots)
    nq_robot = sum(r["standalone"]["nq"] for r in robots)
    hq = np.array(m.qpos0[:nq_robot], dtype=float)
    hc = [None] * m.nu
    q0 = 0
    for r, mp in zip(robots, maps):
        bd = by_dir[r["dir"]]
        nq_i = r["standalone"]["nq"]
        if mp["qpos_indices"] != list(range(q0, q0 + nq_i)):
            raise PlacementError(f"{r['name']}: joint coordinates are not contiguous in the composed model")
        hq[q0:q0 + nq_i] = bd["hq"][:nq_i] if len(bd["hq"]) == nq_i else bd["qpos0"]
        if bd["hc"] is not None and len(bd["hc"]) == len(mp["actuator_ids"]):
            for u, v in zip(mp["actuator_ids"], bd["hc"]):
                hc[u] = float(v)
        q0 += nq_i
    arms_info = []
    for r, mp in zip(robots, maps):
        arms_info.append(dict(name=r["name"], label=r.get("label"), library_id=r.get("library_id"), prefix=r["prefix"], scene_file=by_dir[r["dir"]]["scene"],
                              base_pose={k: round(float(v), 4) for k, v in r["pose"].items()}, pose_source=r["pose_source"],
                              primary=r is prim, stats=r["stats"], standalone=r["standalone"],
                              **{k: mp[k] for k in ("root_bodies", "end_effector_body", "joints", "actuators", "qpos_indices", "qvel_indices", "action_slice",
                                                    "observation_qpos_indices", "observation_qvel_indices")},
                              home_qpos=[round(float(v), 6) for v in hq[mp["qpos_indices"][0]:mp["qpos_indices"][-1] + 1]] if mp["qpos_indices"] else []))
    robot_stats_all = dict(st, nq=int(nq_robot), nv=int(sum(r["standalone"]["nv"] for r in robots)), nu=int(m.nu),
                           joints=[j for a in arms_info for j in a["joints"]], actuators=[u for a in arms_info for u in a["actuators"]],
                           reach_m=st["reach_m"], instances=len(robots), primary=prim["name"])
    info = dict(robot=robot_stats_all, robots=arms_info,
                table=dict(length_m=L, width_m=W, height_m=H, top_m=t, center=[round(float(c[0]), 4), round(float(c[1]), 4)], yaw_deg=round(math.degrees(yaw), 2),
                           pose_source="given" if tpose is not None else "default"),
                places={k: [float(v) for v in places[k]] for k in names}, nq_robot=int(nq_robot), nu_robot=int(m.nu), warnings=warnings,
                home_qpos=hq.tolist(), home_ctrl=hc if any(v is not None for v in hc) else None)
    return host, m, info


def public_arms(info):
    return [{k: a[k] for k in ("name", "label", "library_id", "prefix", "base_pose", "pose_source", "primary", "end_effector_body", "joints",
                               "actuators", "action_slice", "qpos_indices", "qvel_indices")} | dict(reach_m=a["stats"]["reach_m"], nu=a["standalone"]["nu"])
            for a in info.get("robots") or []]


def robot_ctrl(m, d, info):
    hc = info["home_ctrl"]
    if hc is not None and len(hc) == m.nu and all(v is not None for v in hc):
        d.ctrl[:] = hc
        return
    for a in range(m.nu):
        if hc is not None and len(hc) == m.nu and hc[a] is not None:   # multi-arm: arms with a home ctrl keep it
            d.ctrl[a] = hc[a]
            continue
        if int(m.actuator_trntype[a]) == int(mujoco.mjtTrn.mjTRN_JOINT):
            j = m.actuator_trnid[a][0]
            v = d.qpos[m.jnt_qposadr[j]]
            if m.actuator_ctrllimited[a]:
                v = float(np.clip(v, *m.actuator_ctrlrange[a]))
            d.ctrl[a] = v


def reset(m, info, names, lift=0.0, pose=None):
    d = mujoco.MjData(m)
    hq = info["home_qpos"]
    if hq is not None and len(hq) == info["nq_robot"]:
        d.qpos[:info["nq_robot"]] = hq
    places = dict(info["places"])
    if pose:
        places[names[0]] = [pose[0], pose[1], pose[2] if len(pose) > 2 else 0.0]
    for n in names:
        j = m.body_jntadr[m.body(n).id]
        a = m.jnt_qposadr[j]
        x, y, yw = (list(places[n]) + [0.0])[:3]
        r = math.radians(yw)
        d.qpos[a:a + 7] = [x, y, lift, math.cos(r / 2), 0, 0, math.sin(r / 2)]
    robot_ctrl(m, d, info)
    mujoco.mj_forward(m, d)
    return d


def run_drop(m, info, names, lift, pose, seconds):
    d = reset(m, info, names, lift, pose)
    frames, times = [], []
    every = max(1, int(round(1.0 / (FPS * m.opt.timestep))))
    for k in range(int(seconds / m.opt.timestep) + 1):
        if k % every == 0:
            frames.append(np.concatenate([d.xpos, d.xquat], 1).astype(np.float32))
            times.append(round(float(d.time), 4))
        mujoco.mj_step(m, d)
    metrics = {}
    for n in names:
        b = m.body(n).id
        j = m.body_jntadr[b]
        up = d.xmat[b].reshape(3, 3)[:, 2]
        tilt = float(np.degrees(np.arccos(np.clip(up[2], -1, 1))))
        speed = float(np.linalg.norm(d.qvel[m.jnt_dofadr[j]:m.jnt_dofadr[j] + 3]))
        z = float(d.xpos[b][2])
        metrics[n] = dict(mass_kg=round(float(m.body_subtreemass[b]), 5), rest_pos_m=[round(float(v), 4) for v in d.xpos[b]],
                          tilt_deg=round(tilt, 2), speed_mps=round(speed, 5), upright=tilt < 10, settled=speed < 0.01, on_table=z > -0.02)
    ok = all(v["upright"] and v["settled"] and v["on_table"] for v in metrics.values())
    if info.get("robots"):                  # multi-arm: every arm was simulated; report how far each drifted from its home pose
        hq = np.array(info["home_qpos"])
        info["arm_metrics"] = {a["name"]: dict(max_joint_dev_rad=round(float(np.max(np.abs(d.qpos[a["qpos_indices"]] - hq[a["qpos_indices"]]))), 5) if a["qpos_indices"] else 0.0,
                                               end_effector_pos_m=[round(float(v), 4) for v in d.xpos[m.body(a["end_effector_body"]).id]])
                               for a in info["robots"]}
    return np.stack(frames), times, metrics, ok


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("cmd", choices=["robot", "bundle", "simulate"])
    ap.add_argument("--robot-dir", default=""); ap.add_argument("--out", required=True)
    ap.add_argument("--robots", default="", help="JSON list (or @file) of arm instances; first = primary at the world origin")
    ap.add_argument("--assets", default=""); ap.add_argument("--names", default="")
    ap.add_argument("--table", default=""); ap.add_argument("--places", default="")
    ap.add_argument("--front", default="")
    ap.add_argument("--table-pose", default="", help='JSON {"x_m","y_m","yaw_deg"}: table-top centre + length axis in the robot-base frame')
    ap.add_argument("--seed", type=int, default=0); ap.add_argument("--lift", type=float, default=0.05); ap.add_argument("--seconds", type=float, default=3.0)
    ap.add_argument("--x", type=float); ap.add_argument("--y", type=float); ap.add_argument("--yaw", type=float)
    a = ap.parse_args()
    out = Path(a.out); out.mkdir(parents=True, exist_ok=True)
    front = [float(v) for v in a.front.split(",")] if a.front else None
    names = [n for n in a.names.split(",") if n]
    robots = None
    if a.robots:
        try:
            robots = parse_robots(a.robots)
        except PlacementError as e:
            print("WORKCELL_RESULT " + json.dumps(dict(ok=False, error=str(e), kind="placement")))
            sys.exit(2)
        if len(robots) == 1:                 # one arm = the legacy composer, unprefixed (identical model hash)
            a.robot_dir, robots = robots[0]["robot_dir"], None
    if not a.robot_dir and not robots:
        ap.error("--robot-dir or --robots is required")
    if a.cmd == "robot" and robots:
        ap.error("robot previews one model: use --robot-dir")
    if a.cmd == "robot":
        spec, hq, hc = load_robot(a.robot_dir)
        m = spec.compile()
        st = robot_stats(m, hq, front)
        g = geometry(m, out, [])
        d = mujoco.MjData(m)
        if hq is not None and len(hq) == m.nq:
            d.qpos[:] = hq
        mujoco.mj_forward(m, d)
        g.update(version=2, engine=dict(mujoco=mujoco.__version__, builder="workspace:robot", timestep=m.opt.timestep, nbody=m.nbody, ngeom=m.ngeom),
                 places={}, pose0=np.concatenate([d.xpos, d.xquat], 1).astype(float).round(6).tolist(), workcell=dict(robot=st, table=None, preview="robot"))
        txt = json.dumps(g, separators=(",", ":"))
        (out / "scene.json").write_text(txt)
        h = hashlib.sha256(txt.encode() + (out / "meshes.bin").read_bytes()).hexdigest()
        print("WORKCELL_RESULT " + json.dumps(dict(ok=True, stats=st, scene_file=scene_file(a.robot_dir).name, model_hash=h)))
        return
    table = [float(v) for v in a.table.split(",")] if a.table else None
    places = json.loads(a.places) if a.places else None
    try:
        if robots:
            spec, m, info = compose_multi(robots, a.assets, names, table, places, a.table_pose or None)
        else:
            spec, m, info = compose(a.robot_dir, a.assets, names, table, places, front, a.table_pose or None)
    except ResourceError as e:
        print("WORKCELL_RESULT " + json.dumps(dict(ok=False, error=str(e), kind="resource")))
        sys.exit(2)
    except PlacementError as e:
        print("WORKCELL_RESULT " + json.dumps(dict(ok=False, error=str(e), kind="placement")))
        sys.exit(2)
    multi = {"robots": public_arms(info)} if robots else {}
    if robots:
        info["_multi"] = multi
    engine = dict(mujoco=mujoco.__version__, builder="workspace:workcell", timestep=m.opt.timestep, nbody=m.nbody, ngeom=m.ngeom)
    if a.cmd == "bundle":
        g = geometry(m, out, names)
        d0 = reset(m, info, names, 0.0)
        g.update(version=2, engine=engine, places=info["places"], pose0=np.concatenate([d0.xpos, d0.xquat], 1).astype(float).round(6).tolist(),
                 workcell=dict(robot=info["robot"], table=info["table"], preview="workcell", **multi))
        txt = json.dumps(g, separators=(",", ":"))
        (out / "scene.json").write_text(txt)
        h = hashlib.sha256(txt.encode() + (out / "meshes.bin").read_bytes()).hexdigest()
        print("WORKCELL_RESULT " + json.dumps(dict(ok=True, engine=engine, model_hash=h, places=info["places"], table=info["table"], robot=info["robot"], warnings=info["warnings"], **multi)))
        return
    rng = np.random.default_rng(a.seed)
    pose = None
    if a.x is not None and a.y is not None:
        pose = (a.x, a.y, a.yaw or 0.0)
    elif names and a.seed:
        x0, y0 = info["places"][names[0]][:2]
        pose = (x0 + rng.uniform(-0.03, 0.03), y0 + rng.uniform(-0.03, 0.03), rng.uniform(-30, 30))
    traj, times, metrics, ok = run_drop(m, info, names, a.lift, pose, a.seconds)
    if robots:
        multi["arm_metrics"] = info.get("arm_metrics")
    (out / "traj.bin").write_bytes(traj.tobytes())
    res = dict(fps=FPS, frames=int(traj.shape[0]), bodies=int(traj.shape[1]), times=times, seed=a.seed,
               pose=dict(x=pose[0], y=pose[1], yaw_deg=pose[2]) if pose else None, lift_m=a.lift, seconds=a.seconds,
               start="workspace:home+placements", metrics=metrics, **multi, result="pass" if ok else "fail", mode="drop-test", engine=engine)
    (out / "traj.json").write_text(json.dumps(res))
    print("WORKCELL_RESULT " + json.dumps(dict(ok=True, result=res["result"], metrics=metrics, pose=res["pose"], engine=engine, start=res["start"],
                                               places=info["places"], table=info["table"], robot=info["robot"], warnings=info["warnings"], **multi)))


if __name__ == "__main__":
    main()
