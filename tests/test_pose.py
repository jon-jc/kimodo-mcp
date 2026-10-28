import numpy as np
import pytest

from kimodo_mcp.pose import _matrix_to_axis_angle, _soma_layout, extract_pose_constraint
from kimodo_mcp.schemas import GenerateRequest


def rotation(vector):
    angle = np.linalg.norm(vector)
    if angle == 0:
        return np.eye(3)
    x, y, z = vector / angle
    cross = np.array([[0, -z, y], [z, 0, -x], [-y, x, 0]])
    return np.eye(3) + np.sin(angle) * cross + (1 - np.cos(angle)) * cross @ cross


def fixture(path, joints=77, **changes):
    names, parents = _soma_layout(joints)
    positions = np.zeros((2, 3, joints, 3))
    positions[1, 2, 0] = [4, 0.9, -3]
    rotations = np.broadcast_to(np.eye(3), (2, 3, joints, 3, 3)).copy()
    rotations[1, 2, 0] = rotation(np.array([0, 1.2, 0]))
    data = dict(
        posed_joints=positions,
        local_rot_mats=rotations,
        joint_names=np.array(names),
        joint_parents=np.array(parents),
        skeleton=np.array(f"somaskel{joints}"),
        fps=np.array(30),
    )
    data.update(changes)
    np.savez(path, **{key: value for key, value in data.items() if value is not None})
    return path


@pytest.mark.parametrize("joints", [30, 77])
def test_selected_pose_preserves_layout_world_root_and_destination(tmp_path, joints):
    path = fixture(tmp_path / "motion.npz", joints)
    before = path.read_bytes()
    pose = extract_pose_constraint(path, source_frame=2, sample_index=1, target_frame=5)
    assert pose.frame_indices == [5]
    assert pose.root_positions == [[4, 0.9, -3]]
    assert pose.smooth_root_2d == [[4, -3]]
    assert len(pose.local_joints_rot[0]) == joints
    np.testing.assert_allclose(pose.local_joints_rot[0][0], [0, 1.2, 0], atol=1e-12)
    GenerateRequest(segments=[{"text": "Continue walking", "duration": 1.0}], constraints=[pose])
    assert path.read_bytes() == before


@pytest.mark.parametrize("angle", [0.0, 1e-13, 1e-7, 1.3, np.pi - 1e-9, np.pi, np.pi + 1e-9])
@pytest.mark.parametrize("axis", [[1, 0, 0], [0, 1, 0], [0, 0, 1], [-1, 2, -3]])
def test_rotation_roundtrip_near_zero_and_pi(angle, axis):
    axis = np.asarray(axis, dtype=float)
    matrix = rotation(axis / np.linalg.norm(axis) * angle)
    vector = _matrix_to_axis_angle(matrix[None])[0]
    assert np.isfinite(vector).all()
    assert np.linalg.norm(vector) <= np.pi + 1e-12
    np.testing.assert_allclose(rotation(vector), matrix, atol=1e-12)


def test_float32_roundoff_allowed_but_scaling_and_reflection_rejected():
    matrix = rotation(np.array([1.7, -2, 0.4])).astype(np.float32)
    vector = _matrix_to_axis_angle(matrix[None])[0]
    np.testing.assert_allclose(rotation(vector), matrix, atol=1e-7)
    for invalid in [np.eye(3) * 0.99, np.diag([-1, 1, 1]), np.ones((3, 3))]:
        with pytest.raises(ValueError, match="proper rotation"):
            _matrix_to_axis_angle(invalid[None])


@pytest.mark.parametrize(
    "changes",
    [
        {"skeleton": np.array("g1")},
        {"joint_names": None},
        {"joint_parents": None},
        {"local_rot_mats": None},
    ],
)
def test_missing_or_unsupported_metadata_rejected(tmp_path, changes):
    with pytest.raises(ValueError):
        extract_pose_constraint(fixture(tmp_path / "bad.npz", **changes), source_frame=0)


def test_same_count_with_wrong_order_or_hierarchy_rejected(tmp_path):
    names, parents = _soma_layout(77)
    names[1], names[2] = names[2], names[1]
    with pytest.raises(ValueError, match="canonical"):
        extract_pose_constraint(fixture(tmp_path / "names.npz", joint_names=np.array(names)), source_frame=0)
    parents[2] = 0
    with pytest.raises(ValueError, match="canonical"):
        extract_pose_constraint(
            fixture(tmp_path / "parents.npz", joint_parents=np.array(parents)), source_frame=0
        )


@pytest.mark.parametrize(
    "arguments",
    [
        {"source_frame": -1},
        {"source_frame": True},
        {"source_frame": 3},
        {"source_frame": 0, "sample_index": 2},
        {"source_frame": 0, "target_frame": 2400},
        {"source_frame": 0, "target_frame": 1.0},
        {"source_frame": 0, "target_model": "kimodo-g1-rp"},
        {"source_frame": 0, "constraint_type": "root2d"},
    ],
)
def test_invalid_selection_and_targets_rejected(tmp_path, arguments):
    with pytest.raises(ValueError):
        extract_pose_constraint(fixture(tmp_path / "motion.npz"), **arguments)


def test_limb_constraint_and_request_conflict_checks(tmp_path):
    path = fixture(tmp_path / "motion.npz")
    pose = extract_pose_constraint(path, source_frame=2, sample_index=1, constraint_type="right-hand")
    assert pose.type == "right-hand" and pose.smooth_root_2d == [[4, -3]]
    with pytest.raises(ValueError, match="Conflicting"):
        GenerateRequest(
            segments=[{"text": "Wave", "duration": 1.0}],
            constraints=[
                pose,
                {"type": "root2d", "frame_indices": [0], "smooth_root_2d": [[0.0, 0.0]]},
            ],
        )
