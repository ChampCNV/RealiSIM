"""Several arms: each one is known by its USB board's serial number, so Windows renumbering COM ports never
mixes them up. Every arm has its own calibration files; one arm is "active" at a time, and every robot script
(setup_motors, calibrate, gripper_check, goto_pose, run, ...) works on the active arm.

    python robot/arms.py list                 # the arms, which is active, which are plugged in
    python robot/arms.py add left             # register a new arm: plug in its board when asked
    python robot/arms.py add right --model so101
    python robot/arms.py use right            # make it the active arm
    python robot/arms.py remove right
    python robot/arms.py view                 # every plugged-in arm side by side in the sim, copying the real ones

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


ROBOT_XML = {"so101": ("robotstudio_so101/so101.xml", "base"), "so100": ("trs_so_arm100/so_arm100.xml", "Base")}


def view(cfg, spacing=0.45):
    """Every plugged-in arm in one sim, side by side, copying the real arms (torque off: move them by hand).
    Calibrated arms are copied exactly; uncalibrated ones from their raw motor positions (motor middle = joint
    middle, as in a standard assembly), good enough to see each joint move."""
    import mujoco
    import mujoco.viewer
    import numpy as np

    from arm import MAX_POS, SOArm
    from sim_map import MENAGERIE, SIM_JOINTS, SimMap, load_sim

    root = CONFIG.parent.parent
    plugged = boards()
    names = [n for n, a in cfg["arms"].items() if a["serial"] in plugged]
    if not names:
        raise SystemExit("no registered arm is plugged in (python robot/arms.py list)")
    spec = mujoco.MjSpec()
    spec.worldbody.add_light(pos=[0, -0.5, 1.5], dir=[0, 0.3, -1], diffuse=[0.8] * 3)
    spec.worldbody.add_geom(type=mujoco.mjtGeom.mjGEOM_PLANE, size=[1, 1, 0.01], rgba=[0.35, 0.38, 0.42, 1])
    for i, n in enumerate(names):
        xml, base = ROBOT_XML[cfg["arms"][n]["model"]]
        child = mujoco.MjSpec.from_file(str(MENAGERIE / xml))
        for k in ("timestep", "iterations", "ls_iterations", "impratio", "integrator", "cone"):
            setattr(spec.option, k, getattr(child.option, k))  # same settings: no attach warnings
        x = (i - (len(names) - 1) / 2) * spacing
        spec.worldbody.add_frame(pos=[x, 0, 0]).attach_body(child.body(base), f"{n}_", "")
    model = spec.compile()
    data = mujoco.MjData(model)
    arms = []
    for n in names:
        a = cfg["arms"][n]
        arm = SOArm(plugged[a["serial"]], root / a["calibration"], cfg["baudrate"], serial=a["serial"])
        arm.connect()
        arm.torque(False)
        joints = SIM_JOINTS[a["model"]]
        qadr = [model.jnt_qposadr[model.joint(f"{n}_{joints[m]}").id] for m in joints]
        mapping = SimMap(root / a["sim_mapping"], arm.calibration) if arm.calibration else None
        if mapping is not None and not mapping.ready:
            mapping = None
        sim, _ = load_sim(a["model"])
        middle = (sim.jnt_range[:6, 0] + sim.jnt_range[:6, 1]) / 2
        arms.append((n, arm, joints, qadr, mapping, middle))
        print(f"{n}: {'calibrated: copied exactly' if mapping else 'NOT calibrated: raw motor positions'}")
    print("both arms are limp now (torque off): move them by hand, watch the sim. Close the sim window to stop.")
    try:
        with mujoco.viewer.launch_passive(model, data) as viewer:
            while viewer.is_running():
                for n, arm, joints, qadr, mapping, middle in arms:
                    if mapping is not None:
                        q = mapping.to_sim(arm.read_degrees())
                    else:
                        raw = arm.read_raw()
                        q = middle + np.radians([(raw[m] - MAX_POS / 2) * 360 / (MAX_POS + 1) for m in joints])
                    data.qpos[qadr] = q
                mujoco.mj_kinematics(model, data)
                viewer.sync()
                time.sleep(0.03)
    finally:
        for _, arm, *_ in arms:
            arm.disconnect(torque_off=True)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("action", choices=["list", "add", "use", "remove", "view"])
    ap.add_argument("name", nargs="?")
    ap.add_argument("--model", choices=MODELS, default="so101")
    args = ap.parse_args()
    cfg = load()
    if args.action == "list":
        return show(cfg)
    if args.action == "view":
        return view(cfg)
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
