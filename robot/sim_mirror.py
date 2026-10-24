"""Safe first test: the policy grabs the object IN SIMULATION, the real arm copies the sim arm slowly.

    python robot/sim_mirror.py runs/<run>/actor_best.pt --object lego_tree
    python robot/sim_mirror.py runs/<run>/actor_best.pt --playback 0.25 --max-speed 20

No real camera or object needed. The sim attempt is recorded first (retrying until one succeeds), then the
sim (viewer) and the real arm replay it together: slowed down (--playback), interpolated at 50 Hz, and every
joint limited to --max-speed deg/s. Start and end are slow eased moves; Ctrl+C folds the arm, then torque off.
"""
import argparse
import json
import sys
import time
from pathlib import Path

import mujoco
import mujoco.viewer
import numpy as np
import torch

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "sim2real"))
from lift_env import LiftEnv, start_pose  # noqa: E402
from train_sac import Actor, to_img  # noqa: E402

from arm import SOArm  # noqa: E402
from sim_map import SimMap, sim_pose  # noqa: E402

RATE = 50  # Hz of commands to the real arm


def record_attempt(env, actor, tries):
    """Run the policy in sim until an attempt succeeds; return the full sim state of every control step."""
    for t in range(1, tries + 1):
        o = env.reset()
        states, success = [env.data.qpos.copy()], False
        for _ in range(env.max_steps):
            with torch.no_grad():
                mean, _ = actor(to_img(o["image"][None], "cpu", False), torch.as_tensor(o["state"][None]))
            o, _, _, _, info = env.step(torch.tanh(mean)[0].numpy())
            states.append(env.data.qpos.copy())
            success |= info["success"]
        print(f"sim attempt {t}: {'SUCCESS' if success else 'fail'}")
        if success:
            return np.array(states)
    print("no successful attempt; replaying the last one anyway")
    return np.array(states)


class PreviewArm:
    """Stands in for the real arm in --preview: remembers the commanded joints, starts folded at rest."""

    calibration = {}

    def __init__(self, mapping, start_qpos):
        self.deg = mapping.to_real(start_qpos)

    def read_degrees(self):
        return dict(self.deg)

    def write_degrees(self, deg):
        self.deg = dict(deg)

    def connect(self): pass
    def configure(self, **pid): pass
    def torque(self, on): pass
    def disconnect(self, torque_off=True): pass


class Mirror:
    """Sends sim joint angles to the real arm, never faster than max_speed per joint."""

    def __init__(self, arm, mapping, max_speed_deg, on_command=None):
        self.arm, self.mapping, self.on_command = arm, mapping, on_command
        self.max_step = np.radians(max_speed_deg) / RATE
        self.current = mapping.to_sim(arm.read_degrees())

    def go(self, goal):
        self.current = self.current + np.clip(goal - self.current, -self.max_step, self.max_step)
        self.arm.write_degrees(self.mapping.to_real(self.current))
        if self.on_command:
            self.on_command(self.current)  # show the arm exactly as commanded

    def ease_to(self, goal, min_seconds=3.0):
        """Smooth move to `goal`. Duration grows with distance, so the speed limit is never what stops it
        (the eased curve peaks at ~1.6x its average speed; aim for ~60% of the limit at the peak)."""
        start = self.current.copy()
        biggest = np.abs(goal - start).max()
        seconds = max(min_seconds, 1.6 * biggest / (0.6 * self.max_step * RATE))
        print(f"   moving {np.degrees(biggest):.0f} deg (largest joint) over {seconds:.1f} s")
        for s in np.linspace(0, 1, int(seconds * RATE)):
            w = 0.5 - 0.5 * np.cos(np.pi * s)  # smooth start and stop
            self.go(start + (goal - start) * w)
            time.sleep(1 / RATE)
        while np.abs(goal - self.current).max() > 1e-4:  # finish any remainder, still speed-limited
            self.go(goal)
            time.sleep(1 / RATE)


def main():
    p = argparse.ArgumentParser()
    p.add_argument("checkpoint")
    p.add_argument("--object", default="lego_tree")
    p.add_argument("--playback", type=float, default=0.25, help="replay speed vs real time (0.25 = 4x slower)")
    p.add_argument("--max-speed", type=float, default=20.0, help="max joint speed on the real arm, deg/s")
    p.add_argument("--tries", type=int, default=10, help="sim attempts to find a successful one")
    p.add_argument("--preview", action="store_true",
                   help="no robot: show in the viewer exactly the motion the real arm would make")
    args = p.parse_args()

    cfg = json.loads((Path(__file__).parent / "config.json").read_text())
    if args.preview:
        mapping = SimMap(ROOT / cfg["sim_mapping"])
    else:
        arm = SOArm(cfg["port"], ROOT / cfg["calibration"], cfg["baudrate"])
        mapping = SimMap(ROOT / cfg["sim_mapping"], arm.calibration)
        if arm.calibration is None or not mapping.ready:
            sys.exit("Run robot/calibrate.py (option 1: joint limits + directions) first.")

    ck = torch.load(args.checkpoint, map_location="cpu")
    a = ck["args"]
    env = LiftEnv(args.object, seed=11, camera=a["camera"], image_size=a["image_size"],
                  start=a.get("start", "home"), domain_randomization=False, marker=a.get("marker", False))
    actor = Actor(env.state_dim, env.action_dim, a["image_size"])
    actor.load_state_dict(ck["actor"])
    actor.eval()
    print("recording the attempt in simulation...")
    states = record_attempt(env, actor, args.tries)
    arm_q = states[:, env.qadr]

    model, data = env.model, env.data
    rest = sim_pose(model, "rest")
    if args.preview:
        arm = PreviewArm(mapping, rest)
    scene = {"state": states[0]}  # object pose shown in the viewer; the arm is drawn as commanded

    with mujoco.viewer.launch_passive(model, data) as viewer:
        def show(arm_qpos):
            data.qpos[:] = scene["state"]
            data.qpos[env.qadr] = arm_qpos
            mujoco.mj_forward(model, data)
            viewer.sync()

        arm.connect()
        arm.configure(**cfg.get("pid", {}))
        arm.torque(True)
        mirror = Mirror(arm, mapping, args.max_speed, on_command=show)
        show(mirror.current)
        def pause(message):
            if args.preview:
                print(message.split(".")[0] + ".")
                time.sleep(1.5)
            else:
                input(message)

        zero = start_pose(model, "zero")
        try:
            print("moving the arm slowly to the zero pose (all joints at the middle of their range)...")
            mirror.ease_to(zero)
            pause("Arm is at the zero pose. Press ENTER to continue (Ctrl+C stops)...")
            if np.abs(arm_q[0] - zero).max() > 1e-3:
                print(f"moving slowly from zero to this policy's start pose ('{a.get('start', 'home')}')...")
                mirror.ease_to(arm_q[0])
            pause("Arm is at the start pose. Press ENTER to replay the grab slowly (Ctrl+C stops)...")
            print(f"replaying the grab at {args.playback:.2f}x speed...")
            frame_time = env.control_dt / args.playback
            for k in range(len(states) - 1):
                for s in np.linspace(0, 1, max(1, int(frame_time * RATE)), endpoint=False):
                    scene["state"] = states[k] + (states[k + 1] - states[k]) * s
                    mirror.go(arm_q[k] + (arm_q[k + 1] - arm_q[k]) * s)
                    time.sleep(1 / RATE)
            print("done; easing back to the start pose, then to zero")
            mirror.ease_to(arm_q[0])
            mirror.ease_to(zero)
        except KeyboardInterrupt:
            print("\nstopped by user")
        finally:
            print("folding the arm to rest, then torque off...")
            try:
                mirror.ease_to(rest)
            finally:
                arm.disconnect(torque_off=True)
        if args.preview:
            print("preview finished; close the viewer window")
            while viewer.is_running():
                time.sleep(0.1)


if __name__ == "__main__":
    main()
