"""Exercise agent access through validated tool calls and completed-job provenance."""

import asyncio
import hashlib
import json
import sys
import threading
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pytest
from mcp.server.fastmcp.exceptions import ToolError

from kimodo_mcp import export
from kimodo_mcp.config import Settings
from kimodo_mcp.jobs import JobStore, atomic_json
from kimodo_mcp.pose import _soma_layout
from kimodo_mcp.schemas import GenerateRequest
from kimodo_mcp.server import create_server


@pytest.fixture
def managed_motion(tmp_path):
    settings = Settings(tmp_path, sys.executable)
    store = JobStore(settings)
    request = GenerateRequest(segments=[{"text": "A person waves.", "duration": 1.0}])
    job = store.submit(request.model_dump(mode="json"), launch=False)
    path = store.directory(job["id"]) / "motion_00.npz"
    names, parents = _soma_layout(77)
    positions = np.zeros((30, 77, 3))
    positions[:, :, 0] = np.arange(30)[:, None] / 10
    positions[:, :, 1] = 1
    np.savez(path, posed_joints=positions, joint_names=np.array(names), joint_parents=np.array(parents),
             fps=30, skeleton="somaskel77", model=request.model,
             local_rot_mats=np.broadcast_to(np.eye(3), (30, 77, 3, 3)))
    atomic_json(path.parent / "manifest.json", {
        "samples": 1, "fps": 30, "frames": 30, "skeleton": "somaskel77", "artifacts": [{
            "path": path.name, "format": "npz", "sample_index": 0, "bytes": path.stat().st_size,
            "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
        }],
    })
    store.update(job["id"], "completed")
    return create_server(settings), store, job["id"], path


async def test_agent_reads_actual_frames_and_builds_valid_constraints(managed_motion):
    server, store, job_id, path = managed_motion
    before = path.read_bytes()
    _, frames = await server.call_tool("inspect_motion_frames", {"job_id": job_id, "frame_indices": [0, 29]})
    assert frames["source_sha256"] == hashlib.sha256(before).hexdigest()
    assert "2.9" in json.dumps(frames)
    _, result = await server.call_tool("extract_motion_constraints", {
        "job_id": job_id, "source_frames": [0, 29], "target_frames": [0, 59], "constraint_type": "right-hand",
    })
    constraint = result["constraint"]
    assert constraint["frame_indices"] == [0, 59]
    assert constraint["root_positions"] == [[0.0, 1.0, 0.0], [2.9, 1.0, 0.0]]
    _, validated = await server.call_tool("validate_motion", {"request": {
        "segments": [{"text": "A person continues waving.", "duration": 2.0}], "constraints": [constraint],
    }})
    assert validated["valid"]
    assert path.read_bytes() == before
    assert len(store.list()) == 1


@pytest.mark.parametrize("tool,arguments", [
    ("inspect_motion_frames", {"frame_indices": [0]}),
    ("extract_motion_constraints", {"source_frames": [0], "target_frames": [0]}),
    ("inspect_motion", {}), ("preview_motion", {}), ("list_job_outputs", {}),
])
async def test_changed_motion_cannot_be_inspected_or_advertised(managed_motion, tool, arguments):
    server, _, job_id, path = managed_motion
    path.write_bytes(path.read_bytes() + b"changed")
    with pytest.raises(ToolError, match="(?i)(changed|checksum|size|match|differs)"):
        await server.call_tool(tool, {"job_id": job_id, **arguments})


async def test_agent_target_and_studio_share_saved_settings(tmp_path, monkeypatch):
    monkeypatch.delenv("KIMODO_CHARACTER_FBX", raising=False)
    settings = Settings(tmp_path, sys.executable)
    server = create_server(settings)
    target = {"project_file": str(tmp_path / "Project.uproject"), "skeleton_path": "/Game/Hero/Skeleton",
              "skeletal_mesh_path": "/Game/Hero/Mesh", "destination_path": "/Game/Motions", "asset_name": "Wave_02"}
    atomic_json(tmp_path / "studio.json", {"future_setting": "retained"})
    await server.call_tool("save_unreal_target", {"target": target})
    _, saved = await server.call_tool("get_unreal_target", {})
    _, state = await server.call_tool("get_workspace_state", {"recent_jobs": 0})
    assert saved["target"] == state["unreal_target"]
    assert saved["configured"]
    assert state["jobs"] == []
    assert "capacity" in state["health"] and "editors" in state["health"]
    assert json.loads((tmp_path / "studio.json").read_text())["future_setting"] == "retained"


async def test_preview_tool_uses_shared_workflow(managed_motion, monkeypatch):
    server, _, job_id, _ = managed_motion
    from kimodo_mcp.workspace import Workspace

    calls = []

    def build(self, job_id, sample_index, attempt, *, prepare, execute):
        calls.append((job_id, sample_index, attempt, prepare.__name__, execute.__name__))
        return {"job_id": job_id, "attempt": 2, "reused": True}

    monkeypatch.setattr(Workspace, "build_character_preview", build)
    _, result = await server.call_tool("build_character_preview", {"job_id": job_id})
    assert result["reused"]
    assert calls == [(job_id, 0, None, "prepare_character_handoff", "execute_blender_handoff")]


async def test_export_tool_and_derived_blender_handoff_preserve_original_job(managed_motion, monkeypatch):
    server, store, job_id, path = managed_motion
    before = {item.name: item.read_bytes() for item in (path, path.parent / "manifest.json")}
    calls = []

    def convert(command, **kwargs):
        calls.append(command)
        request = export.ExportRequest.model_validate_json(Path(command[4]).read_text())
        motion = export.load_source(request)
        output = Path(request.output_directory) / request.basename
        # The subprocess boundary is mocked here; native canonical exporters
        # are validated separately in the optional local inference runtime.
        output.write_text("HIERARCHY\nROOT Root\n{\nOFFSET 0 0 0\n"
                          "CHANNELS 6 Xposition Yposition Zposition Zrotation Yrotation Xrotation\n}\n"
                          "MOTION\nFrames: 30\nFrame Time: 0.0333333333\n" + "0 0 0 0 0 0\n" * 30)
        atomic_json(output.parent / "export-report.json", export.make_report(request, motion, output))
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr(export.subprocess, "run", convert)
    arguments = {"job_id": job_id, "format": "bvh", "bvh_standard_tpose": True}
    _, result = await server.call_tool("export_motion", arguments)
    _, reused = await server.call_tool("export_motion", arguments)
    assert result == reused and len(calls) == 1
    assert result["artifact"]["url"].endswith("/motion_00.bvh")
    assert result["artifact"]["uri"].startswith("file:")
    _, handoff = await server.call_tool("prepare_blender_handoff", {
        "job_id": job_id, "export_id": result["export_id"], "attempt": 1,
    })
    assert handoff["source_export_id"] == result["export_id"]
    assert handoff["export_options"]["bvh_standard_tpose"] is True
    prepared = json.loads((path.parent / "handoffs/skeleton-motion_00-attempt01/handoff.json").read_text())
    assert prepared["source_sha256"] == result["artifact"]["sha256"]
    assert prepared["source_bvh"] == result["artifact"]["absolute_path"]
    assert before == {item.name: item.read_bytes() for item in (path, path.parent / "manifest.json")}
    assert len(store.list()) == 1 and store.get(job_id)["status"] == "completed"


@pytest.mark.parametrize("arguments", [
    {"sample_index": True}, {"sample_index": -1}, {"format": "fbx"},
    {"bvh_standard_tpose": 1}, {"amass_z_up": False}, {"mujoco_rest_zero": True},
])
async def test_export_tool_rejects_invalid_controls_without_dispatch(managed_motion, monkeypatch, arguments):
    server, _, job_id, path = managed_motion
    monkeypatch.setattr(export.subprocess, "run", lambda *a, **k: pytest.fail("Invalid input dispatched a process"))
    with pytest.raises(ToolError):
        await server.call_tool("export_motion", {"job_id": job_id, **arguments})
    assert not (path.parent / "exports").exists()


async def test_export_does_not_block_other_agent_operations(managed_motion, monkeypatch):
    server, _, job_id, _ = managed_motion
    started, release, finished = threading.Event(), threading.Event(), threading.Event()

    def conversion(*args, **kwargs):
        started.set()
        release.wait(timeout=5)
        finished.set()
        raise ValueError("Controlled converter failure")

    monkeypatch.setattr(export, "export_motion", conversion)
    task = asyncio.create_task(server.call_tool("export_motion", {"job_id": job_id}))
    try:
        for _ in range(100):
            if started.is_set():
                break
            await asyncio.sleep(0.01)
        assert started.is_set() and not finished.is_set()
        _, models = await server.call_tool("list_models", {})
        assert models["models"] and not finished.is_set()
    finally:
        release.set()
        with pytest.raises(ToolError, match="Controlled converter failure"):
            await task
