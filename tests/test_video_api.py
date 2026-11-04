"""Shared capture tools and Studio boundaries, without running video inference."""

import asyncio
import sys

import numpy as np
import pytest
from starlette.testclient import TestClient

from kimodo_mcp import video_api
from kimodo_mcp import video_mocap as capture
from kimodo_mcp.config import Settings
from kimodo_mcp.jobs import atomic_json
from kimodo_mcp.server import create_server
from kimodo_mcp.studio import create_app
from kimodo_mcp.video_mocap_math import SMPL_NAMES, SMPL_PARENTS


@pytest.fixture
def settings(tmp_path):
    return Settings(data_dir=tmp_path / "data", python=sys.executable)


def completed(settings):
    identifier = "c" * 32
    directory = settings.data_dir / "video-mocap" / "jobs" / identifier
    directory.mkdir(parents=True)
    controls = capture.VideoMocapRequest(video_path=str(directory / "source.mp4"), duration_seconds=1, fps=6)
    request = {"id": identifier, "request": controls.model_dump(), "source": {"sha256": "a" * 64}}
    atomic_json(directory / "request.json", request)
    atomic_json(directory / "job.json", {"id": identifier, "status": "completed",
        "request": request["request"], "source": request["source"], "request_sha256": capture._canonical(request)})
    np.savez(directory / "motion.npz", posed_joints=np.arange(6 * 24 * 3).reshape(6, 24, 3) / 100,
        joint_names=np.asarray(SMPL_NAMES), joint_parents=SMPL_PARENTS,
        source_time_seconds=np.arange(6) / 6, keypoints2d=np.ones((6, 25, 3)))
    for name in capture.ARTIFACT_NAMES - {"motion.npz"}:
        (directory / name).write_bytes(b"synthetic API fixture, not processed footage")
    atomic_json(directory / "manifest.json", {"version": 1, "job_id": identifier,
        "request_sha256": capture._canonical(request), "source_sha256": request["source"]["sha256"],
        "reviewed_revision": capture.EASYMOCAP_REVISION, "frames": 6, "fps": 6,
        "skeleton": "smpl24", "model": "easymocap-smpl",
        "coordinates": {"up_axis": "Y", "units": "estimated meters", "space": "camera-relative"},
        "artifacts": [{"name": name, "path": name, "bytes": (directory / name).stat().st_size,
                       "sha256": capture._hash(directory / name)} for name in sorted(capture.ARTIFACT_NAMES)]})
    return identifier, directory


async def call(server, name, arguments):
    result = await server.call_tool(name, arguments)
    assert isinstance(result, tuple), "Tool must expose structured MCP results"
    return result[1]


async def test_capture_catalog_and_readiness_do_not_launch(settings, monkeypatch):
    monkeypatch.setattr(capture.subprocess, "Popen", lambda *a, **k: pytest.fail("Unexpected runtime launch"))
    server = create_server(settings)
    catalog = await call(server, "get_capabilities", {})
    assert catalog["tool_count"] == 103
    assert len(catalog["schemas"]) == 15 and len(catalog["workflows"]) == 20
    assert set(video_api.TOOL_NAMES) <= {tool["name"] for tool in catalog["tools"]}
    result = await call(server, "plan_video_mocap", {"request": {"video_path": str(settings.data_dir / "absent.mp4")}})
    assert result["ready"] is False
    assert not (settings.data_dir / "video-mocap" / "jobs").exists()


async def test_capture_frame_inspection_preserves_requested_order_and_exact_data(settings):
    identifier, directory = completed(settings)
    server = create_server(settings)
    result = await call(server, "inspect_video_mocap_frames", {"job_id": identifier, "frame_indices": [5, 1]})
    assert [frame["frame_index"] for frame in result["frames_inspected"]] == [5, 1]
    assert result["frames_inspected"][0]["source_time_seconds"] == 5 / 6
    with np.load(directory / "motion.npz") as motion:
        assert result["frames_inspected"][0]["positions_m"] == motion["posed_joints"][5].tolist()
    assert result["editable"]["supported"] is False and result["contact_status"] == "unavailable"
    default = await call(server, "inspect_video_mocap_frames", {"job_id": identifier})
    assert default["frames_inspected"][0]["frame_index"] == 0
    artifact = await call(server, "get_video_mocap_artifact", {"job_id": identifier, "name": "motion.bvh"})
    assert artifact["sha256"] == capture._hash(directory / "motion.bvh")


@pytest.mark.parametrize("indices", [[-1], [True], [6], [0, 0], list(range(17))])
async def test_capture_inspection_rejects_invalid_selection(settings, indices):
    identifier, _ = completed(settings)
    with pytest.raises(Exception):
        await create_server(settings).call_tool("inspect_video_mocap_frames", {"job_id": identifier, "frame_indices": indices})


@pytest.fixture
def client(settings, tmp_path):
    web = tmp_path / "web"
    web.mkdir()
    (web / "index.html").write_text("<html>fixture</html>")
    with TestClient(create_app(settings, web), base_url="http://127.0.0.1:8768") as value:
        state = value.get("/api/state")
        assert state.status_code == 200, state.text
        yield value, {"x-studio-token": state.json()["csrf_token"]}


def test_browser_copy_token_origin_and_stream_size(client, settings, monkeypatch):
    browser, headers = client
    route = "/api/video-mocap/reference?extension=mp4"
    assert browser.post(route, content=b"video").status_code == 403
    assert browser.post(route, content=b"video", headers={**headers, "origin": "http://remote.example"}).status_code == 403
    assert browser.post(route + "&extension=mov", content=b"video", headers=headers).status_code == 400
    monkeypatch.setattr(video_api, "MAX_UPLOAD", 4)
    oversized = browser.post(route, content=b"12345", headers=headers)
    assert oversized.status_code == 400
    references = settings.data_dir / "video-mocap" / "references"
    assert not list(references.glob("*"))
    saved = browser.post(route, content=b"1234", headers=headers)
    assert saved.status_code == 200
    path = saved.json()["path"]
    assert path.endswith(".mp4") and saved.json()["bytes"] == 4
    assert len(list(references.glob("*.mp4"))) == 1
    assert not list(references.glob("*.upload"))
    assert not (settings.data_dir / "video-mocap" / "jobs").exists()


def test_http_and_mcp_share_verified_capture_and_reject_tampering(client, settings):
    browser, headers = client
    identifier, directory = completed(settings)
    state = browser.get("/api/state").json()
    assert state["video_mocap_jobs"]["jobs"][0]["id"] == identifier
    workspace = asyncio.run(call(create_server(settings), "get_workspace_state", {}))
    assert workspace["video_mocap_jobs"]["jobs"][0]["id"] == identifier
    preview = browser.get(f"/api/video-mocap/{identifier}/preview")
    assert preview.status_code == 200 and preview.json()["frames"] == 6
    action = browser.post("/api/actions/inspect_video_mocap_frames", json={"job_id": identifier, "frame_indices": [2]}, headers=headers)
    assert action.status_code == 200
    assert action.json()["result"]["frames_inspected"][0]["frame_index"] == 2
    download = f"/api/video-mocap/{identifier}/files/motion.bvh"
    assert browser.get(download).content == (directory / "motion.bvh").read_bytes()
    assert browser.get(f"/api/video-mocap/{identifier}/files/request.json").status_code == 404
    (directory / "motion.bvh").write_bytes(b"changed")
    assert browser.get(download).status_code == 404
    assert browser.get(f"/api/video-mocap/{identifier}/preview").status_code == 400


def test_http_start_is_explicit_validated_and_passes_exact_request(client, monkeypatch):
    browser, headers = client
    calls = []
    monkeypatch.setattr(capture, "start_video_mocap", lambda settings, request: calls.append(request.model_dump()) or {"id": "d" * 32, "status": "queued"})
    body = {"request": {"video_path": "fixture.mp4", "fps": 10, "duration_seconds": 0.6, "detector": "yolo-hrnet"}}
    assert browser.post("/api/actions/start_video_mocap", json=body).status_code == 403
    invalid = {"request": {**body["request"], "duration_seconds": 0.5}}
    assert browser.post("/api/actions/start_video_mocap", json=invalid, headers=headers).status_code == 400
    assert not calls
    result = browser.post("/api/actions/start_video_mocap", json=body, headers=headers)
    assert result.status_code == 200 and result.json()["result"]["status"] == "queued"
    assert calls[0] == capture.VideoMocapRequest.model_validate(body["request"]).model_dump()


async def test_start_source_checksum_is_validated_and_forwarded(settings, monkeypatch):
    calls = []
    monkeypatch.setattr(capture, "start_video_mocap", lambda settings, request, **kwargs: calls.append(kwargs) or {"status": "queued"})
    server = create_server(settings)
    arguments = {"request": {"video_path": "fixture.mp4"}, "expected_source_sha256": "a" * 64}
    assert (await call(server, "start_video_mocap", arguments))["status"] == "queued"
    assert calls == [{"expected_source_sha256": "a" * 64}]
    with pytest.raises(Exception):
        await server.call_tool("start_video_mocap", {**arguments, "expected_source_sha256": "invalid"})
    assert len(calls) == 1
