"""Several arms: each one is known by its USB board's serial number, so Windows renumbering COM ports never
mixes them up. Every arm has its own calibration files; one arm is "active" at a time, and every robot script
(setup_motors, calibrate, gripper_check, goto_pose, run, ...) works on the active arm.

    python robot/arms.py list                 # the arms, which is active, which are plugged in
    python robot/arms.py add left             # register a new arm: plug in its board when asked
    python robot/arms.py add right --model so101
    python robot/arms.py use right            # make it the active arm
    python robot/arms.py remove right
    python robot/arms.py view                 # every plugged-in arm side by side in the sim, copying the real ones
    python robot/arms.py identify             # move one arm by hand: tells you which name it has

robot/config.json "arms": {name: {serial, model, calibration, sim_mapping}}; the active arm's entries are also
written to the top-level "port", "serial", "calibration", "sim_mapping" and "arm_model" the scripts read.
"""
import argparse
import json
import time
from pathlib import Path

from serial.tools import list_ports

CONFIG = Path(__file__).parent / "config.json"
MODELS = ("so101", "so100")


def boards():
    """{USB serial number: COM port} of the plugged-in serial boards (Bluetooth links left out)."""
    return {p.serial_number or p.device: p.device for p in list_ports.comports()
            if "BTHENUM" not in (p.hwid or "") and "bluetooth" not in (p.description or "").lower()}


def load():
    cfg = json.loads(CONFIG.read_text())
    cfg.setdefault("arms", {})
    return cfg


def save(cfg):
    CONFIG.write_text(json.dumps(cfg, indent=2))


def use(cfg, name):
    arm = cfg["arms"][name]
    port = boards().get(arm["serial"])
    cfg.update(active_arm=name, serial=arm["serial"], calibration=arm["calibration"], sim_mapping=arm["sim_mapping"],
               arm_model=arm["model"])
    if arm.get("env_camera"):  # the camera as calibrated with this arm (its own robot frame)
        cfg["env_camera"] = dict(cfg.get("env_camera", {}), **arm["env_camera"])
    if port:
        cfg["port"] = port
    save(cfg)
    print(f"active arm: {name} ({arm['model']}, board {arm['serial']}, " + (f"on {port})" if port else "not plugged in)"))


def add(cfg, name, model):
    if name in cfg["arms"]:
        raise SystemExit(f"an arm called '{name}' exists already (remove it first)")
    known = {a["serial"] for a in cfg["arms"].values()}
    print(f"plug in the USB cable of the '{name}' arm's board now (other arms may stay plugged in), board powered")
    for _ in range(60):  # wait up to 30 s for exactly one board nobody owns yet
        new = [s for s in boards() if s not in known]
        if len(new) == 1:
            break
        if len(new) > 1:
            raise SystemExit(f"{len(new)} unregistered boards are plugged in ({new}): plug in only the new arm's board")
        time.sleep(0.5)
    else:
        raise SystemExit("no new board found: check the USB cable (some are charge-only) and the CH340/CH343 driver")
    serial = new[0]
    cfg["arms"][name] = dict(serial=serial, model=model, calibration=f"robot/calibration/{name}_follower.json",
                             sim_mapping=f"robot/calibration/{name}_sim_mapping.json")
    print(f"'{name}' = board {serial} on {boards()[serial]}")
    use(cfg, name)
    print("next for this arm: python robot/setup_motors.py (new servos only, one at a time), then "
          "python robot/calibrate.py")


def show(cfg):
    plugged = boards()
    if not cfg["arms"]:
        print("no arms registered yet: python robot/arms.py add <name>")
    for name, a in cfg["arms"].items():
        root = CONFIG.parent.parent
        calibrated = (root / a["calibration"]).exists() and (root / a["sim_mapping"]).exists()
        print(f"{'*' if cfg.get('active_arm') == name else ' '} {name}: {a['model']}, board {a['serial']} "
              f"{'on ' + plugged[a['serial']] if a['serial'] in plugged else '(not plugged in)'}, "
              f"{'calibrated' if calibrated else 'NOT calibrated'}")
    other = [s for s in plugged if s not in {a["serial"] for a in cfg["arms"].values()}]
    if other:
        print(f"  unregistered boards plugged in: {', '.join(f'{s} ({plugged[s]})' for s in other)}")


def arm_poses(cfg, names=None):
    """Where every arm stands, in one arm's robot frame (the reference: the active arm if it has a camera
    calibration). Each arm's camera calibration says where the (same) camera is seen from that arm; combined they
    give one arm's base in the other's frame. Both stand on the same table, so only the sideways/forward shift and
    the turn are kept (level, on the table). Returns (reference name, {name: (shift xyz m, turn rad)}, report)."""
    import numpy as np
    cams = {n: (np.array(a["env_camera"]["pos"]), np.array(a["env_camera"]["R"]))
            for n, a in cfg["arms"].items() if a.get("env_camera") and (names is None or n in names)}
    if not cams:
        return None, {}, []
    ref = cfg.get("active_arm") if cfg.get("active_arm") in cams else next(iter(cams))
    p_ref, R_ref = cams[ref]
    poses, report = {ref: (np.zeros(3), 0.0)}, []
    for n, (p, R) in cams.items():
        if n == ref:
            continue
        A = R_ref @ R.T  # arm n's frame -> the reference arm's frame
        t = p_ref - A @ p
        turn = float(np.arctan2(A[1, 0], A[0, 0]))
        tilt = float(np.degrees(np.arccos(np.clip(A[2, 2], -1, 1))))
        poses[n] = (np.array([t[0], t[1], 0.0]), turn)
        report.append(f"{n}: {t[0] * 100:+.1f} cm x, {t[1] * 100:+.1f} cm y from {ref}, turned {np.degrees(turn):+.1f} "
                      f"deg (calibration mismatch: {t[2] * 100:+.1f} cm height, {tilt:.1f} deg tilt)")
    return ref, poses, report


def plugged_arms(cfg):
    """The registered arms whose boards are plugged in; arms sharing a board (one board moved between arms)
    count once: the active one, else the first registered."""
    plugged, names, used = boards(), [], set()
    order = sorted(cfg["arms"], key=lambda n: n != cfg.get("active_arm"))
    for n in order:
        s = cfg["arms"][n]["serial"]
        if s in plugged and s not in used:
            names.append(n)
            used.add(s)
    names = [n for n in cfg["arms"] if n in names]
    if not names:
        raise SystemExit("no registered arm is plugged in (python robot/arms.py list)")
    return names


def arms_scene_spec(cfg, names, spacing=0.45):
    """MjSpec: the single-arm scene's background (sky, checker floor, light) with these arms at their measured
    spots (arm_poses; placeholders `spacing` m apart if not measured), joints named "<arm>_<joint>". Also returns
    the reference arm (whose robot frame this is) and its camera calibration."""
    import mujoco
    import numpy as np
    from sim_map import MENAGERIE, ROBOT_XML, arm_placement
    scene = (MENAGERIE / "trs_so_arm100" / "scene.xml").read_text()
    spec = mujoco.MjSpec.from_string(scene.replace('<include file="so_arm100.xml"/>', ""))
    ref, poses, report = arm_poses(cfg, names)  # measured relative to one of THESE arms
    for line in report:
        print("measured:", line)
    for i, n in enumerate(names):
        xml, base = ROBOT_XML[cfg["arms"][n]["model"]]
        child = mujoco.MjSpec.from_file(str(MENAGERIE / xml))
        for k in ("timestep", "iterations", "ls_iterations", "impratio", "integrator", "cone"):
            setattr(spec.option, k, getattr(child.option, k))  # same settings: no attach warnings
        pos, quat = arm_placement(cfg["arms"][n]["model"])  # in its own robot frame, facing -y like the SO-100
        shift, turn = poses.get(n, (np.array([i * spacing, 0.0, 0.0]), 0.0))  # measured, else a placeholder
        if n not in poses:
            print(f"{n}: not measured yet (camera calibration with it): shown {i * spacing * 100:.0f} cm to the side")
        c, s_ = np.cos(turn), np.sin(turn)
        yaw = turn + 2 * np.arctan2(quat[3], quat[0])
        pos = shift + np.array([[c, -s_, 0], [s_, c, 0], [0, 0, 1.0]]) @ pos
        spec.worldbody.add_frame(pos=pos, quat=[np.cos(yaw / 2), 0, 0, np.sin(yaw / 2)]).attach_body(
            child.body(base), f"{n}_", "")
    ec = (cfg["arms"][ref].get("env_camera") if ref else None) or cfg.get("env_camera") or {}
    return spec, ref, ec


def connect_arms(cfg, names, model):
    """Each arm connected (torque off) with what is needed to copy it into `model`: list of dicts."""
    import numpy as np
    from arm import SOArm
    from sim_map import SIM_JOINTS, SimMap, load_sim
    root, plugged, arms = CONFIG.parent.parent, boards(), []
    for n in names:
        a = cfg["arms"][n]
        arm = SOArm(plugged[a["serial"]], root / a["calibration"], cfg["baudrate"], serial=a["serial"])
        arm.connect()
        arm.torque(False)
        joints = SIM_JOINTS[a["model"]]
        mapping = SimMap(root / a["sim_mapping"], arm.calibration, a["model"]) if arm.calibration else None
        if mapping is not None and not mapping.ready:
            mapping = None
        sim, _ = load_sim(a["model"])
        arms.append(dict(name=n, arm=arm, joints=joints, mapping=mapping,
                         qadr=[model.jnt_qposadr[model.joint(f"{n}_{joints[m]}").id] for m in joints],
                         middle=(sim.jnt_range[:6, 0] + sim.jnt_range[:6, 1]) / 2))
        print(f"{n}: {'calibrated: copied exactly' if mapping else 'NOT calibrated: raw motor positions'}")
    return arms


def copy_arms(arms, data):
    """Every connected arm's joint angles into the sim (calibrated: exactly; else raw motor positions)."""
    import numpy as np
    from arm import MAX_POS
    for a in arms:
        if a["mapping"] is not None:
            q = a["mapping"].to_sim(a["arm"].read_degrees())
        else:
            raw = a["arm"].read_raw()
            q = a["middle"] + np.radians([(raw[m] - MAX_POS / 2) * 360 / (MAX_POS + 1) for m in a["joints"]])
        data.qpos[a["qadr"]] = q


def view(cfg, spacing=0.45, object_name="lego_tree"):
    """Every plugged-in arm in the tree scene (the object in its placement area, the calibrated environment
    camera), copying the real arms (torque off: move them by hand), each at its measured spot.
    For live object tracking with the arms: python photo2grasp.py track <object>."""
    import mujoco
    import mujoco.viewer
    import numpy as np

    import sys
    sys.path.insert(0, str(CONFIG.parent.parent / "sim2real"))
    from tag_env import DEFAULT_AREA, from_ruler

    names = plugged_arms(cfg)
    spec, ref, ec = arms_scene_spec(cfg, names, spacing)
    if object_name:  # standing in the middle of the placement area, facing the robot
        a = DEFAULT_AREA
        obj = mujoco.MjSpec.from_file(str(CONFIG.parent.parent / "assets" / object_name / f"{object_name}.xml"))
        spec.worldbody.add_frame(pos=from_ruler((a["front_min"] + a["front_max"]) / 2, 0.0, 0.0)).attach_body(
            obj.body(object_name), "", "")
    if "pos" in ec:  # where the calibrated environment camera stands
        q = np.zeros(4)
        mujoco.mju_mat2Quat(q, (np.array(ec["R"]) @ np.diag([1, -1, -1])).ravel())  # OpenCV -> MuJoCo axes
        spec.worldbody.add_camera(name="envcam", pos=ec["pos"], quat=q, fovy=ec["fovy_used"])
    model = spec.compile()
    data = mujoco.MjData(model)
    arms = connect_arms(cfg, names, model)
    print("the arms are limp now (torque off): move them by hand, watch the sim. Close the sim window to stop.")
    try:
        with mujoco.viewer.launch_passive(model, data) as viewer:
            viewer.opt.flags[mujoco.mjtVisFlag.mjVIS_CAMERA] = 1  # draw the environment camera
            while viewer.is_running():
                copy_arms(arms, data)
                mujoco.mj_kinematics(model, data)
                viewer.sync()
                time.sleep(0.03)
    finally:
        for a in arms:
            a["arm"].disconnect(torque_off=True)


def identify(cfg, seconds=20):
    """Which physical arm is which: move one arm by hand, it is named (raw motor positions, torque untouched)."""
    from arm import SOArm
    root, plugged = CONFIG.parent.parent, boards()
    arms = {}
    for n, a in cfg["arms"].items():
        if a["serial"] in plugged:
            arm = SOArm(plugged[a["serial"]], root / a["calibration"], cfg["baudrate"], serial=a["serial"])
            arm.connect()
            arms[n] = (arm, arm.read_raw())
    print(f"move ONE arm by hand (any joint) for a moment; watching for {seconds} s ...")
    t_end, last = time.time() + seconds, None
    try:
        while time.time() < t_end:
            moved = {n: max(abs(v - start[m]) for m, v in arm.read_raw().items()) for n, (arm, start) in arms.items()}
            name = max(moved, key=moved.get)
            if moved[name] > 60 and name != last:  # ~5 deg on some joint
                print(f"   you are moving '{name}' (board {cfg['arms'][name]['serial']})"
                      + ("  <- the ACTIVE arm" if cfg.get("active_arm") == name else ""))
                last = name
            time.sleep(0.1)
    finally:
        for arm, _ in arms.values():
            arm.disconnect(torque_off=False)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("action", choices=["list", "add", "use", "remove", "view", "identify"])
    ap.add_argument("name", nargs="?")
    ap.add_argument("--model", choices=MODELS, default="so101")
    ap.add_argument("--spacing", type=float, default=0.45, help="view: m between the arms (until measured)")
    ap.add_argument("--object", default="lego_tree", help="view: the object in the scene ('' for none)")
    args = ap.parse_args()
    cfg = load()
    if args.action == "list":
        return show(cfg)
    if args.action == "view":
        return view(cfg, args.spacing, args.object)
    if args.action == "identify":
        return identify(cfg)
    if not args.name:
        raise SystemExit(f"{args.action} needs an arm name")
    if args.action == "add":
        add(cfg, args.name, args.model)
    elif args.name not in cfg["arms"]:
        raise SystemExit(f"no arm called '{args.name}' ({', '.join(cfg['arms']) or 'none registered'})")
    elif args.action == "use":
        use(cfg, args.name)
    else:
        del cfg["arms"][args.name]
        if cfg.get("active_arm") == args.name:
            cfg.pop("active_arm")
        save(cfg)
        print(f"removed '{args.name}' (its calibration files stay in robot/calibration/)")


if __name__ == "__main__":
    main()
