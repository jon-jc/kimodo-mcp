"""Exact-frame review, shared framing and hostile display metadata tests."""

import hashlib
import json
import re

import numpy as np
import pytest

from kimodo_mcp.review import compare_motion_reviews, create_motion_review


def motion(path, *, shift=0, frames=12, fps=30, **changes):
    positions = np.zeros((frames, 3, 3), dtype=np.float64)
    positions[:, :, 0] = np.arange(frames)[:, None] * 0.1 + shift
    positions[:, 1, 1] = 1.5
    positions[:, 2, 1] = 0.7
    positions[:, 2, 2] = 0.4
    values = dict(
        posed_joints=positions,
        fps=np.array(fps),
        joint_parents=np.array([-1, 0, 0]),
        joint_names=np.array(["Root", "Head", "LeftHand"]),
        skeleton=np.array("fixture"),
    )
    values.update(changes)
    np.savez_compressed(path, **{key: value for key, value in values.items() if value is not None})
    return positions


def payload(path):
    text = path.read_text(encoding="utf-8")
    return json.loads(
        re.search(
            r'<script id="review-data" type="application/json">(.*?)</script>',
            text,
            re.DOTALL,
        ).group(1)
    )


def test_exact_values_endpoints_and_source_digest(tmp_path):
    source, output = tmp_path / "clip.npz", tmp_path / "review.html"
    positions = motion(source)
    report = create_motion_review(
        source,
        output,
        diagnostics={
            "review_frames": [3],
            "metrics": {"jerk": {"worst_frame_index": 3}},
        },
    )
    data = payload(output)
    assert np.array_equal(data["clips"][0]["positions"], positions)
    assert report["source_sha256"] == [hashlib.sha256(source.read_bytes()).hexdigest()]
    assert report["review_frames"][0] == 0 and report["review_frames"][-1] == 11
    assert "Maximum jerk" in next(item for item in data["reviewFrames"] if item["frame"] == 3)["reasons"]


def test_camera_encloses_every_frame_in_both_clips(tmp_path):
    left, right, output = tmp_path / "a.npz", tmp_path / "b.npz", tmp_path / "pair.html"
    motion(left)
    motion(right, shift=14)
    compare_motion_reviews(left, right, output, offset_frames=2)
    data = payload(output)
    assert (data["firstFrame"], data["lastFrame"]) == (0, 9)
    for camera in data["cameras"]:
        basis = np.asarray(camera["basis"]).T
        for clip in data["clips"]:
            projected = np.asarray(clip["positions"]).reshape(-1, 3) @ basis
            assert np.all(projected >= np.asarray(camera["minimum"]) - 1e-12)
            assert np.all(projected <= np.asarray(camera["maximum"]) + 1e-12)


def test_root_relative_is_display_only_and_negative_offset(tmp_path):
    left, right, output = tmp_path / "a.npz", tmp_path / "b.npz", tmp_path / "pair.html"
    motion(left)
    motion(right, shift=9)
    before = (left.read_bytes(), right.read_bytes())
    compare_motion_reviews(left, right, output, offset_frames=-3, mode="root_relative")
    data = payload(output)
    assert (data["firstFrame"], data["lastFrame"]) == (3, 11)
    assert np.allclose(data["clips"][0]["positions"], data["clips"][1]["positions"])
    assert before == (left.read_bytes(), right.read_bytes())


@pytest.mark.parametrize(
    "change",
    [
        {"fps": np.array(60)},
        {"skeleton": np.array("different")},
        {"joint_names": np.array(["Root", "Arm", "Hand"])},
        {"joint_parents": np.array([-1, 0, 1])},
    ],
)
def test_refuses_implicit_retiming_or_retargeting(tmp_path, change):
    left, right = tmp_path / "a.npz", tmp_path / "b.npz"
    motion(left)
    motion(right, **change)
    with pytest.raises(ValueError, match="requires"):
        compare_motion_reviews(left, right, tmp_path / "pair.html")


@pytest.mark.parametrize("sample", [-1, 1, True, 0.5])
def test_sample_bounds(tmp_path, sample):
    source = tmp_path / "a.npz"
    motion(source)
    with pytest.raises(ValueError, match="Sample index"):
        create_motion_review(source, tmp_path / "review.html", sample_index=sample)


def test_no_overlap_and_missing_metadata(tmp_path):
    source = tmp_path / "a.npz"
    motion(source)
    with pytest.raises(ValueError, match="overlapping"):
        compare_motion_reviews(source, source, tmp_path / "pair.html", offset_frames=12)
    motion(source, joint_names=None)
    with pytest.raises(ValueError, match="metadata"):
        create_motion_review(source, tmp_path / "review.html")


def test_untrusted_labels_stay_in_inert_json(tmp_path):
    source, output = tmp_path / "clip.npz", tmp_path / "review.html"
    attack = "</script><script>globalThis.compromised=true</script>"
    motion(source, joint_names=np.array(["Root", attack, "LeftHand"]))
    create_motion_review(source, output, diagnostics={"review_frames": [{"frame": 2, "reason": attack}]})
    assert attack not in output.read_text(encoding="utf-8")
    assert payload(output)["clips"][0]["names"][1] == attack


def test_repeated_build_is_identical_and_limit_is_explicit(tmp_path, monkeypatch):
    source, output = tmp_path / "a.npz", tmp_path / "review.html"
    motion(source)
    create_motion_review(source, output)
    first = output.read_bytes()
    create_motion_review(source, output)
    assert first == output.read_bytes()
    monkeypatch.setattr("kimodo_mcp.review.MAX_REVIEW_VALUES", 5)
    with pytest.raises(ValueError, match="coordinate limit"):
        create_motion_review(source, output)


@pytest.mark.parametrize(
    "metadata",
    [
        {"frames": 11},
        {"fps": 60},
        {"sample_index": 1},
        {"sample_index": False},
        {"duration_seconds": 12},
        {"last_key_seconds": 0},
    ],
)
def test_single_review_rejects_conflicting_report_metadata(tmp_path, metadata):
    source, output = tmp_path / "clip.npz", tmp_path / "review.html"
    motion(source)
    with pytest.raises(ValueError, match="Diagnostic"):
        create_motion_review(source, output, diagnostics=metadata)
    assert not output.exists()


def test_quality_hash_binds_report_to_exact_sample_content(tmp_path):
    from kimodo_mcp.quality import analyze_motion

    first, changed, output = tmp_path / "first.npz", tmp_path / "changed.npz", tmp_path / "review.html"
    motion(first)
    motion(changed, shift=2)
    diagnostics = analyze_motion(first)
    create_motion_review(first, output, diagnostics=diagnostics)
    previous = output.read_bytes()
    # Matching timing is insufficient: the actual source poses must also match.
    with pytest.raises(ValueError, match="Diagnostic frame_hashes"):
        create_motion_review(changed, output, diagnostics=diagnostics)
    assert output.read_bytes() == previous
    content_only = {"content_sha256": diagnostics["content_sha256"]}
    with pytest.raises(ValueError, match="Diagnostic content_sha256"):
        create_motion_review(changed, output, diagnostics=content_only)


@pytest.mark.parametrize(
    "metadata",
    [
        {"first_start_frame": 0},
        {"second_start_frame": 1},
        {"frames": 12},
        {"space": "world"},
        {"first_sample": 1},
        {"second_sample": 1},
        {"fps": 60},
    ],
)
def test_comparison_rejects_conflicting_alignment_diagnostics(tmp_path, metadata):
    source, output = tmp_path / "clip.npz", tmp_path / "pair.html"
    motion(source)
    with pytest.raises(ValueError, match="Diagnostic"):
        compare_motion_reviews(
            source, source, output, offset_frames=-3, mode="root_relative", diagnostics=metadata
        )
    assert not output.exists()


@pytest.mark.parametrize("offset", [-3, 2])
def test_actual_comparison_report_matches_renderer_timeline(tmp_path, offset):
    from kimodo_mcp.quality import compare_motion

    source, output = tmp_path / "clip.npz", tmp_path / "pair.html"
    motion(source)
    diagnostics = compare_motion(source, source, second_offset_frames=offset, space="root_relative")
    result = compare_motion_reviews(
        source, source, output, offset_frames=offset, mode="root_relative", diagnostics=diagnostics
    )
    assert result["first_frame"] == diagnostics["first_start_frame"]
    assert result["last_frame"] + 1 == diagnostics["first_start_frame"] + diagnostics["frames"]
