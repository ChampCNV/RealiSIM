"""Watch a trained policy control the SO-100 live in the MuJoCo viewer.

    python sim2real/play_policy.py runs/<run>/actor_best.pt --object lego_tree

The policy still only sees the 16x16 camera image + joint angles; the viewer is just for you.
"""
import argparse
import time

import mujoco
import mujoco.viewer
import torch

from lift_env import LiftEnv
from train_sac import Actor, to_img


def main():
    p = argparse.ArgumentParser()
    p.add_argument("checkpoint")
    p.add_argument("--object", default="lego_tree")
    p.add_argument("--episodes", type=int, default=20)
    p.add_argument("--speed", type=float, default=1.0, help="1.0 = real time")
    p.add_argument("--randomize", action="store_true", help="random lighting/colours/physics as in training")
    args = p.parse_args()

    ckpt = torch.load(args.checkpoint, map_location="cpu")
    cam, size = ckpt["args"].get("camera", "third"), ckpt["args"].get("image_size", 16)  # older runs: third/16
    start = ckpt["args"].get("start", "home")  # runs before the zero-pose change started from "home"
    env = LiftEnv(args.object, seed=123, camera=cam, image_size=size, start=start,
                  domain_randomization=args.randomize, marker=ckpt["args"].get("marker", False))
    print(f"camera {cam} {size}x{size}, starting from the '{start}' pose")
    actor = Actor(env.state_dim, env.action_dim, size)
    actor.load_state_dict(ckpt["actor"])
    actor.eval()

    successes = 0
    with mujoco.viewer.launch_passive(env.model, env.data) as viewer:
        viewer.cam.lookat[:] = [0, -0.18, 0.05]
        viewer.cam.distance, viewer.cam.azimuth, viewer.cam.elevation = 0.6, 130, -25
        for ep in range(args.episodes):
            o, success = env.reset(), False
            for _ in range(env.max_steps):
                if not viewer.is_running():
                    return
                t = time.time()
                with torch.no_grad():
                    mean, _ = actor(to_img(o["image"][None], "cpu", False), torch.as_tensor(o["state"][None]))
                o, _, _, trunc, info = env.step(torch.tanh(mean)[0].numpy())
                success |= info["success"]
                viewer.sync()
                time.sleep(max(0, env.control_dt / args.speed - (time.time() - t)))
            successes += success
            print(f"episode {ep + 1}: {'SUCCESS' if success else 'fail'}  ({successes}/{ep + 1})", flush=True)
            time.sleep(0.8)


if __name__ == "__main__":
    main()
