"""Run inside Unreal Editor Python; imports only into an explicitly matched rig."""

import hashlib
import json
import math
from pathlib import Path

import unreal


def digest(path):
    result = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            result.update(block)
    return result.hexdigest()


def set_properties(obj, values):
    # Required properties deliberately fail before importing on unsupported
    # engine versions, instead of silently falling back to editor defaults.
    for name, value in values.items():
        obj.set_editor_property(name, value)


def run():
    directory = Path(__file__).resolve().parent
    request = json.loads((directory / "handoff.json").read_text(encoding="utf-8"))
    if request.get("kind") != "unreal" or request.get("schema_version") != 1:
        raise ValueError("Unsupported Unreal handoff request.")
    target = request["target"]
    project = Path(unreal.Paths.convert_relative_path_to_full(unreal.Paths.get_project_file_path())).resolve()
    if project != Path(target["project_file"]).resolve():
        raise RuntimeError("Open the exact project recorded in this handoff before running it.")
    source = Path(request["source_fbx"]).resolve(strict=True)
    if digest(source) != request["source_sha256"]:
        raise ValueError("FBX changed after handoff preparation.")
    if digest(Path(request["blender_report"])) != request["blender_report_sha256"]:
        raise ValueError("Blender report changed after handoff preparation.")
    report_path = directory / "unreal-report.json"
    if report_path.exists():
        raise FileExistsError("Unreal handoff report already exists.")
    destination = target["destination_path"] + "/" + target["asset_name"]
    if unreal.EditorAssetLibrary.does_asset_exist(destination):
        raise FileExistsError("Destination animation already exists; choose a new asset name.")
    skeleton = unreal.load_asset(target["skeleton_path"])
    mesh = unreal.load_asset(target["skeletal_mesh_path"])
    if not isinstance(skeleton, unreal.Skeleton) or not isinstance(mesh, unreal.SkeletalMesh):
        raise ValueError("Both an existing Skeleton and its SkeletalMesh are required.")
    if mesh.get_editor_property("skeleton") != skeleton:
        raise ValueError("The chosen SkeletalMesh does not use the chosen Skeleton.")
    # An unattached component exposes the reference hierarchy without spawning
    # an actor or altering the world, skeleton, mesh, or retarget settings.
    component = unreal.SkeletalMeshComponent()
    component.set_skeletal_mesh_asset(mesh)
    actual = {}
    for index in range(component.get_num_bones()):
        name = str(component.get_bone_name(index))
        parent = str(component.get_parent_bone(name))
        actual[name] = None if parent == "None" else parent
    expected = {bone["name"]: bone["parent"] for bone in request["expected_bones"]}
    pose = unreal.AnimPoseExtensions.get_reference_pose(skeleton)
    skeleton_names = {str(name) for name in unreal.AnimPoseExtensions.get_bone_names(pose)}
    if actual != expected or skeleton_names != set(expected):
        missing = sorted(set(expected) - set(actual))
        extra = sorted(set(actual) - set(expected))
        parents = sorted(name for name in set(actual) & set(expected) if actual[name] != expected[name])
        raise ValueError(
            "Incompatible source and target rig; import stopped before creating an asset. "
            "Retarget SOMA motion onto this rig separately before export. "
            + json.dumps({"missing_bones": missing, "extra_bones": extra, "parent_mismatches": parents})
        )
    options = unreal.FbxImportUI()
    set_properties(options, {
        "automated_import_should_detect_type": False,
        "mesh_type_to_import": unreal.FBXImportType.FBXIT_ANIMATION,
        "import_mesh": False, "import_as_skeletal": True, "import_animations": True,
        "import_materials": False, "import_textures": False,
        "create_physics_asset": False, "skeleton": skeleton,
        "override_animation_name": target["asset_name"],
    })
    animation_options = options.get_editor_property("anim_sequence_import_data")
    set_properties(animation_options, {
        "animation_length": unreal.FBXAnimationLengthImportType.FBXALIT_EXPORTED_TIME,
        "use_default_sample_rate": False, "custom_sample_rate": int(request["fps"]),
        "import_bone_tracks": True, "import_custom_attribute": False,
        "add_curve_metadata_to_skeleton": False, "convert_scene": True,
        "convert_scene_unit": True, "force_front_x_axis": False,
        "import_uniform_scale": 1.0, "remove_redundant_keys": False,
    })
    task = unreal.AssetImportTask()
    set_properties(task, {
        "filename": str(source), "destination_path": target["destination_path"],
        "destination_name": target["asset_name"], "automated": True,
        "replace_existing": False, "replace_existing_settings": False,
        "save": False, "async_": False, "factory": unreal.FbxFactory(), "options": options,
    })
    # Exclusive reservation prevents two editor invocations from racing the
    # same output. A failed run leaves this marker for explicit diagnosis.
    with (directory / "execution.lock").open("x", encoding="utf-8") as marker:
        marker.write(str(project))
        unreal.AssetToolsHelpers.get_asset_tools().import_asset_tasks([task])
        imported = list(task.get_objects())
        if len(imported) != 1 or not isinstance(imported[0], unreal.AnimSequence):
            raise RuntimeError("Import did not produce exactly one AnimSequence; nothing was saved by this script.")
        animation = imported[0]
        expected_object_path = destination + "." + target["asset_name"]
        if animation.get_path_name() != expected_object_path:
            raise RuntimeError("Importer used an unexpected asset path; nothing was saved by this script.")
        if animation.get_editor_property("skeleton") != skeleton:
            raise RuntimeError("Imported animation references an unexpected skeleton.")
        # Recent engines leave the deprecated concrete DataModel property null
        # and expose the active model through DataModelInterface instead.
        model = animation.get_editor_property("data_model_interface")
        if model is None:
            model = animation.get_editor_property("data_model")
        if model is None:
            raise RuntimeError("Unreal did not expose an animation data model; nothing was saved by this script.")
        rate = model.get_frame_rate()
        fps = float(rate.numerator) / float(rate.denominator)
        frames = int(model.get_number_of_frames())
        keys = int(model.get_number_of_keys())
        duration = float(model.get_play_length())
        tracks = [str(name) for name in model.get_bone_track_names()]
        # Unreal counts intervals separately from sampled keys: an inclusive
        # Blender range 1..N normally becomes N keys and N-1 frame intervals.
        if fps != request["fps"] or keys != request["frames"] or frames != keys - 1:
            raise RuntimeError("Imported animation timing differs from the source; nothing was saved by this script.")
        if not math.isfinite(duration) or abs(duration - (keys - 1) / fps) > 1e-5:
            raise RuntimeError("Imported animation duration is inconsistent.")
        # Unreal FName identity is case-insensitive; some engine versions return
        # lower-case track names even when reference bones retain their casing.
        # Reject collisions before comparing so normalization cannot hide a
        # missing track or two source bones that Unreal treats as one name.
        expected_tracks = {name.casefold(): name for name in expected}
        actual_tracks = {name.casefold(): name for name in tracks}
        if (
            len(expected_tracks) != len(expected)
            or len(actual_tracks) != len(tracks)
            or set(actual_tracks) != set(expected_tracks)
        ):
            raise RuntimeError(
                "Imported bone tracks differ from the verified source hierarchy: "
                + json.dumps({
                    "missing_tracks": sorted(set(expected_tracks) - set(actual_tracks)),
                    "extra_tracks": sorted(set(actual_tracks) - set(expected_tracks)),
                    "name_collision": len(expected_tracks) != len(expected)
                    or len(actual_tracks) != len(tracks),
                })
            )
        if not unreal.EditorAssetLibrary.save_loaded_asset(animation, only_if_is_dirty=False):
            raise RuntimeError("Unreal could not save the new animation asset.")
        report = {
            "schema_version": 1, "kind": "unreal", "status": "completed",
            "engine_version": unreal.SystemLibrary.get_engine_version(),
            "project_file": str(project), "animation_path": animation.get_path_name(),
            "skeleton_path": skeleton.get_path_name(), "source_fbx": str(source),
            "source_sha256": request["source_sha256"], "fps": fps,
            "sampled_keys": keys, "frame_intervals": frames, "duration_seconds": duration,
            "bone_count": len(expected), "track_count": len(tracks),
            "tracks": [expected_tracks[name.casefold()] for name in tracks],
            "raw_track_names": tracks,
        }
        with report_path.open("x", encoding="utf-8") as stream:
            json.dump(report, stream, indent=2, allow_nan=False)
        unreal.log(json.dumps(report))


if __name__ == "__main__":
    run()
