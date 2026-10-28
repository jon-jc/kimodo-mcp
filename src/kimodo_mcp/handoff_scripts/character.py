"""Fit and animate the supplied static character in an isolated Blender scene.

This is an asset-specific rig, not a general automatic character rigger. The
landmarks were fitted to the inspected FBX in its original metre coordinates.
Run with Blender Python. Source files and other scenes are never edited.
"""

import hashlib
import json
import math
from pathlib import Path

import numpy as np

SOURCE_SHA256 = "afd2725b308000d74c04df2823fc82a488a803a15fbd6bc2dfb8e423c49a42eb"


def validate_target(source_fbx):
    """Reject a different asset before importing or modifying Blender data."""
    source_fbx = Path(source_fbx).resolve(strict=True)
    if source_fbx.suffix.lower() != ".fbx" or not source_fbx.is_file():
        raise ValueError("The registered target must be an FBX file")
    if source_fbx.stat().st_size > 16 * 1024 * 1024:
        raise ValueError("The registered target exceeds its size limit")
    if hashlib.sha256(source_fbx.read_bytes()).hexdigest() != SOURCE_SHA256:
        raise ValueError("This fitted rig supports only the inspected character FBX")
    return source_fbx


def smooth(a, b, x):
    t = max(0.0, min(1.0, (x - a) / (b - a)))
    return t * t * (3 - 2 * t)


def landmarks():
    """Bone heads and tails are in the imported character's world space."""
    joints = {
        "Root": ((0, 0, 0), (0, 0, 0.08), None),
        "Hips": ((0, -0.06, 0.59), (0, -0.06, 0.65), "Root"),
        "Spine1": ((0, -0.06, 0.65), (0, -0.06, 0.69), "Hips"),
        "Spine2": ((0, -0.06, 0.69), (0, -0.06, 0.74), "Spine1"),
        "Chest": ((0, -0.06, 0.74), (0, -0.06, 0.815), "Spine2"),
        "Neck1": ((0, -0.06, 0.815), (0, -0.06, 0.84), "Chest"),
        "Neck2": ((0, -0.06, 0.84), (0, -0.06, 0.865), "Neck1"),
        "Head": ((0, -0.06, 0.865), (0, -0.06, 0.98), "Neck2"),
    }
    for side, sign in [("Left", 1), ("Right", -1)]:

        def p(x, y, z):
            return sign * x, y, z

        joints.update(
            {
                side + "Shoulder": (p(0.025, -0.055, 0.80), p(0.12, -0.055, 0.80), "Chest"),
                side + "Arm": (p(0.12, -0.055, 0.80), p(0.265, -0.055, 0.795), side + "Shoulder"),
                side + "ForeArm": (p(0.265, -0.055, 0.795), p(0.38, -0.055, 0.795), side + "Arm"),
                side + "Hand": (p(0.38, -0.055, 0.795), p(0.455, -0.055, 0.795), side + "ForeArm"),
                side + "Leg": (p(0.068, -0.06, 0.565), p(0.07, -0.067, 0.325), "Hips"),
                side + "Shin": (p(0.07, -0.067, 0.325), p(0.072, -0.05, 0.085), side + "Leg"),
                side + "Foot": (p(0.072, -0.05, 0.085), p(0.072, -0.115, 0.025), side + "Shin"),
                side + "ToeBase": (p(0.072, -0.115, 0.025), p(0.072, -0.148, 0.018), side + "Foot"),
            }
        )
    return joints


def weights_for_vertex(v, category):
    """Partition anatomical regions before blending adjacent joints.

    The archive contains disconnected cloth strips and hair shells. Heat weights
    alone can bind them to nearby limbs; these explicit regions keep head pieces
    together and blend adjacent joints without cross-body influence.
    """
    x, y, z = v
    side = "Left" if x >= 0 else "Right"
    ax = abs(x)
    if category == "head":
        return {"Head": 1.0}
    if category == "sleeve" or (z > 0.755 and ax > 0.05):
        if ax < 0.09:
            w = smooth(0.05, 0.09, ax)
            return {"Chest": 1 - w, side + "Shoulder": w}
        if ax < 0.15:
            w = smooth(0.09, 0.15, ax)
            return {side + "Shoulder": 1 - w, side + "Arm": w}
        if ax < 0.295:
            w = smooth(0.24, 0.29, ax)
            return {side + "Arm": 1 - w, side + "ForeArm": w}
        w = smooth(0.36, 0.405, ax)
        return {side + "ForeArm": 1 - w, side + "Hand": w}
    if category == "skirt":
        # Long outer fabric follows pelvis and same-side thigh, never the shin.
        # This intentionally provides no cloth simulation or collision response.
        w = 0.65 * (1 - smooth(0.48, 0.65, z))
        return {"Hips": 1 - w, side + "Leg": w}
    if z < 0.60:
        hip = smooth(0.52, 0.61, z)
        if z > 0.47:
            return {"Hips": hip, side + "Leg": 1 - hip}
        if z > 0.27:
            w = 1 - smooth(0.29, 0.36, z)
            return {side + "Leg": 1 - w, side + "Shin": w}
        if z > 0.09:
            w = 1 - smooth(0.09, 0.15, z)
            return {side + "Shin": 1 - w, side + "Foot": w}
        w = 1 - smooth(-0.137, -0.11, y)
        return {side + "Foot": 1 - w, side + "ToeBase": w}
    intervals = [
        (0.62, 0.67, "Hips", "Spine1"),
        (0.67, 0.71, "Spine1", "Spine2"),
        (0.71, 0.77, "Spine2", "Chest"),
        (0.80, 0.83, "Chest", "Neck1"),
        (0.83, 0.855, "Neck1", "Neck2"),
        (0.855, 0.88, "Neck2", "Head"),
    ]
    for low, high, a, b in intervals:
        if z < high:
            w = smooth(low, high, z)
            return {a: 1 - w, b: w}
    return {"Head": 1.0}


def part_category(points):
    lo = np.min(points, axis=0)
    hi = np.max(points, axis=0)
    # Facial geometry and long hair are disconnected parts but share head motion.
    if hi[2] > 0.88 or lo[2] > 0.845:
        return "head"
    if (lo[0] > 0.105 or hi[0] < -0.105) and lo[2] > 0.61:
        return "sleeve"
    if hi[2] > 0.60 and lo[2] < 0.51 and (lo[0] > 0.025 or hi[0] < -0.025):
        return "skirt"
    return "body"


def build_character(source_fbx, output_dir):
    import bpy
    from mathutils import Matrix

    source_fbx = validate_target(source_fbx)
    output_dir = Path(output_dir).resolve()
    output_dir.mkdir(parents=True, exist_ok=True)
    previous = bpy.context.window.scene
    scene = bpy.data.scenes.new("KimodoFittedCharacter")
    try:
        bpy.context.window.scene = scene
        bpy.ops.import_scene.fbx(filepath=str(source_fbx), use_anim=False, automatic_bone_orientation=False)
        meshes = [o for o in scene.objects if o.type == "MESH"]
        if any(o.type == "ARMATURE" for o in scene.objects):
            raise ValueError("Unexpected existing rig; refusing to replace it")
        # Bake imported object transforms into independent mesh datablocks.
        for obj in meshes:
            world = obj.matrix_world.copy()
            obj.data = obj.data.copy()
            obj.data.transform(world)
            obj.parent = None
            obj.matrix_world = Matrix.Identity(4)
        bpy.ops.object.select_all(action="DESELECT")
        data = bpy.data.armatures.new("AnimeGirlSkeleton")
        rig = bpy.data.objects.new("AnimeGirlRig", data)
        scene.collection.objects.link(rig)
        bpy.context.view_layer.objects.active = rig
        rig.select_set(True)
        bpy.ops.object.mode_set(mode="EDIT")
        for name, (head, tail, parent) in landmarks().items():
            bone = data.edit_bones.new(name)
            bone.head = head
            bone.tail = tail
            if parent:
                bone.parent = data.edit_bones[parent]
            bone.use_connect = False
            bone.use_deform = name != "Root"
        bpy.ops.object.mode_set(mode="OBJECT")
        classifications = {}
        for obj in meshes:
            pts = np.array([list(v.co) for v in obj.data.vertices])
            category = part_category(pts)
            classifications[obj.name] = category
            groups = {n: obj.vertex_groups.new(name=n) for n in landmarks() if n != "Root"}
            for v in obj.data.vertices:
                weights = {k: w for k, w in weights_for_vertex(v.co, category).items() if w > 1e-8}
                total = sum(weights.values())
                for bone, w in weights.items():
                    groups[bone].add([v.index], w / total, "REPLACE")
            modifier = obj.modifiers.new("CharacterSkin", "ARMATURE")
            modifier.object = rig
            # FBX/Unreal use linear blend skinning; preview the same deformation.
            modifier.use_deform_preserve_volume = False
            obj.parent = rig
            # Smooth shading preserves the original geometry and UV coordinates.
            for poly in obj.data.polygons:
                poly.use_smooth = True
        scene.unit_settings.system = "METRIC"
        scene.unit_settings.scale_length = 1.0
        scene.render.fps = 30
        rig.show_in_front = True
        rig["source_sha256"] = SOURCE_SHA256
        rig["rigging_method"] = "Asset-fitted T-pose landmarks and normalized anatomical weights"
        rig["root_motion_mode"] = "root_horizontal_translation_and_heading_hips_vertical"
        report = {
            "source": str(source_fbx),
            "source_sha256": SOURCE_SHA256,
            "scene": scene.name,
            "rig": rig.name,
            "bones": [
                {
                    "name": b.name,
                    "parent": b.parent.name if b.parent else None,
                    "head": list(b.head_local),
                    "tail": list(b.tail_local),
                    "rest_matrix": [list(r) for r in b.matrix_local],
                }
                for b in data.bones
            ],
            "parts": classifications,
            "vertices": sum(len(o.data.vertices) for o in meshes),
            "polygons": sum(len(o.data.polygons) for o in meshes),
            "height_m": float(max(v.co.z for o in meshes for v in o.data.vertices)),
            "limitations": [
                "No finger or facial animation; source fingers lack separate topology.",
                "Hair follows the head rigidly; no hair physics.",
                "Cloth uses skeletal weights; collisions and cloth simulation are not included.",
            ],
        }
        (output_dir / "rig-report.json").write_text(json.dumps(report, indent=2))
        return scene, rig, meshes, report
    finally:
        bpy.context.window.scene = previous


def apply_motion(scene, rig, motion_file, source_metadata=None, root_motion=True):
    """Retarget SOMA global rotations with explicit rest/axis conversion.

    Kimodo uses +Y up,+Z forward; target Blender uses +Z up,-Y forward.
    Source rotation matrices rotate its neutral offsets (identity bind rotation).
    For each target bone with rest orientation B, desired orientation is C G C^-1 B.
    Target head positions use target rest offsets transformed by the posed parent;
    this preserves target lengths instead of copying source joint positions.
    """
    import bpy
    from mathutils import Matrix, Vector

    meta = json.loads(Path(source_metadata).read_text()) if source_metadata else {}
    with np.load(motion_file, allow_pickle=False) as data:
        rots = np.array(data["global_rot_mats"], dtype=np.float64)
        positions = np.array(data["posed_joints"], dtype=np.float64)
        if "joint_names" not in data and "joint_names" not in meta:
            raise ValueError("Motion needs joint_names metadata")
        names = [str(n) for n in data["joint_names"]] if "joint_names" in data else meta["joint_names"]
        fps = float(data["fps"]) if "fps" in data else 30.0
    if rots.ndim != 4 or rots.shape[2:] != (3, 3) or positions.shape != rots.shape[:2] + (3,):
        raise ValueError("Motion arrays must have shapes T,J,3,3 and T,J,3")
    if not 1 <= len(rots) <= 2400 or rots.shape[1] not in (30, 77) or not 1 <= fps <= 120:
        raise ValueError("Expected bounded SOMA motion at 1..120 FPS")
    if len(names) != rots.shape[1] or not np.isfinite(rots).all() or not np.isfinite(positions).all():
        raise ValueError("Invalid motion metadata or nonfinite values")
    if not np.allclose(rots @ np.swapaxes(rots, -1, -2), np.eye(3), atol=0.015) or not np.allclose(
        np.linalg.det(rots), 1, atol=0.015
    ):
        raise ValueError("Motion contains invalid rotation matrices")
    names_to_index = {n: i for i, n in enumerate(names)}
    missing = set(landmarks()) - {"Root"} - set(names)
    if missing:
        raise ValueError("Missing mapped joints: " + ", ".join(sorted(missing)))
    c = Matrix(((1, 0, 0), (0, 0, -1), (0, 1, 0)))
    # Match the target's total leg-chain length, preserving its own proportions.
    target_leg = sum(
        (rig.data.bones[b].tail_local - rig.data.bones[b].head_local).length for b in ["LeftLeg", "LeftShin"]
    )
    # Segment lengths are invariant under the source's rigid FK rotations.
    source_leg = sum(
        float(np.linalg.norm(positions[0, names_to_index[a]] - positions[0, names_to_index[b]]))
        for a, b in [("LeftLeg", "LeftShin"), ("LeftShin", "LeftFoot")]
    )
    if source_leg <= 1e-6:
        raise ValueError("Source leg segments must have positive length")
    scale = target_leg / source_leg
    hips_idx = names_to_index["Hips"]
    root_start = c @ Vector(positions[0, hips_idx])
    start_forward = (c @ Matrix(rots[0, hips_idx].tolist()) @ c.transposed()) @ Vector((0, -1, 0))
    start_yaw = math.atan2(start_forward.x, -start_forward.y)
    rest_hips = rig.data.bones["Hips"].head_local.copy()
    if rig.animation_data:
        rig.animation_data_clear()
    previous = bpy.context.window.scene
    try:
        bpy.context.window.scene = scene
        scene.render.fps = round(fps)
        scene.frame_start = 1
        scene.frame_end = len(rots)
        for frame in range(len(rots)):
            scene.frame_set(frame + 1)
            root = c @ Vector(positions[frame, hips_idx])
            translation = (root - root_start) * scale
            horizontal = Vector((translation.x, translation.y, 0)) if root_motion else Vector((0, 0, 0))
            forward = (c @ Matrix(rots[frame, hips_idx].tolist()) @ c.transposed()) @ Vector((0, -1, 0))
            yaw = math.atan2(forward.x, -forward.y) - start_yaw if root_motion else 0.0
            heading = Matrix.Rotation(yaw, 4, "Z")
            poses = {}
            for bone in rig.data.bones:
                if bone.name == "Root":
                    matrix = Matrix.Translation(horizontal) @ heading @ bone.matrix_local
                else:
                    rot = c @ Matrix(rots[frame, names_to_index[bone.name]].tolist()) @ c.transposed()
                    orientation = rot @ bone.matrix_local.to_3x3()
                    if bone.name == "Hips":
                        head = heading @ rest_hips + horizontal + Vector((0, 0, translation.z))
                    else:
                        parent = bone.parent
                        head = poses[parent.name] @ parent.matrix_local.inverted() @ bone.head_local
                    matrix = orientation.to_4x4()
                    matrix.translation = head
                poses[bone.name] = matrix
                pb = rig.pose.bones[bone.name]
                parent_pose = poses[bone.parent.name] if bone.parent else Matrix.Identity(4)
                parent_rest = bone.parent.matrix_local if bone.parent else Matrix.Identity(4)
                basis = bone.convert_local_to_pose(
                    matrix,
                    bone.matrix_local,
                    parent_matrix=parent_pose,
                    parent_matrix_local=parent_rest,
                    invert=True,
                )
                pb.rotation_mode = "QUATERNION"
                loc, quat, scl = basis.decompose()
                # Equivalent quaternion signs are made consistent for interpolation.
                if frame and pb.rotation_quaternion.dot(quat) < 0:
                    quat.negate()
                pb.location = loc
                pb.rotation_quaternion = quat
                pb.scale = scl
                pb.keyframe_insert("location", frame=frame + 1, group=pb.name)
                pb.keyframe_insert("rotation_quaternion", frame=frame + 1, group=pb.name)
        rig.animation_data.action.name = "Kimodo_" + Path(motion_file).stem
        rig["motion_source"] = str(Path(motion_file).resolve())
        rig["translation_scale"] = scale
        scene.frame_set(1)
        return {
            "frames": len(rots),
            "fps": fps,
            "translation_scale": scale,
            "root_motion": root_motion,
            "mapped_bones": len(landmarks()) - 1,
            "motion_source": str(Path(motion_file).resolve()),
        }
    finally:
        bpy.context.window.scene = previous


def export_character(scene, rig, meshes, output_fbx, animated=True):
    import bpy

    previous = bpy.context.window.scene
    original_name = rig.name
    if bpy.data.objects.get("Armature") not in (None, rig):
        raise RuntimeError("A different object named Armature prevents deterministic Unreal export")
    try:
        bpy.context.window.scene = scene
        # Unreal removes this conventional wrapper, leaving the animated Root as
        # the first skeleton bone instead of a static extra armature-object bone.
        rig.name = "Armature"
        bpy.ops.object.select_all(action="DESELECT")
        for obj in [rig] + meshes:
            obj.select_set(True)
        bpy.context.view_layer.objects.active = rig
        bpy.ops.export_scene.fbx(
            filepath=str(Path(output_fbx).resolve()),
            use_selection=True,
            object_types={"ARMATURE", "MESH"},
            add_leaf_bones=False,
            use_armature_deform_only=False,
            bake_anim=animated,
            bake_anim_use_all_bones=True,
            bake_anim_use_nla_strips=False,
            bake_anim_use_all_actions=False,
            bake_anim_step=1.0,
            bake_anim_simplify_factor=0.0,
            # Bake metres into centimetre numeric transforms. Some native Unreal
            # import routes do not enable FBX scene-unit conversion by default.
            axis_forward="-Y",
            axis_up="Z",
            apply_unit_scale=True,
            apply_scale_options="FBX_SCALE_NONE",
            path_mode="COPY",
            embed_textures=True,
        )
    finally:
        rig.name = original_name
        bpy.context.window.scene = previous


def main(argv=None):
    """Blender CLI entry point; all paths are assigned by the local operator."""
    import argparse
    import sys

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-fbx", required=True)
    parser.add_argument("--motion", required=True)
    parser.add_argument("--source-metadata")
    parser.add_argument("--output-dir", required=True)
    parser.add_argument("--in-place", action="store_true")
    if argv is None:
        argv = sys.argv[sys.argv.index("--") + 1 :] if "--" in sys.argv else []
    args = parser.parse_args(argv)
    out = Path(args.output_dir).resolve()
    if out.exists() and any(out.iterdir()):
        raise ValueError("Output directory must be new or empty")
    import bpy

    scene, rig, meshes, rig_report = build_character(args.source_fbx, out)
    motion_report = apply_motion(scene, rig, args.motion, args.source_metadata, not args.in_place)
    for material in {slot.material for obj in meshes for slot in obj.material_slots if slot.material}:
        if material.use_nodes:
            for node in material.node_tree.nodes:
                if node.type == "TEX_IMAGE" and node.image:
                    node.image.pack()
    export_character(scene, rig, meshes, out / "character-skeletal-mesh.fbx", False)
    export_character(scene, rig, [], out / "character-animation.fbx", True)
    export_character(scene, rig, meshes, out / "character-animated.fbx", True)
    bpy.data.libraries.write(str(out / "character.blend"), {scene}, fake_user=True, compress=True)
    (out / "motion-report.json").write_text(json.dumps(motion_report, indent=2))
    return motion_report


def validate_animation(scene, rig, meshes):
    """Check every frame and vertex, not just a few rendered poses."""
    import bpy

    previous = bpy.context.window.scene
    report = {
        "frames": scene.frame_end,
        "finite": True,
        "unweighted_vertices": 0,
        "max_weight_sum_error": 0.0,
        "max_bone_length_error_m": 0.0,
        "bounds_min": [1e20] * 3,
        "bounds_max": [-1e20] * 3,
    }
    feet = {"LeftFoot": [], "RightFoot": []}
    try:
        bpy.context.window.scene = scene
        for obj in meshes:
            for vertex in obj.data.vertices:
                report["unweighted_vertices"] += int(not vertex.groups)
                error = abs(sum(g.weight for g in vertex.groups) - 1)
                report["max_weight_sum_error"] = max(report["max_weight_sum_error"], error)
        for frame in range(1, scene.frame_end + 1):
            scene.frame_set(frame)
            bpy.context.view_layer.update()
            deps = bpy.context.evaluated_depsgraph_get()
            for bone in rig.pose.bones:
                error = abs((bone.tail - bone.head).length - bone.bone.length)
                report["max_bone_length_error_m"] = max(report["max_bone_length_error_m"], error)
            for name in feet:
                feet[name].append(list(rig.pose.bones[name].head))
            points = np.array(
                [
                    list(obj.matrix_world @ v.co)
                    for obj in meshes
                    for v in obj.evaluated_get(deps).data.vertices
                ]
            )
            report["finite"] = report["finite"] and bool(np.isfinite(points).all())
            report["bounds_min"] = np.minimum(report["bounds_min"], points.min(axis=0)).tolist()
            report["bounds_max"] = np.maximum(report["bounds_max"], points.max(axis=0)).tolist()
        report["foot_motion_m"] = {
            name: {
                "horizontal_extent": float(np.linalg.norm(np.ptp(np.array(path)[:, :2], axis=0))),
                "vertical_range": float(np.ptp(np.array(path)[:, 2])),
            }
            for name, path in feet.items()
        }
        if not report["finite"] or report["unweighted_vertices"] or report["max_weight_sum_error"] > 1e-5:
            raise RuntimeError("Character deformation validation failed")
        if report["max_bone_length_error_m"] > 1e-4:
            raise RuntimeError("Retarget changed the fitted bone lengths")
        scene.frame_set(1)
        return report
    finally:
        bpy.context.window.scene = previous


def run():
    """Execute a checksummed managed handoff adjacent to this copied script."""
    directory = Path(__file__).resolve().parent
    request_path = directory / "handoff.json"
    if request_path.stat().st_size > 1024 * 1024:
        raise ValueError("Handoff request exceeds its size limit")
    request = json.loads(request_path.read_text(encoding="utf-8"))
    if request.get("kind") != "character" or request.get("schema_version") != 1:
        raise ValueError("Unsupported character handoff")
    character = validate_target(request["character_fbx"])
    if request["character_sha256"] != SOURCE_SHA256:
        raise ValueError("Character profile checksum changed")
    motion = Path(request["source_motion"]).resolve(strict=True)
    if motion.suffix.lower() != ".npz" or motion.stat().st_size > 128 * 1024 * 1024:
        raise ValueError("Expected a bounded numeric motion archive")
    if hashlib.sha256(motion.read_bytes()).hexdigest() != request["source_sha256"]:
        raise ValueError("Motion changed after handoff preparation")
    outputs = [
        "animation.fbx",
        "character.fbx",
        "character.blend",
        "blender-report.json",
        "rig-report.json",
        "motion-report.json",
        "validation-report.json",
    ]
    if any((directory / name).exists() for name in outputs):
        raise FileExistsError("Handoff outputs already exist; use a new handoff")
    import bpy

    if bpy.context.mode != "OBJECT":
        raise RuntimeError("Leave Edit/Pose mode before running the handoff")
    with (directory / "execution.lock").open("x", encoding="utf-8") as marker:
        marker.write(str(motion))
    scene, rig, meshes, rig_report = build_character(character, directory)
    motion_report = apply_motion(scene, rig, motion, root_motion=request.get("root_motion", True))
    if motion_report["fps"] != request["fps"] or motion_report["frames"] != request["frames"]:
        raise ValueError("Motion timing differs from the managed manifest")
    validation = validate_animation(scene, rig, meshes)
    for material in {slot.material for obj in meshes for slot in obj.material_slots if slot.material}:
        if material.use_nodes:
            for node in material.node_tree.nodes:
                if node.type == "TEX_IMAGE" and node.image:
                    node.image.pack()
    export_character(scene, rig, meshes, directory / "character.fbx", False)
    export_character(scene, rig, [], directory / "animation.fbx", True)
    bpy.data.libraries.write(str(directory / "character.blend"), {scene}, fake_user=True, compress=True)
    bones = [{"name": b.name, "parent": b.parent.name if b.parent else None} for b in rig.data.bones]
    report = {
        "schema_version": 1,
        "kind": "blender",
        "source_kind": "character",
        "status": "completed",
        "blender_version": bpy.app.version_string,
        "source_motion": str(motion),
        "source_sha256": request["source_sha256"],
        "character_sha256": SOURCE_SHA256,
        "fps": motion_report["fps"],
        "frames": motion_report["frames"],
        "sample_index": request.get("sample_index", 0),
        "frame_range": [1, motion_report["frames"]],
        "bone_count": len(bones),
        "bones": bones,
        "fbx_bones": bones,
        "fbx_path": str(directory / "animation.fbx"),
        "fbx_sha256": hashlib.sha256((directory / "animation.fbx").read_bytes()).hexdigest(),
        "character_fbx_path": str(directory / "character.fbx"),
        "character_fbx_sha256": hashlib.sha256((directory / "character.fbx").read_bytes()).hexdigest(),
        "blend_path": str(directory / "character.blend"),
        "blend_sha256": hashlib.sha256((directory / "character.blend").read_bytes()).hexdigest(),
        "units": "meters",
        "fbx_units": "centimeters",
        "fbx_forward": "-Y",
        "fbx_up": "Z",
        "fbx_armature_wrapper": "Armature",
        "unreal_removes_armature_wrapper": True,
        "root_motion": motion_report["root_motion"],
        "limitations": rig_report["limitations"],
        "validation": validation,
    }
    for filename, payload in [
        ("motion-report.json", motion_report),
        ("validation-report.json", validation),
        ("blender-report.json", report),
    ]:
        with (directory / filename).open("x", encoding="utf-8") as stream:
            json.dump(payload, stream, indent=2, allow_nan=False)
    return report


if __name__ == "__main__":
    # A host editor may have unrelated command-line arguments after '--'.
    # Managed scripts always consume their adjacent prepared JSON request.
    run()
