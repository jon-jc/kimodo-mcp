"""NumPy-only SMPL24 normalization and BVH export, with independent FK checks.

This module can be imported by an external Python 3.9 fitting runtime without
loading the MCP package or model assets. Camera conversion is a proper rotation:
OpenCV +X right, +Y down, +Z away becomes +X right, +Y up, -Z away.
It establishes no floor alignment, heading, measured scale or contact labels.
"""

import math
from pathlib import Path

import numpy as np

SMPL_NAMES = (
    "Pelvis",
    "L_Hip",
    "R_Hip",
    "Spine1",
    "L_Knee",
    "R_Knee",
    "Spine2",
    "L_Ankle",
    "R_Ankle",
    "Spine3",
    "L_Foot",
    "R_Foot",
    "Neck",
    "L_Collar",
    "R_Collar",
    "Head",
    "L_Shoulder",
    "R_Shoulder",
    "L_Elbow",
    "R_Elbow",
    "L_Wrist",
    "R_Wrist",
    "L_Hand",
    "R_Hand",
)
SMPL_PARENTS = np.array(
    [-1, 0, 0, 0, 1, 2, 3, 4, 5, 6, 7, 8, 9, 9, 9, 12, 13, 14, 16, 17, 18, 19, 20, 21], dtype=np.int64
)
CAMERA_TO_Y_UP = np.diag([1.0, -1.0, -1.0])
FK_TOLERANCE_M = 0.0001


def _numeric(value, name):
    array = np.asarray(value)
    if array.dtype.kind not in "iuf" or not np.isfinite(array).all():
        raise ValueError(name + " must contain finite real numbers")
    return array.astype(np.float64, copy=True)


def _fps(value):
    array = _numeric(value, "fps")
    if array.shape != () or not 1.0 <= float(array) <= 240.0:
        raise ValueError("fps must be a finite scalar from 1 to 240")
    return float(array)


def axis_angle_matrices(vectors):
    """Rodrigues rotation with stable small-angle coefficients, including zero."""
    vectors = _numeric(vectors, "axis-angle rotations")
    if vectors.shape[-1:] != (3,) or np.max(np.abs(vectors), initial=0) > 100:
        raise ValueError("Axis-angle vectors must end in three bounded components")
    squared = np.sum(vectors * vectors, axis=-1)
    theta = np.sqrt(squared)
    small = squared < 1e-12
    a = np.divide(np.sin(theta), theta, out=np.ones_like(theta), where=~small)
    b = np.divide(1 - np.cos(theta), squared, out=np.full_like(theta, 0.5), where=~small)
    a = np.where(small, 1 - squared / 6 + squared * squared / 120, a)
    b = np.where(small, 0.5 - squared / 24 + squared * squared / 720, b)
    skew = np.zeros(vectors.shape[:-1] + (3, 3), dtype=np.float64)
    x, y, z = np.moveaxis(vectors, -1, 0)
    skew[..., 0, 1], skew[..., 0, 2] = -z, y
    skew[..., 1, 0], skew[..., 1, 2] = z, -x
    skew[..., 2, 0], skew[..., 2, 1] = -y, x
    return np.eye(3) + a[..., None, None] * skew + b[..., None, None] * (skew @ skew)


def _rotations(value, shape, name):
    rotations = _numeric(value, name)
    if rotations.shape != shape:
        raise ValueError(name + " has an unexpected shape")
    gram = np.swapaxes(rotations, -1, -2) @ rotations
    if not np.allclose(gram, np.eye(3), atol=1e-5, rtol=0) or not np.allclose(
        np.linalg.det(rotations), 1, atol=1e-5, rtol=0
    ):
        raise ValueError(name + " must contain proper orthonormal rotations")
    return rotations


def forward_kinematics(local_rotations, root_positions, rest_joints):
    """SMPL24 FK with fixed shaped rest offsets and explicit world root position."""
    root = _numeric(root_positions, "root positions")
    rest = _numeric(rest_joints, "rest joints")
    if root.ndim != 2 or root.shape[1] != 3 or not 1 <= len(root) <= 2400 or rest.shape != (24, 3):
        raise ValueError("Expected 1..2400 root positions and 24 rest joints")
    if np.max(np.abs(root)) > 1e6 or np.max(np.abs(rest)) > 1e6:
        raise ValueError("SMPL coordinates exceed the supported finite spatial bounds")
    local = _rotations(local_rotations, (len(root), 24, 3, 3), "local rotations")
    positions = np.zeros((len(root), 24, 3), dtype=np.float64)
    global_ = np.empty_like(local)
    positions[:, 0], global_[:, 0] = root, local[:, 0]
    for joint, parent in enumerate(SMPL_PARENTS[1:], 1):
        global_[:, joint] = global_[:, parent] @ local[:, joint]
        offset = rest[joint] - rest[parent]
        positions[:, joint] = positions[:, parent] + np.einsum("tij,j->ti", global_[:, parent], offset)
    return positions, global_


def normalize_smpl(params, rest_joints, upstream_joints, *, fps):
    """Normalize EasyMocap parameters and verify every joint against fitted output.

    rest_joints must be the SAME fitted, fixed-shape SMPL24 rest joints used to
    produce upstream_joints. The latter must be model joints, not BODY25 regressed
    landmarks. Rh is separate global orientation; a 72-vector's root must be zero.
    """
    rate = _fps(fps)
    rh, th = _numeric(params["Rh"], "Rh"), _numeric(params["Th"], "Th")
    poses, shapes = _numeric(params["poses"], "poses"), _numeric(params["shapes"], "shapes")
    rest, fitted = _numeric(rest_joints, "rest joints"), _numeric(upstream_joints, "fitted joints")
    if rh.ndim != 2 or rh.shape[1] != 3 or not 1 <= len(rh) <= 2400:
        raise ValueError("Rh must contain 1..2400 three-component frame rotations")
    frames = len(rh)
    if th.shape != (frames, 3) or poses.shape not in ((frames, 69), (frames, 72)):
        raise ValueError("Rh, Th and 69/72 pose components must share the complete frame timeline")
    if shapes.shape not in ((1, 10), (frames, 10)) or not np.allclose(shapes, shapes[:1], atol=1e-6, rtol=0):
        raise ValueError("SMPL24 export requires one fixed ten-coefficient body shape")
    if "scale" in params and not np.allclose(_numeric(params["scale"], "scale"), 1.0, atol=1e-8, rtol=0):
        raise ValueError("Non-unit fitting scale must be resolved explicitly before SMPL24 export")
    if rest.shape != (24, 3) or fitted.shape != (frames, 24, 3):
        raise ValueError(
            "Expected the exact 24 SMPL rest and fitted model joints, not regressed BODY25 landmarks"
        )
    if poses.shape[1] == 72:
        if np.max(np.abs(poses[:, :3])) > 1e-7:
            raise ValueError("EasyMocap root pose must be zero when global Rh is separate")
        body = poses[:, 3:]
    else:
        body = poses
    local = np.empty((frames, 24, 3, 3), dtype=np.float64)
    global_root = axis_angle_matrices(rh)
    local[:, 0] = CAMERA_TO_Y_UP @ global_root
    local[:, 1:] = axis_angle_matrices(body.reshape(frames, 23, 3))
    # EasyMocap rotates LBS output around the model origin, not around its pelvis.
    # Replacing Rh with the root's local rotation without this center term shifts it.
    root = (np.einsum("tij,j->ti", global_root, rest[0]) + th) @ CAMERA_TO_Y_UP.T
    positions, global_ = forward_kinematics(local, root, rest)
    expected = fitted @ CAMERA_TO_Y_UP.T
    error = float(np.linalg.norm(positions - expected, axis=-1).max())
    if error > FK_TOLERANCE_M:
        raise ValueError("SMPL24 FK disagrees with upstream fitted joints (maximum %.9g m)" % error)
    return {
        "posed_joints": positions,
        "global_rot_mats": global_,
        "local_rot_mats": local,
        "root_positions": root,
        "rest_joints": rest,
        "joint_names": np.array(SMPL_NAMES),
        "joint_parents": SMPL_PARENTS.copy(),
        "fps": np.array(rate),
        "skeleton": np.array("smpl24"),
        "coordinate_transform": CAMERA_TO_Y_UP.copy(),
        "fk_max_error_m": error,
    }


def matrices_to_xyz(rotations):
    """Continuous intrinsic XYZ Euler channels for R=Rx(x)@Ry(y)@Rz(z)."""
    array = np.asarray(rotations)
    if array.ndim != 4 or array.shape[-2:] != (3, 3) or not len(array):
        raise ValueError("Euler conversion requires a frame/joint/3/3 rotation array")
    array = _rotations(array, array.shape, "Euler input rotations")
    result = np.empty(array.shape[:2] + (3,), dtype=np.float64)
    for frame in range(len(array)):
        for joint, rotation in enumerate(array[frame]):
            y = math.asin(float(np.clip(rotation[0, 2], -1, 1)))
            previous = result[frame - 1, joint] if frame else np.zeros(3)
            if abs(math.cos(y)) < 1e-7:
                z = previous[2]
                x = math.atan2(rotation[2, 1], rotation[1, 1]) - (1 if y >= 0 else -1) * z
                candidates = np.array([[x, y, z]])
            else:
                x = math.atan2(-rotation[1, 2], rotation[2, 2])
                z = math.atan2(-rotation[0, 1], rotation[0, 0])
                candidates = np.array([[x, y, z], [x + math.pi, math.pi - y, z + math.pi]])
            candidates += 2 * math.pi * np.round((previous - candidates) / (2 * math.pi))
            result[frame, joint] = candidates[np.argmin(np.sum((candidates - previous) ** 2, axis=1))]
    return result


def write_bvh(motion, output_path):
    """Write a new SMPL24 BVH in meters; root offset is zero, channels are XYZ.

    Depth-first channel order follows the emitted hierarchy, not SMPL index order.
    The motion must be the verified normalization output with its shaped rest joints.
    """
    rate = _fps(motion["fps"])
    if tuple(np.asarray(motion["joint_names"]).tolist()) != SMPL_NAMES or not np.array_equal(
        np.asarray(motion["joint_parents"]), SMPL_PARENTS
    ):
        raise ValueError("BVH export requires the canonical SMPL24 names and hierarchy")
    rest = _numeric(motion["rest_joints"], "rest joints")
    root = _numeric(motion["root_positions"], "root positions")
    positions, _ = forward_kinematics(motion["local_rot_mats"], root, rest)
    supplied = _numeric(motion["posed_joints"], "posed joints")
    if (
        supplied.shape != positions.shape
        or np.max(np.linalg.norm(supplied - positions, axis=-1)) > FK_TOLERANCE_M
    ):
        raise ValueError("BVH input positions and rotations do not describe the same motion")
    eulers = np.rad2deg(matrices_to_xyz(motion["local_rot_mats"]))
    lines, order = ["HIERARCHY"], []

    def hierarchy(joint, depth):
        indent = "  " * depth
        order.append(joint)
        lines.extend([indent + ("ROOT " if joint == 0 else "JOINT ") + SMPL_NAMES[joint], indent + "{"])
        offset = np.zeros(3) if joint == 0 else rest[joint] - rest[SMPL_PARENTS[joint]]
        lines.append(indent + "  OFFSET " + " ".join("%.10f" % item for item in offset))
        lines.append(
            indent
            + "  CHANNELS "
            + ("6 Xposition Yposition Zposition " if joint == 0 else "3 ")
            + "Xrotation Yrotation Zrotation"
        )
        children = np.flatnonzero(SMPL_PARENTS == joint)
        if not len(children):
            lines.extend([indent + "  End Site", indent + "  {", indent + "    OFFSET 0 0 0", indent + "  }"])
        for child in children:
            hierarchy(int(child), depth + 1)
        lines.append(indent + "}")

    hierarchy(0, 0)
    lines.extend(["MOTION", "Frames: %d" % len(root), "Frame Time: %.12f" % (1.0 / rate)])
    for frame in range(len(root)):
        values = np.concatenate([root[frame], eulers[frame, order].reshape(-1)])
        lines.append(" ".join("%.10f" % item for item in values))
    # Exclusive publication prevents accidental overwrite; the managed job owner
    # remains responsible for directory containment and incomplete-job handling.
    with Path(output_path).open("x", encoding="utf-8", newline="\n") as stream:
        stream.write("\n".join(lines) + "\n")
