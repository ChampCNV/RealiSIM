"""Minimal driver for the SO-100 / SO-101 follower arm (6 Feetech STS3215 servos, IDs 1-6).

Follows LeRobot's SO follower (lerobot/robots/so_follower) but without its framework:
  - same motor names/IDs, same motor settings (PID 16/0/32, gripper torque limits)
  - same calibration procedure (half-turn homing + recorded ranges) and the same calibration file format
  - positions in degrees from the middle of each joint's range (LeRobot's DEGREES mode)
Uses Feetech's official SDK (`pip install feetech-servo-sdk`), which works on Python 3.10.
"""
import json
import sys
import time
from pathlib import Path

import scservo_sdk as scs

MOTORS = {"shoulder_pan": 1, "shoulder_lift": 2, "elbow_flex": 3, "wrist_flex": 4, "wrist_roll": 5, "gripper": 6}
FULL_TURN_MOTORS = ["wrist_roll"]  # can spin freely, gets the whole encoder range
RESOLUTION = 4096  # 12-bit encoder
MAX_POS = RESOLUTION - 1

# STS3215 control table: name -> (address, bytes). From LeRobot's feetech tables.
REG = {
    "ID": (5, 1), "Baud_Rate": (6, 1), "Return_Delay_Time": (7, 1), "Min_Position_Limit": (9, 2), "Max_Position_Limit": (11, 2),
    "Max_Torque_Limit": (16, 2), "P_Coefficient": (21, 1), "D_Coefficient": (22, 1), "I_Coefficient": (23, 1),
    "Protection_Current": (28, 2), "Homing_Offset": (31, 2), "Operating_Mode": (33, 1), "Overload_Torque": (36, 1),
    "Torque_Enable": (40, 1), "Acceleration": (41, 1), "Goal_Position": (42, 2), "Lock": (55, 1),
    "Present_Position": (56, 2), "Maximum_Acceleration": (85, 1),
}
SIGN_BIT = {"Homing_Offset": 11, "Present_Position": 15}  # sign-magnitude registers

# Middle-of-range pose for calibration, from LeRobot's so_follower.py (Apache-2.0)
HOMING_POSITION_DIAGRAM = r"""
       ╭─────┬────────────────────┬──────╮ ◉╲═════╗   ← moveable claw
       │     │      forearm       │  ▤▤  │╤══╲════╝   ← fixed claw
       ╰┬───┬┴────────────────────┴──────┴┴═══════╝
        │   │
        │   │   upper arm
        │   │
   ╭────┴───┴────╮
   │   base ◉    │
   ╰──┬───────┬──╯
 ═══════════════════════════════════════════════════
                table edge
"""
BAUD_CODES = {1_000_000: 0, 500_000: 1, 250_000: 2, 128_000: 3, 115_200: 4, 57_600: 5, 38_400: 6, 19_200: 7}


def _encode(name, value):
    bit = SIGN_BIT.get(name)
    return (1 << bit) | -value if bit is not None and value < 0 else value


def _decode(name, value):
    bit = SIGN_BIT.get(name)
    return -(value & ((1 << bit) - 1)) if bit is not None and value & (1 << bit) else value


def read_key():
    """A key pressed in the terminal, or None (non-blocking, no Enter needed). Enter comes back as "\\r"."""
    if sys.platform == "win32":
        import msvcrt
        return msvcrt.getwch() if msvcrt.kbhit() else None
    import select
    if select.select([sys.stdin], [], [], 0)[0]:
        key = sys.stdin.read(1)
        return "\r" if key == "\n" else key
    return None


def enter_pressed():
    return read_key() == "\r"


class SOArm:
    def __init__(self, port, calibration_path, baudrate=1_000_000):
        self.port, self.baudrate = port, baudrate
        self.calibration_path = Path(calibration_path)
        self.calibration = json.loads(self.calibration_path.read_text()) if self.calibration_path.exists() else None

    # ── connection ─────────────────────────────────────────────────────────
    def connect(self):
        self.port_h = scs.PortHandler(self.port)
        self.packet = scs.PacketHandler(0)  # STS series: little-endian protocol
        if not self.port_h.openPort() or not self.port_h.setBaudRate(self.baudrate):
            raise ConnectionError(f"cannot open {self.port} at {self.baudrate} baud")
        missing = [m for m, i in MOTORS.items() if self.packet.ping(self.port_h, i)[1] != scs.COMM_SUCCESS]
        if missing:
            raise ConnectionError(f"motors not answering on {self.port}: {missing}")

    def disconnect(self, torque_off=True):
        if torque_off:
            try:
                self.torque(False)
            except IOError as e:  # never crash while shutting down; the arm just stays as it is
                print(f"warning: could not turn torque off cleanly ({e})")
        self.port_h.closePort()

    # ── single register access ─────────────────────────────────────────────
    # Feetech buses occasionally drop or corrupt a reply ("There is no status packet!", "Incorrect status
    # packet!"); LeRobot retries for the same reason. EEPROM registers (addresses below 40) also get a short
    # pause after writing so the servo can store them before the next command.
    RETRIES = 4

    def _retry(self, what, fn):
        """Run an SDK call (writes return (result, error), reads return (value, result, error)) with retries."""
        for attempt in range(self.RETRIES + 1):
            out = fn()
            result = out[1] if len(out) == 3 else out[0]
            if result == scs.COMM_SUCCESS:
                return out
            self.port_h.clearPort()
            time.sleep(0.01 * (attempt + 1))
        raise IOError(f"{what} failed after {self.RETRIES + 1} tries: {self.packet.getTxRxResult(result)} "
                      "(check the 3-pin cables and the power supply)")

    def write(self, name, motor, value):
        addr, size = REG[name]
        fn = self.packet.write1ByteTxRx if size == 1 else self.packet.write2ByteTxRx
        self._retry(f"write {name}={value} to {motor}",
                    lambda: fn(self.port_h, MOTORS[motor], addr, _encode(name, int(value))))
        if addr < REG["Torque_Enable"][0]:
            time.sleep(0.01)

    def read(self, name, motor):
        addr, size = REG[name]
        fn = self.packet.read1ByteTxRx if size == 1 else self.packet.read2ByteTxRx
        value, _, _ = self._retry(f"read {name} from {motor}", lambda: fn(self.port_h, MOTORS[motor], addr))
        return _decode(name, value)

    # ── all motors at once ─────────────────────────────────────────────────
    def read_raw(self, retries=4):
        """Present position of every motor, raw encoder steps (0-4095)."""
        addr, size = REG["Present_Position"]
        for attempt in range(retries + 1):
            group = scs.GroupSyncRead(self.port_h, self.packet, addr, size)
            for i in MOTORS.values():
                group.addParam(i)
            if group.txRxPacket() == scs.COMM_SUCCESS:
                return {m: _decode("Present_Position", group.getData(i, addr, size)) for m, i in MOTORS.items()}
            self.port_h.clearPort()
            time.sleep(0.01)
        raise IOError("sync read of Present_Position failed (check the 3-pin cables and the power supply)")

    def write_raw(self, goals):
        addr, size = REG["Goal_Position"]
        group = scs.GroupSyncWrite(self.port_h, self.packet, addr, size)
        for m, v in goals.items():
            v = int(v)
            group.addParam(MOTORS[m], [scs.SCS_LOBYTE(v), scs.SCS_HIBYTE(v)])
        group.txPacket()

    def torque(self, enabled):
        for m in MOTORS:  # like LeRobot: EEPROM lock follows torque
            self.write("Torque_Enable", m, int(enabled))
            self.write("Lock", m, int(enabled))

    # ── degrees (LeRobot DEGREES mode: 0 = middle of the recorded range) ──
    def _mid(self, m):
        c = self.calibration[m]
        return (c["range_min"] + c["range_max"]) / 2

    def read_degrees(self):
        return {m: (raw - self._mid(m)) * 360 / MAX_POS for m, raw in self.read_raw().items()}

    def write_degrees(self, goals):
        raw = {}
        for m, deg in goals.items():
            c = self.calibration[m]
            raw[m] = min(c["range_max"], max(c["range_min"], deg * MAX_POS / 360 + self._mid(m)))
        self.write_raw(raw)

    # ── setup ──────────────────────────────────────────────────────────────
    def configure(self, p=16, i=0, d=32):
        """Motor settings LeRobot writes on connect (its PID defaults: 16/0/32; set yours in config.json "pid").
        Torque stays off."""
        self.torque(False)
        for m in MOTORS:
            self.write("Return_Delay_Time", m, 0)
            self.write("Maximum_Acceleration", m, 254)
            self.write("Acceleration", m, 254)
            self.write("Operating_Mode", m, 0)  # position mode
            self.write("P_Coefficient", m, p)
            self.write("I_Coefficient", m, i)
            self.write("D_Coefficient", m, d)
        self.write("Max_Torque_Limit", "gripper", 500)  # 50%: avoids burning out the gripper motor
        self.write("Protection_Current", "gripper", 250)
        self.write("Overload_Torque", "gripper", 25)

    # ── calibration, in LeRobot's steps (calibrate.py runs them with the simulation alongside) ──
    def start_calibration(self):
        """Torque off (you move the arm by hand), position mode."""
        self.torque(False)
        for m in MOTORS:
            self.write("Operating_Mode", m, 0)

    def set_middle(self):
        """Call with every joint in the middle of its range: that pose becomes half a turn (2047) on each motor."""
        for m in MOTORS:  # clear old calibration so we read true positions
            self.write("Homing_Offset", m, 0)
            self.write("Min_Position_Limit", m, 0)
            self.write("Max_Position_Limit", m, MAX_POS)
        pos = self.read_raw()
        offsets = {m: pos[m] - MAX_POS // 2 for m in MOTORS}  # Feetech: present = actual - offset
        for m, off in offsets.items():
            self.write("Homing_Offset", m, off)
        return offsets

    def finish_calibration(self, offsets, lo, hi):
        """Store the recorded ranges (full-turn joints get the whole encoder), write limits, save the file."""
        lo, hi = dict(lo), dict(hi)
        for m in FULL_TURN_MOTORS:
            lo[m], hi[m] = 0, MAX_POS
        if bad := [m for m in MOTORS if lo[m] == hi[m]]:
            raise ValueError(f"these joints were not moved: {bad}")
        self.calibration = {m: dict(id=MOTORS[m], drive_mode=0, homing_offset=int(offsets[m]),
                                    range_min=int(lo[m]), range_max=int(hi[m])) for m in MOTORS}
        for m, c in self.calibration.items():
            self.write("Min_Position_Limit", m, c["range_min"])
            self.write("Max_Position_Limit", m, c["range_max"])
        self.calibration_path.parent.mkdir(parents=True, exist_ok=True)
        self.calibration_path.write_text(json.dumps(self.calibration, indent=2))
        print(f"calibration saved to {self.calibration_path}")
