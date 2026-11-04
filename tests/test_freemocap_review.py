"""Synthetic saved-result review tests; no tracker, service, inference or editor."""
import json

import numpy as np
import pytest
from starlette.applications import Starlette
from starlette.testclient import TestClient

from kimodo_mcp import freemocap as capture
from kimodo_mcp import freemocap_review as review
from kimodo_mcp.config import Settings
from kimodo_mcp.freemocap_api import freemocap_routes
from kimodo_mcp.freemocap_profiles import get_landmark_profile


@pytest.fixture
def saved(tmp_path, monkeypatch):
    monkeypatch.setattr(capture.FreeMoCapClient, "_request", lambda *a, **k: pytest.fail("Review contacted runtime"))
    def make(detector="mediapipe", aspect="body", frames=12, cameras=1, timing=True):
        root = tmp_path / (detector + aspect + str(cameras))
        directory = root / "freemocap"
        references, recordings = root / "references", root / "recordings"
        for path in (directory / "jobs", references, recordings):
            path.mkdir(parents=True)
        (directory / "runtime.json").write_text(json.dumps({"base_url": "http://127.0.0.1:53117",
            "recordings_root": str(recordings), "references_root": str(references)}))
        settings = Settings(data_dir=root, python="unused")
        client = capture.FreeMoCapClient(settings)
        folder = recordings / "take"
        (folder / "output_data").mkdir(parents=True)
        (folder / "synchronized_videos").mkdir()
        sources = []
        for i in range(cameras):
            source = folder / f"synchronized_videos/cam{i}.mp4"
            source.write_bytes(b"synthetic video")
            sources.append({"path": source.relative_to(folder).as_posix(), "bytes": source.stat().st_size,
                            "sha256": capture._digest(source)})
        (folder / ".kimodo-freemocap.json").write_text(json.dumps({"recording_id": "take",
            "runtime_key": client.runtime_key, "videos": sources}))
        profile = get_landmark_profile(detector, aspect)
        values = np.zeros((frames, len(profile["names"]), 3))
        values[:, :, 0] = np.arange(frames)[:, None]
        path = folder / f"output_data/{detector}_{aspect}_3d_xyz.npy"
        np.save(path, values)
        controls = {"operation": "process", "recording_id": "take", "allow_planar": cameras == 1,
                    "process_options": {"detector_type": detector}}
        receipt = {"schema_version": 1, "job_id": "a" * 32, "runtime_key": client.runtime_key,
            "recording_id": "take", "operation": "process", "status": "completed",
            "pipeline_completion_verified": True, "request": controls, "sources": sources,
            "outputs": [{"path": path.relative_to(folder).as_posix(), "bytes": path.stat().st_size,
                         "sha256": capture._digest(path)}]}
        if timing:
            receipt["video_metadata"] = {"videos": [{"frame_count": frames, "fps": 30.0} for _ in sources]}
        receipt_path = directory / "jobs" / (receipt["job_id"] + ".json")
        receipt_path.write_text(json.dumps(receipt))
        def replace(values):
            np.save(path, values)
            receipt["outputs"][0].update(bytes=path.stat().st_size, sha256=capture._digest(path))
            receipt_path.write_text(json.dumps(receipt))
        return settings, receipt, path, values, replace, receipt_path
    return make


@pytest.mark.parametrize("detector,points", [("mediapipe", 37), ("rtmpose", 27)])
def test_body_frames_are_offline_original_native_positions(saved, detector, points):
    settings, receipt, path, values, _, _ = saved(detector)
    (path.parent.parent / "tracker_schema.json").write_text('{"name":"wrong_layout"}')
    before = path.read_bytes()
    result = review.inspect_freemocap_frames(settings, receipt["job_id"], [7, 2])
    assert len(result["joint_names"]) == points
    assert [r["frame_index"] for r in result["frames_inspected"]] == [7, 2]
    assert result["frames_inspected"][0]["positions_native"] == values[7].tolist()
    assert result["frames_inspected"][0]["time_seconds"] == 7 / 30
    assert result["coordinates"]["units"] == "pixels"
    assert result["tracked_mask"].count(False) == 4
    assert path.read_bytes() == before


@pytest.mark.parametrize("aspect,points", [("left_hand", 21), ("right_hand", 21), ("face", 136)])
def test_known_aspects_use_saved_profile_names(saved, aspect, points):
    settings, receipt, _, _, _, _ = saved(aspect=aspect)
    result = review.inspect_freemocap_frames(settings, receipt["job_id"], aspect=aspect)
    assert len(result["joint_names"]) == points


def test_missing_triplets_are_null_and_never_bridge_derivative_gaps(saved):
    settings, receipt, _, values, replace, _ = saved()
    values[3:6, 0] = np.nan
    values[8, 1, 1] = np.nan
    replace(values)
    result = review.inspect_freemocap_frames(settings, receipt["job_id"], [4, 8])
    assert result["frames_inspected"][0]["positions_native"][0] == [None, None, None]
    assert result["frames_inspected"][1]["positions_native"][1] == [None, None, None]
    json.dumps(result, allow_nan=False)
    quality = review.analyze_freemocap_quality(settings, receipt["job_id"])
    assert quality["coverage"]["longest_missing_run_frames"][0] == 3
    assert quality["movement"]["maximum"] == 30
    assert quality["movement"]["eligible_intervals"] == 11 * 37 - 4 - 2
    assert quality["movement"]["unit"] == "pixels/s"
    assert quality["quality_score"] is None


def test_all_missing_and_single_frame_report_unavailable_movement(saved):
    settings, receipt, _, values, replace, _ = saved(frames=1)
    values[:] = np.nan
    replace(values)
    quality = review.analyze_freemocap_quality(settings, receipt["job_id"])
    assert not quality["movement"]["available"]
    assert quality["coverage"]["valid_fraction"] == 0
    assert quality["review_frames"] == [0]
    json.dumps(quality, allow_nan=False)


def test_legacy_unknown_time_and_manual_calibration_units_are_not_fabricated(saved):
    settings, receipt, _, _, _, _ = saved(cameras=2, timing=False)
    quality = review.analyze_freemocap_quality(settings, receipt["job_id"])
    assert quality["fps"] is None
    assert quality["coordinates"]["units"] == "calibration_units"
    assert quality["movement"]["unit"] == "calibration_units/frame"
    assert review.inspect_freemocap_frames(settings, receipt["job_id"])["frames_inspected"][0]["time_seconds"] is None


@pytest.mark.parametrize("selection", [[True], [-1], [12], [1, 1], list(range(17)), [1.5]])
def test_frame_bounds_and_strict_indices(saved, selection):
    settings, receipt, *_ = saved()
    with pytest.raises(ValueError):
        review.inspect_freemocap_frames(settings, receipt["job_id"], selection)


@pytest.mark.parametrize("bad", ["shape", "object", "infinity", "trailing_bytes"])
def test_unsafe_or_ambiguous_arrays_rejected_before_review(saved, bad):
    settings, receipt, path, values, replace, receipt_path = saved()
    if bad == "shape":
        replace(values[:, :-1])
    elif bad == "object":
        replace(values.astype(object))
    elif bad == "infinity":
        values[0, 0, 0] = np.inf
        replace(values)
    else:
        with path.open("ab") as stream:
            stream.write(b"trailing")
        receipt["outputs"][0].update(bytes=path.stat().st_size, sha256=capture._digest(path))
        receipt_path.write_text(json.dumps(receipt))
    with pytest.raises(ValueError):
        review.inspect_freemocap_frames(settings, receipt["job_id"])


def test_source_output_and_receipt_completion_are_bound(saved):
    settings, receipt, path, _, _, receipt_path = saved()
    receipt["status"] = "submitted"
    receipt_path.write_text(json.dumps(receipt))
    with pytest.raises(ValueError, match="completed"):
        review.preview_freemocap(settings, receipt["job_id"])
    receipt["status"] = "completed"
    receipt_path.write_text(json.dumps(receipt))
    path.write_bytes(b"changed")
    with pytest.raises(ValueError):
        review.preview_freemocap(settings, receipt["job_id"])


def test_during_read_mutation_is_rejected(saved, monkeypatch):
    settings, receipt, path, *_ = saved()
    original = review._array
    def changed(path, points):
        mapped = original(path, points)
        values = np.array(mapped)
        mapped._mmap.close()
        path.write_bytes(b"changed during read")
        return values
    monkeypatch.setattr(review, "_array", changed)
    with pytest.raises(ValueError, match="changed"):
        review.inspect_freemocap_frames(settings, receipt["job_id"])


def test_window_is_contiguous_original_timeline_and_routes_validate_queries(saved):
    settings, receipt, *_ = saved()
    result = review.preview_freemocap(settings, receipt["job_id"], 8, 8)
    assert result["frame_indices"] == [8, 9, 10, 11]
    assert result["frames"] == 4 and result["total_frames"] == 12
    assert result["source_time_seconds"][0] == 8 / 30
    with TestClient(Starlette(routes=freemocap_routes(settings))) as browser:
        base = f"/api/freemocap/{receipt['job_id']}/preview"
        assert browser.get(base + "?start_frame=8&count=8").json()["frame_indices"] == [8, 9, 10, 11]
        for query in ["count=2401", "count=-1", "count=1&count=2", "other=2", "start_frame=true"]:
            assert browser.get(base + "?" + query).status_code == 400


def test_exact_artifact_download_offline_and_no_arbitrary_paths(saved):
    settings, receipt, path, *_ = saved()
    result = review.get_freemocap_artifact(settings, receipt["job_id"], receipt["outputs"][0]["path"])
    assert result["absolute_path"] == str(path)
    for bad in ["../runtime.json", str(path), "output_data/not-recorded.npy"]:
        with pytest.raises(ValueError):
            review.get_freemocap_artifact(settings, receipt["job_id"], bad)


def test_completed_get_job_works_with_runtime_unavailable(saved):
    settings, receipt, *_ = saved()
    result = capture.get_freemocap_job(settings, receipt["job_id"])
    assert result["status"] == "completed"
    assert result["recording"]["upstream_readiness"]["available"] is False

@pytest.mark.parametrize("failure", ["indices", "timing", "infinity"])
def test_memory_maps_close_on_all_failed_review_paths(saved, monkeypatch, failure):
    settings, receipt, _, values, replace, receipt_path = saved()
    if failure == "timing":
        receipt["video_metadata"]["videos"][0]["frame_count"] += 1
        receipt_path.write_text(json.dumps(receipt))
    elif failure == "infinity":
        values[0, 0, 0] = np.inf
        replace(values)
    opened = []
    original = review._array
    def track(*args):
        value = original(*args)
        opened.append(value)
        return value
    monkeypatch.setattr(review, "_array", track)
    with pytest.raises(ValueError):
        review.inspect_freemocap_frames(settings, receipt["job_id"], [999] if failure == "indices" else [0])
    assert opened and all(value._mmap.closed for value in opened)


def test_export_review_preserves_process_timing_and_detects_process_receipt_race(saved, monkeypatch):
    settings, process, path, _, _, process_path = saved()
    exported = dict(process, job_id="b" * 32, operation="export",
                    request={"operation": "export", "recording_id": "take"})
    export_path = process_path.with_name("b" * 32 + ".json")
    export_path.write_text(json.dumps(exported))
    result = review.inspect_freemocap_frames(settings, exported["job_id"], [1])
    assert result["processing_job_id"] == process["job_id"] and result["fps"] == 30
    original = review._frames
    def changed(*args):
        result = original(*args)
        process["video_metadata"]["videos"][0]["fps"] = 60
        process_path.write_text(json.dumps(process))
        return result
    monkeypatch.setattr(review, "_frames", changed)
    with pytest.raises(ValueError, match="receipt changed"):
        review.inspect_freemocap_frames(settings, exported["job_id"], [1])


@pytest.mark.parametrize("matching", [True, False])
def test_millimeters_require_exact_selected_owned_calibration_output(saved, matching):
    settings, process, body, _, _, process_path = saved(cameras=2)
    folder = body.parent.parent
    calibration = folder / "camera_calibration.toml"
    calibration.write_text("synthetic calibration bytes")
    selected = calibration if matching else folder / "other_calibration.toml"
    if not matching:
        selected.write_text("other calibration")
    source = {"path": str(selected), "bytes": selected.stat().st_size, "sha256": capture._digest(selected)}
    process["sources"].append(source)
    process["request"]["calibration_recording_id"] = "take"
    process_path.write_text(json.dumps(process))
    completed = dict(process, job_id="c" * 32, operation="calibrate", sources=process["sources"][:2],
                     request={"operation": "calibrate", "recording_id": "take",
                              "board": {"squares_x": 5, "squares_y": 7, "square_length_mm": 40}},
                     outputs=[{"path": calibration.name, "bytes": calibration.stat().st_size,
                               "sha256": capture._digest(calibration)}])
    process_path.with_name("c" * 32 + ".json").write_text(json.dumps(completed))
    result = review.inspect_freemocap_frames(settings, process["job_id"])
    assert result["coordinates"]["units"] == ("millimeters" if matching else "calibration_units")


def test_contiguous_preview_can_page_beyond_first_window_without_downsampling(saved):
    settings, receipt, *_ = saved(frames=2411)
    first = review.preview_freemocap(settings, receipt["job_id"], 0, 2400)
    last = review.preview_freemocap(settings, receipt["job_id"], 2400, 2400)
    assert first["frame_indices"] == list(range(2400))
    assert last["frame_indices"] == list(range(2400, 2411))
    assert last["source_time_seconds"][0] == 80
    assert last["positions"][0][0][0] == 2400


def test_changed_original_video_prevents_landmark_review(saved):
    settings, receipt, body, *_ = saved()
    (body.parent.parent / receipt["sources"][0]["path"]).write_bytes(b"different video")
    with pytest.raises(ValueError, match="reference video changed"):
        review.inspect_freemocap_frames(settings, receipt["job_id"])


def test_export_review_rechecks_underlying_process_calibration_source_after_read(saved, monkeypatch):
    settings, process, body, _, _, process_path = saved(cameras=2)
    calibration = settings.data_dir / "references/calibration.toml"
    calibration.write_text("original calibration")
    process["request"]["calibration_file"] = "calibration.toml"
    process["sources"].append({"path": str(calibration), "bytes": calibration.stat().st_size,
                               "sha256": capture._digest(calibration)})
    process_path.write_text(json.dumps(process))
    exported = dict(process, job_id="b" * 32, operation="export", sources=process["sources"][:2],
                    request={"operation": "export", "recording_id": "take"})
    process_path.with_name("b" * 32 + ".json").write_text(json.dumps(exported))
    original = review._frames
    def changed(*args):
        result = original(*args)
        calibration.write_text("changed calibration")
        return result
    monkeypatch.setattr(review, "_frames", changed)
    with pytest.raises(ValueError, match="source changed"):
        review.inspect_freemocap_frames(settings, exported["job_id"], [1])


def test_calibration_scale_receipt_is_pinned_during_review(saved, monkeypatch):
    settings, process, body, _, _, process_path = saved(cameras=2)
    calibration = body.parent.parent / "camera_calibration.toml"
    calibration.write_text("original calibration")
    process["request"]["calibration_recording_id"] = "take"
    process["sources"].append({"path": str(calibration), "bytes": calibration.stat().st_size,
                               "sha256": capture._digest(calibration)})
    process_path.write_text(json.dumps(process))
    completed = dict(process, job_id="c" * 32, operation="calibrate", sources=process["sources"][:2],
                     request={"operation": "calibrate", "recording_id": "take",
                              "board": {"squares_x": 5, "squares_y": 7, "square_length_mm": 40}},
                     outputs=[{"path": calibration.name, "bytes": calibration.stat().st_size,
                               "sha256": capture._digest(calibration)}])
    proof = process_path.with_name("c" * 32 + ".json")
    proof.write_text(json.dumps(completed))
    original = review._frames
    def changed(*args):
        result = original(*args)
        completed["request"]["board"]["square_length_mm"] = 400
        proof.write_text(json.dumps(completed))
        return result
    monkeypatch.setattr(review, "_frames", changed)
    with pytest.raises(ValueError, match="Calibration receipt changed"):
        review.inspect_freemocap_frames(settings, process["job_id"], [1])
