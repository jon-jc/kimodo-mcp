import copy

import numpy as np
import pytest

from kimodo_mcp.video_gap import bridge_detections


def observation(frame, status="detected", shift=0):
    return {"frame": frame, "status": status, "person_id": 0 if status == "detected" else None,
            "keypoints": [[10 + joint + shift, 20 + joint, .9] for joint in range(25)] if status == "detected" else None}


def test_short_gap_preserves_observations_and_explicitly_marks_bounded_low_confidence_estimates():
    records = [observation(0), observation(1, "missing"), observation(2, "insufficient"), observation(3, shift=6)]
    before = copy.deepcopy(records)
    filled, frames = bridge_detections(records, 2)
    assert frames == [1, 2] and records == before
    for frame, shift in [(1, 2), (2, 4)]:
        assert filled[frame]["keypoints"] is None and filled[frame]["status"] == records[frame]["status"]
        assert filled[frame]["estimated"] and filled[frame]["estimate"]["before_frame"] == 0
        np.testing.assert_allclose(np.array(filled[frame]["fitted_keypoints"])[:, 0], np.arange(25) + 10 + shift)
        assert np.max(np.array(filled[frame]["fitted_keypoints"])[:, 2]) == .25


@pytest.mark.parametrize("status", ["ambiguous", "invalid", "degenerate"])
def test_ambiguous_or_invalid_observations_are_never_bridged(status):
    records = [observation(0), observation(1, status), observation(2)]
    assert bridge_detections(records, 15) == (records, [])


def test_endpoints_long_gaps_identity_mismatch_and_large_discontinuities_remain_unresolved():
    for records, bound in [
        ([observation(0, "missing"), observation(1)], 15),
        ([observation(0), observation(1, "missing")], 15),
        ([observation(0), observation(1, "missing"), observation(2, "missing"), observation(3)], 1),
        ([observation(0), observation(1, "missing"), observation(2, shift=1000)], 15),
        ([observation(0), {**observation(1, "insufficient"), "person_id": 4}, observation(2)], 15),
    ]:
        assert bridge_detections(records, bound) == (records, [])


def test_strict_default_and_invalid_gap_limits():
    records = [observation(0), observation(1, "missing"), observation(2)]
    assert bridge_detections(records, 0) == (records, [])
    for value in [-1, 16, True, 1.5]:
        with pytest.raises(ValueError):
            bridge_detections(records, value)


def test_collapsing_intermediate_body_rejects_the_entire_gap():
    left, right = observation(0), observation(2)
    right["keypoints"] = right["keypoints"][::-1]
    records = [left, observation(1, "missing"), right]
    assert bridge_detections(records, 1) == (records, [])


def test_explicit_gap_policy_reaches_fitting_with_original_detection_and_estimate_both_retained(tmp_path, monkeypatch):
    import json

    import test_video_mocap as base

    from kimodo_mcp import video_mocap as capture
    from kimodo_mcp import video_mocap_worker as worker
    from kimodo_mcp.config import Settings

    settings = Settings(data_dir=tmp_path / "data", python="unused", timeout=30)
    source = tmp_path / "fixture.mp4"
    source.write_bytes(b"synthetic fixture; no real video")
    original_plan = base.ready_plan

    def plan(*args):
        value = original_plan(*args)
        value["request"]["max_gap_frames"] = 1
        return value

    monkeypatch.setattr(base, "ready_plan", plan)
    directory, _, _ = base.create_job(settings, source, monkeypatch)
    stages = []

    def run(*args, **kwargs):
        stages.append(args[1])
        base.synthetic_stages(*args, **kwargs)
        if args[1] == "detect":
            (directory / "dataset/annots/video/000001.json").unlink()
        if args[1] == "fit":
            fitted = json.loads((directory / "dataset/annots/video/000001.json").read_text())
            assert fitted["annots"][0]["personID"] == 0
            assert fitted["annots"][0]["keypoints"][0][2] == .25

    monkeypatch.setattr(worker, "_run_stage", run)
    assert worker.run_job(directory) == 0 and "fit" in stages
    result = capture.get_video_mocap_job(settings, directory.name)
    assert result["status"] == "completed" and result["diagnostics"]["estimated_frames"] == [1]
    assert result["diagnostics"]["unresolved_frame_count"] == 0
    record = json.loads((directory / "detections.json").read_text())["frames"][1]
    assert record["keypoints"] is None and record["status"] == "missing"
    assert record["fitted_keypoints"] and record["estimated"]
