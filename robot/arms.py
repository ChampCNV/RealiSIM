"""Several arms: each one is known by its USB board's serial number, so Windows renumbering COM ports never
mixes them up. Every arm has its own calibration files; one arm is "active" at a time, and every robot script
(setup_motors, calibrate, gripper_check, goto_pose, run, ...) works on the active arm.

    python robot/arms.py list                 # the arms, which is active, which are plugged in
    python robot/arms.py add left             # register a new arm: plug in its board when asked
    python robot/arms.py add right --model so101
    python robot/arms.py use right            # make it the active arm
    python robot/arms.py remove right

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


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("action", choices=["list", "add", "use", "remove"])
    ap.add_argument("name", nargs="?")
    ap.add_argument("--model", choices=MODELS, default="so101")
    args = ap.parse_args()
    cfg = load()
    if args.action == "list":
        return show(cfg)
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
