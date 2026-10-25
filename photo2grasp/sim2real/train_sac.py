"""SAC on the SO-100 lift task, Squint-style, sized for a 4 GB laptop GPU.

    python sim2real/train_sac.py --object lego_tree --steps 300000

Actor input  = 16x16 RGB image (colour-jittered) + noisy joint angles + joint targets  -> what the real robot has
Critic input = the same + privileged sim state (object/fingertip positions)            -> training only
"""
import argparse
import csv
import time
from pathlib import Path

import cv2
import numpy as np
import torch
import torch.nn as nn
import torch.nn.functional as F

from lift_env import LiftEnv

ROOT = Path(__file__).resolve().parent.parent


# ── Networks ────────────────────────────────────────────────────────────────
class Encoder(nn.Module):
    """RGB image -> 50-d feature, using Squint's CNN for that image size + projection."""

    def __init__(self, image_size):
        super().__init__()
        if image_size == 64:
            layers = [nn.Conv2d(3, 32, 8, stride=4), nn.ReLU(), nn.Conv2d(32, 64, 4, stride=2), nn.ReLU(),
                      nn.Conv2d(64, 64, 3, stride=1), nn.ReLU()]
        elif image_size == 16:
            layers = [nn.Conv2d(3, 32, 4, stride=2), nn.ReLU(), nn.Conv2d(32, 64, 4, stride=1), nn.ReLU()]
        else:
            raise ValueError(f"no encoder for image size {image_size}")
        self.conv = nn.Sequential(*layers, nn.Flatten())  # both sizes end at 64 x 4 x 4
        self.proj = nn.Sequential(nn.Linear(64 * 4 * 4, 50), nn.LayerNorm(50), nn.Tanh())

    def forward(self, img):  # img: float (B,H,W,3) already jittered and scaled to [0,1]
        return self.proj(self.conv(img.permute(0, 3, 1, 2) - 0.5))


def mlp(inp, out, hidden=256):
    return nn.Sequential(nn.Linear(inp, hidden), nn.LayerNorm(hidden), nn.ReLU(),
                         nn.Linear(hidden, hidden), nn.LayerNorm(hidden), nn.ReLU(), nn.Linear(hidden, out))


class Actor(nn.Module):
    def __init__(self, state_dim, act_dim, image_size):
        super().__init__()
        self.enc = Encoder(image_size)
        self.net = mlp(50 + state_dim, 2 * act_dim)

    def forward(self, img, state):
        mean, log_std = self.net(torch.cat([self.enc(img), state], -1)).chunk(2, -1)
        return mean, torch.clamp(log_std, -5, 2)

    def sample(self, img, state):
        mean, log_std = self(img, state)
        std = log_std.exp()
        u = mean + std * torch.randn_like(mean)
        a = torch.tanh(u)
        logp = (-0.5 * ((u - mean) / std) ** 2 - log_std - 0.5 * np.log(2 * np.pi)).sum(-1)
        logp -= torch.log(1 - a.pow(2) + 1e-6).sum(-1)
        return a, logp


class Critic(nn.Module):
    def __init__(self, state_dim, priv_dim, act_dim, image_size):
        super().__init__()
        self.enc = Encoder(image_size)
        self.q1 = mlp(50 + state_dim + priv_dim + act_dim, 1)
        self.q2 = mlp(50 + state_dim + priv_dim + act_dim, 1)

    def forward(self, img, state, priv, act):
        x = torch.cat([self.enc(img), state, priv, act], -1)
        return self.q1(x).squeeze(-1), self.q2(x).squeeze(-1)


# ── Colour jitter (Squint: removing it dropped real-world success 91% -> 72.5%) ──
def color_jitter(img, strength=0.2):
    """img float (B,H,W,3) in [0,1]; random brightness, contrast, saturation and per-channel gain."""
    b = img.shape[0]
    rnd = lambda: 1 + (torch.rand(b, 1, 1, 1, device=img.device) * 2 - 1) * strength
    img = img * rnd()
    mean = img.mean((1, 2, 3), keepdim=True)
    img = (img - mean) * rnd() + mean
    grey = img.mean(-1, keepdim=True)
    img = (img - grey) * rnd() + grey
    img = img * (1 + (torch.rand(b, 1, 1, 3, device=img.device) * 2 - 1) * strength / 2)
    return img.clamp(0, 1)


def to_img(x, device, jitter):
    img = torch.as_tensor(x, device=device).float() / 255
    return color_jitter(img) if jitter else img


# ── Replay buffer (CPU RAM, uint8 images) ───────────────────────────────────
class Buffer:
    def __init__(self, size, env):
        self.size, self.i, self.full = size, 0, False
        s = (size,)
        self.img = np.zeros(s + (env.image_size, env.image_size, 3), np.uint8)
        self.next_img = np.zeros_like(self.img)
        self.state = np.zeros(s + (env.state_dim,), np.float32)
        self.next_state = np.zeros_like(self.state)
        self.priv = np.zeros(s + (env.priv_dim,), np.float32)
        self.next_priv = np.zeros_like(self.priv)
        self.act = np.zeros(s + (env.action_dim,), np.float32)
        self.rew = np.zeros(s, np.float32)

    def add(self, o, a, r, o2):
        i = self.i
        self.img[i], self.state[i], self.priv[i] = o["image"], o["state"], o["priv"]
        self.next_img[i], self.next_state[i], self.next_priv[i] = o2["image"], o2["state"], o2["priv"]
        self.act[i], self.rew[i] = a, r
        self.i = (i + 1) % self.size
        self.full |= self.i == 0

    def sample(self, n, device):
        idx = np.random.randint(0, self.size if self.full else self.i, n)
        t = lambda x: torch.as_tensor(x[idx], device=device)
        return (t(self.img), t(self.state), t(self.priv), t(self.act), t(self.rew),
                t(self.next_img), t(self.next_state), t(self.next_priv))


# ── Evaluation ──────────────────────────────────────────────────────────────
def evaluate(actor, env, device, episodes, video_path=None):
    successes, returns, frames = [], [], []
    for ep in range(episodes):
        o, ret, success = env.reset(), 0.0, False
        for _ in range(env.max_steps):
            with torch.no_grad():
                mean, _ = actor(to_img(o["image"][None], device, False), torch.as_tensor(o["state"][None], device=device))
            o, r, _, trunc, info = env.step(torch.tanh(mean)[0].cpu().numpy())
            ret += r
            success |= info["success"]
            if video_path and ep == 0:
                big = env.render_rgb()
                small = cv2.resize(o["image"], big.shape[:2], interpolation=cv2.INTER_NEAREST)
                frames.append(np.hstack([big, small]))
            if trunc:
                break
        successes.append(success)
        returns.append(ret)
    if frames:
        h, w = frames[0].shape[:2]
        vw = cv2.VideoWriter(str(video_path), cv2.VideoWriter_fourcc(*"mp4v"), 10, (w * 2, h * 2))
        for f in frames:
            vw.write(cv2.cvtColor(cv2.resize(f, (w * 2, h * 2), interpolation=cv2.INTER_NEAREST), cv2.COLOR_RGB2BGR))
        vw.release()
    return float(np.mean(successes)), float(np.mean(returns))


# ── Training ────────────────────────────────────────────────────────────────
def main():
    p = argparse.ArgumentParser()
    p.add_argument("--object", default="lego_tree")
    p.add_argument("--background", default=None, help="photo of the real table from the robot camera")
    p.add_argument("--camera", choices=["wrist", "third"], default="wrist")
    p.add_argument("--start", choices=["zero", "home"], default="zero", help="arm pose at episode start")
    p.add_argument("--snapshot-every", type=int, default=250_000, help="keep a copy of the actor every N steps")
    p.add_argument("--curriculum", type=float, nargs=2, default=[0.2, 0.7], metavar=("FROM", "UNTIL"),
                   help="share of episodes starting from --start: FROM at the beginning, rising to 1.0 at "
                        "fraction UNTIL of training (evaluation always uses --start)")
    p.add_argument("--marker", action="store_true", help="ArUco card on the object (faces the robot)")
    p.add_argument("--image-size", type=int, choices=[16, 64], default=64)
    p.add_argument("--steps", type=int, default=300_000)
    p.add_argument("--num-envs", type=int, default=4)
    p.add_argument("--updates-per-step", type=float, default=0.5, help="gradient updates per env step")
    p.add_argument("--batch", type=int, default=256)
    p.add_argument("--buffer", type=int, default=100_000, help="64x64 images: 100k transitions ~ 2.5 GB RAM")
    p.add_argument("--learning-starts", type=int, default=5_000)
    p.add_argument("--gamma", type=float, default=0.9)
    p.add_argument("--tau", type=float, default=0.01)
    p.add_argument("--lr", type=float, default=3e-4)
    p.add_argument("--eval-every", type=int, default=20_000)
    p.add_argument("--eval-episodes", type=int, default=10)
    p.add_argument("--seed", type=int, default=0)
    args = p.parse_args()

    torch.manual_seed(args.seed)
    np.random.seed(args.seed)
    device = torch.device("cuda" if torch.cuda.is_available() else "cpu")
    run_dir = ROOT / "runs" / f"{args.object}{'_marker' if args.marker else ''}_{args.camera}{args.image_size}_{time.strftime('%Y%m%d_%H%M%S')}"
    run_dir.mkdir(parents=True)

    env_kw = dict(camera=args.camera, image_size=args.image_size, start=args.start, marker=args.marker)
    envs = [LiftEnv(args.object, args.background, seed=args.seed + i, **env_kw) for i in range(args.num_envs)]
    eval_env = LiftEnv(args.object, args.background, seed=10_000 + args.seed, **env_kw)
    e0 = envs[0]
    actor = Actor(e0.state_dim, e0.action_dim, args.image_size).to(device)
    critic = Critic(e0.state_dim, e0.priv_dim, e0.action_dim, args.image_size).to(device)
    critic_target = Critic(e0.state_dim, e0.priv_dim, e0.action_dim, args.image_size).to(device)
    critic_target.load_state_dict(critic.state_dict())
    log_alpha = torch.zeros(1, device=device, requires_grad=True)
    target_entropy = -e0.action_dim
    opt_actor = torch.optim.Adam(actor.parameters(), lr=args.lr)
    opt_critic = torch.optim.Adam(critic.parameters(), lr=args.lr)
    opt_alpha = torch.optim.Adam([log_alpha], lr=args.lr)
    buf = Buffer(args.buffer, e0)

    def update(step):
        img, st, pr, a, r, img2, st2, pr2 = buf.sample(args.batch, device)
        img, img2 = to_img(img, device, True), to_img(img2, device, True)
        alpha = log_alpha.exp().detach()
        with torch.no_grad():
            a2, logp2 = actor.sample(img2, st2)
            q1t, q2t = critic_target(img2, st2, pr2, a2)
            # Episodes only end by time limit, so always bootstrap (Squint's default)
            target = r + args.gamma * (torch.min(q1t, q2t) - alpha * logp2)
        q1, q2 = critic(img, st, pr, a)
        critic_loss = F.mse_loss(q1, target) + F.mse_loss(q2, target)
        opt_critic.zero_grad()
        critic_loss.backward()
        opt_critic.step()
        stats = dict(critic_loss=critic_loss.item())
        if step % 2 == 0:  # delayed actor updates
            a_new, logp = actor.sample(img, st)
            q1n, q2n = critic(img, st, pr, a_new)
            actor_loss = (alpha * logp - torch.min(q1n, q2n)).mean()
            opt_actor.zero_grad()
            actor_loss.backward()
            opt_actor.step()
            alpha_loss = -(log_alpha * (logp.detach() + target_entropy)).mean()
            opt_alpha.zero_grad()
            alpha_loss.backward()
            opt_alpha.step()
            stats.update(actor_loss=actor_loss.item(), alpha=log_alpha.exp().item())
        with torch.no_grad():
            for p_, pt in zip(critic.parameters(), critic_target.parameters()):
                pt.lerp_(p_, args.tau)
        return stats

    log = open(run_dir / "log.csv", "w", newline="")
    writer = csv.writer(log)
    writer.writerow(["step", "minutes", "train_success", "train_knocked_over", "eval_success", "eval_return", "alpha",
                     "critic_loss"])
    obs = [e.reset() for e in envs]
    ep_success = [False] * len(envs)
    ep_knocked = [False] * len(envs)
    recent, recent_knocked = [], []
    step, n_updates, update_debt, stats, t0 = 0, 0, 0.0, {}, time.time()
    best = -1.0
    while step < args.steps:
        p0, until = args.curriculum
        start_prob = min(1.0, p0 + (1 - p0) * step / (until * args.steps))
        for e in envs:
            e.start_prob = start_prob
        if step < args.learning_starts:
            acts = [np.random.uniform(-1, 1, e0.action_dim) for _ in envs]
        else:
            with torch.no_grad():
                img = to_img(np.stack([o["image"] for o in obs]), device, True)
                st = torch.as_tensor(np.stack([o["state"] for o in obs]), device=device)
                acts = list(actor.sample(img, st)[0].cpu().numpy())
        for i, e in enumerate(envs):
            o2, r, _, trunc, info = e.step(acts[i])
            buf.add(obs[i], acts[i], r, o2)
            ep_success[i] |= info["success"]
            ep_knocked[i] |= info["knocked_over"]
            if trunc:
                recent = (recent + [ep_success[i]])[-100:]
                recent_knocked = (recent_knocked + [ep_knocked[i]])[-100:]
                ep_success[i] = ep_knocked[i] = False
                o2 = e.reset()
            obs[i] = o2
            step += 1
        if step >= args.learning_starts:
            update_debt += args.updates_per_step * len(envs)
            while update_debt >= 1:
                stats.update(update(n_updates))  # keep last actor/alpha stats on critic-only updates
                n_updates += 1
                update_debt -= 1

        if step % args.eval_every < len(envs) and step >= args.learning_starts:
            sr, ret = evaluate(actor, eval_env, device, args.eval_episodes, run_dir / f"eval_{step}.mp4")
            mins = (time.time() - t0) / 60
            train_sr = float(np.mean(recent)) if recent else 0.0
            train_ko = float(np.mean(recent_knocked)) if recent_knocked else 0.0
            writer.writerow([step, round(mins, 1), round(train_sr, 3), round(train_ko, 3), sr, round(ret, 2),
                             round(stats.get("alpha", 0), 4), round(stats.get("critic_loss", 0), 4)])
            log.flush()
            print(f"step {step:>7} | {mins:5.1f} min | train success {train_sr:.2f} | knocked over {train_ko:.2f} "
                  f"| zero-start share {start_prob:.2f} | eval success {sr:.2f} "
                  f"| eval return {ret:6.1f} | alpha {stats.get('alpha', 0):.3f}", flush=True)
            torch.save(dict(actor=actor.state_dict(), args=vars(args)), run_dir / "actor_last.pt")
            if step // args.snapshot_every != (step - len(envs)) // args.snapshot_every:
                torch.save(dict(actor=actor.state_dict(), args=vars(args)), run_dir / f"actor_{step // 1000}k.pt")
            if sr >= best:
                best = sr
                torch.save(dict(actor=actor.state_dict(), args=vars(args)), run_dir / "actor_best.pt")
    print(f"done. best eval success {best:.2f}. run dir: {run_dir}")


if __name__ == "__main__":
    main()
