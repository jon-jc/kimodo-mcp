"""Handoff boundaries: data/code separation, no overwrite, provenance and rig paths."""

import hashlib
import json
import runpy
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from kimodo_mcp.handoff import UnrealTarget, prepare_blender_handoff, prepare_unreal_handoff


@pytest.fixture
def source(tmp_path):
    path = tmp_path / "motion's unusual name.bvh"
    path.write_text("HIERARCHY\nROOT Root\n{\n}\nMOTION\nFrames: 30\nFrame Time: 0.033333333\n")
    return path


def prepare(source, directory, **changes):
    arguments = dict(fps=30, frames=30, source_sha256=hashlib.sha256(source.read_bytes()).hexdigest(), skeleton="somaskel77")
    arguments.update(changes)
    return prepare_blender_handoff(source, directory, **arguments)


def test_preparation_is_repeatable_and_separates_data_from_code(source, tmp_path):
    directory = tmp_path / "handoff"
    first = prepare(source, directory)
    assert prepare(source, directory) == first
    script = Path(first.script_path).read_text()
    assert str(source) not in script
    assert json.loads(Path(first.request_path).read_text())["source_bvh"] == str(source)
    compile(script, first.script_path, "exec")
    assert not (directory / "animation.fbx").exists()


def test_changed_request_and_partial_directory_are_not_overwritten(source, tmp_path):
    directory = tmp_path / "handoff"
    result = prepare(source, directory)
    config = Path(result.request_path)
    original = config.read_bytes()
    with pytest.raises(FileExistsError):
        prepare(source, directory, skeleton="somaskel30")
    assert config.read_bytes() == original
    other = tmp_path / "occupied"
    other.mkdir()
    (other / "precious.txt").write_text("retain me")
    with pytest.raises(FileExistsError):
        prepare(source, other)
    assert (other / "precious.txt").read_text() == "retain me"


@pytest.mark.parametrize("changes", [{"fps": 60}, {"frames": 31}, {"skeleton": "g1skel34"}, {"source_sha256": "0" * 64}])
def test_manifest_disagreement_rejected_without_output(source, tmp_path, changes):
    directory = tmp_path / "handoff"
    with pytest.raises(ValueError):
        prepare(source, directory, **changes)
    assert not directory.exists()


def target(tmp_path, **changes):
    values = dict(project_file=str(tmp_path / "Character.uproject"), skeleton_path="/Game/Character/Skeleton.Skeleton",
                  skeletal_mesh_path="/Game/Character/Mesh.Mesh", destination_path="/Game/Animations", asset_name="Walk")
    values.update(changes)
    return UnrealTarget(**values)


@pytest.mark.parametrize("changes", [
    {"destination_path": "/Game/../Engine"}, {"skeleton_path": "/Script/Engine.Skeleton"},
    {"asset_name": "A;import os"}, {"project_file": "relative.uproject"},
    {"destination_path": "/Game/Animation.Walk"},
])
def test_unreal_requires_explicit_project_and_content_paths(tmp_path, changes):
    with pytest.raises(ValidationError):
        target(tmp_path, **changes)


def test_unreal_preparation_verifies_blender_provenance(tmp_path):
    blender_dir = tmp_path / "blender"
    blender_dir.mkdir()
    fbx = blender_dir / "animation.fbx"
    fbx.write_bytes(b"fixture: not a live Blender export")
    report = blender_dir / "blender-report.json"
    report.write_text(json.dumps({
        "schema_version": 1, "kind": "blender", "status": "completed", "fps": 30, "frames": 30,
        "fbx_path": str(fbx), "fbx_sha256": hashlib.sha256(fbx.read_bytes()).hexdigest(),
        "fbx_bones": [{"name": "KimodoRig", "parent": None}, {"name": "Root", "parent": "KimodoRig"}],
    }))
    result = prepare_unreal_handoff(report, tmp_path / "unreal", target=target(tmp_path))
    compile(Path(result.script_path).read_text(), result.script_path, "exec")
    fbx.write_bytes(b"changed")
    with pytest.raises(ValueError, match="checksum"):
        prepare_unreal_handoff(report, tmp_path / "other", target=target(tmp_path))
    assert not (tmp_path / "other").exists()


def test_unreal_refuses_mismatched_rig_before_import(tmp_path, monkeypatch):
    directory = tmp_path / "unreal"
    directory.mkdir()
    fbx = tmp_path / "animation.fbx"
    fbx.write_bytes(b"mock export")
    report = tmp_path / "blender-report.json"
    report.write_text("{}")
    request = {
        "schema_version": 1, "kind": "unreal", "target": target(tmp_path).model_dump(),
        "source_fbx": str(fbx), "source_sha256": hashlib.sha256(fbx.read_bytes()).hexdigest(),
        "blender_report": str(report), "blender_report_sha256": hashlib.sha256(report.read_bytes()).hexdigest(),
        "expected_bones": [{"name": "KimodoRig", "parent": None}, {"name": "Hips", "parent": "KimodoRig"}],
    }
    (directory / "handoff.json").write_text(json.dumps(request))
    import kimodo_mcp.handoff as handoff

    script = directory / "unreal_handoff.py"
    script.write_text((Path(handoff.__file__).with_name("handoff_scripts") / "unreal.py").read_text())

    class Skeleton:
        pass

    skeleton = Skeleton()

    class SkeletalMesh:
        def get_editor_property(self, name):
            assert name == "skeleton"
            return skeleton

    class Component:
        def set_skeletal_mesh_asset(self, mesh):
            pass

        def get_num_bones(self):
            return 2

        def get_bone_name(self, index):
            return ["root", "pelvis"][index]

        def get_parent_bone(self, name):
            return "None" if name == "root" else "root"

    editor = SimpleNamespace(does_asset_exist=lambda path: False)
    fake = SimpleNamespace(
        Paths=SimpleNamespace(get_project_file_path=lambda: request["target"]["project_file"],
                              convert_relative_path_to_full=lambda path: path),
        EditorAssetLibrary=editor, Skeleton=Skeleton, SkeletalMesh=SkeletalMesh,
        SkeletalMeshComponent=Component,
        load_asset=lambda path: skeleton if path == request["target"]["skeleton_path"] else SkeletalMesh(),
        AnimPoseExtensions=SimpleNamespace(get_reference_pose=lambda skeleton: None,
                                          get_bone_names=lambda pose: ["root", "pelvis"]),
    )
    monkeypatch.setitem(sys.modules, "unreal", fake)
    with pytest.raises(ValueError, match="Incompatible source and target rig"):
        runpy.run_path(str(script), run_name="__main__")
    assert not (directory / "execution.lock").exists()
    assert not (directory / "unreal-report.json").exists()
