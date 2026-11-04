"""The browser uses the same saved requests as MCP, without runtime execution."""

import sys

import pytest
from starlette.testclient import TestClient

from kimodo_mcp import studio
from kimodo_mcp.animation_plans import AnimationPlanStore
from kimodo_mcp.config import Settings
from kimodo_mcp.jobs import JobStore
from kimodo_mcp.schemas import GenerateRequest


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.delenv("KIMODO_CHARACTER_FBX", raising=False)
    monkeypatch.setattr(studio, "capacity_snapshot", lambda: {})
    monkeypatch.setattr(studio, "editor_connections", lambda: {})
    settings = Settings(data_dir=tmp_path / "data", python=sys.executable)
    with TestClient(studio.create_app(settings), base_url="http://127.0.0.1:8768") as browser:
        response = browser.get("/api/state")
        assert response.status_code == 200, response.text
        headers = {"x-studio-token": response.json()["csrf_token"]}
        yield browser, settings, headers


def request_data():
    return GenerateRequest(
        segments=[{"text": "A person approaches.", "duration": 2.0},
                  {"text": "A person stops and waves.", "duration": 2.0}],
        seed=419, samples=2, steps=37, cfg_text=3.25, cfg_constraint=1.75,
        transition_frames=7, first_heading_angle=0.3, root_margin=0.07,
        bvh_standard_tpose=True, formats=["npz", "bvh"],
        constraints=[{"type": "root2d", "frame_indices": [0, 119],
                      "smooth_root_2d": [[0.0, 0.0], [0.0, 1.0]]}],
    ).model_dump(mode="json")


def call(client, name, arguments):
    browser, _, headers = client
    response = browser.post(f"/api/actions/{name}", json=arguments, headers=headers)
    assert response.status_code == 200, response.text
    return response.json()["result"]


def test_authoring_roundtrip_preserves_full_request_without_submitting(client, monkeypatch):
    _, settings, _ = client

    def forbidden(*args, **kwargs):
        pytest.fail("Saved-plan authoring must not submit a generation job")

    monkeypatch.setattr(JobStore, "submit", forbidden)
    values = request_data()
    saved = call(client, "save_animation_plan", {
        "title": "Approach and greet", "description": "Keep the root path.", "request": values,
    })
    loaded = call(client, "get_animation_plan", {"plan_id": saved["plan_id"]})
    assert loaded["request"] == values
    assert loaded["plan"]["segment_frames"] == [60, 60]
    revision = {"beats": [{"text": "A person slowly approaches.", "duration": 2.0},
                          {"text": "A person settles and waves gently.", "duration": 2.0}]}
    preview = call(client, "revise_animation_plan", {"plan_id": saved["plan_id"], "revision": revision})
    assert "plan_id" not in preview
    assert set(preview["changes"]) == {"segments"}
    assert {key: value for key, value in preview["request"].items() if key != "segments"} == {
        key: value for key, value in values.items() if key != "segments"
    }
    assert call(client, "list_animation_plans", {})["total"] == 1
    child = call(client, "revise_animation_plan", {
        "plan_id": saved["plan_id"], "revision": revision, "title": "Gentler greeting", "save": True,
    })
    assert child["parent_plan_id"] == saved["plan_id"]
    assert call(client, "get_animation_plan", {"plan_id": saved["plan_id"]})["request"] == values
    listing = call(client, "list_animation_plans", {"limit": 1, "offset": 1})
    assert listing["total"] == 2 and len(listing["plans"]) == 1 and listing["invalid_count"] == 0
    assert "request" not in listing["plans"][0]
    assert not JobStore(settings).list()


def test_explicit_generate_route_passes_exact_saved_request_and_provenance(client, monkeypatch):
    _, settings, _ = client
    saved = AnimationPlanStore(settings.data_dir).save("Exact take", GenerateRequest.model_validate(request_data()))
    calls = []

    def capture(self, request, **kwargs):
        calls.append((request, kwargs))
        return {"id": "a" * 32, "status": "queued"}

    monkeypatch.setattr(JobStore, "submit", capture)
    result = call(client, "generate_animation_plan", {"plan_id": saved["plan_id"]})
    assert result["saved_plan_id"] == saved["plan_id"] and result["status"] == "queued"
    assert calls == [(saved["request"], {"context": {
        "saved_plan_id": saved["plan_id"], "request_sha256": saved["request_sha256"],
    }})]
    assert not JobStore(settings).list()


@pytest.mark.parametrize("name", ["save_animation_plan", "get_animation_plan", "list_animation_plans",
                                 "revise_animation_plan", "generate_animation_plan"])
def test_saved_plan_actions_retain_request_token_requirement(client, name):
    browser, _, _ = client
    assert browser.post(f"/api/actions/{name}", json={}).status_code == 403


def test_saved_plan_errors_do_not_create_records_or_jobs(client):
    browser, settings, headers = client
    response = browser.post("/api/actions/save_animation_plan", json={
        "title": "Invalid", "request": {**request_data(), "steps": "37"},
    }, headers=headers)
    assert response.status_code == 400 and "error" in response.json()
    response = browser.post("/api/actions/get_animation_plan", json={
        "plan_id": "../request.json",
    }, headers=headers)
    assert response.status_code == 400
    assert call(client, "list_animation_plans", {})["total"] == 0
    assert not JobStore(settings).list()


def test_periodic_studio_state_does_not_rescan_saved_plan_library(client, monkeypatch):
    browser, _, _ = client

    def forbidden(*args, **kwargs):
        pytest.fail("Periodic state polling must not rescan the saved-plan library")

    monkeypatch.setattr(AnimationPlanStore, "list", forbidden)
    assert browser.get("/api/state").status_code == 200
