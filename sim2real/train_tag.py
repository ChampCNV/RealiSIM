"""SAC for the AprilTag lift task (no images: small MLPs, runs on a CPU, e.g. a Colab CPU runtime).

    python sim2real/train_tag.py --steps 600000
    python sim2real/train_tag.py --object celsius_can --area wide --init runs/tag_envcam_2250k   # new object,
                                                         # starting from a trained model (learns much faster)

Actor: policy input from tag_env.tag_state (joints, targets, tag estimate, seen flags).
Critic: same + privileged sim state (asymmetric actor-critic, as in train_sac.py).
Writes runs/tag_<time>/: actor_best.pt, actor_last.pt, log.csv.
"""
import argparse
import csv
import time
from pathlib import Path

import numpy as np
import torch
import torch.nn as nn
import torch.nn.functional as F

from find_look_pose import look_pose
from tag_env import (BACK_TAG_ID, DEFAULT_AREA, ENV_CAMERA, SCAN_FRAMES, SCAN_TURNS, SETUP_VERSION, TAG, WEBCAM,
                     WIDE_AREA, TagLiftEnv, check_object)

ROOT = Path(__file__).resolve().parent.parent


def mlp(i, o, h=256):
    return nn.Sequential(nn.Linear(i, h), nn.ReLU(), nn.Linear(h, h), nn.ReLU(), nn.Linear(h, h), nn.ReLU(),
                         nn.Linear(h, o))


class Actor(nn.Module):
    def __init__(self, state_dim, action_dim):
        super().__init__()
        self.net = mlp(state_dim, 2 * action_dim)

    def forward(self, s):
        mean, log_std = self.net(s).chunk(2, -1)
        return mean, log_std.clamp(-5, 2)

    def sample(self, s):
        mean, log_std = self(s)
        dist = torch.distributions.Normal(mean, log_std.exp())
        u = dist.rsample()
        a = torch.tanh(u)
        logp = (dist.log_prob(u) - torch.log(1 - a.pow(2) + 1e-6)).sum(-1, keepdim=True)
        return a, logp

    def act(self, s):
        """Deterministic action for evaluation / the real robot."""
        return torch.tanh(self(s)[0])


class Critic(nn.Module):
    def __init__(self, state_dim, priv_dim, action_dim):
        super().__init__()
        self.q1, self.q2 = mlp(state_dim + priv_dim + action_dim, 1), mlp(state_dim + priv_dim + action_dim, 1)

    def forward(self, s, p, a):
        x = torch.cat([s, p, a], -1)
        return self.q1(x), self.q2(x)


class Buffer:
    def __init__(self, size, state_dim, priv_dim, action_dim):
        self.s, self.s2 = np.zeros((size, state_dim), np.float32), np.zeros((size, state_dim), np.float32)
        self.p, self.p2 = np.zeros((size, priv_dim), np.float32), np.zeros((size, priv_dim), np.float32)
        self.a, self.r = np.zeros((size, action_dim), np.float32), np.zeros((size, 1), np.float32)
        self.size, self.i, self.full = size, 0, False

    def add(self, o, a, r, o2):
        i = self.i
        self.s[i], self.p[i], self.a[i], self.r[i], self.s2[i], self.p2[i] = \
            o["state"], o["priv"], a, r, o2["state"], o2["priv"]
        self.i = (i + 1) % self.size
        self.full |= self.i == 0

    def sample(self, n, device):
        idx = np.random.randint(0, self.size if self.full else self.i, n)
        return [torch.as_tensor(x[idx], device=device) for x in (self.s, self.p, self.a, self.r, self.s2, self.p2)]


def evaluate(env, actor, device, episodes):
    succ, ret = 0, 0.0
    for _ in range(episodes):
        o = env.reset()
        for _ in range(env.max_steps):
            with torch.no_grad():
                a = actor.act(torch.as_tensor(o["state"][None], device=device))[0].cpu().numpy()
            o, r, _, _, info = env.step(a)
            ret += r
        succ += info["success"]
    return succ / episodes, ret / episodes


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--object", default="lego_tree")
    p.add_argument("--steps", type=int, default=1_000_000)
    p.add_argument("--num-envs", type=int, default=8)
    p.add_argument("--updates-per-step", type=float, default=1.0, help="gradient updates per env step")
    p.add_argument("--batch", type=int, default=256)
    p.add_argument("--buffer", type=int, default=1_000_000)
    p.add_argument("--learning-starts", type=int, default=10_000)
    p.add_argument("--gamma", type=float, default=0.9)
    p.add_argument("--tau", type=float, default=0.01)
    p.add_argument("--lr", type=float, default=3e-4)
    p.add_argument("--eval-every", type=int, default=25_000)
    p.add_argument("--eval-episodes", type=int, default=30)
    p.add_argument("--seed", type=int, default=0)
    p.add_argument("--out", default=str(ROOT / "runs"))
    p.add_argument("--resume", help="run dir with train_state.pt: continue that run (up to --steps in total)")
    p.add_argument("--area", choices=["default", "wide"], default="default",
                   help="placement area: default 14.5-23.5 cm x +-8 cm, wide 12-28 cm x +-15 cm (from the axis)")
    p.add_argument("--init", help="run dir (train_state.pt): start from its weights, e.g. for a new object")
    p.add_argument("--force", action="store_true", help="train even if the object looks hard to grasp")
    args = p.parse_args()
    problems = check_object(args.object)
    if problems:
        print("object check: " + "; ".join(problems))
        if not args.force:
            raise SystemExit("not training (use --force to try anyway)")
    area = WIDE_AREA if args.area == "wide" else DEFAULT_AREA
    start = look_pose(args.object, area)

    torch.manual_seed(args.seed)
    np.random.seed(args.seed)
    device = torch.device("cuda" if torch.cuda.is_available() else "cpu")
    torch.set_num_threads(max(1, torch.get_num_threads()))
    run_dir = Path(args.resume) if args.resume else Path(args.out) / f"tag_{args.object}_{time.strftime('%Y%m%d_%H%M%S')}"
    run_dir.mkdir(parents=True, exist_ok=True)
    envs = [TagLiftEnv(args.object, seed=args.seed + i, start=start, area=area) for i in range(args.num_envs)]
    eval_env = TagLiftEnv(args.object, seed=10_000 + args.seed, start=start, area=area)
    e0 = envs[0]
    sd, pd, ad = e0.state_dim, e0.priv_dim, e0.action_dim
    actor = Actor(sd, ad).to(device)
    critic, critic_t = Critic(sd, pd, ad).to(device), Critic(sd, pd, ad).to(device)
    critic_t.load_state_dict(critic.state_dict())
    log_alpha = torch.zeros(1, device=device, requires_grad=True)
    opt_a = torch.optim.Adam(actor.parameters(), lr=args.lr)
    opt_c = torch.optim.Adam(critic.parameters(), lr=args.lr)
    opt_al = torch.optim.Adam([log_alpha], lr=args.lr)
    buf = Buffer(args.buffer, sd, pd, ad)
    if args.init and not args.resume:  # same inputs/outputs for every object: reuse what a trained model knows
        st = torch.load(Path(args.init) / "train_state.pt", map_location=device)
        actor.load_state_dict(st["actor"])
        critic.load_state_dict(st["critic"])
        critic_t.load_state_dict(st["critic_t"])
        with torch.no_grad():
            log_alpha.copy_(st["log_alpha"].to(device))
        print(f"starting from the weights of {args.init} (step {st['step']})", flush=True)
    # everything the real robot needs to rebuild the same inputs goes into the checkpoint
    meta = dict(args=vars(args), object=args.object, area=area, tag_height=float(e0.tag_height),
                start=list(map(float, start)), scan_turns=SCAN_TURNS, scan_frames=SCAN_FRAMES,
                webcam=WEBCAM, env_camera=ENV_CAMERA, back_tag_id=BACK_TAG_ID, tag_depth=float(e0.tag_depth),
                tag={k: v for k, v in TAG.items() if k != "dictionary"}, tag_family="DICT_APRILTAG_36h11",
                state_dim=sd, action_dim=ad, action_scale=e0.action_scale.tolist(), control_dt=e0.control_dt,
                max_steps=e0.max_steps)

    def save(path):
        torch.save(dict(actor=actor.state_dict(), **meta), path)

    def save_state(step, best, minutes):
        """Everything needed to continue this run later (the replay buffer refills after a resume)."""
        torch.save(dict(actor=actor.state_dict(), critic=critic.state_dict(), critic_t=critic_t.state_dict(),
                        log_alpha=log_alpha.detach().cpu(), opt_a=opt_a.state_dict(), opt_c=opt_c.state_dict(),
                        opt_al=opt_al.state_dict(), step=step, best=best, minutes=minutes, setup=SETUP_VERSION),
                   run_dir / "train_state.pt")

    def update(step):
        s, pr, a, r, s2, pr2 = buf.sample(args.batch, device)
        alpha = log_alpha.exp().detach()
        with torch.no_grad():
            a2, logp2 = actor.sample(s2)
            q1t, q2t = critic_t(s2, pr2, a2)
            target = r + args.gamma * (torch.min(q1t, q2t) - alpha * logp2)  # time-limit only: always bootstrap
        q1, q2 = critic(s, pr, a)
        loss_c = F.mse_loss(q1, target) + F.mse_loss(q2, target)
        opt_c.zero_grad()
        loss_c.backward()
        opt_c.step()
        if step % 2 == 0:
            a_new, logp = actor.sample(s)
            q1n, q2n = critic(s, pr, a_new)
            loss_a = (alpha * logp - torch.min(q1n, q2n)).mean()
            opt_a.zero_grad()
            loss_a.backward()
            opt_a.step()
            loss_al = -(log_alpha * (logp.detach() - ad)).mean()
            opt_al.zero_grad()
            loss_al.backward()
            opt_al.step()
        with torch.no_grad():
            for q, qt in zip(critic.parameters(), critic_t.parameters()):
                qt.lerp_(q, args.tau)

    step, best, minutes0, fill_until = 0, -1.0, 0.0, args.learning_starts
    if args.resume:
        st = torch.load(run_dir / "train_state.pt", map_location=device)
        actor.load_state_dict(st["actor"])
        critic.load_state_dict(st["critic"])
        critic_t.load_state_dict(st["critic_t"])
        with torch.no_grad():
            log_alpha.copy_(st["log_alpha"].to(device))
        for opt, k in ((opt_a, "opt_a"), (opt_c, "opt_c"), (opt_al, "opt_al")):
            opt.load_state_dict(st[k])
        step, best, minutes0 = st["step"], st["best"], st["minutes"]
        if st.get("setup") != SETUP_VERSION:  # the task changed (e.g. a camera was added): old scores do not count
            best = -1.0
        fill_until = step + args.learning_starts  # refill the (empty) replay buffer with the current policy first
        print(f"resuming at step {step} (best eval success so far {best:.2f})", flush=True)
    new_log = not (run_dir / "log.csv").exists()
    log = open(run_dir / "log.csv", "a", newline="")
    writer = csv.writer(log)
    if new_log:
        writer.writerow(["step", "minutes", "train_success", "eval_success", "eval_return", "alpha"])
    obs = [e.reset() for e in envs]
    recent, ep_succ = [], [False] * len(envs)
    debt, t0 = 0.0, time.time()
    print(f"run dir {run_dir} | device {device} | {args.num_envs} envs | state {sd} priv {pd}", flush=True)
    while step < args.steps:
        if step < args.learning_starts and not (args.resume or args.init):
            acts = [np.random.uniform(-1, 1, ad) for _ in envs]
        else:
            with torch.no_grad():
                st = torch.as_tensor(np.stack([o["state"] for o in obs]), device=device)
                acts = list(actor.sample(st)[0].cpu().numpy())
        for i, e in enumerate(envs):
            o2, r, _, trunc, info = e.step(acts[i])
            buf.add(obs[i], acts[i], r, o2)
            ep_succ[i] |= info["success"]
            if trunc:
                recent = (recent + [ep_succ[i]])[-100:]
                ep_succ[i] = False
                o2 = e.reset()
            obs[i] = o2
        step += len(envs)
        if step >= fill_until:
            debt += len(envs) * args.updates_per_step
            while debt >= 1:
                update(step + int(debt))
                debt -= 1
        if step % args.eval_every < len(envs):
            sr, ret = evaluate(eval_env, actor, device, args.eval_episodes)
            minutes = minutes0 + (time.time() - t0) / 60
            writer.writerow([step, round(minutes, 1), np.mean(recent) if recent else 0, sr, round(ret, 2),
                             round(log_alpha.exp().item(), 4)])
            log.flush()
            save(run_dir / "actor_last.pt")
            if sr >= best:
                best = sr
                save(run_dir / "actor_best.pt")
            save_state(step, best, minutes)
            print(f"step {step:7d} | {minutes:5.1f} min | train success {np.mean(recent) if recent else 0:.2f} | "
                  f"eval success {sr:.2f} | eval return {ret:7.1f} | alpha {log_alpha.exp().item():.3f}",
                  flush=True)
    save(run_dir / "actor_last.pt")
    print(f"done. best eval success {best:.2f}. run dir: {run_dir}")


if __name__ == "__main__":
    main()
