"""Direct MCP registration tests; never contact or start a FreeMoCap runtime."""

import sys

import pytest
from mcp.server.fastmcp import FastMCP
from starlette.testclient import TestClient

from kimodo_mcp import freemocap as capture
from kimodo_mcp.config import Settings
from kimodo_mcp.freemocap_api import TOOL_NAMES, register_freemocap_tools
from kimodo_mcp.studio import create_app


@pytest.fixture
def server(tmp_path):
    mcp = FastMCP("Capture API fixture")
    settings = Settings(data_dir=tmp_path, python=sys.executable)
    register_freemocap_tools(mcp, settings)
    return mcp, settings


async def invoke(mcp, name, arguments):
    result = await mcp.call_tool(name, arguments)
    assert isinstance(result, tuple), "Every tool must expose structured data"
    return result[1]


async def test_exact_inventory_annotations_and_no_runtime_import(server):
    mcp, _ = server
    tools = {tool.name: tool for tool in await mcp.list_tools()}
    assert set(tools) == set(TOOL_NAMES) and len(tools) == 10
    assert tools["plan_freemocap_capture"].annotations.readOnlyHint
    assert tools["start_freemocap_capture"].annotations.idempotentHint is False
    assert tools["start_freemocap_capture"].annotations.destructiveHint
    assert tools["cancel_freemocap_job"].annotations.destructiveHint
    assert not any(name.split(".")[0] == "freemocap" for name in sys.modules)
    request_schema = tools["plan_freemocap_capture"].inputSchema["$defs"]["FreeMocapRequest"]
    assert request_schema["additionalProperties"] is False
    assert set(request_schema["properties"]["operation"]["enum"]) == set(capture.ROUTES)


async def test_unconfigured_discovery_returns_actionable_state_without_http(server, monkeypatch):
    mcp, _ = server
    monkeypatch.setattr(capture.httpx, "Client", lambda *a, **k: pytest.fail("Unexpected runtime request"))
    result = await invoke(mcp, "get_freemocap_capabilities", {})
    assert result["configured"] is False and result["compatible"] is False
    assert result["reviewed_revision"] == capture.UPSTREAM_REVISION


@pytest.mark.parametrize(
    ("name", "arguments", "controller", "expected"),
    [
        ("list_freemocap_recordings", {"limit": 4}, "list_freemocap_recordings", 4),
        (
            "inspect_freemocap_recording",
            {"recording_id": "take-001"},
            "inspect_freemocap_recording",
            "take-001",
        ),
        ("get_freemocap_job", {"job_id": "a" * 32}, "get_freemocap_job", "a" * 32),
        ("cancel_freemocap_job", {"job_id": "b" * 32}, "cancel_freemocap_job", "b" * 32),
    ],
)
async def test_tools_forward_only_typed_identity_and_settings(
    server, monkeypatch, name, arguments, controller, expected
):
    mcp, settings = server
    calls = []
    monkeypatch.setattr(
        capture,
        controller,
        lambda actual_settings, value: calls.append((actual_settings, value)) or {"observed": value},
    )
    result = await invoke(mcp, name, arguments)
    assert calls == [(settings, expected)]
    assert result == {"observed": expected}


@pytest.mark.parametrize("name", ["plan_freemocap_capture", "start_freemocap_capture"])
async def test_exact_process_request_preserved_in_typed_operation(server, monkeypatch, name):
    mcp, settings = server
    calls = []
    monkeypatch.setattr(
        capture,
        name,
        lambda actual_settings, request: (
            calls.append((actual_settings, request))
            or {"status": "submitted", "pipeline_completion_verified": False}
        ),
    )
    request = {
        "operation": "process",
        "recording_id": "take-001",
        "allow_planar": True,
        "process_options": {"detector_type": "rtmpose", "cutoff_hz": 4.5, "filter_order": 2},
    }
    result = await invoke(mcp, name, {"request": request})
    assert calls[0][0] == settings and isinstance(calls[0][1], capture.FreeMocapRequest)
    assert calls[0][1].model_dump(mode="json", exclude_unset=True) == request
    assert result["pipeline_completion_verified"] is False


@pytest.mark.parametrize(
    ("name", "arguments"),
    [
        ("list_freemocap_recordings", {"limit": True}),
        ("list_freemocap_recordings", {"limit": 101}),
        ("inspect_freemocap_recording", {"recording_id": "../escape"}),
        ("inspect_freemocap_recording", {"recording_id": "http://example.com"}),
        ("get_freemocap_job", {"job_id": "a" * 64}),
        ("cancel_freemocap_job", {"job_id": "all"}),
        ("plan_freemocap_capture", {"request": {"operation": "shutdown"}}),
        (
            "start_freemocap_capture",
            {"request": {"operation": "process", "recording_id": "take", "allow_planar": "true"}},
        ),
        (
            "start_freemocap_capture",
            {"request": {"operation": "import", "video_files": ["a.mp4"], "board": {}}},
        ),
    ],
)
async def test_invalid_inputs_rejected_before_any_controller_call(server, monkeypatch, name, arguments):
    mcp, _ = server
    monkeypatch.setattr(capture, name, lambda *a, **k: pytest.fail("Invalid input reached controller"))
    with pytest.raises(Exception):
        await mcp.call_tool(name, arguments)


async def test_export_request_is_explicit_and_does_not_enable_automatic_processing_export(
    server, monkeypatch
):
    mcp, _ = server
    received = []
    monkeypatch.setattr(
        capture,
        "start_freemocap_capture",
        lambda settings, request: received.append(request) or {"status": "submitted"},
    )
    await invoke(
        mcp,
        "start_freemocap_capture",
        {
            "request": {
                "operation": "export",
                "recording_id": "take",
                "export_options": {"formats": ["bvh"], "rest_pose": "apose", "apply_foot_locking": False},
            }
        },
    )
    assert received[0].operation == "export"
    assert received[0].export_options.formats == ["bvh"]
    assert received[0].export_options.rest_pose == "apose"


def test_studio_exposes_all_actions_without_polling_external_runtime(tmp_path, monkeypatch):
    settings = Settings(data_dir=tmp_path / "data", python=sys.executable)
    web = tmp_path / "web"
    web.mkdir()
    (web / "index.html").write_text("<html>fixture</html>")
    calls = []
    monkeypatch.setattr(capture, "get_freemocap_capabilities", lambda settings: calls.append("capabilities") or {"compatible": False})
    monkeypatch.setattr(capture, "list_freemocap_recordings", lambda settings, limit: calls.append("list") or {"recordings": [], "limit": limit})
    with TestClient(create_app(settings, web), base_url="http://127.0.0.1:8768") as browser:
        state = browser.get("/api/state")
        assert state.status_code == 200 and not calls
        headers = {"x-studio-token": state.json()["csrf_token"]}
        assert browser.get("/api/state").status_code == 200 and not calls
        assert browser.post("/api/actions/get_freemocap_capabilities", json={}).status_code == 403
        result = browser.post("/api/actions/get_freemocap_capabilities", json={}, headers=headers)
        assert result.status_code == 200 and result.json()["result"]["compatible"] is False
        listed = browser.post("/api/actions/list_freemocap_recordings", json={"limit": 3}, headers=headers)
        assert listed.status_code == 200 and listed.json()["result"]["limit"] == 3
        assert calls == ["capabilities", "list"]
        from kimodo_mcp.studio import _ACTIONS
        assert set(TOOL_NAMES) <= _ACTIONS


async def test_review_tools_are_readonly_typed_and_forward_exact_selectors(server, monkeypatch):
    from kimodo_mcp import freemocap_review as review
    mcp, settings = server
    seen = []
    monkeypatch.setattr(review, "inspect_freemocap_frames", lambda *args: seen.append(args) or {"frames_inspected": []})
    result = await invoke(mcp, "inspect_freemocap_frames", {"job_id": "a" * 32, "frame_indices": [4, 2], "aspect": "left_hand"})
    assert result == {"frames_inspected": []}
    assert seen == [(settings, "a" * 32, [4, 2], "left_hand")]
    for name in ["inspect_freemocap_frames", "analyze_freemocap_quality", "get_freemocap_artifact"]:
        tool = next(tool for tool in await mcp.list_tools() if tool.name == name)
        assert tool.annotations.readOnlyHint and not tool.annotations.destructiveHint
    for bad in [{"frame_indices": [True]}, {"frame_indices": [-1]}, {"frame_indices": list(range(17))}, {"aspect": "unknown"}]:
        with pytest.raises(Exception):
            await mcp.call_tool("inspect_freemocap_frames", {"job_id": "a" * 32, **bad})
