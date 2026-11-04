import json
import shutil
from pathlib import Path

import httpx
import pytest

from kimodo_mcp.config import Settings
from kimodo_mcp.freemocap import (
    PREFIX,
    ROUTES,
    UPSTREAM_REVISION,
    UPSTREAM_VERSION,
    FreeMoCapClient,
    FreeMocapRequest,
    cancel_freemocap_job,
    get_freemocap_capabilities,
    get_freemocap_job,
    inspect_freemocap_recording,
    list_freemocap_recordings,
    plan_freemocap_capture,
    start_freemocap_capture,
)

REAL_REQUEST = FreeMoCapClient._request


@pytest.fixture
def runtime(tmp_path, monkeypatch):
    settings = Settings(data_dir=tmp_path, python="unused")
    directory = tmp_path / "freemocap"
    directory.mkdir()
    references = tmp_path / "references"
    recordings = tmp_path / "recordings"
    references.mkdir()
    recordings.mkdir()
    config = {
        "base_url": "http://127.0.0.1:53117",
        "recordings_root": str(recordings),
        "references_root": str(references),
    }
    (directory / "runtime.json").write_text(json.dumps(config))
    for name in ["cam0.mp4", "cam1.mp4"]:
        (references / name).write_bytes((name * 20).encode())
    calls = []

    def fake(self, method, route, payload=None, *, query=None, timeout=20):
        calls.append((method, route, payload))
        if route == "/health":
            return {"alive": True, "pid": 1}
        if route == "/kimodo-status/v1":
            return {"schema_version": 1, "reviewed_revision": UPSTREAM_REVISION,
                    "instance_id": "f" * 32, "terminal_publication_observer": True}
        if route.startswith("/kimodo-status/v1/pipelines/"):
            operation = next(call for call in reversed(calls) if call[0] == "POST" and call[1].endswith(("/process", "/calibrate")))
            config = operation[2]
            return {"instance_id": "f" * 32, "pipeline_id": route.rsplit("/", 1)[1],
                    "recording_path": config.get("mocapRecordingDirectory", config.get("calibrationRecordingDirectory")),
                    "pipeline_type": "mocap" if operation[1].endswith("/process") else "calibration",
                    "phase": "collecting_camera_output", "workers_alive": True, "terminal_verified": False}
        if route == "/openapi.json":
            return {
                "info": {"version": UPSTREAM_VERSION},
                "paths": {PREFIX + path: {"post": {}} for path in ROUTES.values()},
            }
        if route.endswith("/check_sync"):
            return {
                "synchronized": True,
                "videos": [
                    {
                        "filename": Path(path).name,
                        "camera_id": f"cam{index}",
                        "frame_count": 120,
                        "fps": 30.0,
                        "duration_seconds": 4.0,
                    }
                    for index, path in enumerate(payload["videoPaths"])
                ],
            }
        if route.endswith("/import"):
            folder = recordings / payload["recordingName"]
            (folder / "synchronized_videos").mkdir(parents=True)
            for item in payload["videoPaths"]:
                shutil.copyfile(item, folder / "synchronized_videos" / Path(item).name)
            return {"success": True, "recording_path": str(folder), "recording_name": folder.name}
        if route.endswith("/synchronize"):
            return {"job_id": "sync-123"}
        if "/synchronize/" in route:
            return {"pending": True}
        if route.endswith("/process") or route.endswith("/calibrate"):
            return {"success": True, "pipeline_id": "pipeline-123"}
        if method == "DELETE":
            return {"success": True, "pipeline_id": route.rsplit("/", 1)[1]}
        if route.endswith("/status"):
            return {"blender_export_ready": False, "stages": []}
        if route.endswith("/recordings"):
            return [{"name": path.name, "path": str(path)} for path in recordings.iterdir() if path.is_dir()]
        raise AssertionError((method, route, payload))

    monkeypatch.setattr(FreeMoCapClient, "_request", fake)
    return settings, references, recordings, calls, fake


def imported(runtime, count=1):
    settings = runtime[0]
    request = FreeMocapRequest(
        operation="import", video_files=[f"cam{i}.mp4" for i in range(count)], synchronization_confirmed=True
    )
    return start_freemocap_capture(settings, request)


@pytest.mark.parametrize(
    "url",
    [
        "https://127.0.0.1",
        "http://example.com",
        "http://127.0.0.1/path",
        "http://a:b@127.0.0.1",
        "http://127.0.0.1?x=1",
        "http://127.0.0.1#x",
    ],
)
def test_operator_endpoint_must_be_plain_loopback(runtime, url):
    settings = runtime[0]
    path = settings.data_dir / "freemocap/runtime.json"
    value = json.loads(path.read_text())
    value["base_url"] = url
    path.write_text(json.dumps(value))
    with pytest.raises(ValueError, match="loopback"):
        FreeMoCapClient(settings)


def test_capability_probe_never_starts_runtime_or_processing(runtime):
    settings, _, _, calls, _ = runtime
    result = get_freemocap_capabilities(settings)
    assert result["compatible"] and result["license"] == "AGPL-3.0"
    assert [item[:2] for item in calls] == [("GET", "/health"), ("GET", "/openapi.json"), ("GET", "/kimodo-status/v1")]


def test_unconfigured_capabilities_are_actionable_without_creating_files(tmp_path):
    result = get_freemocap_capabilities(Settings(data_dir=tmp_path, python="unused"))
    assert not result["configured"] and not result["compatible"]
    assert "Configure the separate FreeMoCap service" in result["error"]
    assert str((tmp_path / "freemocap/runtime.json").resolve()) in result["error"]
    assert not (tmp_path / "freemocap").exists()


@pytest.mark.parametrize(
    "reference",
    ["../cam0.mp4", "C:/private.mp4", "/tmp/private.mp4", "sub\\cam0.mp4", "file:///tmp/video.mp4"],
)
def test_reference_paths_cannot_escape_operator_root(runtime, reference):
    with pytest.raises(ValueError):
        plan_freemocap_capture(runtime[0], FreeMocapRequest(operation="check_sync", video_files=[reference]))


def test_sync_rejects_duplicate_basenames_before_upstream_copy(runtime):
    settings, references, _, calls, _ = runtime
    (references / "nested").mkdir()
    (references / "nested/cam0.mp4").write_bytes(b"different")
    with pytest.raises(ValueError, match="basenames"):
        plan_freemocap_capture(
            settings, FreeMocapRequest(operation="synchronize", video_files=["cam0.mp4", "nested/cam0.mp4"])
        )
    assert not calls


@pytest.mark.parametrize(
    "args",
    [
        {"operation": "process", "recording_id": "valid", "allow_planar": "true"},
        {"operation": "export", "recording_id": "../bad"},
        {"operation": "check_sync", "video_files": ["cam0.mp4"], "export_options": {}},
        {"operation": "calibrate", "recording_id": "valid"},
        {"operation": "export", "recording_id": "valid", "export_options": {"formats": ["gltf"]}},
    ],
)
def test_requests_reject_ambiguous_and_nonapplicable_controls(args):
    with pytest.raises(ValueError):
        FreeMocapRequest(**args)


def test_plan_only_reads_metadata_and_uses_actual_sync_enum(runtime):
    settings, _, _, calls, _ = runtime
    request = FreeMocapRequest(
        operation="synchronize", video_files=["cam0.mp4", "cam1.mp4"], sync_method="brightness"
    )
    plan = plan_freemocap_capture(settings, request)
    assert plan["payload"]["method"] == "brightness change detection"
    assert all(route.endswith("check_sync") for _, route, _ in calls)
    assert not (settings.data_dir / "freemocap/jobs").exists()


def test_equal_frame_counts_never_claim_temporal_sync(runtime):
    settings = runtime[0]
    request = FreeMocapRequest(operation="import", video_files=["cam0.mp4", "cam1.mp4"])
    with pytest.raises(ValueError, match="Confirm temporal"):
        plan_freemocap_capture(settings, request)
    plan = plan_freemocap_capture(settings, request.model_copy(update={"synchronization_confirmed": True}))
    assert plan["video_metadata"]["synchronization_verified"] is False


def test_import_creates_new_owned_recording_and_never_modifies_reference(runtime):
    settings, references, recordings, _, _ = runtime
    source = (references / "cam0.mp4").read_bytes()
    first = imported(runtime)
    second = imported(runtime)
    assert first["recording_id"] != second["recording_id"]
    assert first["status"] == "completed"
    assert (references / "cam0.mp4").read_bytes() == source
    assert (recordings / first["recording_id"] / ".kimodo-freemocap.json").is_file()
    assert list_freemocap_recordings(settings)["total"] == 2


def test_single_camera_requires_explicit_planar_and_forces_no_blender(runtime):
    settings = runtime[0]
    receipt = imported(runtime)
    with pytest.raises(ValueError, match="planar"):
        plan_freemocap_capture(
            settings, FreeMocapRequest(operation="process", recording_id=receipt["recording_id"])
        )
    request = FreeMocapRequest(operation="process", recording_id=receipt["recording_id"], allow_planar=True)
    plan = plan_freemocap_capture(settings, request)
    config = plan["payload"]["mocapTaskConfig"]
    assert config["exportToBlender"] is False and config["autoOpenBlendFile"] is False
    assert config["filterConfig"]["sampling_rate"] == 30
    process = start_freemocap_capture(settings, request)
    observed = get_freemocap_job(settings, process["job_id"])
    assert observed["status"] == "submitted" and observed["pipeline_completion_verified"] is False


def test_multicamera_requires_calibration_and_camera_id_match(runtime):
    settings, references, _, _, _ = runtime
    receipt = imported(runtime, 2)
    request = FreeMocapRequest(operation="process", recording_id=receipt["recording_id"])
    with pytest.raises(ValueError, match="explicit calibration"):
        plan_freemocap_capture(settings, request)
    path = references / "calibration.toml"
    path.write_text('[cam0]\nname="other"\n')
    request = FreeMocapRequest(
        operation="process", recording_id=receipt["recording_id"], calibration_file=path.name
    )
    with pytest.raises(ValueError, match="camera IDs"):
        plan_freemocap_capture(settings, request)
    path.write_text('[cam0]\nname="cam0"\n[cam1]\nname="cam1"\n')
    plan = plan_freemocap_capture(settings, request)
    assert plan["payload"]["mocapTaskConfig"]["calibrationTomlPath"] == str(path)


def test_changed_owned_reference_and_existing_outputs_refuse_processing(runtime):
    settings, _, recordings, _, _ = runtime
    receipt = imported(runtime)
    folder = recordings / receipt["recording_id"]
    request = FreeMocapRequest(operation="process", recording_id=receipt["recording_id"], allow_planar=True)
    video = folder / "synchronized_videos/cam0.mp4"
    original = video.read_bytes()
    video.write_bytes(b"changed")
    with pytest.raises(ValueError, match="changed"):
        plan_freemocap_capture(settings, request)
    video.write_bytes(original)
    (folder / "output_data").mkdir()
    with pytest.raises(ValueError, match="already has outputs"):
        plan_freemocap_capture(settings, request)


def test_filter_cutoff_cannot_exceed_actual_nyquist(runtime):
    receipt = imported(runtime)
    request = FreeMocapRequest(
        operation="process",
        recording_id=receipt["recording_id"],
        allow_planar=True,
        process_options={"cutoff_hz": 15},
    )
    with pytest.raises(ValueError, match="half"):
        plan_freemocap_capture(runtime[0], request)


def test_cancellation_is_bound_to_exact_owned_pipeline(runtime):
    settings, _, _, calls, _ = runtime
    receipt = imported(runtime)
    request = FreeMocapRequest(operation="process", recording_id=receipt["recording_id"], allow_planar=True)
    process = start_freemocap_capture(settings, request)
    cancelled = cancel_freemocap_job(settings, process["job_id"])
    assert cancelled["status"] == "cancelled"
    assert calls[-1][:2] == ("DELETE", "/freemocap/posthoc/pipeline/pipeline-123")
    with pytest.raises(ValueError, match="Only"):
        cancel_freemocap_job(settings, receipt["job_id"])


def test_timeout_is_durable_unknown_and_duplicate_process_is_not_retried(runtime, monkeypatch):
    settings, _, _, calls, fake = runtime
    receipt = imported(runtime)
    request = FreeMocapRequest(operation="process", recording_id=receipt["recording_id"], allow_planar=True)

    def timeout(self, method, route, payload=None, **kwargs):
        if route.endswith("/process"):
            raise TimeoutError("connection ended after submission")
        return fake(self, method, route, payload, **kwargs)

    monkeypatch.setattr(FreeMoCapClient, "_request", timeout)
    with pytest.raises(RuntimeError, match="unverified"):
        start_freemocap_capture(settings, request)
    records = [json.loads(path.read_text()) for path in (settings.data_dir / "freemocap/jobs").glob("*.json")]
    assert any(item["status"] == "submission_unknown" for item in records)
    with pytest.raises(ValueError, match="no verified terminal"):
        start_freemocap_capture(settings, request)


def test_sync_pending_is_observed_without_inventing_completion(runtime):
    settings = runtime[0]
    job = start_freemocap_capture(
        settings, FreeMocapRequest(operation="synchronize", video_files=["cam0.mp4", "cam1.mp4"])
    )
    result = get_freemocap_job(settings, job["job_id"])
    assert result["status"] == "submitted" and result["synchronization"]["pending"]
    assert not result["pipeline_completion_verified"]


def test_foreign_recordings_are_readable_but_never_mutated(runtime):
    settings, _, recordings, _, _ = runtime
    (recordings / "external").mkdir()
    inspect_freemocap_recording(settings, "external")
    with pytest.raises(ValueError, match="JSON"):
        plan_freemocap_capture(
            settings, FreeMocapRequest(operation="process", recording_id="external", allow_planar=True)
        )


def test_export_rejects_rtmpose_and_existing_blender_outputs(runtime):
    settings, _, recordings, _, _ = runtime
    receipt = imported(runtime)
    folder = recordings / receipt["recording_id"]
    output = folder / "output_data"
    output.mkdir()
    (output / "rtmpose_body_3d_xyz.npy").write_bytes(b"data")
    request = FreeMocapRequest(operation="export", recording_id=receipt["recording_id"])
    with pytest.raises(ValueError, match="RTMPose"):
        plan_freemocap_capture(settings, request)
    (folder / f"{folder.name}.blend").write_bytes(b"existing")
    with pytest.raises(ValueError, match="overwritten"):
        plan_freemocap_capture(settings, request)


def test_http_transport_refuses_redirects_and_ignores_environment_proxies(runtime, monkeypatch):
    settings = runtime[0]
    monkeypatch.setattr(FreeMoCapClient, "_request", REAL_REQUEST)
    original_client = httpx.Client
    seen = []

    def factory(**kwargs):
        assert kwargs["trust_env"] is False and kwargs["follow_redirects"] is False

        def respond(request):
            seen.append(str(request.url))
            return httpx.Response(302, headers={"Location": "https://example.com/private"})

        return original_client(transport=httpx.MockTransport(respond), **kwargs)

    monkeypatch.setattr(httpx, "Client", factory)
    with pytest.raises(RuntimeError, match="302"):
        FreeMoCapClient(settings)._request("GET", "/health")
    assert seen == ["http://127.0.0.1:53117/health"]


def test_http_transport_bounds_response_bytes(runtime, monkeypatch):
    monkeypatch.setattr(FreeMoCapClient, "_request", REAL_REQUEST)
    original_client = httpx.Client
    monkeypatch.setattr(
        httpx,
        "Client",
        lambda **kwargs: original_client(
            transport=httpx.MockTransport(lambda _: httpx.Response(200, content=b"x" * (8 * 1024**2 + 1))),
            **kwargs,
        ),
    )
    with pytest.raises(ValueError, match="oversized"):
        FreeMoCapClient(runtime[0])._request("GET", "/health")


def test_sync_response_requires_exact_source_set_and_consistent_counts(runtime, monkeypatch):
    settings, _, _, _, fake = runtime
    job = start_freemocap_capture(
        settings, FreeMocapRequest(operation="synchronize", video_files=["cam0.mp4", "cam1.mp4"])
    )
    result = {
        "videos_before": [{"video_name": "cam0"}, {"video_name": "cam1"}],
        "videos_after": [
            {"video_name": "synced_cam0", "frame_count": 100},
            {"video_name": "synced_cam1", "frame_count": 99},
        ],
        "synchronized_frame_count": 100,
    }

    def response(self, method, route, payload=None, **kwargs):
        return result if "/synchronize/" in route else fake(self, method, route, payload, **kwargs)

    monkeypatch.setattr(FreeMoCapClient, "_request", response)
    with pytest.raises(ValueError, match="Synchronization result"):
        get_freemocap_job(settings, job["job_id"])
    result["videos_after"][1]["frame_count"] = 100
    assert get_freemocap_job(settings, job["job_id"])["status"] == "completed"


def test_export_completion_requires_all_requested_formats(runtime, monkeypatch):
    settings, _, recordings, _, fake = runtime
    receipt = imported(runtime)
    process = start_freemocap_capture(settings, FreeMocapRequest(operation="process", recording_id=receipt["recording_id"], allow_planar=True))
    folder = recordings / receipt["recording_id"]
    output = folder / "output_data"
    output.mkdir()
    for suffix in [
        "body_3d_xyz",
        "right_hand_3d_xyz",
        "left_hand_3d_xyz",
        "face_3d_xyz",
        "body_total_body_center_of_mass",
        "body_segment_center_of_mass",
    ]:
        (output / f"mediapipe_{suffix}.npy").write_bytes(b"fixture")

    def export(self, method, route, payload=None, **kwargs):
        if route.startswith("/kimodo-status/v1/pipelines/"):
            return {**fake(self, method, route, payload, **kwargs), "phase": "complete",
                    "terminal_verified": True, "workers_alive": False}
        if route.endswith("/export"):
            assert payload["autoOpenBlendFile"] is False
            blend = folder / f"{folder.name}.blend"
            blend.write_bytes(b"fixture blend")
            return {"success": True, "blender_file_path": str(blend)}
        return fake(self, method, route, payload, **kwargs)

    monkeypatch.setattr(FreeMoCapClient, "_request", export)
    assert get_freemocap_job(settings, process["job_id"])["status"] == "completed"
    with pytest.raises(RuntimeError, match="FBX/BVH"):
        start_freemocap_capture(
            settings, FreeMocapRequest(operation="export", recording_id=receipt["recording_id"])
        )


def test_mutation_rejects_other_runtime_version(runtime, monkeypatch):
    settings, _, _, calls, fake = runtime

    def changed(self, method, route, payload=None, **kwargs):
        value = fake(self, method, route, payload, **kwargs)
        if route == "/openapi.json":
            value["info"]["version"] = "9.0.0"
        return value

    monkeypatch.setattr(FreeMoCapClient, "_request", changed)
    with pytest.raises(ValueError, match="contract"):
        imported(runtime)
    assert not any(method == "POST" for method, _, _ in calls)


def test_runtime_change_cannot_cancel_pipeline_on_different_server(runtime):
    settings = runtime[0]
    receipt = imported(runtime)
    process = start_freemocap_capture(
        settings,
        FreeMocapRequest(operation="process", recording_id=receipt["recording_id"], allow_planar=True),
    )
    config_file = settings.data_dir / "freemocap/runtime.json"
    config = json.loads(config_file.read_text())
    config["base_url"] = "http://127.0.0.1:53118"
    config_file.write_text(json.dumps(config))
    with pytest.raises(ValueError, match="different configured runtime"):
        cancel_freemocap_job(settings, process["job_id"])


def test_terminal_observation_unlocks_export_only_after_workers_stop(runtime, monkeypatch):
    settings, _, recordings, calls, fake = runtime
    imported_job = imported(runtime)
    recording_id = imported_job["recording_id"]
    job = start_freemocap_capture(settings, FreeMocapRequest(operation="process", recording_id=recording_id, allow_planar=True))
    state = {"phase": "complete", "workers_alive": True, "terminal_verified": False}

    def observed(self, method, route, payload=None, **kwargs):
        value = fake(self, method, route, payload, **kwargs)
        if route.startswith("/kimodo-status/v1/pipelines/"):
            value.update(state)
        return value

    monkeypatch.setattr(FreeMoCapClient, "_request", observed)
    assert get_freemocap_job(settings, job["job_id"])["status"] == "submitted"
    output = recordings / recording_id / "output_data"
    output.mkdir()
    (output / "fixture.npy").write_bytes(b"synthetic completed pipeline output")
    state.update(workers_alive=False, terminal_verified=True)
    complete = get_freemocap_job(settings, job["job_id"])
    assert complete["status"] == "completed" and complete["pipeline_completion_verified"]
    assert complete["outputs"][0]["path"] == "output_data/fixture.npy"
    (output / "fixture.npy").write_bytes(b"modified")
    with pytest.raises(ValueError, match="output changed"):
        get_freemocap_job(settings, job["job_id"])


def test_cross_stage_calibration_refuses_live_processing_writer(runtime):
    settings, _, _, calls, _ = runtime
    imported_job = imported(runtime, 2)
    recording_id = imported_job["recording_id"]
    board = {"squares_x": 5, "squares_y": 7, "square_length_mm": 40}
    start_freemocap_capture(settings, FreeMocapRequest(operation="calibrate", recording_id=recording_id, board=board))
    calibration = runtime[1] / "manual.toml"
    calibration.write_text('[cam0]\nid="cam0"\n[cam1]\nid="cam1"\n')
    with pytest.raises(ValueError, match="no verified terminal"):
        start_freemocap_capture(settings, FreeMocapRequest(operation="process", recording_id=recording_id, calibration_file="manual.toml"))
    assert not any(route.endswith("/process") for _, route, _ in calls)


def test_terminal_failure_and_restarted_instance_cannot_be_reported_success(runtime, monkeypatch):
    settings, _, _, _, fake = runtime
    recording = imported(runtime)["recording_id"]
    job = start_freemocap_capture(settings, FreeMocapRequest(operation="process", recording_id=recording, allow_planar=True))

    def restarted(self, method, route, payload=None, **kwargs):
        value = fake(self, method, route, payload, **kwargs)
        if route == "/kimodo-status/v1":
            value["instance_id"] = "a" * 32
        return value

    monkeypatch.setattr(FreeMoCapClient, "_request", restarted)
    with pytest.raises(ValueError, match="restarted"):
        get_freemocap_job(settings, job["job_id"])
    with pytest.raises(ValueError, match="previous"):
        cancel_freemocap_job(settings, job["job_id"])

    def failed(self, method, route, payload=None, **kwargs):
        value = fake(self, method, route, payload, **kwargs)
        if route.startswith("/kimodo-status/v1/pipelines/"):
            value.update(phase="failed", workers_alive=False, terminal_verified=True, detail="synthetic solver failure")
        return value

    monkeypatch.setattr(FreeMoCapClient, "_request", failed)
    result = get_freemocap_job(settings, job["job_id"])
    assert result["status"] == "failed" and "solver" in result["error"]


@pytest.mark.parametrize("changed", ["modified", "deleted"])
def test_completed_export_revalidates_original_receipt_outputs(runtime, monkeypatch, changed):
    settings, _, recordings, _, fake = runtime
    recording_id = imported(runtime)["recording_id"]
    process = start_freemocap_capture(settings, FreeMocapRequest(
        operation="process", recording_id=recording_id, allow_planar=True))
    folder = recordings / recording_id
    output = folder / "output_data"
    output.mkdir()
    for suffix in ["body_3d_xyz", "right_hand_3d_xyz", "left_hand_3d_xyz", "face_3d_xyz",
                   "body_total_body_center_of_mass", "body_segment_center_of_mass"]:
        (output / f"mediapipe_{suffix}.npy").write_bytes(b"synthetic input")

    def completed(self, method, route, payload=None, **kwargs):
        if route.startswith("/kimodo-status/v1/pipelines/"):
            return {**fake(self, method, route, payload, **kwargs), "phase": "complete",
                    "terminal_verified": True, "workers_alive": False}
        if route.endswith("/export"):
            blend = folder / f"{folder.name}.blend"
            blend.write_bytes(b"synthetic blend")
            return {"success": True, "blender_file_path": str(blend)}
        return fake(self, method, route, payload, **kwargs)

    monkeypatch.setattr(FreeMoCapClient, "_request", completed)
    assert get_freemocap_job(settings, process["job_id"])["status"] == "completed"
    exported = start_freemocap_capture(settings, FreeMocapRequest(
        operation="export", recording_id=recording_id, export_options={"formats": []}))
    assert get_freemocap_job(settings, exported["job_id"])["status"] == "completed"
    blend = folder / f"{folder.name}.blend"
    if changed == "modified":
        blend.write_bytes(b"changed blend")
    else:
        blend.unlink()
    with pytest.raises((ValueError, FileNotFoundError)):
        get_freemocap_job(settings, exported["job_id"])
    stored = json.loads((settings.data_dir / "freemocap/jobs" / f"{exported['job_id']}.json").read_text())
    assert stored["outputs"] == exported["outputs"]


def test_calibration_recording_requires_verified_owned_receipt(runtime, monkeypatch):
    settings, _, recordings, calls, fake = runtime
    recording_id = imported(runtime, 2)["recording_id"]
    calibration_id = imported(runtime, 2)["recording_id"]
    calibration_folder = recordings / calibration_id
    calibration = calibration_folder / "camera_calibration.toml"
    calibration.write_text('[cam0]\nid="cam0"\n[cam1]\nid="cam1"\n')
    request = FreeMocapRequest(operation="process", recording_id=recording_id,
                              calibration_recording_id=calibration_id)
    with pytest.raises(ValueError, match="verified successful calibration receipt"):
        start_freemocap_capture(settings, request)
    assert not any(route.endswith("/process") for _, route, _ in calls)
    calibration.unlink()
    job = start_freemocap_capture(settings, FreeMocapRequest(
        operation="calibrate", recording_id=calibration_id,
        board={"squares_x": 5, "squares_y": 7, "square_length_mm": 40}))
    calibration.write_text('[cam0]\nid="cam0"\n[cam1]\nid="cam1"\n')

    def completed(self, method, route, payload=None, **kwargs):
        value = fake(self, method, route, payload, **kwargs)
        if route.startswith("/kimodo-status/v1/pipelines/"):
            value.update(phase="complete", terminal_verified=True, workers_alive=False)
        return value

    monkeypatch.setattr(FreeMoCapClient, "_request", completed)
    assert get_freemocap_job(settings, job["job_id"])["status"] == "completed"
    calibration.write_text('[cam0]\nid="cam0"\n[cam1]\nid="cam1"\n# modified')
    with pytest.raises(ValueError, match="output changed"):
        start_freemocap_capture(settings, request)
    calibration.write_text('[cam0]\nid="cam0"\n[cam1]\nid="cam1"\n')
    assert start_freemocap_capture(settings, request)["status"] == "submitted"


def test_existing_invalid_runtime_configuration_keeps_specific_error(runtime):
    settings = runtime[0]
    (settings.data_dir / "freemocap/runtime.json").write_text("{}")
    result = get_freemocap_capabilities(settings)
    assert "requires exactly" in result["error"]
    assert "Configure the separate" not in result["error"]
