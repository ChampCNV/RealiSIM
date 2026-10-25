"""RealiSIM server-authoritative MuJoCo worker (web workspace adapter; runs in the repo's Python environment).

    python sim_worker.py bundle   --repo REPO --assets DIR --names a[,b] --out OUT
    python sim_worker.py simulate --repo REPO --assets DIR --names a --out OUT [--seed N] [--x X --y Y --yaw DEG]
                                  [--lift 0.05] [--seconds 3]

The scene is built by the repo's own make_scene.build_scene (both its APIs: build_scene(names) -> (model, places)
and the older build_scene(name) -> model), from asset files restored from the system of record (DIR/<name>/...),
not from the live working tree. `bundle` writes geometry (scene.json + meshes.bin + textures) for the browser viewer;
`simulate` runs the repo's drop test from a seeded or chosen initial pose and writes the trajectory (traj.bin + traj.json).
Same model build in both, so body order matches.
"""
import argparse, hashlib, inspect, json, math, sys, tempfile, traceback
from pathlib import Path
import numpy as np
import mujoco

FPS = 60


def build(repo, assets, names):
    tmp = Path(tempfile.mkdtemp(prefix="p2g_scene_"))
    (tmp / "assets").mkdir()
    for n in names:
        (tmp / "assets" / n).symlink_to(Path(assets).resolve() / n)
    sys.path.insert(0, str(repo))
    try:
        import make_scene as ms
        ms.ROOT = tmp  # read the restored asset files (ARM_SCENE was already resolved from the repo at import)
        if hasattr(ms, "build_scene"):
            for arg in (list(names), names[0]):
                try:
                    res = ms.build_scene(arg)
                except Exception:
                    continue
                if isinstance(res, tuple) and isinstance(res[0], mujoco.MjModel):
                    return res[0], (res[1] if len(res) > 1 else None), ms, "repo:make_scene.build_scene"
                if isinstance(res, mujoco.MjModel):
                    return res, None, ms, "repo:make_scene.build_scene"
    except Exception:
        traceback.print_exc()
    # fallback: the menagerie SO-100 scene with the objects in a row in front of the arm
    arm = Path(repo) / "third_party" / "mujoco_menagerie" / "trs_so_arm100" / "scene.xml"
    spec = mujoco.MjSpec.from_file(str(arm))
    for k in list(spec.keys):
        spec.delete(k)
    places = {}
    for i, n in enumerate(names):
        x = (i - (len(names) - 1) / 2) * 0.12
        obj = mujoco.MjSpec.from_file(str(Path(assets) / n / f"{n}.xml"))
        spec.worldbody.add_frame(pos=[x, -0.25, 0]).attach_body(obj.body(n), "", "")
        places[n] = (x, -0.25)
    return spec.compile(), places, None, "fallback:menagerie+row"


def reset_state(m, places, ms, lift, pose, names):
    if ms is not None and places is not None and hasattr(ms, "reset"):
        params = inspect.signature(ms.reset).parameters
        kw = {"lift": lift} if "lift" in params else {}
        if "hand" in params:
            kw["hand"] = None   # repo >= 9fa727d: no human hand in workspace scenes (legacy drop test)
        if pose:
            places = dict(places)
            places[names[0]] = (pose[0], pose[1])
        d = ms.reset(m, places, **kw)
        start = "repo:make_scene.reset"
    else:
        d = mujoco.MjData(m)
        key = next((i for i in range(m.nkey) if m.key(i).name == "rest"), None)
        if key is not None:
            mujoco.mj_resetDataKeyframe(m, d, key)
        for i, n in enumerate(names):
            j = m.body_jntadr[m.body(n).id]
            a = m.jnt_qposadr[j]
            x, y = pose[:2] if (pose and i == 0) else (places or {}).get(n, (0, -0.25))
            d.qpos[a:a + 7] = [x, y, lift, 1, 0, 0, 0]
        mujoco.mj_forward(m, d)
        start = "keyframe+lift"
    if pose and len(pose) > 2:  # yaw of the first object
        j = m.body_jntadr[m.body(names[0]).id]
        a = m.jnt_qposadr[j]
        yaw = math.radians(pose[2])
        d.qpos[a + 3:a + 7] = [math.cos(yaw / 2), 0, 0, math.sin(yaw / 2)]
        mujoco.mj_forward(m, d)
    return d, start


def geometry(m, out, names):
    buf = bytearray()

    def put(arr):
        nonlocal buf
        while len(buf) % 4:
            buf += b"\0"
        off = len(buf)
        b = np.ascontiguousarray(arr).tobytes()
        buf += b
        return [off, len(b)]

    meshes = []
    for i in range(m.nmesh):
        va, vn, fa, fn = m.mesh_vertadr[i], m.mesh_vertnum[i], m.mesh_faceadr[i], m.mesh_facenum[i]
        na, nn, ta, tn = m.mesh_normaladr[i], m.mesh_normalnum[i], m.mesh_texcoordadr[i], m.mesh_texcoordnum[i]
        V = m.mesh_vert[va:va + vn].astype(np.float32)
        F = m.mesh_face[fa:fa + fn].astype(np.uint32)
        FN = m.mesh_facenormal[fa:fa + fn] if nn else None
        FT = m.mesh_facetexcoord[fa:fa + fn] if tn and ta >= 0 else None
        N = m.mesh_normal[na:na + nn].astype(np.float32) if nn else None
        T = m.mesh_texcoord[ta:ta + tn].astype(np.float32) if (tn and ta >= 0) else None
        e = dict(name=m.mesh(i).name)
        if N is not None and nn == vn and np.array_equal(FN, F) and (T is None or (tn == vn and np.array_equal(FT, F))):
            e.update(indexed=True, count=int(vn), pos=put(V), nrm=put(N), idx=put(F))
            if T is not None:
                e["uv"] = put(T)
        else:
            e.update(indexed=False, count=int(fn * 3), pos=put(V[F.reshape(-1)]))
            if N is not None:
                e["nrm"] = put(N[FN.reshape(-1)])
            if T is not None:
                e["uv"] = put(T[FT.reshape(-1)])
        meshes.append(e)
    textures = []
    from PIL import Image
    for t in range(m.ntex):
        if m.tex_type[t] != 0:
            textures.append(None)
            continue
        w, h, c = int(m.tex_width[t]), int(m.tex_height[t]), int(m.tex_nchannel[t])
        px = np.array(m.tex_data[m.tex_adr[t]:m.tex_adr[t] + w * h * c], dtype=np.uint8).reshape(h, w, c)
        fn = f"tex_{t}.png"
        Image.fromarray(px[:, :, :3] if c >= 3 else px[:, :, 0]).save(out / fn)
        textures.append(dict(file=fn, name=m.tex(t).name, w=w, h=h))
    role = int(mujoco.mjtTextureRole.mjTEXROLE_RGB)
    mats = []
    for i in range(m.nmat):
        tid = int(m.mat_texid[i][role]) if m.mat_texid.ndim == 2 else int(m.mat_texid[i])
        mats.append(dict(name=m.mat(i).name, rgba=[round(float(v), 4) for v in m.mat_rgba[i]],
                         tex=tid if 0 <= tid < len(textures) and textures[tid] else -1))
    gt = {int(v): k.replace("mjGEOM_", "").lower() for k, v in mujoco.mjtGeom.__members__.items()}
    geoms = [dict(name=m.geom(g).name, body=int(m.geom_bodyid[g]), type=gt.get(int(m.geom_type[g]), "?"),
                  size=[round(float(v), 6) for v in m.geom_size[g]], pos=[round(float(v), 6) for v in m.geom_pos[g]],
                  quat=[round(float(v), 6) for v in m.geom_quat[g]], rgba=[round(float(v), 4) for v in m.geom_rgba[g]],
                  group=int(m.geom_group[g]), mesh=int(m.geom_dataid[g]) if m.geom_type[g] == mujoco.mjtGeom.mjGEOM_MESH else -1,
                  mat=int(m.geom_matid[g])) for g in range(m.ngeom)]
    bodies = [dict(name=m.body(b).name, parent=int(m.body_parentid[b])) for b in range(m.nbody)]
    objects = [dict(name=n, body=int(m.body(n).id)) for n in names]
    (out / "meshes.bin").write_bytes(bytes(buf))
    return dict(bodies=bodies, geoms=geoms, meshes=meshes, materials=mats, textures=textures, objects=objects)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("cmd", choices=["bundle", "simulate"])
    ap.add_argument("--repo", required=True); ap.add_argument("--assets", required=True)
    ap.add_argument("--names", required=True); ap.add_argument("--out", required=True)
    ap.add_argument("--seed", type=int, default=0)
    ap.add_argument("--x", type=float); ap.add_argument("--y", type=float); ap.add_argument("--yaw", type=float)
    ap.add_argument("--lift", type=float, default=0.05); ap.add_argument("--seconds", type=float, default=3.0)
    a = ap.parse_args()
    names = [n for n in a.names.split(",") if n]
    out = Path(a.out); out.mkdir(parents=True, exist_ok=True)
    m, places, ms, builder = build(Path(a.repo), Path(a.assets), names)
    engine = dict(mujoco=mujoco.__version__, builder=builder, timestep=m.opt.timestep, nbody=m.nbody, ngeom=m.ngeom)
    if a.cmd == "bundle":
        g = geometry(m, out, names)
        d0, _ = reset_state(m, places, ms, 0.0, None, names)   # resting pose, for static previews
        pose0 = np.concatenate([d0.xpos, d0.xquat], 1).astype(float).round(6).tolist()
        g.update(version=2, engine=engine, places=places, pose0=pose0)
        txt = json.dumps(g, separators=(",", ":"))
        (out / "scene.json").write_text(txt)
        h = hashlib.sha256(txt.encode() + (out / "meshes.bin").read_bytes()).hexdigest()
        print("SIM_RESULT " + json.dumps(dict(ok=True, engine=engine, model_hash=h, places=places)))
        return
    rng = np.random.default_rng(a.seed)
    pose = None
    if a.x is not None and a.y is not None:
        pose = (a.x, a.y, a.yaw if a.yaw is not None else 0.0)
    elif places:
        x0, y0 = places[names[0]]  # seeded placement around the repo's default spot (+-4 cm, +-30 deg)
        pose = (x0 + rng.uniform(-0.04, 0.04), y0 + rng.uniform(-0.04, 0.04), rng.uniform(-30, 30))
    d, start = reset_state(m, places, ms, a.lift, pose, names)
    frames, times = [], []
    every = max(1, int(round(1.0 / (FPS * m.opt.timestep))))
    for k in range(int(a.seconds / m.opt.timestep) + 1):
        if k % every == 0:
            frames.append(np.concatenate([d.xpos, d.xquat], 1).astype(np.float32))
            times.append(round(float(d.time), 4))
        mujoco.mj_step(m, d)
    metrics = {}
    for n in names:
        b = m.body(n).id
        j = m.body_jntadr[b]
        up = d.xmat[b].reshape(3, 3)[:, 2]
        tilt = float(np.degrees(np.arccos(np.clip(up[2], -1, 1))))
        speed = float(np.linalg.norm(d.qvel[m.jnt_dofadr[j]:m.jnt_dofadr[j] + 3])) if j >= 0 else 0.0
        metrics[n] = dict(mass_kg=round(float(m.body_subtreemass[b]), 5), rest_pos_m=[round(float(v), 4) for v in d.xpos[b]],
                          tilt_deg=round(tilt, 2), speed_mps=round(speed, 5), upright=tilt < 10, settled=speed < 0.01)
    ok = all(v["upright"] and v["settled"] for v in metrics.values())
    traj = np.stack(frames)
    (out / "traj.bin").write_bytes(traj.tobytes())
    info = dict(fps=FPS, frames=int(traj.shape[0]), bodies=int(traj.shape[1]), times=times, seed=a.seed,
                pose=dict(x=pose[0], y=pose[1], yaw_deg=pose[2]) if pose else None, lift_m=a.lift, seconds=a.seconds,
                start=start, metrics=metrics, result="pass" if ok else "fail", mode="drop-test", engine=engine)
    (out / "traj.json").write_text(json.dumps(info))
    print("SIM_RESULT " + json.dumps(dict(ok=True, result=info["result"], metrics=metrics, pose=info["pose"], engine=engine, start=start)))


if __name__ == "__main__":
    main()
