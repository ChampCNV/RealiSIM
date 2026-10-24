"""Step 0: find the arm's USB port (like lerobot-find-port) and list cameras; saves the port to config.json.

    python robot/find_port.py

Works before the servos have IDs: it compares the serial ports before and after you unplug the board's USB
cable. Bluetooth serial links are ignored.
"""
import json
from pathlib import Path

from serial.tools import list_ports

from camera import list_cameras

CONFIG = Path(__file__).parent / "config.json"


def usb_ports():
    return {p.device for p in list_ports.comports()
            if "BTHENUM" not in (p.hwid or "") and "bluetooth" not in (p.description or "").lower()}


if __name__ == "__main__":
    cfg = json.loads(CONFIG.read_text())
    before = usb_ports()
    print(f"Finding all available ports for the MotorBus.\n{sorted(before)}")
    if not before:
        raise SystemExit("No USB serial port at all: check the board's power, the USB cable (some are charge-only) "
                         "and the CH340/CH343 driver (Device Manager > Ports).")
    input("Remove the USB cable from your MotorsBus and press Enter when done.")
    gone = before - usb_ports()
    if len(gone) != 1:
        raise SystemExit(f"Could not tell which port it is (ports that disappeared: {sorted(gone)}). Try again.")
    port = gone.pop()
    print(f"The port of this MotorsBus is {port}\nReconnect the USB cable.")
    cfg["port"] = port
    CONFIG.write_text(json.dumps(cfg, indent=2))
    print(f"saved to {CONFIG}")
    print(f"cameras found at indices: {list_cameras()}  (pick the wrist one in robot/camera_setup.py)")
