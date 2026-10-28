import numpy as np
import pytest

from kimodo_mcp import quality


def fixture(path, frames=12, fps=30, velocity=0.0, acceleration=0.0, jerk=0.0, contacts=True, **changes):
    names = ["Hips", "LeftFoot", "LeftToeBase", "LeftToeEnd", "RightFoot", "RightToeBase", "RightToeEnd"]
    base = np.array(
        [[0, 1, 0], [-0.1, 0, 0], [-0.1, 0, 0.1], [-0.1, 0, 0.15], [0.1, 0, 0], [0.1, 0, 0.1], [0.1, 0, 0.15]]
    )
    p = np.broadcast_to(base, (frames, 7, 3)).copy()
    t = np.arange(frames) / fps
    p[:, :, 0] += (velocity * t + acceleration * t * t / 2 + jerk * t * t * t / 6)[:, None]
    data = dict(
        posed_joints=p,
        joint_parents=np.array([-1, 0, 1, 2, 0, 4, 5]),
        joint_names=np.array(names),
        fps=np.array(fps),
        skeleton=np.array("somaskel77"),
        global_rot_mats=np.broadcast_to(np.eye(3), (frames, 7, 3, 3)),
    )
    if contacts:
        data["foot_contacts"] = np.ones((frames, 6))
    data.update(changes)
    np.savez(path, **data)
    return path


def test_stationary_motion_and_timing(tmp_path):
    report = quality.analyze_motion(fixture(tmp_path / "still.npz"))
    for key in (
        "speed",
        "acceleration",
        "jerk",
        "bone_length_drift",
        "angular_speed",
        "contact_sliding",
        "foot_floor_proxy",
    ):
        assert report["metrics"][key]["max"] == pytest.approx(0, abs=1e-10)
    assert report["duration_seconds"] == 0.4
    assert report["last_key_seconds"] == pytest.approx(11 / 30)
    assert report["review_frames"][0] == 0 and report["review_frames"][-1] == 11
    assert len(report["review_frames"]) <= 12


def test_analytic_derivatives_and_length_invariance(tmp_path):
    report = quality.analyze_motion(fixture(tmp_path / "cubic.npz", jerk=12))
    assert report["metrics"]["jerk"]["mean"] == pytest.approx(12, abs=1e-9)
    assert report["metrics"]["bone_length_drift"]["max"] < 1e-12
    constant = quality.analyze_motion(fixture(tmp_path / "linear.npz", velocity=2))
    assert constant["metrics"]["speed"]["mean"] == pytest.approx(2)
    assert constant["metrics"]["acceleration"]["max"] < 1e-10


def test_contact_mask_requires_both_endpoints_and_known_mapping(tmp_path):
    contacts = np.zeros((12, 6))
    contacts[3:6, 4] = 1
    report = quality.analyze_motion(fixture(tmp_path / "contact.npz", velocity=0.2, foot_contacts=contacts))
    sliding = report["metrics"]["contact_sliding"]
    assert sliding["eligible_values"] == 2
    assert sliding["worst_joint"] == "RightToeBase"
    assert sliding["max"] == pytest.approx(0.2)
    missing = quality.analyze_motion(fixture(tmp_path / "missing.npz", contacts=False))
    assert not missing["metrics"]["contact_sliding"]["available"]
    unknown = quality.analyze_motion(fixture(tmp_path / "unknown.npz", skeleton=np.array("unknown")))
    assert not unknown["metrics"]["contact_sliding"]["available"]


def test_rotation_geodesic_and_invalid_rotation_not_silently_clipped(tmp_path):
    rots = np.broadcast_to(np.eye(3), (12, 7, 3, 3)).copy()
    for i in range(12):
        theta = i * 0.04
        rots[i, 0] = [[np.cos(theta), -np.sin(theta), 0], [np.sin(theta), np.cos(theta), 0], [0, 0, 1]]
    report = quality.analyze_motion(fixture(tmp_path / "spin.npz", global_rot_mats=rots))
    assert report["metrics"]["angular_speed"]["max"] == pytest.approx(1.2)
    rots[3, 0, 0, 0] = 2
    invalid = quality.analyze_motion(fixture(tmp_path / "invalid.npz", global_rot_mats=rots))
    assert not invalid["metrics"]["angular_speed"]["available"]


def test_hashes_are_content_based_and_comparison_alignment_explicit(tmp_path):
    a = fixture(tmp_path / "a.npz", velocity=0.1)
    b = fixture(tmp_path / "other-name.npz", velocity=0.1)
    assert quality.analyze_motion(a)["content_sha256"] == quality.analyze_motion(b)["content_sha256"]
    with np.load(a) as data:
        shifted = {k: data[k] for k in data.files}
    shifted["posed_joints"] = shifted["posed_joints"] + np.array([2, 0, 0])
    np.savez(b, **shifted)
    world = quality.compare_motion(a, b)
    assert world["position_rmse_m"] == pytest.approx(2)
    assert world["exact_numeric_frames_equal"] == 0
    assert quality.compare_motion(a, b, space="root_relative")["position_rmse_m"] < 1e-14
    aligned = quality.compare_motion(a, a, second_offset_frames=2)
    assert (
        aligned["frames"] == 10 and aligned["first_start_frame"] == 0 and aligned["second_start_frame"] == 2
    )
    assert aligned["position_rmse_m"] == pytest.approx(0.1 * 2 / 30)


def test_short_clip_floor_and_sensitivities(tmp_path):
    short = quality.analyze_motion(fixture(tmp_path / "one.npz", frames=1), thresholds={"floor_y_m": 0.05})
    assert not short["metrics"]["jerk"]["available"]
    assert short["metrics"]["foot_floor_proxy"]["max"] == pytest.approx(0.05)
    assert short["loop_seam"]["velocity_max_gap_m_s"] is None
    for bad in (float("nan"), float("inf"), True, -1, 1e9):
        with pytest.raises(ValueError):
            quality.ReviewThresholds(jerk_m_s3=bad)
    with pytest.raises(TypeError):
        quality.analyze_motion(tmp_path / "x", thresholds={"invented": 3})


def test_compare_rejects_timing_hierarchy_or_no_overlap(tmp_path):
    a = fixture(tmp_path / "a.npz")
    b = fixture(tmp_path / "b.npz", fps=60)
    with pytest.raises(ValueError, match="matching FPS"):
        quality.compare_motion(a, b)
    with pytest.raises(ValueError, match="overlapping"):
        quality.compare_motion(a, a, second_offset_frames=50)
    with pytest.raises(ValueError, match="sample_index"):
        quality.analyze_motion(a, sample_index=-1)


def test_near_valid_stationary_matrices_do_not_create_false_rotation(tmp_path):
    almost_rotation = np.broadcast_to(np.eye(3) * 0.999, (12, 7, 3, 3)).copy()
    path = fixture(tmp_path / "near.npz", global_rot_mats=almost_rotation)
    before = path.read_bytes()
    report = quality.analyze_motion(path)
    assert report["metrics"]["angular_speed"]["max"] == pytest.approx(0, abs=1e-8)
    assert path.read_bytes() == before
    reflection = np.broadcast_to(np.diag([-1, 1, 1]), (12, 7, 3, 3))
    invalid = quality.analyze_motion(fixture(tmp_path / "reflection.npz", global_rot_mats=reflection))
    assert not invalid["metrics"]["angular_speed"]["available"]


def test_no_contact_and_bad_contact_range_do_not_claim_zero_slide(tmp_path):
    for contacts in (np.zeros((12, 6)), np.ones((12, 5)), np.full((12, 6), 2.0)):
        report = quality.analyze_motion(fixture(tmp_path / "contacts.npz", foot_contacts=contacts))
        assert not report["metrics"]["contact_sliding"]["available"]


def test_batched_sample_selection_and_negative_offset(tmp_path):
    path = fixture(tmp_path / "batched.npz", velocity=0.1)
    with np.load(path) as source:
        data = {key: source[key] for key in source.files}
    for key in ("posed_joints", "global_rot_mats", "foot_contacts"):
        data[key] = np.stack([data[key], data[key]])
    data["posed_joints"][1, :, :, 0] += 2
    np.savez(path, **data)
    result = quality.compare_motion(path, path, first_sample=0, second_sample=1, second_offset_frames=-2)
    assert result["first_start_frame"] == 2 and result["second_start_frame"] == 0
    assert result["frames"] == 10
    assert result["position_rmse_m"] == pytest.approx(2 - 0.1 * 2 / 30)
    assert quality.analyze_motion(path, sample_index=1)["sample_index"] == 1
    with pytest.raises(ValueError, match="sample_index"):
        quality.analyze_motion(path, sample_index=True)


def test_hash_tracks_rotation_and_contact_changes_without_changing_positions(tmp_path):
    original = fixture(tmp_path / "original.npz")
    rotation = np.broadcast_to(np.eye(3), (12, 7, 3, 3)).copy()
    rotation[4, 0] = [[0, -1, 0], [1, 0, 0], [0, 0, 1]]
    changed = fixture(tmp_path / "rotated.npz", global_rot_mats=rotation)
    comparison = quality.compare_motion(original, changed)
    assert comparison["position_rmse_m"] == 0
    assert comparison["exact_numeric_frames_equal"] == 11
    contacts = np.ones((12, 6))
    contacts[7, 3] = 0
    changed = fixture(tmp_path / "contact.npz", foot_contacts=contacts)
    assert quality.compare_motion(original, changed)["exact_numeric_frames_equal"] == 11
    report = quality.analyze_motion(changed)
    assert len(report["review_frames"]) <= 12
    assert report["review_frames"][0] == 0 and report["review_frames"][-1] == 11


@pytest.mark.parametrize("thresholds", [False, [], "", 0])
def test_invalid_threshold_containers_are_not_silently_ignored(tmp_path, thresholds):
    with pytest.raises(ValueError, match="thresholds must be an object"):
        quality.analyze_motion(tmp_path / "unused.npz", thresholds=thresholds)
