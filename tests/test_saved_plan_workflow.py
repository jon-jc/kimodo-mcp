"""The studio and MCP must share exact saved requests, including advanced controls."""

import sys

import pytest
from starlette.testclient import TestClient

from kimodo_mcp import studio as studio_module
from kimodo_mcp.config import Settings
from kimodo_mcp.jobs import JobStore
from kimodo_mcp.server import create_server
from kimodo_mcp.studio import create_app


@pytest.mark.asyncio
async def test_saved_sequence_round_trips_between_clients_without_losing_constraints(tmp_path, monkeypatch):
    settings = Settings(tmp_path / "data", sys.executable)
    monkeypatch.delenv("KIMODO_CHARACTER_FBX", raising=False)
    monkeypatch.setattr(studio_module, "capacity_snapshot", lambda: {})
    monkeypatch.setattr(studio_module, "editor_connections", lambda: {})
    # Test the submission contract without starting a worker or an editor.
    original_submit = JobStore.submit

    def submit(self, request, *, launch=True, context=None):
        return original_submit(self, request, launch=False, context=context)

    monkeypatch.setattr(JobStore, "submit", submit)
    server = create_server(settings)
    explicit_request = {
        "segments": [
            {"text": "A person lowers into a ready stance.", "duration": 1.5},
            {"text": "From a ready stance, the person steps forward and raises their right arm.", "duration": 2.0},
            {"text": "The person lowers their raised right arm and settles into standing.", "duration": 1.5},
        ],
        "seed": 439,
        "samples": 2,
        "steps": 37,
        "cfg_text": 3.25,
        "cfg_constraint": 4.5,
        "transition_frames": 7,
        "first_heading_angle": 0.5,
        "formats": ["npz", "bvh"],
        "bvh_standard_tpose": True,
        "constraints": [{"type": "root2d", "frame_indices": [0, 149],
                         "smooth_root_2d": [[0.0, 0.0], [0.0, 0.8]]}],
    }
    _, saved = await server.call_tool("save_animation_plan", {
        "title": "Ready, step and recover", "description": "A reusable staged movement.",
        "request": explicit_request,
    })
    assert JobStore(settings).list() == []
    original = saved["request"]
    assert saved["plan"]["segment_frames"] == [45, 60, 45]
    changed_beats = [dict(beat) for beat in original["segments"]]
    changed_beats[1]["text"] = "From a ready stance, the person steps deliberately forward and raises their right arm."

    web = tmp_path / "web"
    web.mkdir()
    (web / "index.html").write_text("<html>Studio fixture</html>")
    with TestClient(create_app(settings, web), base_url="http://127.0.0.1:8768") as client:
        token = client.get("/api/state").json()["csrf_token"]

        def action(name, arguments):
            response = client.post(f"/api/actions/{name}", json=arguments,
                                   headers={"x-studio-token": token})
            assert response.status_code == 200, response.text
            return response.json()["result"]

        listed = action("list_animation_plans", {})
        assert saved["plan_id"] in [item["plan_id"] for item in listed["plans"]]
        assert action("get_animation_plan", {"plan_id": saved["plan_id"]})["request"] == original
        revision = {"beats": changed_beats}
        preview = action("revise_animation_plan", {"plan_id": saved["plan_id"], "revision": revision})
        assert preview["request"]["constraints"] == original["constraints"]
        assert action("list_animation_plans", {})["total"] == 1
        assert JobStore(settings).list() == []
        revised = action("revise_animation_plan", {
            "plan_id": saved["plan_id"], "revision": revision, "save": True,
        })
        assert revised["plan_id"] != saved["plan_id"]
        assert revised["parent_plan_id"] == saved["plan_id"]
        _, from_mcp = await server.call_tool("get_animation_plan", {"plan_id": revised["plan_id"]})
        assert from_mcp["request"] == revised["request"]
        assert from_mcp["request"]["steps"] == 37
        assert from_mcp["request"]["cfg_constraint"] == 4.5
        assert from_mcp["request"]["transition_frames"] == 7
        assert from_mcp["description"] == saved["description"]
        submitted = action("generate_animation_plan", {"plan_id": revised["plan_id"]})
        assert submitted["request"] == revised["request"]
        assert submitted["context"]["saved_plan_id"] == revised["plan_id"]
        assert submitted["context"]["request_sha256"] == revised["request_sha256"]

    _, unchanged = await server.call_tool("get_animation_plan", {"plan_id": saved["plan_id"]})
    assert unchanged["request"] == original
    assert unchanged["request_sha256"] == saved["request_sha256"]
    assert len(JobStore(settings).list()) == 1
