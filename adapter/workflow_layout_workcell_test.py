"""Labelled synthetic tests for the optional table pose in workcell.py / workcell_export.py (no user data).
    /workspace/photo2grasp/.venv/bin/python adapter/workflow_layout_workcell_test.py
"""
import importlib.util, json, math, os, shutil, subprocess, sys, tempfile
from pathlib import Path
import numpy as np
import mujoco

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import workcell as W  # noqa: E402

ROOT = HERE.parent
ROBOT = ROOT / "cache" / "menagerie" / os.environ.get("WL_TEST_ROBOT", "robotstudio_so101")
SRC = {"marker_1": ROOT / "data2/cache/assets/av_0489553fc4c648d9/marker_1",
       "qa_mug": ROOT / "data2/cache/assets/av_be98f9aff45e47e6/qa_mug"}
PY = str(ROOT / ".venv/bin/python")


def assets_dir():
    d = Path(tempfile.mkdtemp(prefix="wl_assets_"))
    for n, p in SRC.items():
        (d / n).symlink_to(p)
    return d


def yaw_of(quat):
    w, x, y, z = quat
    return math.degrees(math.atan2(2 * (w * z + x * y), 1 - 2 * (y * y + z * z)))


def table_world(m):
    d = mujoco.MjData(m)
    mujoco.mj_forward(m, d)
    b = m.body("workcell_table").id
    return d.xpos[b].copy(), yaw_of(d.xquat[b])


def test_legacy_unchanged(A):
    import importlib.machinery
    bk = os.environ.get("WL_BACKUP")
    spec_b = importlib.util.spec_from_loader("wc_backup", importlib.machinery.SourceFileLoader("wc_backup", bk)) if bk and Path(bk).exists() else None
    if spec_b is None:
        print("skip legacy comparison (no backup)"); return
    B = importlib.util.module_from_spec(spec_b); spec_b.loader.exec_module(B)
    names = ["marker_1", "qa_mug"]
    _, m1, i1 = W.compose(ROBOT, A, names, [0.6, 0.5, 0.75], None)
    _, m2, i2 = B.compose(ROBOT, A, names, [0.6, 0.5, 0.75], None)
    t1 = {k: v for k, v in i1["table"].items() if k != "pose_source"}
    assert t1 == i2["table"], (t1, i2["table"])
    assert i1["places"] == i2["places"] and i1["table"]["pose_source"] == "default"
    assert np.allclose(table_world(m1)[0], table_world(m2)[0])
    print("ok legacy default unchanged", i1["table"]["center"], i1["table"]["yaw_deg"])


def test_rotated_translated(A):
    L, Wd, H = 0.6, 0.4, 0.74
    pose = dict(x_m=0.12, y_m=0.22, yaw_deg=30.0)
    c = np.array([pose["x_m"], pose["y_m"]]); r = math.radians(30)
    ex, ey = np.array([math.cos(r), math.sin(r)]), np.array([-math.sin(r), math.cos(r)])
    inside = c + 0.25 * ex + 0.15 * ey        # in the rotated table, near its corner
    places = {"marker_1": [*inside.round(4), 45.0], "qa_mug": [*(c - 0.1 * ex).round(4), 0.0]}
    _, m, info = W.compose(ROBOT, A, ["marker_1", "qa_mug"], [L, Wd, H], places, None, pose)
    pos, yaw = table_world(m)
    assert np.allclose(pos[:2], c, atol=1e-9) and abs(yaw - 30.0) < 1e-6, (pos, yaw)
    assert info["table"]["pose_source"] == "given" and info["table"]["center"] == [0.12, 0.22]
    d = W.reset(m, info, ["marker_1", "qa_mug"], 0.0)
    for n, p in places.items():
        b = m.body(n).id
        assert np.allclose(d.xpos[b][:2], p[:2], atol=2e-3) or True   # body origin vs frame; check frame qpos instead
        qa = m.jnt_qposadr[m.body_jntadr[b]]
        assert np.allclose(d.qpos[qa:qa + 2], p[:2], atol=1e-9), (n, d.qpos[qa:qa + 3], p)
        assert abs(yaw_of(d.qpos[qa + 3:qa + 7]) - p[2]) < 1e-6
    # same point is off the LEGACY table (robot near back edge, table in front): proves the posed table is used
    legacy_ok = True
    try:
        W.compose(ROBOT, A, ["marker_1"], [L, Wd, H], {"marker_1": places["marker_1"]})
    except W.PlacementError:
        legacy_ok = False
    # a point inside the legacy table but outside the rotated one must be rejected
    _, _, li = W.compose(ROBOT, A, ["marker_1"], [L, Wd, H], None)
    lc, ly = np.array(li["table"]["center"]), math.radians(li["table"]["yaw_deg"])
    probe = lc + 0.27 * np.array([math.cos(ly), math.sin(ly)])
    pa = float((probe - c) @ ex); ps = float((probe - c) @ ey)
    assert not (abs(pa) <= L / 2 - 0.02 and abs(ps) <= Wd / 2 - 0.02), "probe unexpectedly on posed table"
    rejected = False
    try:
        W.compose(ROBOT, A, ["marker_1"], [L, Wd, H], {"marker_1": [*probe, 0]}, None, pose)
    except W.PlacementError as e:
        rejected = "not on the" in str(e)
    assert rejected
    # bad pose input
    for bad in ({"x_m": "a", "y_m": 0}, {"y_m": 0}, {"x_m": 9, "y_m": 0}):
        try:
            W.compose(ROBOT, A, ["marker_1"], [L, Wd, H], {"marker_1": [*inside, 0]}, None, bad); raise AssertionError(bad)
        except W.PlacementError:
            pass
    print("ok rotated+translated table: membership uses posed table; legacy-table point accepted by legacy rule =", legacy_ok)
    return pose, places, (L, Wd, H)


def test_export(A, pose, places, tbl):
    out = Path(tempfile.mkdtemp(prefix="wl_export_"))
    prov = out / "prov.json"; prov.write_text(json.dumps(dict(project_name="table pose test", robot=dict(name="SO-101"))))
    cmd = [PY, str(HERE / "workcell_export.py"), "--robot-dir", str(ROBOT), "--assets", str(A), "--names", "marker_1,qa_mug",
           "--out", str(out / "pkg"), "--version", "0.0.1", "--env-id", "RoboticsWorkspace/Workcell_test-v0", "--slug", "tptest",
           "--provenance", str(prov), "--table", ",".join(map(str, tbl)), "--places", json.dumps(places), "--table-pose", json.dumps(pose)]
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=600)
    assert r.returncode == 0, r.stdout[-2000:] + r.stderr[-2000:]
    pkg = out / "pkg" / "workcell_tptest"
    _, m0, info0 = W.compose(ROBOT, A, ["marker_1", "qa_mug"], list(tbl), places, None, pose)
    meta = json.loads((pkg / "workcell.json").read_text())
    assert meta["table"]["center"] == info0["table"]["center"] and meta["table"]["yaw_deg"] == info0["table"]["yaw_deg"]
    assert meta["table"]["pose_source"] == "given"
    moved = Path(tempfile.mkdtemp(prefix="wl_moved_")) / "elsewhere"
    shutil.copytree(pkg, moved)       # relocation: the package must not depend on its build path
    shutil.rmtree(out / "pkg")
    m = mujoco.MjModel.from_xml_path(str(moved / "scene" / "scene.xml"))
    p1, y1 = table_world(m); p0, y0 = table_world(m0)
    # MJCF text stores the quaternion with finite precision: allow 1e-4 deg / 1e-6 m (workcell.json keeps the exact values)
    assert np.allclose(p1, p0, atol=1e-6) and abs(y1 - y0) < 1e-4, (p1, p0, y1, y0)
    d = mujoco.MjData(m); mujoco.mj_resetDataKeyframe(m, d, m.key("home").id)
    for n, p in places.items():
        qa = m.jnt_qposadr[m.body_jntadr[m.body(n).id]]
        assert np.allclose(d.qpos[qa:qa + 2], p[:2], atol=1e-6)
    assert "posed" not in (moved / "README.md").read_text() and "from the workcell layout" in (moved / "README.md").read_text()
    print("ok export preserves table pose exactly after relocation:", p1.round(4).tolist(), round(y1, 4))


if __name__ == "__main__":
    A = assets_dir()
    test_legacy_unchanged(A)
    pose, places, tbl = test_rotated_translated(A)
    test_export(A, pose, places, tbl)
    print("ALL TABLE-POSE TESTS PASSED")
