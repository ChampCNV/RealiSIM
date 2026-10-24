"""Step 2: raw mesh + physical properties -> MuJoCo-ready object (MJCF + meshes)."""
import json
from pathlib import Path

import coacd
import numpy as np
import trimesh
from scipy.spatial import ConvexHull, cKDTree

VISUAL_FACES = 50000
COLLISION_FACES = 30000  # enough to keep rims and thin walls; CoACD time grows with face count
COACD_THRESHOLD = 0.02  # max concavity left in a piece (fraction of object size); 0.05 rounded off edges
ERROR_SAMPLES = 200_000  # surface points for the collision accuracy check (~0.5 mm spacing on a can)


def clean(mesh):
    """Keep the biggest connected piece (drops floating specks) and fix normals."""
    parts = mesh.split(only_watertight=False)
    if len(parts) > 1:
        mesh = max(parts, key=lambda p: p.area)
    mesh.fix_normals()
    return mesh


def simplified(mesh, face_count):
    if len(mesh.faces) <= face_count:
        return mesh.copy()
    return mesh.simplify_quadric_decimation(face_count=face_count)


def straighten(mesh):
    """Rest the object on the flat face closest to 'up' in the photo.

    Image-to-3D output is tilted by the camera angle; this picks the stable
    resting pose that needs the smallest rotation from that.
    """
    poses, _ = mesh.compute_stable_poses()
    up = np.array([0, 0, 1.0])
    # For each pose, how well does the mesh's original up line up with world up after the transform?
    best = max(poses, key=lambda T: (T[:3, :3] @ up) @ up)
    mesh.apply_transform(best)
    return mesh


def level_top(mesh):
    """Rotate so the main upward-facing top surface is exactly horizontal (tables, shelves, trays)."""
    z = mesh.vertices[:, 2]
    top = (mesh.face_normals[:, 2] > 0.8) & (mesh.triangles_center[:, 2] > z.min() + 0.9 * np.ptp(z))
    if top.sum() < 10:
        return mesh
    normal = np.average(mesh.face_normals[top], axis=0, weights=mesh.area_faces[top])
    normal /= np.linalg.norm(normal)
    axis = np.cross(normal, [0, 0, 1])
    angle = np.arccos(np.clip(normal[2], -1, 1))
    if np.linalg.norm(axis) > 1e-9:
        mesh.apply_transform(trimesh.transformations.rotation_matrix(angle, axis / np.linalg.norm(axis),
                                                                     mesh.centroid))
    return mesh


def scale_and_center(mesh, props):
    """Scale to real size and put the body origin at the bottom center.

    `size_m` = [length, width, height] stretches each axis (fixes wrong proportions from the 3D model);
    otherwise `height_m` scales uniformly.
    """
    # Line the footprint up with x/y (tightest rectangle), otherwise per-axis scaling stretches it diagonally.
    to_rect, _ = trimesh.bounds.oriented_bounds_2D(mesh.vertices[:, :2])
    yaw = np.eye(4)
    yaw[:2, :2], yaw[:2, 3] = to_rect[:2, :2], to_rect[:2, 2]
    mesh.apply_transform(yaw)
    if mesh.extents[1] > mesh.extents[0]:  # make x the long side so it matches [length, width]
        mesh.apply_transform(trimesh.transformations.rotation_matrix(np.pi / 2, [0, 0, 1]))
    if "size_m" in props:
        mesh.apply_transform(np.diag(list(np.array(props["size_m"]) / mesh.extents) + [1]))
    else:
        mesh.apply_scale(props["height_m"] / mesh.extents[2])
    lo, hi = mesh.bounds
    mesh.apply_translation([-(lo[0] + hi[0]) / 2, -(lo[1] + hi[1]) / 2, -lo[2]])
    return mesh


def decompose(mesh, out_dir, name, threshold, max_hulls):
    """Split into convex pieces for collision (MuJoCo treats every mesh as convex)."""
    for old in out_dir.glob(f"{name}_collision_*.obj"):
        old.unlink()
    parts = coacd.run_coacd(coacd.Mesh(mesh.vertices, mesh.faces), threshold=threshold,
                            max_convex_hull=max_hulls, preprocess_resolution=100, resolution=4000,
                            mcts_iterations=250, mcts_max_depth=4, max_ch_vertex=256)
    files, pieces = [], []
    for i, (verts, faces) in enumerate(parts):
        f = f"{name}_collision_{i}.obj"
        piece = trimesh.Trimesh(verts, faces)
        piece.export(out_dir / f)
        files.append(f)
        pieces.append(piece)
    return files, pieces


def primitive(kind, extents):
    """Exact collision shape from the measured size: (MJCF geom attributes, the same shape as a mesh)."""
    lx, ly, h = extents
    if kind == "cylinder":
        r = (lx + ly) / 4
        shape = trimesh.creation.cylinder(radius=r, height=h, sections=256)
        attrs = f'type="cylinder" size="{r:.6g} {h / 2:.6g}"'
    elif kind == "box":
        shape = trimesh.creation.box([lx, ly, h])
        attrs = f'type="box" size="{lx / 2:.6g} {ly / 2:.6g} {h / 2:.6g}"'
    else:
        raise ValueError(f"unknown collision shape '{kind}' (use cylinder, box or mesh)")
    shape.apply_translation([0, 0, h / 2])
    return f'{attrs} pos="0 0 {h / 2:.6g}"', shape


def inside_any(points, hulls, tol):
    inside = np.zeros(len(points), bool)
    for eq in hulls:
        inside |= np.all(points @ eq[:, :3].T + eq[:, 3] <= tol, axis=1)
    return inside


def collision_error(mesh, pieces):
    """How far the collision surface strays from the object surface, in mm.

    overshoot: collision surface outside the object (fingers stop early, object looks fatter);
    undershoot: object surface no collision piece covers (fingers sink in, contacts are missed).
    """
    surf, face = trimesh.sample.sample_surface(mesh, ERROR_SAMPLES, seed=0)
    normals = mesh.face_normals[face]
    tree = cKDTree(surf)
    hulls = [ConvexHull(p.vertices).equations for p in pieces]
    tol = 1e-5
    area = sum(p.area for p in pieces)
    out = []
    for i, piece in enumerate(pieces):
        pts, _ = trimesh.sample.sample_surface(piece, max(200, int(ERROR_SAMPLES * piece.area / area)), seed=0)
        others = hulls[:i] + hulls[i + 1:]
        pts = pts[~inside_any(pts, others, -tol)]  # faces buried inside another piece don't touch anything
        dist, j = tree.query(pts)
        outside = ((pts - surf[j]) * normals[j]).sum(1) > 0
        out.append(np.where(outside, dist, 0.0))
    over = np.concatenate(out)
    uncovered = surf[~inside_any(surf, hulls, tol)]
    boundary = np.concatenate([trimesh.sample.sample_surface(p, 20000, seed=0)[0] for p in pieces])
    under = cKDTree(boundary).query(uncovered)[0] if len(uncovered) else np.zeros(1)
    mm = lambda a, q: round(float(np.percentile(a, q)) * 1000, 2)  # noqa: E731
    return {"overshoot_mm_p95": mm(over, 95), "overshoot_mm_max": mm(over, 100),
            "undershoot_mm_p95": mm(under, 95), "undershoot_mm_max": mm(under, 100),
            "surface_within_1mm_pct": round(100 * (1 - (under > 0.001).sum() / len(surf)), 1)}


def inertia(mesh, mass_kg):
    """Center of mass and inertia tensor, assuming the object is solid with uniform density."""
    if not mesh.is_watertight:  # volume is meaningless on an open mesh; its hull is a safe stand-in
        mesh = mesh.convex_hull
    mesh.density = mass_kg / mesh.volume
    I = mesh.moment_inertia  # about the center of mass
    full = [I[0, 0], I[1, 1], I[2, 2], I[0, 1], I[0, 2], I[1, 2]]
    return mesh.center_mass, full


def export_textured(textured_mesh_path, transform, out_dir, name):
    """Write the generator's textured mesh (UVs + colour image) with the given transform; returns the image file."""
    tex = trimesh.load(textured_mesh_path, force="mesh")
    tex.apply_transform(trimesh.transformations.rotation_matrix(np.pi / 2, [1, 0, 0]))  # glTF Y-up -> Z-up
    tex.apply_transform(transform)
    image = tex.visual.material.to_simple().image  # PBR base colour as a plain image
    image_file = f"{name}_texture.png"
    image.convert("RGB").save(out_dir / image_file)
    uv = tex.visual.uv.copy()
    # Plain OBJ with positions + UVs only; MuJoCo gets the texture through the MJCF material instead.
    lines = [f"v {x:.6f} {y:.6f} {z:.6f}" for x, y, z in tex.vertices]
    lines += [f"vt {u:.6f} {v:.6f}" for u, v in uv]
    lines += [f"f {a}/{a} {b}/{b} {c}/{c}" for a, b, c in tex.faces + 1]
    (out_dir / f"{name}_visual.obj").write_text("\n".join(lines) + "\n")
    return image_file


def fmt(values):
    return " ".join(f"{v:.6g}" for v in values)


def mesh_to_mjcf(mesh_path, name, props, out_dir, textured_mesh_path=None):
    """Writes <out_dir>/<name>.xml plus its mesh files and returns the XML path."""
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    mesh = trimesh.load(mesh_path, force="mesh")
    mesh = clean(mesh)
    start = mesh.vertices.copy()
    mesh = straighten(mesh)
    if "rotate_deg" in props:  # manual fix when the automatic pose is wrong, e.g. open frames lying on their side
        rx, ry, rz = np.radians(props["rotate_deg"])
        mesh.apply_transform(trimesh.transformations.euler_matrix(rx, ry, rz))
        mesh = straighten(mesh)  # settle onto the nearest flat face again
    if props.get("level_top"):
        mesh = level_top(mesh)
    mesh = scale_and_center(mesh, props)
    # Everything above is one affine transform; recover it so the textured mesh can get exactly the same one.
    transform = np.eye(4)
    transform[:3, :] = np.linalg.lstsq(np.c_[start, np.ones(len(start))], mesh.vertices, rcond=None)[0].T

    # Cans, bottles and boxes: one primitive at the measured size is the exact contact surface, where any
    # decomposition of the generated mesh would copy its wobble. Everything else: convex pieces.
    kind = props.get("collision", "mesh")
    if kind == "mesh":
        collision_files, pieces = decompose(simplified(mesh, COLLISION_FACES), out_dir, name,
                                            props.get("coacd_threshold", COACD_THRESHOLD),
                                            props.get("max_convex_hulls", -1))
        body_shape = mesh
    else:
        for old in out_dir.glob(f"{name}_collision_*.obj"):
            old.unlink()
        collision_files = []
        primitive_attrs, shape = primitive(kind, mesh.extents)  # extents == size_m once scaled
        pieces = [shape]
        body_shape = shape
    accuracy = collision_error(mesh, pieces)

    mass = props.get("mass_kg")
    if mass is None and props.get("static"):
        mass = 0.0  # fixed in place, mass never used
    elif mass is None:  # mass from size: density (e.g. from a material lookup) x volume
        mass = props["density_kg_m3"] * (body_shape.volume if body_shape.is_watertight
                                         else body_shape.convex_hull.volume)
    com, full_inertia = inertia(body_shape, mass)  # on the full-resolution, watertight shape

    visual_file = f"{name}_visual.obj"
    if textured_mesh_path is not None:
        visual_material = export_textured(textured_mesh_path, transform, out_dir, name)
    else:
        visual = simplified(mesh, VISUAL_FACES)
        trimesh.Trimesh(visual.vertices, visual.faces).export(out_dir / visual_file)
        visual_material = None

    meshes = [f'    <mesh name="{name}_visual" file="{visual_file}"/>']
    meshes += [f'    <mesh name="{Path(f).stem}" file="{f}"/>' for f in collision_files]
    if visual_material:
        meshes += [f'    <texture name="{name}_texture" type="2d" file="{visual_material}"/>',
                   f'    <material name="{name}_material" texture="{name}_texture"/>']
        geoms = [f'      <geom class="{name}_visual" mesh="{name}_visual" material="{name}_material"/>']
    else:
        geoms = [f'      <geom class="{name}_visual" mesh="{name}_visual" rgba="{fmt(props.get("rgba", [0.7, 0.7, 0.7, 1]))}"/>']
    geoms += [f'      <geom class="{name}_collision" mesh="{Path(f).stem}"/>' for f in collision_files]
    if kind != "mesh":
        geoms += [f'      <geom class="{name}_collision" {primitive_attrs}/>']
    nl = "\n"
    if props.get("static"):  # furniture: fixed in place, no joint
        body_dynamics = ""
    else:
        body_dynamics = (f'      <freejoint name="{name}_joint"/>\n'
                         f'      <inertial pos="{fmt(com)}" mass="{mass:.6g}" fullinertia="{fmt(full_inertia)}"/>')

    xml = f"""<mujoco model="{name}">
  <!-- {props.get("source", "")} -->
  <compiler meshdir="."/>
  <default>
    <default class="{name}_visual">
      <geom type="mesh" contype="0" conaffinity="0" group="2"/>
    </default>
    <default class="{name}_collision">
      <geom type="mesh" group="3" condim="4" friction="{fmt(props["friction"])}"/>
    </default>
  </default>
  <asset>
{nl.join(meshes)}
  </asset>
  <worldbody>
    <body name="{name}">
{body_dynamics}
{nl.join(geoms)}
    </body>
  </worldbody>
</mujoco>
"""
    xml_path = out_dir / f"{name}.xml"
    xml_path.write_text(xml)

    summary = {**props, "size_m": mesh.extents.round(4).tolist(), "mass_kg": round(float(mass), 5),
               "center_of_mass": com.round(4).tolist(),
               "collision": kind, "collision_pieces": len(pieces), "collision_accuracy": accuracy}
    (out_dir / "properties.json").write_text(json.dumps(summary, indent=2))
    return xml_path
