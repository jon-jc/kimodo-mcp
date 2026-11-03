"""Numerical policies report evidence, never an aesthetic winner or approval."""

import hashlib
import json

import numpy as np
import pytest
from pydantic import ValidationError
from test_quality import fixture

from kimodo_mcp import assessment, quality
from kimodo_mcp.assessment import CandidateReference, QualityPolicy, assess_motion


def policy(**limits):
    return QualityPolicy(checks=[{"metric": metric, "max_value": value} for metric, value in limits.items()])


def run(path, request, **kwargs):
    return assess_motion(path, policy=request, expected_source_sha256=hashlib.sha256(path.read_bytes()).hexdigest(),
                         **kwargs)


def checks(report):
    return {check["metric"]: check for check in report["checks"]}


def rewrite(path, **updates):
    with np.load(path, allow_pickle=False) as data:
        arrays = {key: data[key] for key in data.files}
    for key, value in updates.items():
        if value is None:
            arrays.pop(key, None)
        else:
            arrays[key] = value
    np.savez(path, **arrays)


@pytest.mark.parametrize("invalid", [
    {}, {"checks": []}, {"checks": [{"metric": "speed", "max_value": 1, "extra": 1}]},
    {"checks": [{"metric": "speed", "max_value": value} for value in (1, 2)]},
    {"checks": [{"metric": "realism", "max_value": 1}]},
    *[{"checks": [{"metric": "speed", "max_value": value}]}
      for value in (-1, 10001, True, "1", float("inf"), float("nan"))],
    {"checks": [{"metric": "speed", "max_value": 1}], "contact_probability": 1.1},
    {"checks": [{"metric": "speed", "max_value": 1}], "floor_y_m": -10001},
    {"checks": [{"metric": "speed", "max_value": 1}], "loop_intent": 1},
    {"checks": [{"metric": "loop_world_pose_gap", "max_value": 1}]},
    {"checks": [{"metric": "speed", "max_value": 1}], "automatic_winner": True},
])
def test_policy_rejects_implicit_duplicate_unbounded_or_coerced_controls(invalid):
    with pytest.raises(ValidationError):
        QualityPolicy(**invalid)


def test_candidate_reference_is_managed_and_bounded():
    assert CandidateReference(job_id="a" * 32, sample_index=3).sample_index == 3
    for values in ({"job_id": "../outside"}, {"job_id": "A" * 32},
                   {"job_id": "a" * 32, "sample_index": True},
                   {"job_id": "a" * 32, "sample_index": 4},
                   {"job_id": "a" * 32, "path": "outside.npz"}):
        with pytest.raises(ValidationError):
            CandidateReference(**values)


def test_pass_is_explicit_policy_compliance_and_does_not_mutate_source(tmp_path):
    path = fixture(tmp_path / "still.npz")
    before = path.read_bytes()
    report = run(path, policy(speed=0, angular_speed=0, bone_length_drift=0, contact_sliding=0))
    assert report["status"] == "pass"
    assert len(report["checks"]) == 4  # No hidden jerk, floor or seam requirement.
    assert report["human_review_required"] is True
    assert report["source_sha256"] == hashlib.sha256(before).hexdigest()
    assert report["archive_sample_index"] == 0
    assert "sample_index" not in report
    assert report["content_sha256"] == quality.analyze_motion(path)["content_sha256"]
    assert report["duration_seconds"] == 0.4
    assert report["last_key_seconds"] == pytest.approx(11 / 30)
    assert report["review_frames"][0] == 0 and report["review_frames"][-1] == 11
    assert all(item["violating_values"] == 0 and not item["issue_windows"] for item in report["checks"])
    assert path.read_bytes() == before
    json.dumps(report, allow_nan=False)


def test_policy_hash_is_stable_under_check_order_and_binds_context(tmp_path):
    path = fixture(tmp_path / "still.npz")
    a = run(path, policy(speed=1, jerk=2))
    b = run(path, policy(jerk=2, speed=1))
    assert a == b
    changed = policy(speed=1, jerk=2)
    changed.contact_probability = 0.9
    c = run(path, changed)
    assert c["policy_sha256"] != a["policy_sha256"]
    assert c["source_sha256"] == a["source_sha256"]
    assert c["content_sha256"] == a["content_sha256"]
    changed.checks[0].max_value = -1
    with pytest.raises(ValidationError):
        run(path, changed)


def test_known_derivatives_match_existing_diagnostics_without_reusing_default_limits(tmp_path):
    path = fixture(tmp_path / "analytic.npz", jerk=12)
    report = run(path, policy(speed=10, acceleration=10, jerk=11, angular_speed=0,
                              bone_length_drift=1e-12, contact_sliding=0.001, foot_floor_proxy=0))
    legacy = quality.analyze_motion(path)
    for name, result in checks(report).items():
        assert result["observed_max"] == pytest.approx(legacy["metrics"][name]["max"])
    assert checks(report)["jerk"]["status"] == "fail"
    assert checks(report)["jerk"]["observed_max"] == pytest.approx(12)
    assert checks(report)["jerk"]["difference_order"] == 3
    assert checks(report)["jerk"]["issue_windows"][0]["source_frame_ranges"] == [
        {"start_frame": 0, "end_frame": 11}
    ]
    assert report["status"] == "fail"


def test_issue_windows_count_all_joints_and_include_actual_derivative_inputs(tmp_path):
    path = fixture(tmp_path / "steps.npz")
    with np.load(path) as data:
        positions = data["posed_joints"].copy()
    positions[2:, :, 0] += 0.1
    positions[3:, :, 0] += 0.1
    rewrite(path, posed_joints=positions)
    speed = checks(run(path, policy(speed=1)))["speed"]
    assert speed["violating_values"] == 14
    assert speed["violating_frame_count"] == 2
    assert speed["affected_frame_count"] == 3
    assert speed["issue_window_count"] == 1
    window = speed["issue_windows"][0]
    assert window["start_frame"] == 2 and window["end_frame"] == 3
    assert window["source_frame_ranges"] == [{"start_frame": 1, "end_frame": 3}]
    assert window["peak_value"] == pytest.approx(3)
    assert window["peak_joint"] in quality._sample(path, 0)["names"]


def test_issue_payload_is_bounded_but_retains_full_counts_and_worst_window(tmp_path):
    path = fixture(tmp_path / "many.npz", frames=130)
    with np.load(path) as data:
        positions = data["posed_joints"].copy()
    peaks = list(range(1, 122, 4))
    for frame in peaks:
        positions[frame:, :, 0] += 0.2 if frame == peaks[-1] else 0.1
    rewrite(path, posed_joints=positions)
    report = run(path, policy(speed=1))
    speed = checks(report)["speed"]
    assert speed["issue_window_count"] == len(peaks)
    assert speed["violating_values"] == len(peaks) * 7
    assert speed["affected_frame_count"] == len(peaks) * 2
    assert len(speed["issue_windows"]) == 16
    assert speed["windows_truncated"] is True
    assert any(window["start_frame"] == peaks[-1] for window in speed["issue_windows"])
    assert peaks[-1] in report["review_frames"]
    assert len(report["review_frames"]) <= 24
    assert report["review_frames_truncated"] is True
    assert len(json.dumps(report)) < 15000


@pytest.mark.parametrize("contact_values", [None, np.zeros((12, 6)), np.ones((12, 4)), np.ones((12, 6)) * 2])
def test_missing_inactive_or_invalid_contact_evidence_is_unavailable(tmp_path, contact_values):
    path = fixture(tmp_path / "contact.npz")
    rewrite(path, foot_contacts=contact_values)
    report = run(path, policy(contact_sliding=0.1))
    assert report["status"] == "unavailable"
    assert checks(report)["contact_sliding"]["reason"]
    assert "observed_max" not in checks(report)["contact_sliding"]
    assert report["human_review_required"]


def test_contacts_require_both_endpoints_and_exact_channel_order(tmp_path):
    labels = np.zeros((12, 6))
    labels[3:6, 4] = 0.7
    path = fixture(tmp_path / "contact.npz", velocity=0.2, foot_contacts=labels)
    report = run(path, policy(contact_sliding=0.1))
    sliding = checks(report)["contact_sliding"]
    assert sliding["eligible_values"] == sliding["violating_values"] == 2
    assert sliding["peak_joint"] == "RightToeBase"
    assert sliding["issue_windows"][0]["source_frame_ranges"] == [{"start_frame": 3, "end_frame": 5}]
    request = policy(contact_sliding=0.1)
    request.contact_probability = 0.8
    assert run(path, request)["status"] == "unavailable"
    rewrite(path, skeleton=np.array("unknown"))
    assert run(path, policy(contact_sliding=0.1, foot_floor_proxy=0.1))["status"] == "unavailable"


def test_missing_metadata_affects_only_dependent_checks(tmp_path):
    path = fixture(tmp_path / "metadata.npz")
    rewrite(path, fps=None, joint_parents=None, global_rot_mats=None)
    report = run(path, policy(speed=1, bone_length_drift=1, angular_speed=1, foot_floor_proxy=1))
    result = checks(report)
    assert all(result[name]["status"] == "unavailable" for name in ("speed", "bone_length_drift", "angular_speed"))
    assert result["foot_floor_proxy"]["status"] == "pass"
    assert report["fps"] is report["duration_seconds"] is report["last_key_seconds"] is None
    assert report["status"] == "unavailable"
    json.dumps(report, allow_nan=False)


def test_short_clip_and_invalid_rotations_cannot_pass_required_checks(tmp_path):
    short = fixture(tmp_path / "one.npz", frames=1)
    report = run(short, policy(jerk=100, angular_speed=10))
    assert report["status"] == "unavailable" and report["review_frames"] == [0]
    path = fixture(tmp_path / "bad-rotation.npz", velocity=1,
                   global_rot_mats=np.broadcast_to(np.eye(3) * 2, (12, 7, 3, 3)))
    report = run(path, policy(speed=0.5, angular_speed=100))
    assert checks(report)["angular_speed"]["status"] == "unavailable"
    assert report["status"] == "fail"  # Existing failure cannot be hidden by missing evidence.


def test_loop_checks_require_intent_and_distinguish_translation_from_pose(tmp_path):
    path = fixture(tmp_path / "travel.npz", velocity=0.1)
    request = QualityPolicy(loop_intent=True, checks=[
        {"metric": "loop_world_pose_gap", "max_value": 0.01},
        {"metric": "loop_root_relative_pose_gap", "max_value": 1e-12},
        {"metric": "loop_root_displacement", "max_value": 0.01},
        {"metric": "loop_velocity_gap", "max_value": 1e-12},
    ])
    result = checks(run(path, request))
    assert result["loop_world_pose_gap"]["status"] == "fail"
    assert result["loop_root_displacement"]["status"] == "fail"
    assert result["loop_root_relative_pose_gap"]["status"] == "pass"
    assert result["loop_velocity_gap"]["status"] == "pass"
    world = result["loop_world_pose_gap"]
    assert world["affected_frame_count"] == 2
    assert world["issue_windows"][0]["source_frame_ranges"] == [
        {"start_frame": 0, "end_frame": 0}, {"start_frame": 11, "end_frame": 11}
    ]
    # One-shot motion is assessed without any inferred loop requirement.
    assert run(path, policy(speed=1))["status"] == "pass"


def test_loop_velocity_uses_distinct_intervals_and_reports_boundary_samples(tmp_path):
    request = QualityPolicy(loop_intent=True, checks=[{"metric": "loop_velocity_gap", "max_value": 0.01}])
    two = fixture(tmp_path / "two.npz", frames=2, velocity=1)
    assert run(two, request)["status"] == "unavailable"
    path = fixture(tmp_path / "accelerate.npz", acceleration=3)
    result = checks(run(path, request))["loop_velocity_gap"]
    assert result["status"] == "fail" and result["affected_frame_count"] == 4
    assert result["issue_windows"][0]["source_frame_ranges"] == [
        {"start_frame": 0, "end_frame": 1}, {"start_frame": 10, "end_frame": 11}
    ]


def test_floor_context_and_bone_reference_are_explicit(tmp_path):
    path = fixture(tmp_path / "floor.npz")
    request = policy(foot_floor_proxy=0.01, bone_length_drift=0.1)
    request.floor_y_m = 0.05
    result = checks(run(path, request))
    assert result["foot_floor_proxy"]["status"] == "fail"
    assert result["foot_floor_proxy"]["observed_max"] == 0.05
    assert result["bone_length_drift"]["reference_frame_index"] == 0


def test_archive_sample_selection_and_source_hashes_are_exact(tmp_path):
    path = fixture(tmp_path / "batch.npz")
    with np.load(path) as data:
        positions = data["posed_joints"]
        rotations = data["global_rot_mats"]
        contacts = data["foot_contacts"]
    changed = positions.copy()
    changed[:, :, 0] += np.arange(len(changed))[:, None] / 30
    rewrite(path, posed_joints=np.stack([positions, changed]), global_rot_mats=np.stack([rotations, rotations]),
            foot_contacts=np.stack([contacts, contacts]))
    first = run(path, policy(speed=0.5), sample_index=0)
    second = run(path, policy(speed=0.5), sample_index=1)
    assert first["status"] == "pass" and second["status"] == "fail"
    assert first["source_sha256"] == second["source_sha256"]
    assert first["content_sha256"] != second["content_sha256"]
    assert second["archive_sample_index"] == 1
    for bad in (-1, 2, True, "0"):
        with pytest.raises(ValueError, match="sample_index"):
            run(path, policy(speed=1), sample_index=bad)
    for digest in ("0" * 64, "A" * 64, True):
        with pytest.raises(ValueError, match="checksum|SHA-256"):
            assess_motion(path, policy=policy(speed=1), expected_source_sha256=digest)


def test_source_change_during_analysis_is_rejected(tmp_path, monkeypatch):
    path = fixture(tmp_path / "source.npz")
    original = assessment._frame_hashes

    def changed(motion):
        result = original(motion)
        path.write_bytes(path.read_bytes() + b"changed")
        return result

    monkeypatch.setattr(assessment, "_frame_hashes", changed)
    with pytest.raises(ValueError, match="changed during assessment"):
        run(path, policy(speed=1))


def test_threshold_boundary_is_inclusive_without_unstated_tolerance(tmp_path):
    path = fixture(tmp_path / "boundary.npz", frames=2, fps=1, velocity=1)
    assert run(path, policy(speed=1))["status"] == "pass"
    assert run(path, policy(speed=np.nextafter(1.0, 0.0)))["status"] == "fail"
