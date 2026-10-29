import numpy as np
import pytest

from kimodo_mcp import motion_access
from kimodo_mcp.pose import _soma_layout, extract_pose_constraint
from kimodo_mcp.schemas import GenerateRequest


def motion(path, joints=77, frames=20, **changes):
    names, parents = _soma_layout(joints)
    positions = np.zeros((frames, joints, 3))
    positions[:, 0, 0] = np.arange(frames) / 10
    positions[:, 0, 1] = 0.9
    positions[:, 0, 2] = -3
    rotations = np.broadcast_to(np.eye(3), (frames, joints, 3, 3)).copy()
    data = dict(
        posed_joints=positions,
        local_rot_mats=rotations,
        global_rot_mats=rotations,
        joint_names=np.array(names),
        joint_parents=np.array(parents),
        fps=np.array(30),
        skeleton=np.array(f"somaskel{joints}"),
        model=np.array("kimodo-soma-rp-v1.2"),
        foot_contacts=np.ones((frames, 6 if joints == 77 else 4)),
    )
    data.update(changes)
    np.savez(path, **{key: value for key, value in data.items() if value is not None})
    return path


def test_inspect_all_joints_and_selected_order_without_mutation(tmp_path):
    path = motion(tmp_path / "motion.npz")
    original = path.read_bytes()
    report = motion_access.inspect_motion_frames(path, list(range(16)))
    assert len(report["frames"]) == 16
    assert len(report["frames"][0]["joint_positions"]) == 77
    assert report["joint_names"] == _soma_layout(77)[0]
    assert report["joint_parents"] == _soma_layout(77)[1]
    assert report["frames"][15]["time_seconds"] == 0.5
    assert report["frames"][15]["root_position"] == [1.5, 0.9, -3]
    assert "local_rot_mats" not in report["frames"][0]
    selected = motion_access.inspect_motion_frames(
        path, [3], joint_names=["RightHand", "Hips"], include_rotations=True
    )
    assert selected["selected_joint_names"] == ["RightHand", "Hips"]
    assert selected["frames"][0]["joint_positions"][1] == [0.3, 0.9, -3]
    assert np.shape(selected["frames"][0]["global_rot_mats"]) == (2, 3, 3)
    assert path.read_bytes() == original


def test_rotation_budget_and_optional_missing_channels(tmp_path):
    path = motion(tmp_path / "motion.npz")
    motion_access.inspect_motion_frames(path, [0, 1, 2], include_rotations=True)
    with pytest.raises(ValueError, match="256 joint poses"):
        motion_access.inspect_motion_frames(path, [0, 1, 2, 3], include_rotations=True)
    motion_access.inspect_motion_frames(path, list(range(16)), joint_names=["Hips"], include_rotations=True)
    path = motion(path, local_rot_mats=None, global_rot_mats=None, foot_contacts=None)
    report = motion_access.inspect_motion_frames(path, [0], include_rotations=True)
    assert report["available_rotation_channels"] == []
    assert report["frames"][0]["foot_contacts"] is None


def test_missing_metadata_is_not_fabricated(tmp_path):
    path = motion(
        tmp_path / "motion.npz", joint_names=None, joint_parents=None, fps=None, skeleton=None, model=None
    )
    report = motion_access.inspect_motion_frames(path, [0])
    assert report["fps"] is report["joint_names"] is report["joint_parents"] is None
    assert report["selected_joint_names"] is report["root_joint_index"] is None
    assert report["frames"][0]["time_seconds"] is report["frames"][0]["root_position"] is None
    with pytest.raises(ValueError, match="explicit joint_names"):
        motion_access.inspect_motion_frames(path, [0], joint_names=["Joint 0"])


def test_position_budget_and_nonzero_root_index(tmp_path):
    path = tmp_path / "wide.npz"
    np.savez(path, posed_joints=np.zeros((16, 256, 3)))
    motion_access.inspect_motion_frames(path, list(range(8)))
    with pytest.raises(ValueError, match="2048 joint poses"):
        motion_access.inspect_motion_frames(path, list(range(9)))
    np.savez(path, posed_joints=np.array([[[9, 8, 7], [1, 2, 3]]]), joint_parents=np.array([1, -1]))
    report = motion_access.inspect_motion_frames(path, [0])
    assert report["root_joint_index"] == 1
    assert report["frames"][0]["root_position"] == [1, 2, 3]
    with pytest.raises(ValueError, match="boolean"):
        motion_access.inspect_motion_frames(path, [0], include_rotations=1)


@pytest.mark.parametrize(
    "selection", [[], [0, 0], [2, 1], [-1], [2400], [True], [1.0], list(range(17)), [20]]
)
def test_bad_frame_selection(tmp_path, selection):
    with pytest.raises(ValueError):
        motion_access.inspect_motion_frames(motion(tmp_path / "motion.npz"), selection)


@pytest.mark.parametrize("selection", [[], ["Hips", "Hips"], ["unknown"], [0], [""], ["x"] * 33])
def test_bad_joint_selection(tmp_path, selection):
    with pytest.raises(ValueError):
        motion_access.inspect_motion_frames(motion(tmp_path / "motion.npz"), [0], joint_names=selection)


def test_duplicate_names_and_invalid_rotation_rejected(tmp_path):
    names, _ = _soma_layout(77)
    names[1] = names[0]
    path = motion(tmp_path / "motion.npz", joint_names=np.array(names))
    with pytest.raises(ValueError, match="unique"):
        motion_access.inspect_motion_frames(path, [0])
    for matrix in (np.diag([-1, 1, 1]), np.eye(3) * 0.5, np.full((3, 3), np.nan)):
        path = motion(path, local_rot_mats=np.broadcast_to(matrix, (20, 77, 3, 3)))
        with pytest.raises(ValueError):
            motion_access.inspect_motion_frames(path, [0], include_rotations=True)


@pytest.mark.parametrize("joints", [30, 77])
@pytest.mark.parametrize("kind", ["fullbody", "left-hand", "right-hand", "left-foot", "right-foot"])
def test_aggregate_matches_existing_extractor_and_validates_request(tmp_path, joints, kind, monkeypatch):
    path = motion(tmp_path / "motion.npz", joints=joints)
    before = path.read_bytes()
    expected = [
        extract_pose_constraint(path, source_frame=source, target_frame=target, constraint_type=kind)
        for source, target in zip([1, 4, 19], [0, 10, 20])
    ]
    original = motion_access._load_motion
    calls = []
    monkeypatch.setattr(motion_access, "_load_motion", lambda path: (calls.append(path), original(path))[1])
    report = motion_access.extract_motion_constraints(path, [1, 4, 19], [0, 10, 20], constraint_type=kind)
    constraint = report["constraint"]
    assert len(calls) == 1
    assert constraint["frame_indices"] == [0, 10, 20]
    for key in ("local_joints_rot", "root_positions", "smooth_root_2d"):
        assert constraint[key] == [getattr(pose, key)[0] for pose in expected]
    GenerateRequest(segments=[{"text": "Walk", "duration": 1}], constraints=[constraint])
    assert path.read_bytes() == before


@pytest.mark.parametrize(
    "source,target",
    [
        ([], []),
        ([0], [0, 1]),
        ([1, 0], [0, 1]),
        ([0, 1], [1, 1]),
        ([0], [2400]),
        ([20], [0]),
        (list(range(129)), list(range(129))),
    ],
)
def test_constraint_selectors_rejected(tmp_path, source, target):
    with pytest.raises(ValueError):
        motion_access.extract_motion_constraints(motion(tmp_path / "motion.npz"), source, target)


def test_unsupported_rigs_and_inconsistent_metadata_rejected(tmp_path):
    path = motion(tmp_path / "motion.npz")
    with pytest.raises(ValueError, match="SOMA"):
        motion_access.extract_motion_constraints(path, [0], [0], target_model="kimodo-g1-rp")
    with pytest.raises(ValueError, match="Unsupported"):
        motion_access.extract_motion_constraints(path, [0], [0], constraint_type="root2d")
    for changes in (
        {"skeleton": np.array("unknown")},
        {"model": np.array("kimodo-g1-rp")},
        {"local_rot_mats": None},
        {"joint_names": None},
    ):
        with pytest.raises(ValueError):
            motion_access.extract_motion_constraints(motion(path, **changes), [0], [0])


def test_multisample_rejected_by_both_helpers(tmp_path):
    path = motion(
        tmp_path / "motion.npz",
        posed_joints=np.zeros((2, 20, 77, 3)),
        local_rot_mats=None,
        global_rot_mats=None,
        foot_contacts=None,
    )
    with pytest.raises(ValueError, match="individual sample"):
        motion_access.inspect_motion_frames(path, [0])
    with pytest.raises(ValueError, match="individual sample"):
        motion_access.extract_motion_constraints(path, [0], [0])


def test_128_rows_and_request_time_and_shared_budget_checks(tmp_path):
    path = motion(tmp_path / "motion.npz", frames=128)
    report = motion_access.extract_motion_constraints(path, list(range(128)), list(range(128)))
    assert len(report["constraint"]["frame_indices"]) == 128
    with pytest.raises(ValueError):
        GenerateRequest(segments=[{"text": "Walk", "duration": 1}], constraints=[report["constraint"]])
    constraints = [
        {**report["constraint"], "type": kind}
        for kind in ("fullbody", "left-hand", "right-hand", "left-foot", "right-foot")
    ]
    with pytest.raises(ValueError, match="512"):
        GenerateRequest(segments=[{"text": "Walk", "duration": 5}], constraints=constraints)
