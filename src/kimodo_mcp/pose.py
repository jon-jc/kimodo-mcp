"""Reuse a vetted motion frame as a Kimodo pose constraint, without PyTorch.

This is pose anchoring, not velocity matching or seamless clip stitching. SOMA
joint identifiers/hierarchy follow the reviewed upstream skeleton definitions;
the upstream constraint loader owns its 77-to-30 model conversion.
"""

from pathlib import Path
from typing import Literal

import numpy as np

from .artifacts import _load_motion
from .catalog import model_info
from .schemas import PoseConstraint

PoseType = Literal["fullbody", "left-hand", "right-hand", "left-foot", "right-foot"]


def _soma_layout(joints: int) -> tuple[list[str], list[int]]:
    """Describe supported layouts explicitly; never infer a rig from its count."""
    bones: list[tuple[str, str | None]] = [("Hips", None)]

    def chain(parent: str, names: list[str]) -> None:
        for name in names:
            bones.append((name, parent))
            parent = name

    chain("Hips", ["Spine1", "Spine2", "Chest", "Neck1", "Neck2", "Head"])
    if joints == 77:
        bones.append(("HeadEnd", "Head"))
    bones.extend((name, "Head") for name in ("Jaw", "LeftEye", "RightEye"))
    for side in ("Left", "Right"):
        chain("Chest", [side + name for name in ("Shoulder", "Arm", "ForeArm", "Hand")])
        hand = side + "Hand"
        if joints == 30:
            bones.extend((hand + name + "End", hand) for name in ("Thumb", "Middle"))
        else:
            for finger in ("Thumb", "Index", "Middle", "Ring", "Pinky"):
                segments = ["1", "2", "3"] + ([] if finger == "Thumb" else ["4"]) + ["End"]
                chain(hand, [hand + finger + segment for segment in segments])
    for side in ("Left", "Right"):
        names = ["Leg", "Shin", "Foot", "ToeBase"] + (["ToeEnd"] if joints == 77 else [])
        chain("Hips", [side + name for name in names])
    names = [name for name, _ in bones]
    return names, [-1 if parent is None else names.index(parent) for _, parent in bones]


def _matrix_to_axis_angle(matrices: np.ndarray) -> np.ndarray:
    """Project numerical roundoff only, then use a stable quaternion logarithm.

    The largest quaternion component branch avoids division by sin(angle),
    including at pi. Canonical positive scalar selects angles in [0, pi].
    """
    matrices = np.asarray(matrices, dtype=np.float64)
    if matrices.ndim != 3 or matrices.shape[1:] != (3, 3) or not np.isfinite(matrices).all():
        raise ValueError("Local rotations must be finite 3-by-3 matrices.")
    error = np.max(np.abs(matrices.swapaxes(-1, -2) @ matrices - np.eye(3)))
    if error > 1e-3 or np.max(np.abs(np.linalg.det(matrices) - 1)) > 1e-3:
        raise ValueError("Local rotations must be proper rotation matrices, without reflection or scaling.")
    u, _, vh = np.linalg.svd(matrices)
    corrected = u @ vh
    result = []
    for r in corrected:
        candidates = np.array(
            [
                1 + np.trace(r),
                1 + r[0, 0] - r[1, 1] - r[2, 2],
                1 - r[0, 0] + r[1, 1] - r[2, 2],
                1 - r[0, 0] - r[1, 1] + r[2, 2],
            ]
        )
        branch = int(candidates.argmax())
        q = np.empty(4)
        q[branch] = np.sqrt(max(candidates[branch], 0)) / 2
        divisor = 4 * q[branch]
        if branch == 0:
            q[1:] = [r[2, 1] - r[1, 2], r[0, 2] - r[2, 0], r[1, 0] - r[0, 1]]
            q[1:] /= divisor
        else:
            i = branch - 1
            j, k = (i + 1) % 3, (i + 2) % 3
            q[0] = (r[k, j] - r[j, k]) / divisor
            q[j + 1] = (r[j, i] + r[i, j]) / divisor
            q[k + 1] = (r[k, i] + r[i, k]) / divisor
        q /= np.linalg.norm(q)
        if q[0] < 0:
            q = -q
        sine = np.linalg.norm(q[1:])
        scale = 2.0 if sine < 1e-12 else 2 * np.arctan2(sine, q[0]) / sine
        result.append(q[1:] * scale)
    return np.asarray(result)


def extract_pose_constraint(
    path: str | Path,
    *,
    source_frame: int,
    target_frame: int = 0,
    sample_index: int = 0,
    target_model: str = "kimodo-soma-rp-v1.2",
    constraint_type: PoseType = "fullbody",
) -> PoseConstraint:
    """Return one validated constraint from a completed, operator-vetted NPZ.

    Indices are zero-based. World root position is retained in meters (Y up).
    Both SOMA layouts remain in their original canonical order: the inference
    loader selects its 30 model joints from a 77-joint pose by name. Detailed
    finger poses therefore are not independently controlled by the 30-joint
    model. Limb constraints also anchor planar root, as upstream requires.

    The caller must enforce completed-job/path provenance and validate the
    resulting full GenerateRequest for destination duration and conflicts.
    """
    if model_info(target_model)["skeleton"] != "SOMA":
        raise ValueError("Pose reuse currently supports only SOMA target models.")
    for label, value, maximum in (
        ("source_frame", source_frame, 2399),
        ("target_frame", target_frame, 2399),
        ("sample_index", sample_index, 15),
    ):
        if type(value) is not int or not 0 <= value <= maximum:
            raise ValueError(f"{label} must be an integer between 0 and {maximum}.")
    if constraint_type not in ("fullbody", "left-hand", "right-hand", "left-foot", "right-foot"):
        raise ValueError("Unsupported pose constraint type.")
    motion = _load_motion(path)
    samples, frames, joints, _ = motion["positions"].shape
    if frames > 2400 or source_frame >= frames or sample_index >= samples:
        raise ValueError("Selected sample/frame is out of bounds or motion exceeds 2400 frames.")
    if joints not in (30, 77) or motion["skeleton"] != f"somaskel{joints}":
        raise ValueError("Pose reuse requires an explicitly identified SOMA30 or SOMA77 skeleton.")
    names, parents = _soma_layout(joints)
    if motion["names"] != names or motion["parents"] != parents:
        raise ValueError("SOMA joint names, order, and hierarchy must match the canonical skeleton.")
    if "local_rot_mats" not in motion["rotations"]:
        raise ValueError("Pose reuse requires local_rot_mats; global rotations alone are insufficient.")
    rotations = motion["rotations"]["local_rot_mats"][sample_index, source_frame]
    root = motion["positions"][sample_index, source_frame, 0]
    return PoseConstraint(
        type=constraint_type,
        frame_indices=[target_frame],
        local_joints_rot=[_matrix_to_axis_angle(rotations).tolist()],
        root_positions=[root.tolist()],
        smooth_root_2d=[root[[0, 2]].tolist()],
    )
