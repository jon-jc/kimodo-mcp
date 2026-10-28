import json
import sys

from kimodo_mcp.config import Settings
from kimodo_mcp.jobs import JobStore
from kimodo_mcp.server import create_server


def test_managed_handoff_attempts_and_missing_reports(tmp_path):
    settings = Settings(tmp_path, sys.executable)
    store = JobStore(settings)
    job = store.submit({}, launch=False)
    directory = store.directory(job["id"])
    (directory / "manifest.json").write_text(json.dumps({"samples": 1, "artifacts": []}))
    store.update(job["id"], "completed")
    server = create_server(settings)
    report_tool = server._tool_manager.get_tool("get_handoff_report").fn
    first = report_tool(job["id"], "character", 0, 0)
    retry = report_tool(job["id"], "character", 0, 1)
    assert first["report_path"] != retry["report_path"]
    assert retry["status"] == "not_executed"
    from pathlib import Path

    path = Path(retry["report_path"])
    path.parent.mkdir(parents=True)
    (path.parent / "execution.lock").write_text("attempted")
    assert report_tool(job["id"], "character", 0, 1)["status"] == "not_completed"


def test_character_configuration_never_comes_from_tool_arguments(tmp_path):
    server = create_server(Settings(tmp_path, sys.executable))
    tool = server._tool_manager.get_tool("prepare_character_handoff")
    assert set(tool.parameters["properties"]) == {"job_id", "sample_index", "attempt"}


def test_unreal_execution_resolves_managed_script_and_operator_engine(tmp_path, monkeypatch):
    import hashlib
    from pathlib import Path

    from kimodo_mcp import unreal_bridge
    from kimodo_mcp.handoff import UnrealTarget

    settings = Settings(tmp_path, sys.executable)
    store = JobStore(settings)
    job = store.submit({}, launch=False)
    directory = store.directory(job["id"])
    (directory / "manifest.json").write_text(json.dumps({"samples": 1, "artifacts": []}))
    store.update(job["id"], "completed")
    target = UnrealTarget(project_file=str(tmp_path / "Studio.uproject"), skeleton_path="/Game/Rig/Skeleton",
                          skeletal_mesh_path="/Game/Rig/Mesh", destination_path="/Game/Clips", asset_name="Wave")
    key = hashlib.sha256(target.model_dump_json().encode()).hexdigest()[:16]
    script = directory / "handoffs" / "character-motion_00" / f"unreal-{key}" / "unreal_handoff.py"
    script.parent.mkdir(parents=True)
    script.write_text("verified by bridge")
    engine = tmp_path / "EngineInstall"
    monkeypatch.setenv("KIMODO_UNREAL_ENGINE", str(engine))
    calls = []
    monkeypatch.setattr(unreal_bridge, "execute_handoff", lambda *args: calls.append(args) or {"status": "completed"})
    tool = create_server(settings)._tool_manager.get_tool("execute_unreal_handoff")
    assert set(tool.parameters["properties"]) == {"job_id", "target", "source_kind", "sample_index", "attempt"}
    assert tool.fn(job["id"], target, "character")["status"] == "completed"
    assert calls == [(script.resolve(), engine, Path(target.project_file))]
