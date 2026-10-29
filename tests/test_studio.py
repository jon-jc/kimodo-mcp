import hashlib
import json
import struct
import sys

import numpy as np
import pytest
from starlette.testclient import TestClient

from kimodo_mcp import studio as studio_module
from kimodo_mcp.config import Settings
from kimodo_mcp.jobs import JobStore, atomic_json
from kimodo_mcp.schemas import GenerateRequest
from kimodo_mcp.studio import create_app


@pytest.fixture
def studio(tmp_path, monkeypatch):
    monkeypatch.delenv("KIMODO_CHARACTER_FBX", raising=False)
    monkeypatch.setattr(
        studio_module,
        "capacity_snapshot",
        lambda: {
            "available_memory_gib": 8.0,
            "commit_headroom_gib": 4.0,
            "cpu_text_encoder_warning": "Insufficient estimated headroom",
        },
    )
    monkeypatch.setattr(
        studio_module,
        "editor_connections",
        lambda: {
            "blender": {"reachable": False},
            "unreal": {"reachable": False},
        },
    )
    settings = Settings(data_dir=tmp_path / "data", python=sys.executable)
    web = tmp_path / "web"
    web.mkdir()
    (web / "index.html").write_text("<html>Local studio</html>")
    (web / "app.js").write_text("console.log('local')")
    app = create_app(settings, web)
    with TestClient(app, base_url="http://127.0.0.1:8768", raise_server_exceptions=False) as client:
        response = client.get("/api/state")
        assert response.status_code == 200, response.text
        token = response.json()["csrf_token"]
        yield client, settings, {"x-studio-token": token}, web


def completed(settings):
    store = JobStore(settings)
    request = GenerateRequest(segments=[{"text": "Walk", "duration": 1.0}]).model_dump(mode="json")
    job = store.submit(request, launch=False)
    directory = store.directory(job["id"])
    content = b"bounded fixture data"
    (directory / "motion_00.npz").write_bytes(content)
    artifact = {"path": "motion_00.npz", "format": "npz", "sample_index": 0,
                "bytes": len(content), "sha256": hashlib.sha256(content).hexdigest()}
    atomic_json(directory / "manifest.json", {"artifacts": [artifact], "fps": 30, "frames": 30, "samples": 1})
    store.update(job["id"], "completed")
    return store, job, directory


def completed_preview(settings, monkeypatch, *, attempt=2):
    """Create a complete receipt fixture, without executing either editor."""
    from kimodo_mcp.handoff_scripts import character

    store, job, directory = completed(settings)
    motion = directory / "motion_00.npz"
    np.savez(motion, posed_joints=np.zeros((30, 77, 3)), fps=30, skeleton="somaskel77")
    checksum = hashlib.sha256(motion.read_bytes()).hexdigest()
    atomic_json(directory / "manifest.json", {"samples": 1, "fps": 30, "frames": 30, "artifacts": [
        {"path": motion.name, "sample_index": 0, "bytes": motion.stat().st_size, "sha256": checksum},
    ]})
    target = settings.data_dir / "fitted-target.fbx"
    target.write_bytes(b"fixture for the registered character checksum")
    character_checksum = hashlib.sha256(target.read_bytes()).hexdigest()
    monkeypatch.setattr(character, "SOURCE_SHA256", character_checksum)
    handoff = directory / "handoffs" / ("character-motion_00" + (f"-attempt{attempt:02d}" if attempt else ""))
    handoff.mkdir(parents=True)
    prepared = {"schema_version": 1, "kind": "character", "source_motion": str(motion),
                "source_sha256": checksum, "character_fbx": str(target), "character_sha256": character_checksum,
                "fps": 30, "frames": 30, "sample_index": 0}
    atomic_json(handoff / "handoff.json", prepared)
    bones = [{"name": name, "parent": values[2]} for name, values in character.landmarks().items()]
    model = {"nodes": [{"name": bone["name"]} for bone in bones],
             "skins": [{"joints": list(range(len(bones)))}], "meshes": [{}],
             "animations": [{"samplers": [{"input": 0}]}],
             "accessors": [{"type": "SCALAR", "componentType": 5126, "count": 30,
                            "min": [0], "max": [29 / 30]}], "buffers": [{"byteLength": 0}]}
    chunk = json.dumps(model).encode()
    chunk += b" " * (-len(chunk) % 4)
    glb = struct.pack("<4sIII4s", b"glTF", 2, len(chunk) + 20, len(chunk), b"JSON") + chunk
    report = {**prepared, "kind": "blender", "source_kind": "character", "status": "completed",
              "frame_range": [1, 30], "bone_count": 24, "bones": bones, "fbx_bones": bones,
              "root_motion": True, "validation": {"frames": 30, "finite": True, "unweighted_vertices": 0,
                "max_weight_sum_error": 0, "max_bone_length_error_m": 0}}
    for key, filename in (("preview_glb", "character-preview.glb"), ("character_fbx", "character.fbx"),
                          ("fbx", "animation.fbx"), ("blend", "character.blend")):
        path = handoff / filename
        path.write_bytes(glb if key == "preview_glb" else b"exported fixture bytes")
        report[key + "_path"] = str(path)
        report[key + "_sha256"] = hashlib.sha256(path.read_bytes()).hexdigest()
    atomic_json(handoff / "blender-report.json", report)
    return store, job, directory, handoff, prepared, report


def test_state_static_headers_and_real_plan_tool(studio):
    client, _, token, _ = studio
    state = client.get("/api/state").json()
    assert state["health"]["server"] == "ready" and state["jobs"] == []
    assert state["health"]["capacity"]["available_memory_gib"] == 8
    assert state["health"]["capacity"]["cpu_text_encoder_warning"]
    assert state["health"]["editors"]["blender"]["reachable"] is False
    assert state["character"]["preview_url"] is None
    response = client.get("/")
    assert response.status_code == 200 and "Local studio" in response.text
    assert response.headers["x-content-type-options"] == "nosniff"
    assert response.headers["cache-control"] == "no-store"
    assert "frame-ancestors 'none'" in response.headers["content-security-policy"]
    # Embedded GLB textures use blob fetches, while external connections stay excluded.
    assert "connect-src 'self' blob:;" in response.headers["content-security-policy"]
    response = client.post(
        "/api/actions/plan_animation", headers=token, json={"brief": {"description": "Wave"}}
    )
    assert response.status_code == 200, response.text
    assert response.json()["result"]["plan"]["total_frames"] == 120
    assert client.get("/api/state").json()["jobs"] == []


@pytest.mark.parametrize("headers", [{}, {"x-studio-token": "wrong"}])
def test_action_requires_current_process_token(studio, headers):
    client, _, _, _ = studio
    response = client.post(
        "/api/actions/plan_animation", headers=headers, json={"brief": {"description": "Wave"}}
    )
    assert response.status_code == 403


@pytest.mark.parametrize(
    "headers,status",
    [
        ({"host": "evil.example"}, 400),
        ({"host": "127.0.0.1.evil.example"}, 400),
        ({"origin": "https://evil.example"}, 403),
        ({"origin": "http://127.0.0.1:9999"}, 403),
        ({"origin": "https://127.0.0.1:8768"}, 403),
        ({"sec-fetch-site": "cross-site"}, 403),
        ({"origin": "null"}, 403),
        ({"origin": "http://["}, 403),
    ],
)
def test_local_origin_and_host_guards(studio, headers, status):
    client, _, _, _ = studio
    assert client.get("/api/state", headers=headers).status_code == status


def test_same_origin_accepted_and_cross_origin_rejected_even_with_token(studio):
    client, _, token, _ = studio
    args = {"brief": {"description": "Wave"}}
    good = {**token, "origin": "http://127.0.0.1:8768"}
    assert client.post("/api/actions/plan_animation", headers=good, json=args).status_code == 200
    bad = {**token, "origin": "http://external.example"}
    assert client.post("/api/actions/plan_animation", headers=bad, json=args).status_code == 403


@pytest.mark.parametrize(
    "body", [b"[1,2]", b"null", b"not-json", b'{"brief":{"description":"Wave","candidates":99}}']
)
def test_bad_action_json_or_tool_arguments_fail_without_jobs(studio, body):
    client, _, token, _ = studio
    response = client.post("/api/actions/plan_animation", headers=token, content=body)
    assert response.status_code == 400, response.text
    assert client.get("/api/state").json()["jobs"] == []


def test_body_limit_unknown_actions_and_disallowed_shell(studio):
    client, _, token, _ = studio
    assert (
        client.post(
            "/api/actions/plan_animation", headers=token, content=b" " * (1024 * 1024 + 1)
        ).status_code
        == 413
    )
    assert (
        client.post("/api/actions/execute_code", headers=token, json={"code": "anything"}).status_code == 404
    )
    assert client.get("/api/actions/generate_animation").status_code in (404, 405)


def test_jobs_artifact_allowlist_and_state_urls(studio):
    client, settings, _, _ = studio
    _, job, directory = completed(settings)
    (directory / "worker.log").write_text("private debug")
    response = client.get(f"/api/jobs/{job['id']}")
    assert response.status_code == 200 and response.json()["status"] == "completed"
    url = response.json()["downloads"][0]["url"]
    assert client.get(url).content == b"bounded fixture data"
    assert client.get(f"/api/files/{job['id']}/worker.log").status_code == 404
    assert client.get(f"/api/files/{job['id']}/request.json").status_code == 404
    assert client.get("/api/jobs/not-a-job").status_code == 404
    assert client.get("/api/jobs/" + "a" * 32).status_code == 404


def test_native_download_checks_current_file_checksum(studio):
    client, settings, _, _ = studio
    _, job, directory = completed(settings)
    path = directory / "motion_00.npz"
    path.write_bytes(b"X" * path.stat().st_size)
    assert client.get(f"/api/files/{job['id']}/motion_00.npz").status_code == 404


def test_static_and_download_traversal_rejected(studio):
    client, settings, _, web = studio
    (web.parent / "secret.txt").write_text("do not serve")
    _, job, _ = completed(settings)
    for path in ["/%2e%2e/secret.txt", "/%2e%2e%5csecret.txt", "/C:%5cWindows%5cwin.ini", "/missing.js"]:
        assert client.get(path).status_code == 404
    assert client.get(f"/api/files/{job['id']}/%2e%2e/secret.txt").status_code == 404


def test_existing_preview_reused_without_running_blender(studio, monkeypatch):
    client, settings, token, _ = studio
    _, job, _, handoff, _, _ = completed_preview(settings, monkeypatch)
    preview = handoff / "character-preview.glb"
    response = client.post("/api/actions/build_character_preview", headers=token, json={"job_id": job["id"]})
    assert response.status_code == 200, response.text
    assert response.json()["result"]["attempt"] == 2
    assert client.get(response.json()["result"]["url"]).content == preview.read_bytes()


@pytest.mark.parametrize(
    "arguments",
    [{"path": "anywhere"}, {"job_id": "x", "sample_index": True}, {"job_id": "x", "sample_index": 4}],
)
def test_preview_input_cannot_supply_code_or_paths(studio, arguments):
    client, _, token, _ = studio
    assert (
        client.post("/api/actions/build_character_preview", headers=token, json=arguments).status_code == 400
    )


def test_cancel_job_uses_same_durable_store(studio):
    client, settings, token, _ = studio
    store = JobStore(settings)
    job = store.submit(
        GenerateRequest(segments=[{"text": "Wave", "duration": 1}]).model_dump(mode="json"), launch=False
    )
    response = client.post("/api/actions/cancel_job", headers=token, json={"job_id": job["id"]})
    assert response.status_code == 200, response.text
    assert store.get(job["id"])["cancel_requested"]
    assert client.get(f"/api/jobs/{job['id']}").json()["status"] == "cancelling"


def test_unreal_target_roundtrip_and_validation(studio):
    client, settings, token, _ = studio
    target = {
        "project_file": str(settings.data_dir / "Example.uproject"),
        "skeleton_path": "/Game/Character/Skeleton",
        "skeletal_mesh_path": "/Game/Character/Mesh",
        "destination_path": "/Game/Animations",
        "asset_name": "Wave",
    }
    response = client.post("/api/actions/save_unreal_target", headers=token, json={"target": target})
    assert response.status_code == 200, response.text
    assert client.get("/api/state").json()["targets"]["unreal"]["asset_name"] == "Wave"
    readback = client.post("/api/actions/get_unreal_target", headers=token, json={})
    assert readback.status_code == 200
    assert readback.json()["result"]["target"] == response.json()["result"]["target"]
    target["destination_path"] = "/Game/../../External"
    assert (
        client.post("/api/actions/save_unreal_target", headers=token, json={"target": target}).status_code
        == 400
    )


@pytest.mark.parametrize("content", ["broken json", "[]", '{"preview_glb":42}', " " * 8193])
def test_bad_registration_degrades_to_unavailable_preview(studio, content):
    client, settings, _, _ = studio
    registration = settings.data_dir / "character" / "target.json"
    registration.parent.mkdir()
    registration.write_text(content)
    response = client.get("/api/state")
    assert response.status_code == 200, response.text
    assert response.json()["character"]["configured"] is False
    assert response.json()["character"]["preview_url"] is None
    assert client.get("/api/character/model.glb").status_code == 404


def test_initial_character_glb_is_explicit_local_file(studio):
    client, settings, _, _ = studio
    path = settings.data_dir / "character" / "final" / "anime-girl-wave-preview.glb"
    path.parent.mkdir(parents=True)
    path.write_bytes(b"glTF fixture")
    response = client.get("/api/character/model.glb")
    assert response.status_code == 200
    assert response.content == b"glTF fixture"
    assert response.headers["content-type"] == "model/gltf-binary"


def test_download_requires_completed_job_and_manifest_membership(studio):
    client, settings, _, _ = studio
    store = JobStore(settings)
    job = store.submit(
        GenerateRequest(segments=[{"text": "Wave", "duration": 1}]).model_dump(mode="json"), launch=False
    )
    path = store.directory(job["id"]) / "motion_00.npz"
    path.write_bytes(b"incomplete output")
    url = f"/api/files/{job['id']}/motion_00.npz"
    assert client.get(url).status_code == 404
    store.update(job["id"], "completed")
    assert client.get(url).status_code == 404


@pytest.mark.parametrize("marker", ["bridge-dispatch.json", "execution.lock"])
def test_uncertain_blender_dispatch_is_not_automatically_retried(studio, marker):
    client, settings, token, _ = studio
    _, job, directory = completed(settings)
    pending = directory / "handoffs" / "character-motion_00"
    pending.mkdir(parents=True)
    atomic_json(pending / marker, {"status": "dispatched"})
    response = client.post("/api/actions/build_character_preview", headers=token, json={"job_id": job["id"]})
    assert response.status_code == 400
    assert "may still be running" in response.json()["error"]
    assert sorted(p.name for p in pending.parent.iterdir()) == ["character-motion_00"]


def test_binary_token_header_is_rejected_without_server_error(studio):
    client, _, _, _ = studio
    response = client.post("/api/actions/plan_animation", headers=[(b"x-studio-token", b"\xff")], json={})
    assert response.status_code == 403


@pytest.mark.parametrize("corruption", ["other_job", "other_sample", "source_checksum", "timing", "legacy"])
def test_preview_receipts_bound_to_job_sample_and_outputs(studio, monkeypatch, corruption):
    client, settings, _, _ = studio
    _, job, directory, handoff, prepared, report = completed_preview(settings, monkeypatch)
    if corruption == "other_job":
        _, other, other_directory = completed(settings)
        foreign = other_directory / "handoffs" / "character-motion_00" / "character-preview.glb"
        foreign.parent.mkdir(parents=True)
        foreign.write_bytes((handoff / "character-preview.glb").read_bytes())
        report["preview_glb_path"] = str(foreign)
    elif corruption == "other_sample":
        report["sample_index"] = 1
    elif corruption == "source_checksum":
        prepared["source_sha256"] = report["source_sha256"] = "a" * 64
        atomic_json(handoff / "handoff.json", prepared)
    elif corruption == "timing":
        prepared["frames"] = report["frames"] = 31
        atomic_json(handoff / "handoff.json", prepared)
    else:
        report = {"status": "completed", "sample_index": 0, "preview_glb_path": report["preview_glb_path"]}
    atomic_json(handoff / "blender-report.json", report)
    response = client.get(f"/api/jobs/{job['id']}")
    assert response.status_code == 200
    assert response.json()["previews"] == []
    relative = (handoff / "character-preview.glb").relative_to(directory).as_posix()
    assert client.get(f"/api/files/{job['id']}/{relative}").status_code == 404


def test_verified_preview_cache_rechecks_changed_output(studio, monkeypatch):
    from kimodo_mcp import blender_bridge

    client, settings, _, _ = studio
    _, job, _, handoff, _, _ = completed_preview(settings, monkeypatch)
    calls = []
    original = blender_bridge._validate_report

    def validate(*args):
        calls.append(True)
        return original(*args)

    monkeypatch.setattr(blender_bridge, "_validate_report", validate)
    endpoint = f"/api/jobs/{job['id']}"
    first = client.get(endpoint).json()["previews"]
    assert len(first) == 1
    for _ in range(3):
        assert client.get(endpoint).json()["previews"] == first
    assert len(calls) == 1
    (handoff / "animation.fbx").write_bytes(b"altered export")
    assert client.get(endpoint).json()["previews"] == []
    assert len(calls) == 2
    assert client.get(first[0]["url"]).status_code == 404


def test_corrupt_motion_invalidates_verified_preview(studio, monkeypatch):
    client, settings, _, _ = studio
    _, job, directory, _, _, _ = completed_preview(settings, monkeypatch)
    endpoint = f"/api/jobs/{job['id']}"
    assert len(client.get(endpoint).json()["previews"]) == 1
    (directory / "motion_00.npz").write_bytes(b"changed source motion")
    assert client.get(endpoint).json()["previews"] == []
