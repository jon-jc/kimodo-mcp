"""Compact rig inspection for clients selecting precise local motion corrections."""

from pathlib import Path

import numpy as np

from .motion_access import _single_motion
from .motion_variants import _read


def body_group(name: str) -> str:
    side = "left" if name.startswith("Left") else "right" if name.startswith("Right") else None
    if side:
        if any(part in name for part in ("Hand", "Thumb", "Index", "Middle", "Ring", "Pinky")):
            return side + "_hand"
        if any(part in name for part in ("Leg", "Shin", "Foot", "Toe")):
            return side + "_leg"
        if any(part in name for part in ("Arm", "Shoulder")):
            return side + "_arm"
    if any(part in name for part in ("Head", "Neck", "Jaw", "Eye")):
        return "head_neck"
    return "torso"


def inspect_rig(path: str | Path, *, original_path: str | Path, frame_index: int = 0) -> dict:
    motion = _single_motion(path)
    positions = motion["positions"][0]
    if type(frame_index) is not int or not 0 <= frame_index < len(positions):
        raise ValueError("Rig inspection frame is outside the selected motion.")
    names = motion["names"] if "joint_names" in motion["channels"] else None
    parents = motion["parents"]
    editable, reason = True, None
    try:
        _read(Path(original_path))
    except (ValueError, OSError, KeyError, TypeError) as error:
        editable, reason = False, str(error)
    rotations = motion["rotations"].get("global_rot_mats")
    joints, groups = [], {}
    for index in range(positions.shape[1]):
        name = names[index] if names else None
        parent = parents[index] if parents is not None else None
        # Group labels aid selection only; they do not infer an absent rig layout.
        group = body_group(name) if name and editable else "unclassified"
        groups.setdefault(group, []).append(index)
        joints.append({
            "index": index, "name": name, "parent_index": parent,
            "children": [i for i, p in enumerate(parents) if p == index] if parents is not None else None,
            "body_group": group, "position_m": positions[frame_index, index].tolist(),
            "bone_length_m": float(np.linalg.norm(positions[frame_index, index] - positions[frame_index, parent]))
            if parent is not None and parent >= 0 else None,
            "local_axes_in_world": rotations[0, frame_index, index].T.tolist() if rotations is not None else None,
            "rotation_adjustable": bool(editable and parent is not None and parent >= 0),
        })
    return {
        "schema_version": 1, "rig_kind": "source_animation_skeleton",
        "skeleton": motion["skeleton"], "model": motion["model"], "fps": motion["fps"],
        "frames": len(positions), "frame_index": frame_index,
        "coordinates": {"units": "meters", "up": "+Y", "forward": "+Z", "space": "world"},
        "joints": joints, "body_groups": groups,
        "editing": {
            "supported": editable, "unavailable_reason": reason, "tool": "create_motion_variant",
            "schema": "variant_edit", "max_adjusted_joints": 16,
            "local_rotation_order": "L_corrected = L_source @ Rx(x) @ Ry(y) @ Rz(z)",
            "angle_unit": "radians", "correction_limit_per_axis": [-float(np.pi / 2), float(np.pi / 2)],
            "frame_range_convention": "original_source_zero_based_half_open",
            "blend": "Smoothstep amplitude taper at both ends; default 3 frames; 0 explicitly disables taper.",
            "root": "Use yaw_radians, translation_xz and root_origin for planar root placement.",
            "bone_lengths_editable": False, "hierarchy_editable": False,
        },
        "notes": [
            "This is the source motion skeleton; a fitted character may expose fewer independent bones.",
            "Local axes are X, Y, Z directions in world space at the inspected frame, not Euler angles.",
            "Correction bounds are tool limits, not anatomical joint limits; inspect neighboring joints and contact.",
            "Corrections rebuild descendant positions, preserve source files and invalidate predicted foot contacts.",
            "On a trimmed variant, inspection frame i corresponds to original source start_frame + i.",
        ],
    }
