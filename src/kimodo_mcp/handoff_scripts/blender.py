"""Run inside Blender; all variable input comes from the adjacent JSON file."""

import hashlib
import json
import math
from pathlib import Path

import bpy


def digest(path):
    result = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            result.update(block)
    return result.hexdigest()


def run():
    directory = Path(__file__).resolve().parent
    request = json.loads((directory / "handoff.json").read_text(encoding="utf-8"))
    if request.get("kind") != "blender" or request.get("schema_version") != 1:
        raise ValueError("Unsupported Blender handoff request.")
    source = Path(request["source_bvh"]).resolve(strict=True)
    if digest(source) != request["source_sha256"]:
        raise ValueError("BVH changed after handoff preparation.")
    fbx_path = directory / "animation.fbx"
    blend_path = directory / "animation.blend"
    report_path = directory / "blender-report.json"
    if any(path.exists() for path in (fbx_path, blend_path, report_path)):
        raise FileExistsError("Handoff outputs already exist; use a new handoff directory.")
    # Resolve operators before modifying the scene. Missing import/export addons
    # produce an actionable error, without installing anything in the editor.
    try:
        bpy.ops.import_anim.bvh.get_rna_type()
        bpy.ops.export_scene.fbx.get_rna_type()
    except Exception as exc:
        raise RuntimeError("Enable Blender's BVH importer and FBX exporter first.") from exc
    if bpy.context.mode != "OBJECT":
        raise RuntimeError("Leave Edit/Pose mode before running the handoff.")
    if bpy.data.objects.get(request["armature_name"]) is not None:
        raise RuntimeError("KimodoRig already exists; run in a fresh Blender session.")
    with (directory / "execution.lock").open("x", encoding="utf-8") as marker:
        marker.write(str(source))
    old_scene = bpy.context.window.scene
    scene = bpy.data.scenes.new("KimodoMotion")
    bpy.context.window.scene = scene
    try:
        scene.unit_settings.system = "METRIC"
        scene.unit_settings.scale_length = 1.0
        scene.render.fps = int(request["fps"])
        scene.render.fps_base = 1.0
        scene.frame_start = 1
        scene.frame_end = int(request["frames"])
        result = bpy.ops.import_anim.bvh(
            filepath=str(source), target="ARMATURE", global_scale=0.01,
            frame_start=1, use_fps_scale=False, update_scene_fps=False,
            update_scene_duration=False, rotate_mode="NATIVE",
            axis_forward="Z", axis_up="Y",
        )
        if "FINISHED" not in result:
            raise RuntimeError("Blender BVH import did not finish.")
        rigs = [obj for obj in scene.objects if obj.type == "ARMATURE"]
        if len(rigs) != 1 or len(scene.objects) != 1:
            raise RuntimeError("BVH import must produce exactly one armature.")
        rig = rigs[0]
        rig.name = request["armature_name"]
        rig.data.name = "KimodoSkeleton"
        if rig.animation_data is None or rig.animation_data.action is None:
            raise RuntimeError("Imported armature has no animation action.")
        action = rig.animation_data.action
        action.name = "KimodoMotion"
        actual_range = [float(value) for value in action.frame_range]
        if any(not math.isfinite(value) for value in actual_range):
            raise RuntimeError("Imported animation has a nonfinite frame range.")
        if abs(actual_range[0] - 1) > 0.001 or abs(actual_range[1] - request["frames"]) > 0.001:
            raise RuntimeError("Imported animation frame range differs from the manifest.")
        rig.select_set(True)
        bpy.context.view_layer.objects.active = rig
        scene.frame_set(1)
        bones = [{"name": bone.name, "parent": bone.parent.name if bone.parent else None}
                 for bone in rig.data.bones]
        # The FBX armature object becomes an explicit hierarchy node. Avoid the
        # name Armature, which Unreal special-cases by removing that node.
        fbx_bones = [{"name": rig.name, "parent": None}] + [
            {"name": bone["name"], "parent": bone["parent"] or rig.name} for bone in bones
        ]
        result = bpy.ops.export_scene.fbx(
            filepath=str(fbx_path), check_existing=False, use_selection=True,
            object_types={"ARMATURE"}, global_scale=1.0, apply_unit_scale=True,
            apply_scale_options="FBX_SCALE_UNITS", axis_forward="-Y", axis_up="Z",
            bake_space_transform=False,
            add_leaf_bones=False, use_armature_deform_only=False,
            armature_nodetype="NULL", bake_anim=True, bake_anim_use_all_bones=True,
            bake_anim_use_all_actions=False, bake_anim_use_nla_strips=False,
            bake_anim_force_startend_keying=True, bake_anim_step=1.0,
            bake_anim_simplify_factor=0.0, use_custom_props=False,
        )
        if "FINISHED" not in result or not fbx_path.is_file() or fbx_path.stat().st_size == 0:
            raise RuntimeError("Blender FBX export did not produce a file.")
        # Write only this scene and its dependencies, preserving the user's open
        # project filename and avoiding unrelated objects in the saved artifact.
        bpy.data.libraries.write(str(blend_path), {scene}, fake_user=True, compress=True)
        if not blend_path.is_file() or blend_path.stat().st_size == 0:
            raise RuntimeError("Blender did not save the standalone scene.")
        report = {
            "schema_version": 1, "kind": "blender", "status": "completed",
            "blender_version": bpy.app.version_string, "source_bvh": str(source),
            "source_sha256": request["source_sha256"], "fps": request["fps"],
            "frames": request["frames"], "frame_range": actual_range,
            "bone_count": len(bones), "bones": bones, "fbx_bones": fbx_bones,
            "fbx_path": str(fbx_path), "fbx_sha256": digest(fbx_path),
            "blend_path": str(blend_path), "blend_sha256": digest(blend_path),
            "units": "meters", "fbx_forward": "-Y", "fbx_up": "Z",
        }
        with report_path.open("x", encoding="utf-8") as stream:
            json.dump(report, stream, indent=2, allow_nan=False)
        print(json.dumps(report))
    finally:
        bpy.context.window.scene = old_scene


if __name__ == "__main__":
    run()
