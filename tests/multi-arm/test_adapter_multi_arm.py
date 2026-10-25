"""Multi-arm adapter tests (repo interpreter: /workspace/photo2grasp/.venv/bin/python). Read-only model sources:
pinned Menagerie checkout via robot_tool stage (copied into a temp dir) and copies of cached QA object assets.
No database, no GPU, no network. FULL=1 also clean-installs the exported 2-arm package in a fresh uv venv."""
import json, os, shutil, subprocess, sys, tempfile, time, zipfile
from pathlib import Path

STAGE = Path(__file__).resolve().parent.parent
LIVE = Path("/workspace/photo2grasp")
PY = str(LIVE / ".venv" / "bin" / "python")
WC, WX = STAGE / "adapter" / "workcell.py", STAGE / "adapter" / "workcell_export.py"
FAIL = []
TMP = Path(tempfile.mkdtemp(prefix="realisim_multiarm_"))


def check(c, m):
    print(("PASS " if c else "FAIL ") + m)
    if not c:
        FAIL.append(m)


def tool(args, marker="WORKCELL_RESULT", expect_ok=True):
    r = subprocess.run([PY, *map(str, args)], capture_output=True, text=True, cwd="/tmp", timeout=900)
    if marker not in r.stdout:
        raise RuntimeError(r.stdout[-800:] + r.stderr[-1500:])
    return json.loads(r.stdout.split(marker + " ")[1].splitlines()[0])


try:
    R = TMP / "robots"
    for lid in ("robotstudio_so101", "trs_so_arm100", "universal_robots_ur5e"):
        tool([LIVE / "adapter" / "robot_tool.py", "stage", "--id", lid, "--out", R / lid], "ROBOT_RESULT")
    A = TMP / "assets"
    src_assets = {"qa_mug": "av_9ecb85c5c0414aa9", "object_1": "av_2c7e7eecd2fb4eec"}
    for n, av in src_assets.items():
        shutil.copytree(LIVE / "data2" / "cache" / "assets" / av / n, A / n, symlinks=False)
    common = ["--assets", A, "--names", "qa_mug", "--table", "1.2,1.0,0.75"]

    # 1) single-arm regression: legacy composer hash unchanged, --robots with one entry identical
    h_live = tool([LIVE / "adapter" / "workcell.py", "bundle", "--robot-dir", R / "robotstudio_so101", *common, "--out", TMP / "s0"])["model_hash"]
    h_stage = tool([WC, "bundle", "--robot-dir", R / "robotstudio_so101", *common, "--out", TMP / "s1"])["model_hash"]
    one = json.dumps([dict(name="robot_1", robot_dir=str(R / "robotstudio_so101"))])
    h_one = tool([WC, "bundle", "--robots", one, *common, "--out", TMP / "s2"])["model_hash"]
    check(h_live == h_stage == h_one, f"single-arm model hash unchanged ({h_live[:12]})")

    # 2) two same-model arms, default second pose (provisional)
    two = json.dumps([dict(name="robot_1", robot_dir=str(R / "robotstudio_so101"), library_id="robotstudio_so101"),
                      dict(name="robot_2", robot_dir=str(R / "robotstudio_so101"), library_id="robotstudio_so101")])
    s2 = tool([WC, "simulate", "--robots", two, *common, "--out", TMP / "two"])
    arms = s2["robots"]
    check([a["name"] for a in arms] == ["robot_1", "robot_2"], "2 same-model: both arms composed")
    check(arms[1]["pose_source"] == "default" and arms[0]["pose_source"] == "origin", "2 same-model: default pose labelled default")
    check(arms[0]["base_pose"] != arms[1]["base_pose"], f"2 same-model: distinct base poses {arms[1]['base_pose']}")
    check(arms[0]["action_slice"] == [0, 6] and arms[1]["action_slice"] == [6, 12], "2 same-model: disjoint actuator slices 0-5 / 6-11")
    check(set(arms[0]["qpos_indices"]).isdisjoint(arms[1]["qpos_indices"]), "2 same-model: disjoint joint indices")
    check(all(j.startswith("robot_2/") for j in arms[1]["joints"] + arms[1]["actuators"]), "2 same-model: namespaced joints/actuators")
    check(s2["result"] == "pass" and set(s2["arm_metrics"]) == {"robot_1", "robot_2"}, "2 same-model: simulated, both arms reported")
    check(all(v["max_joint_dev_rad"] < 0.05 for v in s2["arm_metrics"].values()), "2 same-model: arms hold their home pose in the drop test")

    # 3) three mixed arms, measured poses, posed table, two objects
    three = json.dumps([dict(name="left_so101", robot_dir=str(R / "robotstudio_so101"), library_id="robotstudio_so101"),
                        dict(name="right_so100", robot_dir=str(R / "trs_so_arm100"), library_id="trs_so_arm100", pose=dict(x_m=0.0, y_m=-0.5, yaw_deg=90), pose_source="measured"),
                        dict(name="far_ur5e", robot_dir=str(R / "universal_robots_ur5e"), library_id="universal_robots_ur5e", pose=dict(x_m=0.95, y_m=0.0, yaw_deg=180), pose_source="measured")])
    args3 = ["--robots", three, "--assets", A, "--names", "qa_mug,object_1", "--table", "1.4,1.4,0.75", "--table-pose", json.dumps(dict(x_m=0.45, y_m=-0.2, yaw_deg=0)),
             "--places", json.dumps({"qa_mug": [0.3, -0.1, 0], "object_1": [0.35, -0.3, 30]})]
    s3 = tool([WC, "simulate", *args3, "--out", TMP / "three"])
    a3 = s3["robots"]
    check([a["library_id"] for a in a3] == ["robotstudio_so101", "trs_so_arm100", "universal_robots_ur5e"], "3 mixed: every model composed")
    check(len({json.dumps(a["base_pose"]) for a in a3}) == 3 and all(a["pose_source"] in ("origin", "measured") for a in a3), "3 mixed: distinct measured poses kept")
    check([a["action_slice"] for a in a3] == [[0, 6], [6, 12], [12, 18]], "3 mixed: actuator slices cover all 18 controls")
    check(len({a["end_effector_body"] for a in a3}) == 3, "3 mixed: per-arm end effectors " + ", ".join(a["end_effector_body"] for a in a3))
    check(s3["result"] == "pass" and len(s3["arm_metrics"]) == 3, "3 mixed: drop test pass, all arms simulated")
    ee = s3["arm_metrics"]["far_ur5e"]["end_effector_pos_m"]
    check(ee[0] > 0.5, f"3 mixed: far arm really sits at its own base (EE x {ee[0]})")

    # 4) explicit failures (never silent truncation)
    bad = json.dumps([dict(name="a", robot_dir=str(R / "robotstudio_so101")), dict(name="b", robot_dir=str(R / "robotstudio_so101"), pose=dict(x_m=0.02, y_m=0, yaw_deg=0))])
    r = subprocess.run([PY, str(WC), "bundle", "--robots", bad, *map(str, common), "--out", str(TMP / "bad")], capture_output=True, text=True, cwd="/tmp")
    res = json.loads(r.stdout.split("WORKCELL_RESULT ")[1].splitlines()[0])
    check(not res["ok"] and res["kind"] == "placement" and "intersect" in res["error"], "overlapping arm bases fail explicitly")
    sys.path.insert(0, str(STAGE / "adapter"))
    import workcell as W
    W.MAX_TOTAL_ROBOT_ACTUATORS = 10
    try:
        W.compose_multi(json.loads(two), str(A), ["qa_mug"], [1.2, 1.0, 0.75])
        check(False, "resource limit fails explicitly")
    except W.ResourceError as e:
        check("no arm was dropped" in str(e), "resource limit fails explicitly: " + str(e)[:80])
    W.MAX_TOTAL_ROBOT_ACTUATORS = 512

    # 5) export the 2-arm scene: portable, namespaced, per-arm map
    prov = TMP / "prov.json"
    prov.write_text(json.dumps(dict(project_name="multi-arm test", scene_version="wc_t", scene_version_id="wc_t", asset_versions=[],
                                    robot=dict(name="SO-101 (5-DOF)", library_id="robotstudio_so101"),
                                    robots=[dict(name=n, library_id="robotstudio_so101", model_name="SO-101 (5-DOF)") for n in ("robot_1", "robot_2")])))
    ex = tool([WX, "--robots", two, *common, "--out", TMP / "ex", "--version", "1.0.0", "--env-id", "RealiSIM/Workcell_ma2-v1", "--slug", "ma2", "--provenance", prov], "EXPORT_RESULT")
    pkg = Path(ex["package_dir"])
    xml = (pkg / "scene" / "scene.xml").read_text()
    meta = json.loads((pkg / "workcell.json").read_text())
    check('file="/' not in xml and all((pkg / "scene" / f).is_file() for f in __import__("re").findall(r'file="([^"]+)"', xml)), "export: self-contained relative asset paths")
    check(xml.count('name="robot_1/') > 10 and xml.count('name="robot_2/') > 10, "export: both arm namespaces in scene.xml (bodies, joints, meshes, materials)")
    check([r["name"] for r in meta["robots"]] == ["robot_1", "robot_2"] and meta["end_effector_body"] == meta["robots"][0]["end_effector_body"], "export: per-arm map + primary compat field")
    check(sorted(meta["end_effectors"]) == ["robot_1", "robot_2"], "export: end-effector map")
    import mujoco
    m = mujoco.MjModel.from_xml_path(str(pkg / "scene" / "scene.xml"))
    check(m.nu == 12 and all(m.joint(j).id >= 0 for r in meta["robots"] for j in r["joints"]), "export: standalone scene loads, 12 actuators, every arm joint resolvable")
    sys.path.insert(0, str(pkg.parent))
    try:
        import gymnasium as gym
        import importlib
        mod = importlib.import_module(ex["module"])
        env = gym.make(mod.ENV_ID)
        o, info = env.reset(seed=1)
        a = env.unwrapped.arm_action({"robot_2": [1.0] * 6})
        o2, rwd, term, trunc, info2 = env.step(a)
        check(env.action_space.shape == (12,) and o.shape == env.observation_space.shape and set(info2["end_effectors"]) == {"robot_1", "robot_2"},
              f"gym (repo env): reset/step, action {env.action_space.shape}, obs {o.shape}")
        q = env.unwrapped.data.qpos
        r2 = meta["robots"][1]
        moved = abs(float(q[r2["qpos_indices"][0]]) - r2["home_qpos"][0]) > 1e-4
        r1 = meta["robots"][0]
        still = abs(float(q[r1["qpos_indices"][0]]) - r1["home_qpos"][0]) < 0.05
        check(moved and still, "gym: commanding robot_2 moves robot_2 only (control mapping)")
        env.close()
    except ImportError as e:
        print("SKIP gym in repo env:", e)
    if os.environ.get("FULL") == "1":
        venv = TMP / "venv"
        env_ = dict(os.environ); env_.pop("VIRTUAL_ENV", None); env_.pop("PYTHONPATH", None)
        t0 = time.time()
        r = subprocess.run(f"uv venv {venv} --python 3.11 -q && uv pip install --python {venv}/bin/python '{ex['archive']}' -q", shell=True, capture_output=True, text=True, env=env_, timeout=1200, cwd="/tmp")
        check(r.returncode == 0, f"clean install ({time.time() - t0:.0f} s)")
        r = subprocess.run([str(venv / "bin" / "python"), "-m", ex["smoke_module"]], capture_output=True, text=True, env=env_, timeout=900, cwd="/tmp")
        sm = json.loads(r.stdout.split("SMOKE_RESULT ")[1].splitlines()[0]) if "SMOKE_RESULT" in r.stdout else dict(ok=False, error=r.stderr[-400:])
        check(sm.get("ok") and sm.get("check_env") == "passed" and len(sm.get("arms") or []) == 2, f"clean-venv smoke: check_env + per-arm checks ({sm.get('action_space')}, {sm.get('observation_space')})")
        probe = (f"import json, numpy as np, gymnasium as gym, {ex['module']} as P\n"
                 "env = gym.make(P.ENV_ID); raw = env.unwrapped; r1, r2 = P.META['robots']; out = []\n"
                 "for s in (1.0, -1.0):\n"
                 "    env.reset(seed=1)\n"
                 "    for _ in range(5): env.step(raw.arm_action({'robot_2': [s] * 6}))\n"
                 "    out.append(raw.data.qpos.copy())\n"
                 "d1 = float(np.max(np.abs(out[0][r1['qpos_indices']] - out[1][r1['qpos_indices']])))\n"
                 "d2 = float(np.max(np.abs(out[0][r2['qpos_indices']] - out[1][r2['qpos_indices']])))\n"
                 "print('PROBE ' + json.dumps(dict(d1=d1, d2=d2)))")
        r = subprocess.run([str(venv / "bin" / "python"), "-c", probe], capture_output=True, text=True, env=env_, timeout=300, cwd="/tmp")
        pr = json.loads(r.stdout.split("PROBE ")[1]) if "PROBE " in r.stdout else {}
        check(pr.get("d2", 0) > 0.1 and pr.get("d1", 1) < 1e-3, f"control mapping: robot_2's action slice moves robot_2 ({pr.get('d2', 0):.3f} rad) and leaves robot_1 unchanged ({pr.get('d1', 1):.5f} rad)")
finally:
    shutil.rmtree(TMP, ignore_errors=True)
    print("cleanup:", not TMP.exists())
print(json.dumps(dict(ok=not FAIL, failed=FAIL)))
sys.exit(1 if FAIL else 0)
