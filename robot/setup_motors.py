"""Step 1 (once per arm): give each servo its ID and the bus speed, one servo at a time (like lerobot-setup-motors).

    python robot/setup_motors.py

New servos all come with the same ID, so they must be set up one by one: connect ONLY the servo being set up
to the driver board, it gets found at whatever ID/speed it has, then gets its ID (gripper=6 ... shoulder_pan=1)
and 1 Mbps. Afterwards daisy-chain them all as in the assembly guide.
"""
import json
from pathlib import Path

import scservo_sdk as scs

from arm import BAUD_CODES, MOTORS, REG

CONFIG = Path(__file__).parent / "config.json"


def find_single_servo(port, packet):
    """Scan every speed and ID; return (baudrate, id) of the one servo connected."""
    for baud in BAUD_CODES:
        port.setBaudRate(baud)
        found = [i for i in range(scs.MAX_ID + 1) if packet.ping(port, i)[1] == scs.COMM_SUCCESS]
        if len(found) > 1:
            raise RuntimeError(f"{len(found)} servos answer at {baud} baud: connect only ONE servo")
        if found:
            return baud, found[0]
    raise RuntimeError("no servo found: check that it is plugged in and the board is powered")


def write1(port, packet, servo_id, name, value):
    result, _ = packet.write1ByteTxRx(port, servo_id, REG[name][0], value)
    if result != scs.COMM_SUCCESS:
        raise IOError(f"writing {name} to servo {servo_id} failed: {packet.getTxRxResult(result)}")


def main():
    cfg = json.loads(CONFIG.read_text())
    target_baud = cfg["baudrate"]
    port, packet = scs.PortHandler(cfg["port"]), scs.PacketHandler(0)
    if not port.openPort():
        raise ConnectionError(f"cannot open {cfg['port']} (run robot/find_port.py first)")
    try:
        for motor, target_id in reversed(MOTORS.items()):  # gripper (6) first, like LeRobot
            input(f"Connect the controller board to the '{motor}' motor only and press enter.")
            try:
                baud, current_id = find_single_servo(port, packet)
            except RuntimeError as e:
                raise SystemExit(f"{e}\nCheck: power supply, USB cable, the 3-pin cable from the board to the motor; "
                                 "on a Waveshare board both jumpers must be on the B channel (USB).")
            write1(port, packet, current_id, "Torque_Enable", 0)
            write1(port, packet, current_id, "Lock", 0)  # unlock EEPROM
            if current_id != target_id:
                write1(port, packet, current_id, "ID", target_id)
            if baud != target_baud:
                write1(port, packet, target_id, "Baud_Rate", BAUD_CODES[target_baud])
            port.setBaudRate(target_baud)
            if packet.ping(port, target_id)[1] != scs.COMM_SUCCESS:
                raise IOError(f"'{motor}' does not answer as id {target_id} at {target_baud} baud")
            print(f"'{motor}' motor id set to {target_id}")
        print("\nAll 6 motors set up. Daisy-chain them: each motor to the next, and the 'shoulder_pan' motor "
              "(id 1) to the controller board.")
    finally:
        port.closePort()


if __name__ == "__main__":
    main()
