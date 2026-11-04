"""Trusted Blender script: immutable BVH cleanup and fitted-character retarget.

Only prepared JSON values select inputs. Original capture files and other scenes
are never edited. Quaternion filtering is explicit and does not infer contacts.
"""

import hashlib
import json
import math
import runpy
from pathlib import Path

import numpy as np


def digest(path):
    h = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for block in iter(lambda: stream.read(1024**2), b""):
            h.update(block)
    return h.hexdigest()


def taper(frame, start, end, blend):
    if not start <= frame < end:
        return 0.0
    if not blend:
        return 1.0
    t = min(1.0, (frame - start + 1) / (blend + 1), (end - frame) / (blend + 1))
    return t * t * (3 - 2 * t)


def quaternion_multiply(a, b):
    w, x, y, z = a
    v, i, j, k = b
    return np.array(
        [
            w * v - x * i - y * j - z * k,
            w * i + x * v + y * k - z * j,
            w * j - x * k + y * v + z * i,
            w * k + x * j - y * i + z * v,
        ]
    )


def slerp(a, b, amount):
    dot = float(np.dot(a, b))
    if dot < 0:
        b, dot = -b, -dot
    dot = min(1.0, max(-1.0, dot))
    if dot > 0.9995:
        result = a + amount * (b - a)
    else:
        theta = math.acos(dot)
        result = (math.sin((1 - amount) * theta) * a + math.sin(amount * theta) * b) / math.sin(theta)
    return result / np.linalg.norm(result)


def cleanup_motion(local_quaternions, root_positions, names, options):
    """Original finite windowed filter; no edits outside selected windows."""
    q = np.asarray(local_quaternions, dtype=float)
    root = np.asarray(root_positions, dtype=float)
    if (
        q.ndim != 3
        or q.shape[1:] != (len(names), 4)
        or root.shape != (len(q), 3)
        or not np.isfinite(q).all()
        or not np.isfinite(root).all()
        or not np.allclose(np.linalg.norm(q, axis=-1), 1, atol=1e-5)
    ):
        raise ValueError("Invalid finite normalized source animation")
    original, changed = q.copy(), q.copy()
    position = root.copy()
    start = options["start_frame"]
    low, high = options["smoothing_start_frame"], options["smoothing_end_frame"]
    for f in range(len(q)):
        weight = taper(f + start, low, high, options["blend_frames"])
        if not weight:
            continue
        for radius, angular in (
            (options["rotation_smoothing_radius"], True),
            (options["root_smoothing_radius"], False),
        ):
            if not radius:
                continue
            first, last = max(0, low - start, f - radius), min(len(q), high - start, f + radius + 1)
            if angular:
                for j in range(len(names)):
                    window = q[first:last, j]
                    # Principal eigenvector of qq^T gives a sign-invariant unit quaternion mean.
                    _, vectors = np.linalg.eigh(window.T @ window)
                    changed[f, j] = slerp(q[f, j], vectors[:, -1], weight)
            else:
                position[f] = root[f] * (1 - weight) + root[first:last].mean(axis=0) * weight
    for correction in options["joint_corrections"]:
        joint = names.index(correction["bone"])
        for f in range(len(q)):
            weight = taper(
                f + start, correction["start_frame"], correction["end_frame"], correction["blend_frames"]
            )
            angles = np.radians(correction["rotation_degrees"]) * weight / 2
            delta = np.array([1.0, 0, 0, 0])
            for axis, angle in enumerate(angles):
                turn = np.zeros(4)
                turn[0], turn[axis + 1] = math.cos(angle), math.sin(angle)
                delta = quaternion_multiply(delta, turn)
            changed[f, joint] = quaternion_multiply(changed[f, joint], delta)
    for f in range(1, len(q)):
        changed[f, np.sum(changed[f] * changed[f - 1], axis=-1) < 0] *= -1
    angular = 2 * np.arccos(np.clip(np.abs(np.sum(original * changed, axis=-1)), 0, 1))
    movement_before = 2 * np.arccos(np.clip(np.abs(np.sum(original[1:] * original[:-1], axis=-1)), 0, 1))
    movement_after = 2 * np.arccos(np.clip(np.abs(np.sum(changed[1:] * changed[:-1], axis=-1)), 0, 1))
    report = {
        "source_frame_range": [start, options["end_frame"]],
        "smoothing_frame_range": [low, high],
        "quaternion_filter": "sign-invariant centered quaternion mean with tapered SLERP",
        "correction_order": "local current-pose X then Y then Z postmultiplication",
        "max_rotation_change_degrees": float(np.degrees(angular.max())),
        "max_root_change_m": float(np.linalg.norm(position - root, axis=-1).max()),
        "max_adjacent_rotation_before_degrees": float(np.degrees(movement_before.max()))
        if movement_before.size
        else 0,
        "max_adjacent_rotation_after_degrees": float(np.degrees(movement_after.max()))
        if movement_after.size
        else 0,
        "contacts": "unavailable; smoothing can alter foot placement",
        "automatic_foot_repair": False,
        "corrected_bones": [c["bone"] for c in options["joint_corrections"]],
    }
    return changed, position, report


def apply_capture(scene, rig, source, request):
    import bpy
    from mathutils import Matrix, Quaternion, Vector

    options, mapping = request["options"], request["mapping"]
    names = [b.name for b in rig.data.bones]
    parents = [names.index(b.parent.name) if b.parent else -1 for b in rig.data.bones]
    target_rest = {b.name: b.matrix_local.to_3x3() for b in rig.data.bones}
    source_rest = {
        name: source.matrix_world.to_3x3() @ source.data.bones[name].matrix_local.to_3x3()
        for name in set(mapping.values())
    }
    # Confirm named rest geometry instead of trusting the addon's requested setting.
    for side, sign in (("L", 1), ("R", -1)):
        arm = source.data.bones["upper_arm." + side]
        vector = source.matrix_world.to_3x3() @ (arm.tail_local - arm.head_local)
        if vector.length < 1e-6 or sign * vector.x / vector.length < 0.35:
            raise ValueError("Source rest arm direction does not match the reviewed left/right profile")
        elevation = vector.z / vector.length
        expected = (
            abs(elevation) <= 0.3 if options["source_rest_pose"] == "tpose" else -0.95 < elevation < -0.15
        )
        if not expected:
            raise ValueError("Source arm rest geometry differs from the explicitly selected T/A pose")
    target_reference = {name: matrix.copy() for name, matrix in target_rest.items()}
    if options["source_rest_pose"] == "apose":
        # Preserve the character's skin bind. Align only the retarget reference
        # arm directions so an authored A pose does not silently become a T pose.
        for side in ("Left", "Right"):
            for part in ("Arm", "ForeArm", "Hand"):
                name = side + part
                source_axis = source_rest[mapping[name]].col[1].normalized()
                target_axis = target_rest[name].col[1].normalized()
                alignment = target_axis.rotation_difference(source_axis).to_matrix()
                target_reference[name] = alignment @ target_rest[name]
    count = request["frames"]
    quaternions = np.zeros((count, len(names), 4))
    root_positions = np.zeros((count, 3))
    first_root = None
    for frame in range(count):
        scene.frame_set(frame + 1)
        bpy.context.view_layer.update()
        matrices = {}
        pelvis = source.matrix_world @ source.pose.bones[mapping["Hips"]].matrix
        if first_root is None:
            first_root = pelvis.translation.copy()
        displacement = pelvis.translation - first_root
        root_positions[frame] = tuple(displacement)
        if not options["root_motion"]:
            root_positions[frame, :2] = 0
        hips_delta = pelvis.to_3x3() @ source_rest[mapping["Hips"]].transposed()
        forward = hips_delta @ Vector((0, -1, 0))
        heading = math.atan2(forward.x, -forward.y) if options["root_motion"] else 0
        matrices["Root"] = Matrix.Rotation(heading, 3, "Z") @ target_rest["Root"]
        for bone in rig.data.bones:
            if bone.name != "Root":
                name = mapping[bone.name]
                posed = source.matrix_world.to_3x3() @ source.pose.bones[name].matrix.to_3x3()
                matrices[bone.name] = posed @ source_rest[name].transposed() @ target_reference[bone.name]
            rotation = (
                matrices[bone.parent.name].transposed() @ matrices[bone.name]
                if bone.parent
                else matrices[bone.name]
            )
            quaternion = rotation.to_quaternion().normalized()
            quaternions[frame, names.index(bone.name)] = tuple(quaternion)
    cleaned, root_positions, cleanup = cleanup_motion(quaternions, root_positions, names, options)
    if rig.animation_data:
        rig.animation_data_clear()
    evaluated = np.zeros((count, len(names), 3))
    rotations = np.zeros((count, len(names), 3, 3))
    for frame in range(count):
        scene.frame_set(frame + 1)
        poses = {}
        for index, bone in enumerate(rig.data.bones):
            local = Quaternion(cleaned[frame, index]).to_matrix()
            orientation = poses[bone.parent.name].to_3x3() @ local if bone.parent else local
            if bone.parent:
                parent_pose = poses[bone.parent.name]
                offset = bone.parent.matrix_local.to_3x3().transposed() @ (
                    bone.head_local - bone.parent.head_local
                )
                head = parent_pose.translation + parent_pose.to_3x3() @ offset
                if bone.name == "Hips":
                    head.z += root_positions[frame, 2]
            else:
                head = bone.head_local + Vector((root_positions[frame, 0], root_positions[frame, 1], 0))
            matrix = orientation.to_4x4()
            matrix.translation = head
            poses[bone.name] = matrix
            evaluated[frame, index], rotations[frame, index] = tuple(head), np.asarray(orientation)
            parent_pose = poses[bone.parent.name] if bone.parent else Matrix.Identity(4)
            parent_rest = bone.parent.matrix_local if bone.parent else Matrix.Identity(4)
            basis = bone.convert_local_to_pose(
                matrix,
                bone.matrix_local,
                parent_matrix=parent_pose,
                parent_matrix_local=parent_rest,
                invert=True,
            )
            pb = rig.pose.bones[bone.name]
            location, quaternion, scale = basis.decompose()
            if frame and pb.rotation_quaternion.dot(quaternion) < 0:
                quaternion.negate()
            pb.rotation_mode, pb.location, pb.rotation_quaternion, pb.scale = (
                "QUATERNION",
                location,
                quaternion,
                scale,
            )
            pb.keyframe_insert("location", frame=frame + 1, group=bone.name)
            pb.keyframe_insert("rotation_quaternion", frame=frame + 1, group=bone.name)
    rig.animation_data.action.name = "Capture_" + request["handoff_id"][:12]
    cleanup.update(
        {
            "fps": request["fps"],
            "frames": count,
            "source_rest_pose_verified": options["source_rest_pose"],
            "source_scale_to_meters": options["source_scale_to_meters"],
            "discarded_nonroot_source_translations": True,
            "root_motion": options["root_motion"],
            "target_bone_lengths_preserved": True,
            "toe_motion": "inherits foot; independent toe bending unavailable",
        }
    )
    return cleanup, {
        "posed_joints": evaluated,
        "global_rot_mats": rotations,
        "local_quaternions_wxyz": cleaned,
        "root_positions": root_positions,
        "joint_names": np.asarray(names),
        "joint_parents": np.asarray(parents),
        "fps": np.asarray(request["fps"]),
        "skeleton": np.asarray("registered-character24"),
        "source_frame_indices": np.arange(options["start_frame"], options["end_frame"]),
    }


def run():
    import bpy

    directory = Path(__file__).resolve().parent
    request = json.loads((directory / "handoff.json").read_text(encoding="utf-8"))
    if request.get("kind") != "capture_character" or request.get("schema_version") != 1:
        raise ValueError("Invalid capture character preparation")
    original_request = digest(directory / "handoff.json")

    def verify():
        checks = [
            (Path(request["source_bvh"]), request["source_sha256"]),
            (Path(request["input_bvh"]), request["input_sha256"]),
            (Path(request["character_fbx"]), request["character_sha256"]),
            (directory.parents[1] / "jobs" / (request["job_id"] + ".json"), request["export_receipt_sha256"]),
            (
                directory.parents[1] / "jobs" / (request["process_job_id"] + ".json"),
                request["process_receipt_sha256"],
            ),
            (directory / "handoff.json", original_request),
        ]
        checks.extend((directory / name, checksum) for name, checksum in request["scripts"].items())
        if any(digest(path) != checksum for path, checksum in checks):
            raise ValueError("Capture handoff input changed during execution")

    verify()
    if bpy.context.mode != "OBJECT" or bpy.data.objects.get("Armature") is not None:
        raise RuntimeError("Use Object mode with no conflicting Armature object")
    outputs = [
        "character.blend",
        "character.fbx",
        "animation.fbx",
        "character-preview.glb",
        "blender-report.json",
        "cleaned-motion.npz",
        "cleanup-report.json",
        "failure.json",
    ]
    if any((directory / name).exists() for name in outputs):
        raise FileExistsError("Capture outputs exist; select a new attempt")
    with (directory / "execution.lock").open("x") as stream:
        stream.write(request["handoff_id"])
    helper = runpy.run_path(str(directory / "character_helper.py"))
    previous = bpy.context.window.scene
    try:
        scene, rig, meshes, rig_report = helper["build_character"](request["character_fbx"], directory)
        bpy.context.window.scene = scene
        scene.render.fps, scene.render.fps_base = request["fps"], 1.0
        scene.frame_start, scene.frame_end = 1, request["frames"]
        bpy.ops.import_anim.bvh(
            filepath=request["input_bvh"],
            target="ARMATURE",
            frame_start=1,
            global_scale=request["options"]["source_scale_to_meters"],
            use_fps_scale=False,
            update_scene_fps=False,
            update_scene_duration=False,
            rotate_mode="QUATERNION",
            axis_forward="Y",
            axis_up="Z",
        )
        source = bpy.context.object
        if source is rig or source.type != "ARMATURE":
            raise ValueError("BVH import did not create the expected source armature")
        cleanup, motion = apply_capture(scene, rig, source, request)
        bpy.data.objects.remove(source, do_unlink=True)
        validation = helper["validate_animation"](scene, rig, meshes)
        for material in {slot.material for obj in meshes for slot in obj.material_slots if slot.material}:
            if material.use_nodes:
                for node in material.node_tree.nodes:
                    if node.type == "TEX_IMAGE" and node.image:
                        node.image.pack()
        np.savez_compressed(directory / "cleaned-motion.npz", **motion)
        (directory / "cleanup-report.json").write_text(
            json.dumps(cleanup, indent=2, allow_nan=False), encoding="utf-8"
        )
        helper["export_character"](scene, rig, meshes, directory / "character.fbx", False)
        helper["export_character"](scene, rig, [], directory / "animation.fbx", True)
        helper["export_character_preview"](scene, rig, meshes, directory / "character-preview.glb")
        bpy.data.libraries.write(str(directory / "character.blend"), {scene}, fake_user=True, compress=True)
        verify()
        bones = [{"name": b.name, "parent": b.parent.name if b.parent else None} for b in rig.data.bones]
        report = {
            "schema_version": 1,
            "kind": "blender",
            "source_kind": "capture_character",
            "status": "completed",
            "handoff_id": request["handoff_id"],
            "source_bvh": request["source_bvh"],
            "source_sha256": request["source_sha256"],
            "character_sha256": request["character_sha256"],
            "fps": request["fps"],
            "frames": request["frames"],
            "frame_range": [1, request["frames"]],
            "bone_count": len(bones),
            "bones": bones,
            "fbx_bones": bones,
            "root_motion": request["options"]["root_motion"],
            "units": "meters",
            "fbx_units": "centimeters",
            "validation": validation,
            "cleanup": cleanup,
            "limitations": rig_report["limitations"],
            "blender_version": bpy.app.version_string,
        }
        for key, filename in (
            ("fbx", "animation.fbx"),
            ("character_fbx", "character.fbx"),
            ("blend", "character.blend"),
            ("preview_glb", "character-preview.glb"),
            ("cleanup", "cleanup-report.json"),
            ("cleaned_motion", "cleaned-motion.npz"),
        ):
            report[key + "_path"], report[key + "_sha256"] = (
                str(directory / filename),
                digest(directory / filename),
            )
        with (directory / "blender-report.json").open("x", encoding="utf-8") as stream:
            json.dump(report, stream, indent=2, allow_nan=False)
    except Exception as error:
        with (directory / "failure.json").open("x", encoding="utf-8") as stream:
            json.dump({"status": "failed", "error": str(error)[:2000]}, stream)
        raise
    finally:
        bpy.context.window.scene = previous


if __name__ == "__main__":
    run()
