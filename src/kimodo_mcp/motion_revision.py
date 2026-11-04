"""Exact, no-write correction previews and original-relative partial revisions."""

from __future__ import annotations

import math
from pathlib import Path
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, model_serializer, model_validator

from .artifacts import _check_archive
from .motion_variants import (
    Angle,
    EndFrame,
    Finite,
    Frame,
    JointAdjustment,
    VariantEdit,
    VariantRequest,
    _digest,
    _normalized,
    _offsets,
    _read,
    apply_edit,
    variant_id,
)

FrameSelection = Annotated[list[Frame], Field(max_length=16)]
JointName = Annotated[str, Field(strict=True, min_length=1, max_length=64)]


class _Patch(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    @model_validator(mode="before")
    @classmethod
    def no_explicit_null(cls, value):
        if isinstance(value, dict) and any(item is None for item in value.values()):
            raise ValueError("Omit unchanged fields; explicit null is not a correction value.")
        return value

    @model_serializer(mode="wrap")
    def omit_unchanged(self, handler):
        return {key: value for key, value in handler(self).items() if value is not None}


class JointAdjustmentRevision(_Patch):
    joint_name: JointName
    local_xyz_radians: Annotated[list[Angle], Field(min_length=3, max_length=3)] | None = None
    start_frame: Frame | None = None
    end_frame_exclusive: EndFrame | None = None
    blend_frames: Annotated[int, Field(strict=True, ge=0, le=30)] | None = None

    @model_validator(mode="after")
    def changed_field(self):
        if not self.model_fields_set - {"joint_name"}:
            raise ValueError("Choose at least one joint setting to revise.")
        return self


class VariantRevision(_Patch):
    start_frame: Frame | None = None
    end_frame_exclusive: EndFrame | None = None
    yaw_radians: Annotated[Finite, Field(ge=-math.pi, le=math.pi)] | None = None
    translation_xz: Annotated[
        list[Annotated[Finite, Field(ge=-1000, le=1000)]], Field(min_length=2, max_length=2)
    ] | None = None
    root_origin: Literal["world", "first_frame"] | None = None
    joint_updates: Annotated[list[JointAdjustmentRevision], Field(max_length=16)] = Field(default_factory=list)
    remove_joints: Annotated[list[JointName], Field(max_length=16)] = Field(default_factory=list)

    @model_validator(mode="after")
    def distinct_changes(self):
        names = [item.joint_name for item in self.joint_updates]
        if len(set(names)) != len(names) or len(set(self.remove_joints)) != len(self.remove_joints):
            raise ValueError("Joint updates and removals must each contain unique names.")
        if set(names) & set(self.remove_joints):
            raise ValueError("A joint cannot be updated and removed in the same revision.")
        fields = self.model_fields_set - {"joint_updates", "remove_joints"}
        if not fields and not names and not self.remove_joints:
            raise ValueError("Choose at least one correction setting to revise.")
        return self


def revise_edit(original: VariantEdit, revision: VariantRevision) -> dict:
    """Merge values, never add rotations or silently adjust source-frame windows."""
    before = original.model_dump(mode="json")
    values = original.model_dump(mode="json")
    updates = revision.model_dump(mode="json", exclude_unset=True)
    values.update({key: value for key, value in updates.items() if key not in {"joint_updates", "remove_joints"}})
    joints = {item["joint_name"]: item for item in values["joint_adjustments"]}
    for name in revision.remove_joints:
        if name not in joints:
            raise ValueError(f"Cannot remove a correction that is not present: {name}")
        del joints[name]
    for patch in revision.joint_updates:
        value = {**joints.get(patch.joint_name, {}), **patch.model_dump(mode="json", exclude_unset=True)}
        joints[patch.joint_name] = JointAdjustment.model_validate(value).model_dump(mode="json")
    values["joint_adjustments"] = list(joints.values())
    resolved = VariantEdit.model_validate(values).model_dump(mode="json")
    old = {item["joint_name"]: item for item in before["joint_adjustments"]}
    return {"edit": resolved,
            "changes": {key: {"before": before[key], "after": value} for key, value in resolved.items()
                        if before[key] != value},
            "joint_changes": {"added": [name for name in joints if name not in old],
                              "removed": [name for name in old if name not in joints],
                              "updated": [name for name in joints if name in old and joints[name] != old[name]]}}


def joint_weight(adjustment: JointAdjustment, frame: int) -> float:
    """The exact amplitude taper used by apply_edit, in original-source frames."""
    start, end = adjustment.start_frame, adjustment.end_frame_exclusive
    if not start <= frame < end:
        return 0.0
    if not adjustment.blend_frames:
        return 1.0
    edge = min(frame - start, end - 1 - frame) / adjustment.blend_frames
    edge = min(max(edge, 0.0), 1.0)
    return edge * edge * (3 - 2 * edge)


def preview_edit(source: Path, *, source_sha256: str, job_id: str, sample_index: int,
                 edit: VariantEdit, frame_indices: list[int] | None = None) -> dict:
    """Run the same FK as saved variants; publish no arrays, locks or receipts."""
    source = Path(source).resolve()
    prepared = VariantRequest(job_id=job_id, sample_index=sample_index, source_path=str(source),
                              source_sha256=source_sha256, edit=edit, output_directory="")
    if source.parent.name != job_id or source.name != f"motion_{sample_index:02d}.npz":
        raise ValueError("Correction previews require an original managed job sample.")
    requested = [] if frame_indices is None else frame_indices
    if not isinstance(requested, list) or len(requested) > 16 or any(type(index) is not int for index in requested):
        raise ValueError("Choose at most 16 integer original-source frame indices.")
    if len(set(requested)) != len(requested):
        raise ValueError("Preview frame indices must be unique.")
    _check_archive(source)
    if _digest(source) != source_sha256:
        raise ValueError("Original source differs from its completed-job digest.")
    arrays = _read(source)
    normalized = _normalized(edit, len(arrays["posed_joints"]))
    start, end = normalized.start_frame, normalized.end_frame_exclusive
    frames = requested or [start]
    if any(not start <= frame < end for frame in frames):
        raise ValueError("Preview frames must be original-source indices inside the retained trim.")
    output, details = apply_edit(arrays, normalized)
    try:
        offsets = _offsets(arrays).tolist()
        joint_editing = {"supported": True, "reason": None}
    except ValueError as error:
        offsets = None
        joint_editing = {"supported": False, "reason": str(error)}
    prepared.edit = normalized
    fps = float(arrays["fps"])
    selected = [{"original_frame": frame, "frame_index": frame - start, "time_seconds": (frame - start) / fps,
                 "positions_m": output["posed_joints"][frame - start].tolist(),
                 "global_rot_mats": output["global_rot_mats"][frame - start].tolist(),
                 "original_positions_m": arrays["posed_joints"][frame].tolist(),
                 "original_global_rot_mats": arrays["global_rot_mats"][frame].tolist(),
                 "joint_weights": {item.joint_name: joint_weight(item, frame) for item in normalized.joint_adjustments}}
                for frame in frames]
    if _digest(source) != source_sha256:
        raise ValueError("Source changed while previewing the correction.")
    return {"schema_version": 1, "kind": "motion_variant_preview", "saved": False,
            "job_id": job_id, "sample_index": sample_index,
            "source_sha256": source_sha256, "original_source_sha256": source_sha256,
            "predicted_variant_id": variant_id(prepared), **details,
            "fps": fps, "frames": end - start, "source_frames": len(arrays["posed_joints"]),
            "source_range": {"start_frame": start, "end_frame_exclusive": end},
            "skeleton": str(arrays["skeleton"]), "model": str(arrays["model"]),
            "joint_names": arrays["joint_names"].tolist(), "joint_parents": arrays["joint_parents"].tolist(),
            "parent_local_offsets": offsets, "joint_editing": joint_editing,
            "first_retained_root_position": arrays["root_positions"][start].tolist(),
            "coordinates": {"units": "meters", "up": "+Y", "forward": "+Z", "space": "world"},
            "frames_preview": selected,
            "notes": ["This is exact source-skeleton FK preview, not a saved artifact or fitted-character preview.",
                      "Frame indices and correction windows always refer to the original sample.",
                      "Offsets replace original-relative local XYZ values; no incremental rotation stacking occurs.",
                      "joint_weights are smoothstep amplitude factors; zero at tapered boundaries. No contact or anatomical repair runs."]}
