"""Robotics workspace robot catalog: the allowlisted MuJoCo Menagerie models this workspace can import.

Only these folders, at the pinned commit of the local sparse clone, with an allowlisted license, are importable.
No URL, script or path from a user, a model or the network is ever used to fetch a robot.
"""
import hashlib, re, subprocess
from pathlib import Path

MENAGERIE = Path("/workspace/photo2grasp/cache/menagerie")
REPO = "https://github.com/google-deepmind/mujoco_menagerie"
COMMIT = "f3b6013629c7b94f8ee93fd977b161fa8350f9bc"
LICENSES = {"Apache-2.0", "MIT", "BSD-3-Clause"}
MAX_BYTES = 60 * 1024 * 1024        # referenced model files (xml + meshes + textures + license)
MAX_FILES = 400

# license fingerprints: the first non-empty lines of each folder's LICENSE must match
LICENSE_SIGS = {
    "Apache-2.0": re.compile(r"Apache License\s+Version 2\.0", re.S),
    "MIT": re.compile(r"^MIT License", re.M),
    "BSD-3-Clause": re.compile(r"Redistribution and use in source and binary forms.*Neither the name", re.S),
}

CATALOG = [
    dict(id="trs_so_arm100", name="SO-ARM100 (5-DOF)", maker="The Robot Studio", dof=5, gripper="2-finger jaw", license="Apache-2.0",
         cues=["small open-source desktop arm, about 40 cm reach", "3D-printed white or coloured links with black STS3215 serial-bus servos",
               "static jaw plus one moving jaw", "base usually clamped to the table edge"],
         note="Simulation model used by the photo2grasp repository. Visually close to SO-101: confirm which one you own."),
    dict(id="robotstudio_so101", name="SO-101 (5-DOF)", maker="The Robot Studio", dof=5, gripper="2-finger jaw", license="Apache-2.0",
         cues=["successor of SO-100: same size and servo look", "redesigned wrist and gripper, often a leader/follower pair",
               "3D-printed links, black serial-bus servos"],
         note="Menagerie MJCF of the SO-101. Distinguishing SO-100 from SO-101 by video alone is unreliable."),
    dict(id="universal_robots_ur5e", name="UR5e (6-DOF)", maker="Universal Robots", dof=6, gripper="none (bare flange)", license="BSD-3-Clause",
         cues=["industrial collaborative arm, about 85 cm reach", "light grey/silver round joint housings with dark blue-grey caps",
               "cylindrical links, wrist with three revolute joints, teach pendant nearby"],
         note="No gripper in this model; objects can be composed but not grasped without adding an end effector."),
    dict(id="franka_emika_panda", name="Franka Emika Panda (7-DOF)", maker="Franka Emika", dof=7, gripper="parallel hand", license="Apache-2.0",
         cues=["white smooth research arm with 7 joints and a black/white parallel hand", "about 85 cm reach",
               "status light ring at the base / pilot buttons on the wrist"],
         note="Includes the Franka Hand."),
    dict(id="seeed_rebot_devarm", name="Seeed reBot DevArm (6-DOF)", maker="Seeed Studio", dof=6, gripper="2-finger parallel", license="MIT",
         cues=["compact 6-DOF arm with RobStride quasi-direct-drive actuators (round black motor housings)",
               "2-finger parallel gripper", "desktop scale"],
         note="Recent Menagerie addition; fewer real-world references."),
]
BY_ID = {r["id"]: r for r in CATALOG}


def git(*args):
    r = subprocess.run(["git", "-C", str(MENAGERIE), *args], capture_output=True, text=True, timeout=60)
    return r.returncode, r.stdout.strip(), r.stderr.strip()


def public_catalog():
    return [dict(r, library_id=r["id"], source="MuJoCo Menagerie", repo_path=f"{REPO}/tree/{COMMIT}/{r['id']}", commit=COMMIT,
                 bytes_limit=MAX_BYTES, status="Simulation model (not hardware-validated)") for r in CATALOG]


def scene_xml(folder):
    d = Path(folder)
    if (d / "scene.xml").exists():
        return d / "scene.xml"
    raise ValueError(f"{d.name}: no scene.xml")


def detect_license(folder):
    lic = Path(folder) / "LICENSE"
    if not lic.exists():
        return None
    text = lic.read_text(errors="replace")[:6000]
    return next((k for k, sig in LICENSE_SIGS.items() if sig.search(text)), None)


def verify_source(library_id):
    """Allowlist + pinned commit + clean tree + license check. Returns (folder Path, facts dict)."""
    if library_id not in BY_ID:
        raise ValueError("that robot model is not in the supported catalog")
    entry = BY_ID[library_id]
    folder = (MENAGERIE / library_id).resolve()
    if folder.parent != MENAGERIE.resolve() or not folder.is_dir():
        raise ValueError(f"{library_id} is not present in the pinned Menagerie checkout")
    code, head, err = git("rev-parse", "HEAD")
    if code or head != COMMIT:
        raise ValueError(f"Menagerie checkout is at {head[:12] or '?'}, expected pinned commit {COMMIT[:12]}")
    code, dirty, _ = git("status", "--porcelain", "--", library_id)
    if code or dirty:
        raise ValueError(f"local modifications in {library_id}: refusing to import an unverified model")
    lic = detect_license(folder)
    if lic != entry["license"] or lic not in LICENSES:
        raise ValueError(f"license check failed for {library_id}: found {lic or 'none'}, expected {entry['license']}")
    for p in folder.rglob("*"):
        if p.is_symlink():
            raise ValueError(f"{library_id} contains a symlink ({p.name}); refusing")
    return folder, dict(library_id=library_id, name=entry["name"], license=lic, commit=COMMIT, source="MuJoCo Menagerie", repo=REPO)


def referenced_files(folder):
    """scene.xml, every XML it includes (transitively), every mesh/texture/hfield file the compiled spec references,
    LICENSE and README.md. Paths relative to the folder; refuses anything outside it."""
    import mujoco
    folder = Path(folder).resolve()
    out, todo = set(), [scene_xml(folder)]
    while todo:
        x = todo.pop()
        rel = x.resolve().relative_to(folder)
        if rel in out:
            continue
        out.add(rel)
        for inc in re.findall(r'<include\s+file="([^"]+)"', x.read_text()):
            todo.append((x.parent / inc).resolve())
    spec = mujoco.MjSpec.from_file(str(scene_xml(folder)))
    base = Path(spec.modelfiledir or folder)
    for coll, sub in ((spec.meshes, spec.meshdir), (spec.textures, spec.texturedir), (getattr(spec, "hfields", []), spec.meshdir)):
        for a in coll:
            f = getattr(a, "file", "")
            if f:
                p = (base / (sub or "") / f).resolve() if not Path(f).is_absolute() else Path(f).resolve()
                out.add(p.relative_to(folder))  # raises if outside the folder
    for extra in ("LICENSE", "README.md"):
        if (folder / extra).exists():
            out.add(Path(extra))
    files = sorted(out)
    total = sum((folder / f).stat().st_size for f in files)
    if len(files) > MAX_FILES or total > MAX_BYTES:
        raise ValueError(f"model is too large to import ({len(files)} files, {total / 1e6:.1f} MB; limit {MAX_FILES} files / {MAX_BYTES / 1e6:.0f} MB)")
    return files, total
