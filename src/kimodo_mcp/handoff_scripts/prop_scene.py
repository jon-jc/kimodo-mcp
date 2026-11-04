"""Build a new animated prop scene from verified copies, in a fresh Blender process."""

import hashlib
import importlib.util
import json
import math
import sys
from pathlib import Path


def sha(path):
    value = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            value.update(block)
    return value.hexdigest()


def run(request_path):
    import bpy
    from mathutils import Euler, Matrix, Vector

    request_path = Path(request_path)
    directory = request_path.parent
    request = json.loads(request_path.read_text(encoding="utf-8"))
    for name, expected in request["scripts"].items():
        if sha(Path(__file__).parent / name) != expected:
            raise ValueError("Prop converter changed")
    helper = Path(__file__).with_name("asset_preview.py")
    spec = importlib.util.spec_from_file_location("asset_importer", helper)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    motion = request["motion"]
    watched = [
        (Path(motion["blend_path"]), motion["blend_sha256"]),
        (Path(motion["report_path"]), motion["report_sha256"]),
    ]
    watched += [(Path(asset["absolute_path"]), asset["sha256"]) for asset in request["assets"]]
    for path, expected in watched:
        if sha(path) != expected:
            raise ValueError("Prop source changed")
    # Load only the verified generated scene, never an arbitrary user .blend.
    bpy.ops.wm.open_mainfile(filepath=motion["blend_path"], load_ui=False, use_scripts=False)
    scene = bpy.context.scene
    rigs = [obj for obj in scene.objects if obj.type == "ARMATURE"]
    if len(rigs) != 1 or len(rigs[0].data.bones) != 24:
        raise ValueError("Expected the verified registered character armature")
    rig = rigs[0]
    names = [bone.name for bone in rig.data.bones]
    frames = motion["frames"]
    if (
        scene.frame_start != 1
        or scene.frame_end != frames
        or scene.render.fps / scene.render.fps_base != motion["fps"]
    ):
        raise ValueError("Source Blender timeline differs from its receipt")
    character_objects = list(scene.objects)

    def pose_hash():
        value = hashlib.sha256()
        for frame in range(1, frames + 1):
            scene.frame_set(frame)
            for bone in rig.pose.bones:
                value.update(repr(tuple(float(x) for row in bone.matrix for x in row)).encode())
        return value.hexdigest()

    original_pose = pose_hash()
    all_objects = list(character_objects)
    recipes = []
    for index, (asset, attachment) in enumerate(zip(request["assets"], request["scene"]["attachments"])):
        scratch = directory / f"import_{index:02d}"
        scratch.mkdir()
        objects = module.import_asset(Path(asset["absolute_path"]), asset["format"], scratch)
        if any(obj.type == "ARMATURE" or obj.animation_data is not None for obj in objects):
            raise ValueError("Prop attachments require static geometry without armatures or animation")
        meshes = [obj for obj in objects if obj.type == "MESH"]
        if any(modifier.type == "ARMATURE" for obj in meshes for modifier in obj.modifiers):
            raise ValueError("Skinned assets cannot be attached as rigid props")
        # Export each prop independently, before applying the attachment transform.
        bpy.ops.object.select_all(action="DESELECT")
        for obj in meshes:
            obj.select_set(True)
        bpy.context.view_layer.objects.active = meshes[0]
        filename = f"prop_{index:02d}.fbx"
        bpy.ops.export_scene.fbx(
            filepath=str(directory / filename),
            use_selection=True,
            object_types={"MESH"},
            bake_anim=False,
            add_leaf_bones=False,
            apply_scale_options="FBX_SCALE_NONE",
            axis_forward="-Y",
            axis_up="Z",
            path_mode="COPY",
            embed_textures=True,
        )
        holder = bpy.data.objects.new(f"PropAttachment_{index:02d}", None)
        scene.collection.objects.link(holder)
        roots = [obj for obj in objects if obj.parent not in objects]
        for obj in roots:
            original_world = obj.matrix_world.copy()
            obj.parent = holder
            obj.matrix_parent_inverse = Matrix.Identity(4)
            obj.matrix_basis = original_world
        local = Matrix.LocRotScale(
            Vector(attachment["translation_m"]),
            Euler([math.radians(value) for value in attachment["rotation_degrees"]], "XYZ").to_quaternion(),
            Vector(attachment["scale"]),
        )
        holder.rotation_mode = "QUATERNION"
        previous_rotation = None
        for frame in range(1, frames + 1):
            scene.frame_set(frame)
            anchor = (
                rig.matrix_world @ rig.pose.bones[attachment["bone"]].matrix
                if attachment["bone"]
                else Matrix.Identity(4)
            )
            holder.matrix_world = anchor @ local
            # Equivalent quaternion signs must stay in the same hemisphere so
            # subframe interpolation cannot introduce a spurious full turn.
            rotation = holder.rotation_quaternion.copy()
            if previous_rotation is not None:
                rotation.make_compatible(previous_rotation)
                holder.rotation_quaternion = rotation
            previous_rotation = rotation
            holder.keyframe_insert("location", frame=frame)
            holder.keyframe_insert("rotation_quaternion", frame=frame)
            holder.keyframe_insert("scale", frame=frame)
        recipes.append(
            attachment
            | {
                "static_mesh": filename,
                "coordinate_system": "Blender right-handed Z-up meters; local posed bone axes",
                "unreal_note": "Import prop as a separate static mesh. Convert the recorded local transform to Unreal coordinates for a socket; no character bones were added.",
            }
        )
        all_objects.extend(objects + [holder])
    if names != [bone.name for bone in rig.data.bones] or pose_hash() != original_pose:
        raise ValueError("Character animation changed during prop attachment")
    scene.frame_set(1)
    module.export_glb(directory / "scene-preview.glb", all_objects)
    bpy.ops.wm.save_as_mainfile(filepath=str(directory / "scene.blend"), check_existing=False)
    (directory / "attachment-recipe.json").write_text(
        json.dumps(
            {
                "schema_version": 1,
                "handoff_id": request["handoff_id"],
                "attachments": recipes,
                "source": motion["source"],
                "notes": [
                    "Rigid attachments have no collision or cloth simulation.",
                    "Separate prop meshes are not extra skeleton bones.",
                ],
            },
            indent=2,
        ),
        encoding="utf-8",
    )
    for path, expected in watched:
        if sha(path) != expected:
            raise ValueError("Prop source changed during export")
    names_out = ["scene.blend", "scene-preview.glb", "attachment-recipe.json"] + [
        f"prop_{index:02d}.fbx" for index in range(len(recipes))
    ]
    report = {
        "schema_version": 1,
        "handoff_id": request["handoff_id"],
        "status": "completed",
        "animation_preserved": True,
        "character_bones": names,
        "fps": motion["fps"],
        "frames": frames,
        "outputs": {name: sha(directory / name) for name in names_out},
    }
    (directory / "report.json").write_text(json.dumps(report, indent=2), encoding="utf-8")


if __name__ == "__main__":
    run(sys.argv[sys.argv.index("--") + 1])
