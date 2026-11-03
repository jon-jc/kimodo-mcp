"""Variant selectors must survive every editor preparation and execution hop."""

import hashlib
import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from kimodo_mcp import blender_bridge, unreal_bridge
from kimodo_mcp import server as server_module
from kimodo_mcp.config import Settings
from kimodo_mcp.handoff import UnrealTarget
from kimodo_mcp.jobs import JobStore
from kimodo_mcp.workspace import Workspace, handoff_directory_name


@pytest.fixture
def context(tmp_path, monkeypatch):
    settings = Settings(tmp_path, sys.executable)
    store = JobStore(settings)
    job = store.submit({}, launch=False)
    root = store.directory(job["id"])
    (root / "manifest.json").write_text(
        json.dumps(
            {
                "samples": 1,
                "frames": 120,
                "fps": 30,
                "skeleton": "somaskel77",
                "artifacts": [],
            }
        )
    )
    store.update(job["id"], "completed")
    variant_id, export_id = "b" * 64, "c" * 64
    selected = {
        "absolute_path": str(root / "variants" / variant_id / "motion_00.npz"),
        "sha256": "d" * 64,
        "frames": 90,
        "fps": 30,
        "skeleton": "somaskel77",
        "variant_id": variant_id,
    }
    calls = []

    def resolve(self, identifier, sample=0, variant=None):
        calls.append((identifier, sample, variant))
        assert identifier == job["id"] and sample == 0 and variant == variant_id
        return selected

    monkeypatch.setattr(Workspace, "resolve_motion", resolve)
    monkeypatch.setenv("KIMODO_CHARACTER_FBX", str(tmp_path / "operator.fbx"))
    application = server_module.create_server(settings)
    return application, root, job["id"], variant_id, export_id, selected, calls


def test_character_preparation_uses_variant_timing_and_explicit_selector(context, monkeypatch):
    app, root, job, variant, _, selected, _ = context
    calls = []
    monkeypatch.setattr(
        server_module,
        "prepare_character_files",
        lambda *args, **kwargs: (
            calls.append((args, kwargs)) or SimpleNamespace(model_dump=lambda: {"directory": str(args[2])})
        ),
    )
    result = app._tool_manager.get_tool("prepare_character_handoff").fn(job, attempt=2, variant_id=variant)
    args, kwargs = calls[0]
    assert args[1] == Path(selected["absolute_path"])
    assert args[2] == root / "handoffs" / handoff_directory_name("character", 0, 2, variant)
    assert kwargs["frames"] == 90 and kwargs["source_sha256"] == selected["sha256"]
    assert kwargs["variant_id"] == result["variant_id"] == variant


def test_skeleton_requires_selected_variant_export_and_passes_both_ids(context, monkeypatch):
    app, root, job, variant, export_id, _, _ = context
    calls = []

    def verified(self, identifier, export, *, sample_index=0, variant_id=None):
        assert (identifier, export, sample_index, variant_id) == (job, export_id, 0, variant)
        return {
            "absolute_path": str(root / "exports" / export_id / "motion_00.bvh"),
            "variant_id": variant,
            "format": "bvh",
            "sha256": "e" * 64,
            "options": {"bvh_standard_tpose": True},
        }

    monkeypatch.setattr(Workspace, "verified_motion_export", verified)
    monkeypatch.setattr(
        server_module,
        "prepare_blender_files",
        lambda *args, **kwargs: calls.append((args, kwargs)) or SimpleNamespace(model_dump=lambda: {}),
    )
    tool = app._tool_manager.get_tool("prepare_blender_handoff").fn
    with pytest.raises(ValueError, match="export_id"):
        tool(job, variant_id=variant)
    tool(job, attempt=1, variant_id=variant, export_id=export_id)
    args, kwargs = calls[0]
    assert args[1] == root / "handoffs" / handoff_directory_name("skeleton", 0, 1, variant)
    assert kwargs["frames"] == 90 and kwargs["variant_id"] == variant and kwargs["export_id"] == export_id


@pytest.mark.parametrize("kind", ["skeleton", "character"])
def test_execute_and_read_blender_variant_never_fall_back_to_native(context, monkeypatch, kind):
    app, root, job, variant, _, _, _ = context
    folder = root / "handoffs" / handoff_directory_name(kind, 0, 1, variant)
    folder.mkdir(parents=True)
    script = folder / ("character_handoff.py" if kind == "character" else "blender_handoff.py")
    script.write_text("fixed fixture script; not executed")
    (folder / "blender-report.json").write_text(json.dumps({"status": "completed", "variant_id": variant}))
    calls = []
    monkeypatch.setattr(
        blender_bridge, "execute_handoff", lambda *args: calls.append(args) or {"status": "completed"}
    )
    app._tool_manager.get_tool("execute_blender_handoff").fn(job, kind, 0, 1, variant_id=variant)
    assert calls == [(script.resolve(), kind)]
    result = app._tool_manager.get_tool("get_handoff_report").fn(job, kind, 0, 1, variant_id=variant)
    assert result["variant_id"] == variant


def test_unreal_prepare_execute_and_report_keep_variant_directory(context, monkeypatch, tmp_path):
    app, root, job, variant, _, _, _ = context
    target = UnrealTarget(
        project_file=str(tmp_path / "Project.uproject"),
        skeleton_path="/Game/Rig/Skeleton",
        skeletal_mesh_path="/Game/Rig/Mesh",
        destination_path="/Game/Clips",
        asset_name="Edited",
    )
    folder = root / "handoffs" / handoff_directory_name("character", 0, 1, variant)
    folder.mkdir(parents=True)
    (folder / "blender-report.json").write_text("{}")
    key = hashlib.sha256(target.model_dump_json().encode()).hexdigest()[:16]
    destination = folder / f"unreal-{key}"
    destination.mkdir()
    script = destination / "unreal_handoff.py"
    script.write_text("fixed fixture script; not executed")
    (destination / "unreal-report.json").write_text(
        json.dumps({"status": "completed", "variant_id": variant})
    )
    preparation = []
    monkeypatch.setattr(
        server_module,
        "prepare_unreal_files",
        lambda *args, **kwargs: preparation.append((args, kwargs)) or SimpleNamespace(model_dump=lambda: {}),
    )
    app._tool_manager.get_tool("prepare_unreal_handoff").fn(
        job, target, "character", 0, 1, variant_id=variant
    )
    assert preparation == [
        ((folder / "blender-report.json", destination), {"target": target, "variant_id": variant})
    ]
    engine = tmp_path / "Engine"
    monkeypatch.setenv("KIMODO_UNREAL_ENGINE", str(engine))
    executions = []
    monkeypatch.setattr(
        unreal_bridge, "execute_handoff", lambda *args: executions.append(args) or {"status": "completed"}
    )
    app._tool_manager.get_tool("execute_unreal_handoff").fn(
        job, target, "character", 0, 1, variant_id=variant
    )
    assert executions == [(script.resolve(), engine, Path(target.project_file))]
    result = app._tool_manager.get_tool("get_unreal_report").fn(
        job, target, "character", 0, 1, variant_id=variant
    )
    assert result["variant_id"] == variant
