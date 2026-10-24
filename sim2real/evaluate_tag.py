"""Test an AprilTag policy in simulation before it goes on the real robot.

    python sim2real/evaluate_tag.py runs/tag_.../actor_last.pt [--episodes 100] [--env-only]

Same object, placement area and look pose as in training (read from the checkpoint), full randomization.
Prints and saves (eval_<checkpoint>.json next to it) how many attempts picked the object up, knocked it over,
or missed it. Rule of thumb: >= 80 % in sim before trying the real robot.
"""
import argparse
import collections
import json
from pathlib import Path

import torch

from tag_env import OLD_POLICY_CLEARANCE, TagLiftEnv
from train_tag import Actor


def evaluate(checkpoint, episodes=100, env_only=False, seed=2024):
    ck = torch.load(checkpoint, map_location="cpu", weights_only=False)
    actor = Actor(ck["state_dim"], ck["action_dim"])
    actor.load_state_dict(ck["actor"])
    actor.eval()
    env = TagLiftEnv(ck.get("object", "lego_tree"), seed=seed, start=ck["start"], area=ck.get("area"))
    env.min_camera_clearance = ck.get("min_camera_clearance", OLD_POLICY_CLEARANCE)  # the rule it was trained with
    if env_only:
        env.detect_wrist = lambda: None
    out = collections.Counter()
    for _ in range(episodes):
        o, knocked, grasped = env.reset(), False, False
        for _ in range(env.max_steps):
            with torch.no_grad():
                a = actor.act(torch.as_tensor(o["state"][None]))[0].numpy()
            o, _, _, _, info = env.step(a)
            knocked |= info["knocked_over"]
            grasped |= info["grasped"]
        out["picked up" if info["success"] else "knocked over" if knocked else
            "grasped, not lifted" if grasped else "missed"] += 1
    report = dict(checkpoint=str(checkpoint), object=ck.get("object", "lego_tree"), env_only=env_only,
                  episodes=episodes, outcomes=dict(out.most_common()),
                  success_rate=round(out["picked up"] / episodes, 3))
    Path(checkpoint).with_name(f"eval_{Path(checkpoint).stem}{'_envonly' if env_only else ''}.json").write_text(
        json.dumps(report, indent=2))
    return report


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("checkpoint")
    ap.add_argument("--episodes", type=int, default=100)
    ap.add_argument("--env-only", action="store_true", help="environment camera only (wrist camera off)")
    args = ap.parse_args()
    r = evaluate(args.checkpoint, args.episodes, args.env_only)
    print(f"{r['object']}: picked up {100 * r['success_rate']:.0f}% of {r['episodes']} attempts | {r['outcomes']}")
    print("ready for the real robot" if r["success_rate"] >= 0.8 else "below 80 %: train longer before the real robot")


if __name__ == "__main__":
    main()
