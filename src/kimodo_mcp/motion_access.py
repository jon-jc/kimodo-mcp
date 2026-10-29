"""Bounded numeric access to managed motion and reusable pose constraints.

The server owns completed-job, digest and path checks. These helpers validate
array contents and selectors; they never alter the source or invoke inference.
"""

from pathlib import Path
from typing import Any

from .artifacts import _load_motion
from .catalog import model_info
from .pose import PoseType, _extract_loaded_pose, _matrix_to_axis_angle
from .schemas import PoseConstraint


def _frames(values: list[int], label: str, maximum_count: int) -> None:
    if not isinstance(values, list) or not 1 <= len(values) <= maximum_count:
        raise ValueError(f"{label} must contain 1 to {maximum_count} frame indices.")
    if any(type(value) is not int or not 0 <= value <= 2399 for value in values):
        raise ValueError(f"{label} must contain integer frame indices between 0 and 2399.")
    if any(left >= right for left, right in zip(values, values[1:])):
        raise ValueError(f"{label} must be unique and strictly increasing.")


def _single_motion(path: str | Path) -> dict[str, Any]:
    motion = _load_motion(path)
    samples, frames = motion["positions"].shape[:2]
    if samples != 1 or frames > 2400:
        raise ValueError("Motion access requires an individual sample with at most 2400 frames.")
    return motion


def inspect_motion_frames(
    path: str | Path,
    frame_indices: list[int],
    *,
    joint_names: list[str] | None = None,
    include_rotations: bool = False,
) -> dict[str, Any]:
    """Read up to 16 frames; at most 2048 joint poses, or 256 with rotations.

    All joints are returned by default. Explicit name selections allow up to
    32 unique names. Missing metadata remains unknown, without inferred names,
    timing, root indices or contact labels. Rotation matrices remain unmodified.
    """
    _frames(frame_indices, "frame_indices", 16)
    if type(include_rotations) is not bool:
        raise ValueError("include_rotations must be a boolean.")
    motion = _single_motion(path)
    _, frames, joints, _ = motion["positions"].shape
    if frame_indices[-1] >= frames:
        raise ValueError("Selected frame is outside the source motion.")
    names = motion["names"] if "joint_names" in motion["channels"] else None
    if names is not None and (len(set(names)) != len(names) or any(not name for name in names)):
        raise ValueError("Joint names must be nonempty and unique for unambiguous inspection.")
    indices = list(range(joints))
    if joint_names is not None:
        if (
            not isinstance(joint_names, list)
            or not 1 <= len(joint_names) <= 32
            or any(not isinstance(name, str) or not 1 <= len(name) <= 256 for name in joint_names)
            or len(set(joint_names)) != len(joint_names)
        ):
            raise ValueError("joint_names must contain 1 to 32 unique nonempty names.")
        if names is None:
            raise ValueError("Named selection requires explicit joint_names metadata.")
        unknown = [name for name in joint_names if name not in names]
        if unknown:
            raise ValueError(f"Unknown joint names: {unknown}")
        indices = [names.index(name) for name in joint_names]
    limit = 256 if include_rotations else 2048
    if len(indices) * len(frame_indices) > limit:
        raise ValueError(f"Selection exceeds {limit} joint poses; choose fewer frames or joint_names.")
    parents, fps = motion["parents"], motion["fps"]
    root_index = parents.index(-1) if parents is not None else None
    rows = []
    for frame in frame_indices:
        row = {
            "frame_index": frame,
            "time_seconds": frame / fps if fps is not None else None,
            "root_position": motion["positions"][0, frame, root_index].tolist()
            if root_index is not None
            else None,
            "joint_positions": motion["positions"][0, frame, indices].tolist(),
            "foot_contacts": motion["contacts"][0, frame].tolist()
            if motion["contacts"] is not None
            else None,
        }
        if include_rotations:
            for key, values in motion["rotations"].items():
                selected = values[0, frame, indices]
                _matrix_to_axis_angle(selected)  # Reject scaling/reflections, not just nonfinite values.
                row[key] = selected.tolist()
        rows.append(row)
    return {
        "schema_version": 1,
        "skeleton": motion["skeleton"],
        "model": motion["model"],
        "fps": fps,
        "frame_count": frames,
        "sample_count": 1,
        "coordinates": {"space": "world", "up_axis": "Y", "forward_axis": "+Z",
                        "units": "meters", "convention": "Kimodo"},
        "joint_names": names,
        "joint_parents": parents,
        "root_joint_index": root_index,
        "selected_joint_indices": indices,
        "selected_joint_names": [names[index] for index in indices] if names is not None else None,
        "available_rotation_channels": sorted(motion["rotations"]),
        "contact_channel_names": None,
        "frames": rows,
        "notes": [
            "Contact values retain source channel order; channel labels are unavailable.",
            "Absent names, hierarchy or FPS are returned as null; no metadata is inferred.",
        ],
    }


def extract_motion_constraints(
    path: str | Path,
    source_frames: list[int],
    target_frames: list[int],
    *,
    target_model: str = "kimodo-soma-rp-v1.2",
    constraint_type: PoseType = "fullbody",
) -> dict[str, Any]:
    """Map up to 128 source frames into one canonical SOMA pose constraint.

    This preserves explicit world poses, not velocity or unconstrained frames.
    The eventual GenerateRequest checks destination duration, conflicts and the
    shared 512-row budget across all constraints.
    """
    _frames(source_frames, "source_frames", 128)
    _frames(target_frames, "target_frames", 128)
    if len(source_frames) != len(target_frames):
        raise ValueError("source_frames and target_frames must have equal length.")
    motion = _single_motion(path)
    if motion["model"] is not None and model_info(motion["model"])["skeleton"] != "SOMA":
        raise ValueError("Source model metadata must identify a SOMA model.")
    poses = [
        _extract_loaded_pose(
            motion,
            source_frame=source,
            target_frame=target,
            target_model=target_model,
            constraint_type=constraint_type,
        )
        for source, target in zip(source_frames, target_frames)
    ]
    constraint = PoseConstraint(
        type=constraint_type,
        frame_indices=list(target_frames),
        local_joints_rot=[pose.local_joints_rot[0] for pose in poses],
        root_positions=[pose.root_positions[0] for pose in poses],
        smooth_root_2d=[pose.smooth_root_2d[0] for pose in poses],
    )
    return {
        "constraint": constraint.model_dump(mode="json"),
        "source_frames": list(source_frames),
        "target_frames": list(target_frames),
        "source_model": motion["model"],
        "target_model": target_model,
        "skeleton": motion["skeleton"],
        "fps": motion["fps"],
        "notes": [
            "World root XZ and source joint order are retained.",
            "This anchors poses, not velocity or seamless motion between keyed frames.",
            "Validate the complete request for duration, overlaps and the shared 512-row limit.",
            "SOMA77 finger detail is reduced to the model's SOMA30 controls by the upstream loader.",
        ],
    }
