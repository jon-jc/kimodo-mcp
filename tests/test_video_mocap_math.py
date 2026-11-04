"""Synthetic mathematical cases; these tests do not perform video inference."""

import ast
import copy
import math
import re
from pathlib import Path

import numpy as np
import pytest

from kimodo_mcp.video_mocap_math import (
    CAMERA_TO_Y_UP,
    SMPL_NAMES,
    SMPL_PARENTS,
    axis_angle_matrices,
    matrices_to_xyz,
    normalize_smpl,
    write_bvh,
)


def xyz_matrix(angles):
    x, y, z = angles
    rx = np.array([[1, 0, 0], [0, math.cos(x), -math.sin(x)], [0, math.sin(x), math.cos(x)]])
    ry = np.array([[math.cos(y), 0, math.sin(y)], [0, 1, 0], [-math.sin(y), 0, math.cos(y)]])
    rz = np.array([[math.cos(z), -math.sin(z), 0], [math.sin(z), math.cos(z), 0], [0, 0, 1]])
    return rx @ ry @ rz


def fixture(frames=5):
    # Independent homogeneous-transform reference, including a nonzero pelvis.
    rest = np.zeros((24, 3))
    rest[0] = [0.13, 0.85, -0.09]
    for joint, parent in enumerate(SMPL_PARENTS[1:], 1):
        rest[joint] = rest[parent] + [0.02 * (-1 if joint % 2 else 1), 0.06, 0.01]
    poses = np.zeros((frames, 72))
    poses[:, 3:] = np.linspace(-0.12, 0.18, frames * 69).reshape(frames, 69)
    rh = np.array([[0.1 + frame / 20, -0.2, 0.3] for frame in range(frames)])
    th = np.array([[0.5 + frame / 10, -1.1, 4] for frame in range(frames)])
    params = {"poses": poses, "Rh": rh, "Th": th, "shapes": np.zeros((1, 10))}
    local = axis_angle_matrices(poses.reshape(frames, 24, 3))
    root_rotation = axis_angle_matrices(rh)
    fitted = np.empty((frames, 24, 3))
    for frame in range(frames):
        transforms = []
        for joint, parent in enumerate(SMPL_PARENTS):
            matrix = np.eye(4)
            matrix[:3, :3] = local[frame, joint]
            matrix[:3, 3] = rest[joint] if parent == -1 else rest[joint] - rest[parent]
            transforms.append(matrix if parent == -1 else transforms[parent] @ matrix)
            fitted[frame, joint] = root_rotation[frame] @ transforms[joint][:3, 3] + th[frame]
    return params, rest, fitted


def test_normalization_matches_independent_transforms_and_preserves_inputs():
    params, rest, fitted = fixture()
    snapshot = copy.deepcopy((params, rest, fitted))
    motion = normalize_smpl(params, rest, fitted, fps=29.97)
    np.testing.assert_allclose(motion["posed_joints"], fitted @ CAMERA_TO_Y_UP.T, atol=1e-12)
    np.testing.assert_allclose(motion["root_positions"], fitted[:, 0] @ CAMERA_TO_Y_UP.T, atol=1e-12)
    np.testing.assert_allclose(
        motion["local_rot_mats"][:, 0], CAMERA_TO_Y_UP @ axis_angle_matrices(params["Rh"])
    )
    assert np.linalg.norm(motion["root_positions"][0] - CAMERA_TO_Y_UP @ params["Th"][0]) > 0.5
    assert np.linalg.det(CAMERA_TO_Y_UP) == 1
    assert motion["fk_max_error_m"] < 1e-12
    assert str(motion["skeleton"]) == "smpl24"
    for key in params:
        np.testing.assert_array_equal(params[key], snapshot[0][key])
    np.testing.assert_array_equal(rest, snapshot[1])
    np.testing.assert_array_equal(fitted, snapshot[2])
    for joint, parent in enumerate(SMPL_PARENTS[1:], 1):
        np.testing.assert_allclose(
            np.linalg.norm(motion["posed_joints"][:, joint] - motion["posed_joints"][:, parent], axis=1),
            np.linalg.norm(rest[joint] - rest[parent]),
            atol=1e-12,
        )


def test_69_pose_components_and_shared_or_repeated_shape_are_equivalent():
    params, rest, fitted = fixture()
    original = normalize_smpl(params, rest, fitted, fps=30)
    params["poses"] = params["poses"][:, 3:]
    params["shapes"] = params["shapes"].repeat(len(fitted), axis=0)
    params["scale"] = np.array([1.0])
    result = normalize_smpl(params, rest, fitted, fps=30)
    np.testing.assert_array_equal(result["posed_joints"], original["posed_joints"])


@pytest.mark.parametrize(
    "case", ["root", "shape", "scale", "nan", "bool", "body25", "mismatch", "fps", "timeline"]
)
def test_incompatible_fitted_results_are_rejected(case):
    params, rest, fitted = fixture()
    fps = 30
    if case == "root":
        params["poses"][0, 0] = 0.2
    elif case == "shape":
        params["shapes"] = np.zeros((5, 10))
        params["shapes"][1, 0] = 0.1
    elif case == "scale":
        params["scale"] = np.array([2.0])
    elif case == "nan":
        params["Rh"][0, 0] = np.nan
    elif case == "bool":
        fps = True
    elif case == "body25":
        fitted = np.zeros((5, 25, 3))
    elif case == "mismatch":
        fitted[2, 23, 0] += 0.001
    elif case == "fps":
        fps = 0
    elif case == "timeline":
        params["Th"] = params["Th"][:-1]
    with pytest.raises(ValueError):
        normalize_smpl(params, rest, fitted, fps=fps)


def test_rodrigues_zero_near_zero_and_pi_are_proper_rotations():
    vectors = np.array([[0, 0, 0], [1e-10, -2e-10, 3e-10], [math.pi, 0, 0], [0, 0, -math.pi]])
    rotations = axis_angle_matrices(vectors)
    np.testing.assert_array_equal(rotations[0], np.eye(3))
    np.testing.assert_allclose(rotations[2], np.diag([1, -1, -1]), atol=1e-12)
    np.testing.assert_allclose(
        rotations @ rotations.transpose(0, 2, 1), np.broadcast_to(np.eye(3), rotations.shape), atol=1e-12
    )
    np.testing.assert_allclose(np.linalg.det(rotations), 1, atol=1e-12)


def test_xyz_euler_roundtrip_is_correct_at_gimbal_lock_and_across_branch_cut():
    angles = [
        [0.4, 1.5, -0.7],
        [0.4, math.pi / 2, -0.7],
        [0.4, 1.65, -0.7],
        [-0.3, -math.pi / 2, 0.8],
        [0, 0, math.radians(179)],
        [0, 0, math.radians(181)],
    ]
    matrices = np.array([xyz_matrix(item) for item in angles])[:, None]
    result = matrices_to_xyz(matrices)
    for original, euler in zip(matrices[:, 0], result[:, 0]):
        np.testing.assert_allclose(xyz_matrix(euler), original, atol=1e-7)
    assert np.max(np.abs(result[2, 0] - result[1, 0])) < 0.1
    assert abs(result[-1, 0, 2] - result[-2, 0, 2]) < 0.1


def parse_bvh(text):
    """Small independent parser for the emitted BVH hierarchy and channel stream."""
    hierarchy, motion = text.split("MOTION\n")
    tokens = re.findall(r"[^\s{}]+|[{}]", hierarchy)
    cursor, names, parents, offsets, channels = 1, [], [], [], []

    def joint(parent):
        nonlocal cursor
        token = tokens[cursor]
        cursor += 1
        if token == "End":
            assert tokens[cursor : cursor + 3] == ["Site", "{", "OFFSET"]
            cursor += 7
            return
        assert token in {"ROOT", "JOINT"}
        index = len(names)
        names.append(tokens[cursor])
        parents.append(parent)
        cursor += 1
        assert tokens[cursor : cursor + 2] == ["{", "OFFSET"]
        cursor += 2
        offsets.append([float(item) for item in tokens[cursor : cursor + 3]])
        cursor += 3
        assert tokens[cursor] == "CHANNELS"
        count = int(tokens[cursor + 1])
        cursor += 2
        channels.append(tokens[cursor : cursor + count])
        cursor += count
        while tokens[cursor] != "}":
            joint(index)
        cursor += 1

    joint(-1)
    lines = motion.splitlines()
    count = int(lines[0].split(":")[1])
    interval = float(lines[1].split(":")[1])
    values = np.array([[float(item) for item in line.split()] for line in lines[2:]])
    assert len(values) == count
    return names, parents, np.array(offsets), channels, interval, values


def test_bvh_reimport_preserves_every_joint_position_rotation_and_timing(tmp_path):
    params, rest, fitted = fixture(frames=9)
    motion = normalize_smpl(params, rest, fitted, fps=29.97)
    output = tmp_path / "motion.bvh"
    write_bvh(motion, output)
    names, parents, offsets, channels, interval, rows = parse_bvh(output.read_text())
    assert names != list(SMPL_NAMES)  # BVH hierarchy traverses depth-first.
    assert set(names) == set(SMPL_NAMES) and interval == pytest.approx(1 / 29.97, abs=1e-12)
    assert channels[0] == ["Xposition", "Yposition", "Zposition", "Xrotation", "Yrotation", "Zrotation"]
    assert all(item == ["Xrotation", "Yrotation", "Zrotation"] for item in channels[1:])
    assert rows.shape == (9, 75)
    for frame, row in enumerate(rows):
        global_rotations, positions = [], []
        for index, name in enumerate(names):
            local = xyz_matrix(np.deg2rad(row[3 + index * 3 : 6 + index * 3]))
            parent = parents[index]
            global_rotations.append(local if parent < 0 else global_rotations[parent] @ local)
            positions.append(
                offsets[index] + row[:3]
                if parent < 0
                else positions[parent] + global_rotations[parent] @ offsets[index]
            )
            target = SMPL_NAMES.index(name)
            np.testing.assert_allclose(positions[-1], motion["posed_joints"][frame, target], atol=1e-8)
            np.testing.assert_allclose(
                global_rotations[-1], motion["global_rot_mats"][frame, target], atol=1e-8
            )
    contents = output.read_bytes()
    with pytest.raises(FileExistsError):
        write_bvh(motion, output)
    assert output.read_bytes() == contents


def test_bvh_rejects_wrong_hierarchy_and_inconsistent_motion_before_writing(tmp_path):
    params, rest, fitted = fixture()
    motion = normalize_smpl(params, rest, fitted, fps=30)
    motion["posed_joints"][0, 2, 0] += 0.1
    with pytest.raises(ValueError, match="same motion"):
        write_bvh(motion, tmp_path / "wrong.bvh")
    assert not list(tmp_path.iterdir())
    motion["joint_parents"][2] = 1
    with pytest.raises(ValueError, match="hierarchy"):
        write_bvh(motion, tmp_path / "wrong.bvh")


def test_external_runtime_module_is_python39_and_numpy_only():
    import kimodo_mcp.video_mocap_math as module

    tree = ast.parse(Path(module.__file__).read_text(), feature_version=(3, 9))
    imported = {node.module for node in ast.walk(tree) if isinstance(node, ast.ImportFrom)}
    imported.update(
        alias.name for node in ast.walk(tree) if isinstance(node, ast.Import) for alias in node.names
    )
    assert imported <= {"math", "pathlib", "numpy"}
