"""Capture delivery boundary tests; no Blender or Unreal process is started."""

import hashlib
import json
import sys

import pytest
from starlette.testclient import TestClient

from kimodo_mcp import capture_retarget_api as api
from kimodo_mcp.config import Settings
from kimodo_mcp.handoff import UnrealTarget
from kimodo_mcp.server import create_server
from kimodo_mcp.studio import create_app


@pytest.fixture
def completed(tmp_path, monkeypatch):
    settings = Settings(tmp_path / "data", sys.executable)
    identifier = "a" * 64
    directory = settings.data_dir / "freemocap" / "retargets" / identifier
    directory.mkdir(parents=True)
    request = {"job_id": "b" * 32}
    (directory / "handoff.json").write_text(json.dumps(request))
    report = {"status": "completed", "fps": 60, "frames": 120,
              "fbx_bones": [{"name": "Root", "parent": None}]}
    for key, filename in (("fbx", "animation.fbx"), ("character_fbx", "character.fbx"),
                          ("blend", "character.blend"), ("preview_glb", "character-preview.glb"),
                          ("cleanup", "cleanup-report.json"), ("cleaned_motion", "cleaned-motion.npz")):
        path = directory / filename
        path.write_bytes(b"Synthetic boundary fixture: " + filename.encode())
        report[key + "_path"] = str(path)
        report[key + "_sha256"] = api._digest(path)
    (directory / "blender-report.json").write_text(json.dumps(report))
    monkeypatch.setattr(api, "verify_capture_preparation", lambda s, i: (directory, request, report["fbx_bones"]))
    monkeypatch.setattr(api, "get_capture_character_report", lambda s, i: dict(report))
    target = UnrealTarget(project_file=str(tmp_path / "Test.uproject"),
                          skeleton_path="/Game/Character/Skeleton", skeletal_mesh_path="/Game/Character/Mesh",
                          destination_path="/Game/Animations", asset_name="Capture_Test")
    return settings, identifier, directory, report, target


def test_all_six_artifacts_are_bound_to_completion_and_hash(completed):
    settings, identifier, directory, report, _ = completed
    for filename in ("animation.fbx", "character.fbx", "character.blend", "character-preview.glb",
                     "cleanup-report.json", "cleaned-motion.npz"):
        artifact = api.character_artifact(settings, identifier, filename)
        assert artifact["sha256"] == api._digest(directory / filename)
    (directory / "animation.fbx").write_bytes(b"changed")
    with pytest.raises(ValueError, match="differs"):
        api.character_artifact(settings, identifier, "animation.fbx")
    report["status"] = "prepared"
    with pytest.raises(ValueError, match="completed"):
        api.character_artifact(settings, identifier, "character.blend")


@pytest.mark.parametrize("path", ["../handoff.json", "handoff.json", "failure.json", "C:/private.txt"])
def test_download_rejects_unlisted_paths(completed, path):
    settings, identifier, _, _, _ = completed
    with pytest.raises(ValueError, match="listed"):
        api.character_artifact(settings, identifier, path)


def test_unreal_keeps_capture_rate_and_rejects_tampered_target(completed):
    settings, identifier, _, _, target = completed
    prepared = api.prepare_unreal(settings, identifier, target)
    assert prepared["fps"] == 60 and prepared["frames"] == 120
    directory, request, _ = api._unreal_preparation(settings, identifier, target)
    assert request["fps"] == 60 and request["target"] == target.model_dump(mode="json")
    assert api.unreal_report(settings, identifier, target)["status"] == "not_executed"
    request["target"]["asset_name"] = "Different"
    (directory / "handoff.json").write_text(json.dumps(request))
    with pytest.raises(ValueError, match="differs"):
        api._unreal_preparation(settings, identifier, target)


@pytest.mark.parametrize("fps", [True, 29.97, 121, float("nan")])
def test_unreal_rejects_unverified_timing(completed, fps):
    settings, identifier, _, report, target = completed
    report["fps"] = fps
    with pytest.raises(ValueError, match="timing"):
        api.prepare_unreal(settings, identifier, target)


def test_uncertain_unreal_dispatch_cannot_be_retried(completed, monkeypatch):
    from kimodo_mcp import unreal_bridge
    settings, identifier, directory, _, target = completed
    monkeypatch.setenv("KIMODO_UNREAL_ENGINE", str(directory / "Engine"))
    api.prepare_unreal(settings, identifier, target)
    calls = []

    def disconnected(*args):
        calls.append(args)
        raise ConnectionError("Editor disconnected after dispatch")

    monkeypatch.setattr(unreal_bridge, "execute_handoff", disconnected)
    with pytest.raises(ConnectionError):
        api.execute_unreal(settings, identifier, target)
    assert api.unreal_report(settings, identifier, target)["status"] == "not_completed"
    with pytest.raises(RuntimeError, match="already dispatched"):
        api.execute_unreal(settings, identifier, target)
    assert len(calls) == 1


def test_history_reports_unverified_metadata_and_filters_source(completed):
    settings, identifier, _, _, _ = completed
    history = api.list_handoffs(settings, "b" * 32)
    assert history["metadata_only"] is True
    assert history["handoffs"][0]["handoff_id"] == identifier
    assert history["handoffs"][0]["verification"] == "not_checked"
    assert api.list_handoffs(settings, "c" * 32)["handoffs"] == []
    with pytest.raises(ValueError):
        api.list_handoffs(settings, "../bad")


async def test_mcp_and_studio_use_same_verified_artifact(completed, tmp_path):
    settings, identifier, directory, _, _ = completed
    result = await create_server(settings).call_tool("get_freemocap_character_artifact",
                                                     {"handoff_id": identifier, "path": "cleanup-report.json"})
    assert result[1]["sha256"] == api._digest(directory / "cleanup-report.json")
    web = tmp_path / "web"
    web.mkdir()
    (web / "index.html").write_text("Studio fixture")
    with TestClient(create_app(settings, web), base_url="http://127.0.0.1:8768") as client:
        response = client.get(f"/api/capture-character/{identifier}/files/character-preview.glb")
        assert response.status_code == 200
        assert response.headers["content-type"] == "model/gltf-binary"
        assert response.headers["cache-control"] == "no-store"
        assert hashlib.sha256(response.content).hexdigest() == api._digest(directory / "character-preview.glb")
        assert client.get(f"/api/capture-character/{identifier}/files/handoff.json").status_code == 400
