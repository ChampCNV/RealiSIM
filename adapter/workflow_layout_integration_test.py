"""LABELLED SYNTHETIC INTEGRATION TEST: fixture reconstruction -> upstream find_objects -> workflow_layout CLI scenarios.
    /workspace/photo2grasp/.venv/bin/python adapter/workflow_layout_integration_test.py
"""
import json, math, shutil, subprocess, sys, tempfile
from pathlib import Path
import numpy as np

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
PY = str(ROOT / ".venv/bin/python")
TMP = Path(tempfile.mkdtemp(prefix="wl_it_"))
WORK = TMP / "fx"
subprocess.run([PY, str(HERE / "workflow_layout_fixture.py"), str(WORK)], check=True, capture_output=True)
TR = json.loads((WORK / "truth.json").read_text())
SEL = json.loads((WORK / "selections.json").read_text())


def run(sel, ali, table="80,50,75"):
    sp, ap_, out = TMP / "s.json", TMP / "a.json", TMP / "out.json"
    sp.write_text(json.dumps(sel)); ap_.write_text(json.dumps(ali))
    r = subprocess.run([PY, str(HERE / "workflow_layout.py"), "align", "--work", str(WORK), "--table-cm", table, "--selections", str(sp),
                        "--alignment", str(ap_), "--out", str(out)], capture_output=True, text=True, timeout=900)
    assert out.exists(), r.stdout[-1500:] + r.stderr[-1500:]
    return json.loads(out.read_text()), r.returncode


def truth_in_base(name):
    b = np.array(TR["base"]); y = math.radians(TR["base_yaw"])
    R = np.array([[math.cos(y), math.sin(y)], [-math.sin(y), math.cos(y)]])
    return R @ (np.array(TR["objects"][name]) - b)


def err_cm(lay, robot="so101_main"):
    out = {}
    for o in lay["objects"]:
        rf = o["robot_frame"].get(robot)
        if rf and o["name"] in TR["objects"]:
            out[o["name"]] = float(np.linalg.norm(np.array([rf["x_m"], rf["y_m"]]) - truth_in_base(o["name"]))) * 100
    return out


tag_anchor = {"method": "tag", "tag_family": "tag36h11", "tag_id": 0, "tag_size_m": 0.06, "tag_in_base": TR["tag_in_base"]}
sel_clean = dict(SEL, objects=[o for o in SEL["objects"] if o["name"] != "ghost_mug"])

# 1. floor-drop scale (upstream) + tag anchor, orientation confirmed -> complete, cm-level
lay, rc = run(sel_clean, {"table_frame_flip": False, "robots": {"so101_main": tag_anchor}})
e = err_cm(lay)
assert lay["status"] == "complete" and rc == 0, (lay["status"], lay["blocking"])
assert lay["scale"]["method"] == "floor_drop" and abs(lay["scale"]["m_per_unit"] - TR["k"]) / TR["k"] < 0.01
assert all(v < 1.5 for v in e.values()), e
sp = lay["settings_patch"]
assert set(sp["workcell"]["placements"]) == {"soda_can", "tape_roll"} and sp["table_pose"] == lay["robots"][0]["table_in_base"]
print("ok 1 floor scale + tag anchor: complete, robot-frame errors (cm):", {k: round(v, 2) for k, v in e.items()})

# 2. no anchor -> needs_alignment; table-frame poses still present, no robot-frame, no settings patch
lay, rc = run(sel_clean, {})
assert lay["status"] == "needs_alignment" and lay["settings_patch"] is None
assert "no_robot_anchor" in [b["code"] for b in lay["blocking"]] and all(o["robot_frame"] == {} for o in lay["objects"])
assert all(o["table_frame"] for o in lay["objects"])
print("ok 2 no anchor: needs_alignment, table-frame layout only")

# 3. tape-measured anchor in the reported table frame (use the truth expressed in that frame)
ref, _ = run(sel_clean, {"table_frame_flip": False, "robots": {"so101_main": tag_anchor}})
bt = ref["robots"][0]["base_in_table"]
lay, rc = run(sel_clean, {"table_frame_flip": False, "robots": {"so101_main": {"method": "table_measured", **bt}}})
assert lay["status"] == "complete" and lay["robots"][0]["anchor"]["source"] == "measured"
# flipping the table frame with the SAME numbers must move the robot (proves the flip is honoured, not ignored)
lay_f, _ = run(sel_clean, {"table_frame_flip": True, "robots": {"so101_main": {"method": "table_measured", **bt}}})
assert max(err_cm(lay_f).values()) > 10
print("ok 3 tape-measured anchor matches; same numbers in the flipped frame are detected as a different layout")

# 4. two robots, no primary -> listed separately, needs_alignment; with primary -> patch for that robot only
sel2 = dict(sel_clean, robots=sel_clean["robots"] + [{"name": "so101_second", "library_id": "robotstudio_so101"}])
lay, _ = run(sel2, {"table_frame_flip": False, "robots": {"so101_main": tag_anchor}})
codes = [b["code"] for b in lay["blocking"]]
assert len(lay["robots"]) == 2 and "multiple_robots_no_primary" in codes and lay["status"] == "needs_alignment", codes
lay, _ = run(sel2, {"table_frame_flip": False, "primary_robot": "so101_main", "robots": {"so101_main": tag_anchor}})
assert lay["settings_patch"]["robot"] == "so101_main" and [r["status"] for r in lay["robots"]] == ["anchored", "needs_alignment"]
print("ok 4 multiple robots: not collapsed; primary required")

# 5. unmatched box -> estimated position, never reconstructed; cluster anchor -> needs_review
lay, _ = run(SEL, {"table_frame_flip": False, "robots": {"so101_main": {"method": "cluster", "yaw_deg": ref["robots"][0]["base_in_table"]["yaw_deg"]}}})
g = next(o for o in lay["objects"] if o["name"] == "ghost_mug")
assert g["status"] == "estimated" and g["provenance"] == "estimated_ray_plane" and "ghost_mug" not in (lay["settings_patch"] or {}).get("workcell", {}).get("placements", {})
assert lay["status"] == "needs_review" and {"object_unmatched", "anchor_estimated"} <= {b["code"] for b in lay["blocking"]}
print("ok 5 unmatched box estimated only; cluster anchor flagged; errors (cm):", {k: round(v, 2) for k, v in err_cm(lay).items()})

# 6. wrong table size -> rectangle coverage warning (scale from the height is unaffected, the fit is)
lay, _ = run(sel_clean, {"table_frame_flip": False, "robots": {"so101_main": tag_anchor}}, table="60,40,75")
assert any("fit the entered" in w for w in lay["warnings"]), lay["warnings"]
print("ok 6 wrong table size flagged")
shutil.rmtree(TMP, ignore_errors=True)
print("ALL LAYOUT INTEGRATION TESTS PASSED (synthetic fixture)")
