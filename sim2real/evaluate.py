"""Strict evaluation of a trained policy + an HD video.

    python sim2real/evaluate.py runs/<run>/actor_best.pt --object lego_tree --episodes 100

Success = held, lifted >= 5 cm, still upright (same as training). Failures are sorted by cause.
"""
import argparse
import collections
import json
from pathlib import Path

import cv2
import mujoco
import numpy as np
import torch

from lift_env import LiftEnv
from train_sac import Actor, to_img


def run(env, actor, frames=None, hd=None, cam=None):
    o = env.reset()
    best_h, flags = 0.0, collections.Counter()
    for _ in range(env.max_steps):
        with torch.no_grad():
            mean, _ = actor(to_img(o["image"][None], "cpu", False), torch.as_tensor(o["state"][None]))
        o, _, _, _, info = env.step(torch.tanh(mean)[0].numpy())
        for k in ("success", "grasped", "knocked_over"):
            flags[k] |= bool(info[k])
        flags["reached"] |= info["tcp_dist"] < 0.03
        if info["grasped"]:
            best_h = max(best_h, info["height"])
        if frames is not None:
            hd.update_scene(env.data, cam)
            img = hd.render().copy()
            img[20:276, -276:-20] = cv2.resize(o["image"], (256, 256), interpolation=cv2.INTER_NEAREST)
            if flags["success"]:
                cv2.putText(img, "SUCCESS", (24, 50), cv2.FONT_HERSHEY_SIMPLEX, 1.1, (90, 230, 90), 2, cv2.LINE_AA)
            frames.append(img)
    if flags["success"]:
        return "success", best_h
    if flags["knocked_over"]:
        return "knocked the object over", best_h
    if flags["grasped"]:
        return "gripped but did not lift 5 cm upright", best_h
    if flags["reached"]:
        return "reached but never gripped", best_h
    return "never reached the object", best_h


def main():
    p = argparse.ArgumentParser()
    p.add_argument("checkpoint")
    p.add_argument("--object", default="lego_tree")
    p.add_argument("--episodes", type=int, default=100)
    p.add_argument("--video-episodes", type=int, default=6)
    args = p.parse_args()

    ck = torch.load(args.checkpoint, map_location="cpu")
    a = ck["args"]
    out = Path(args.checkpoint).parent
    env = LiftEnv(args.object, seed=4242, camera=a["camera"], image_size=a["image_size"], start=a.get("start", "home"),
                  marker=a.get("marker", False))
    actor = Actor(env.state_dim, env.action_dim, a["image_size"])
    actor.load_state_dict(ck["actor"])
    actor.eval()

    results, heights = collections.Counter(), []
    for _ in range(args.episodes):
        outcome, h = run(env, actor)
        results[outcome] += 1
        heights.append(h)
    report = dict(checkpoint=str(args.checkpoint), episodes=args.episodes, outcomes=dict(results.most_common()),
                  median_lift_cm=round(float(np.median(heights)) * 100, 1))
    print(json.dumps(report, indent=2))
    (out / f"eval_{Path(args.checkpoint).stem}.json").write_text(json.dumps(report, indent=2))

    # HD video, nominal (non-randomized) visuals so it looks clean
    vid_env = LiftEnv(args.object, seed=7, camera=a["camera"], image_size=a["image_size"],
                      start=a.get("start", "home"), domain_randomization=False, marker=a.get("marker", False))
    vid_env.model.vis.global_.offwidth, vid_env.model.vis.global_.offheight = 1280, 720
    hd = mujoco.Renderer(vid_env.model, 720, 1280)
    cam = mujoco.MjvCamera()
    cam.lookat[:] = [0.0, -0.17, 0.08]
    cam.distance, cam.azimuth, cam.elevation = 0.6, 125, -22
    vw = cv2.VideoWriter(str(out / f"video_{Path(args.checkpoint).stem}.mp4"), cv2.VideoWriter_fourcc(*"mp4v"),
                         10, (1280, 720))
    for _ in range(args.video_episodes):
        frames = []
        run(vid_env, actor, frames, hd, cam)
        for f in frames + frames[-1:] * 5:
            vw.write(cv2.cvtColor(f, cv2.COLOR_RGB2BGR))
    vw.release()
    print(f"video: {out / f'video_{Path(args.checkpoint).stem}.mp4'}")


if __name__ == "__main__":
    main()
