"""Fixed Blender asset importer; receives only a managed JSON request."""

import hashlib
import json
import sys
from pathlib import Path


def sha(path):
    value = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            value.update(block)
    return value.hexdigest()


def import_asset(source, asset_format, scratch):
    import bpy

    before = set(bpy.data.objects)
    if asset_format in {"glb", "vrm"}:
        # The generic glTF importer preserves geometry/skins, not VRM behavior.
        if asset_format == "vrm":
            import shutil

            staged = scratch / "input.glb"
            shutil.copyfile(source, staged)
            source = staged
        bpy.ops.import_scene.gltf(filepath=str(source))
    elif asset_format == "fbx":
        bpy.ops.import_scene.fbx(filepath=str(source), use_image_search=False)
    elif asset_format == "obj":
        # OBJ material libraries may reference arbitrary local files. This first
        # importer intentionally accepts geometry only, without following them.
        staged = scratch / "geometry.obj"
        with Path(source).open("r", encoding="utf-8", errors="strict") as reader:
            with staged.open("x", encoding="utf-8") as writer:
                for line in reader:
                    fields = line.split(maxsplit=1)
                    if not fields or fields[0] not in {"mtllib", "usemtl"}:
                        writer.write(line)
        bpy.ops.wm.obj_import(filepath=str(staged))
    else:
        raise ValueError("Unsupported asset format")
    objects = [obj for obj in bpy.data.objects if obj not in before]
    meshes = [obj for obj in objects if obj.type == "MESH"]
    if not meshes or sum(len(obj.data.vertices) for obj in meshes) > 5_000_000:
        raise ValueError("Asset must contain geometry with at most five million vertices")
    return objects


def export_glb(path, objects, animation=True):
    import bpy

    bpy.ops.object.select_all(action="DESELECT")
    for obj in objects:
        obj.select_set(True)
    bpy.context.view_layer.objects.active = objects[0]
    bpy.ops.export_scene.gltf(
        filepath=str(path),
        export_format="GLB",
        use_selection=True,
        export_animations=animation,
        export_skins=True,
        export_animation_mode="SCENE",
        export_frame_range=True,
        export_force_sampling=True,
        export_anim_slide_to_zero=True,
    )


def run(request_path):
    import math

    import bpy

    request_path = Path(request_path)
    request = json.loads(request_path.read_text(encoding="utf-8"))
    source = Path(request["source"])
    if sha(source) != request["asset_id"] or sha(__file__) != request["script_sha256"]:
        raise ValueError("Source or converter changed")
    bpy.ops.object.select_all(action="SELECT")
    bpy.ops.object.delete(use_global=False)
    objects = import_asset(source, request["format"], request_path.parent)
    meshes = [obj for obj in objects if obj.type == "MESH"]
    points = [obj.matrix_world @ vertex.co for obj in meshes for vertex in obj.data.vertices]
    if not all(math.isfinite(value) for point in points for value in point):
        raise ValueError("Asset contains nonfinite geometry")
    output = Path(request["output"])
    export_glb(output, objects)
    if sha(source) != request["asset_id"]:
        raise ValueError("Source changed during conversion")
    report = {
        "schema_version": 1,
        "status": "completed",
        "asset_id": request["asset_id"],
        "script_sha256": request["script_sha256"],
        "sha256": sha(output),
        "bytes": output.stat().st_size,
        "vertices": len(points),
        "meshes": len(meshes),
        "armatures": sum(obj.type == "ARMATURE" for obj in objects),
        "bounds": [
            [min(p[i] for p in points) for i in range(3)],
            [max(p[i] for p in points) for i in range(3)],
        ],
        "coordinate_system": "glTF right-handed Y-up, meters; source import scale retained",
        "notes": ["OBJ material sidecars are omitted. VRM extensions are not retained."],
    }
    (request_path.parent / "report.json").write_text(json.dumps(report, indent=2), encoding="utf-8")


if __name__ == "__main__":
    run(sys.argv[sys.argv.index("--") + 1])
