"""Immutable SOMA clip edits with explicit geometry and source provenance.

Trimming uses source-frame indices and an exclusive end. Joint corrections
rotate local axes and rebuild descendants through forward kinematics. This
module neither infers motion nor promises contact repair or seamless loops.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import tempfile
from pathlib import Path
from typing import Annotated, Literal

import numpy as np
from filelock import FileLock, Timeout
from pydantic import BaseModel, ConfigDict, Field, model_validator

from .artifacts import _check_archive
from .catalog import model_info
from .jobs import atomic_json
from .pose import _soma_layout

VERSION = 1
Finite = Annotated[float, Field(strict=True, allow_inf_nan=False)]
Frame = Annotated[int, Field(strict=True, ge=0, le=2399)]
EndFrame = Annotated[int, Field(strict=True, ge=1, le=2400)]
Angle = Annotated[Finite, Field(ge=-math.pi / 2, le=math.pi / 2)]
SHA = Annotated[str, Field(strict=True, pattern=r"^[0-9a-f]{64}$")]
NATIVE = {
    "posed_joints",
    "local_rot_mats",
    "global_rot_mats",
    "root_positions",
    "smooth_root_pos",
    "global_root_heading",
    "foot_contacts",
}
METADATA = {"fps", "model", "skeleton", "joint_names", "joint_parents"}


class JointAdjustment(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    joint_name: Annotated[str, Field(min_length=1, max_length=64)]
    local_xyz_radians: Annotated[list[Angle], Field(min_length=3, max_length=3)]
    start_frame: Frame
    end_frame_exclusive: EndFrame
    blend_frames: Annotated[int, Field(strict=True, ge=0, le=30)] = 3

    @model_validator(mode="after")
    def validate_window(self):
        if self.end_frame_exclusive - self.start_frame < 2 * self.blend_frames + 1:
            raise ValueError("Adjustment window must contain at least 2*blend_frames+1 source frames.")
        return self


class VariantEdit(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    start_frame: Frame = 0
    end_frame_exclusive: EndFrame | None = None
    yaw_radians: Annotated[Finite, Field(ge=-math.pi, le=math.pi)] = 0.0
    translation_xz: Annotated[
        list[Annotated[Finite, Field(ge=-1000, le=1000)]], Field(min_length=2, max_length=2)
    ] = Field(default_factory=lambda: [0.0, 0.0])
    root_origin: Literal["world", "first_frame"] = "world"
    joint_adjustments: Annotated[list[JointAdjustment], Field(max_length=16)] = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_edits(self):
        if self.end_frame_exclusive is not None and self.end_frame_exclusive - self.start_frame < 2:
            raise ValueError("Trim must retain at least two frames.")
        names = [adjustment.joint_name for adjustment in self.joint_adjustments]
        if len(set(names)) != len(names):
            raise ValueError("Each joint may have only one adjustment per variant.")
        for adjustment in self.joint_adjustments:
            if adjustment.start_frame < self.start_frame or (
                self.end_frame_exclusive is not None
                and adjustment.end_frame_exclusive > self.end_frame_exclusive
            ):
                raise ValueError("Joint adjustment source-frame windows must lie inside the retained trim.")
        return self


class VariantRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    schema_version: Literal[1] = 1
    job_id: Annotated[str, Field(pattern=r"^[0-9a-f]{32}$")]
    sample_index: Annotated[int, Field(ge=0, le=3)] = 0
    source_path: str
    source_sha256: SHA
    edit: VariantEdit
    output_directory: str


def _digest(path: Path) -> str:
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def variant_id(request: VariantRequest) -> str:
    value = {
        "transform_version": VERSION,
        "source_sha256": request.source_sha256,
        "sample_index": request.sample_index,
        "edit": request.edit.model_dump(mode="json"),
    }
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()
    ).hexdigest()


def _paths(request: VariantRequest) -> tuple[Path, Path]:
    source = Path(request.source_path).resolve()
    output = Path(request.output_directory).resolve()
    if source.name != f"motion_{request.sample_index:02d}.npz" or source.parent.name != request.job_id:
        raise ValueError("Variants require an original managed job sample; source chains are not supported.")
    if output != source.parent / "variants" / variant_id(request) or not output.is_relative_to(source.parent):
        raise ValueError("Variant output must be assigned inside its source job.")
    return source, output


def _read(path: Path, *, contacts_required: bool = True) -> dict[str, np.ndarray]:
    _check_archive(path)
    with np.load(path, allow_pickle=False) as archive:
        arrays = {key: archive[key] for key in archive.files}
    _validate(arrays, contacts_required=contacts_required)
    return arrays


def _validate(arrays: dict, *, contacts_required: bool = True) -> None:
    required = NATIVE | METADATA
    if not contacts_required:
        required -= {"foot_contacts"}
    if not required.issubset(arrays) or set(arrays) - NATIVE - METADATA:
        raise ValueError(
            "Motion requires known native channels and metadata; unknown or missing channels are rejected."
        )
    positions = arrays["posed_joints"]
    if positions.ndim != 3 or positions.shape[-1] != 3:
        raise ValueError("Variant input must contain a single unbatched motion sample.")
    frames, joints, _ = positions.shape
    if not 2 <= frames <= 2400 or joints not in (30, 77):
        raise ValueError("Variants require 2..2400 frames and a SOMA30 or SOMA77 skeleton.")
    for key in ("model", "skeleton"):
        if arrays[key].shape != () or arrays[key].dtype.kind != "U":
            raise ValueError(f"{key} must be explicit scalar Unicode metadata.")
    if (
        model_info(str(arrays["model"]))["skeleton"] != "SOMA"
        or str(arrays["skeleton"]) != f"somaskel{joints}"
    ):
        raise ValueError("Variant model and skeleton must both identify canonical SOMA.")
    names, parents = _soma_layout(joints)
    if arrays["joint_names"].tolist() != names or arrays["joint_parents"].tolist() != parents:
        raise ValueError("SOMA joint names, order and parents must match the canonical layout.")
    if arrays["joint_parents"].dtype.kind not in "iu" or arrays["joint_names"].dtype.kind != "U":
        raise ValueError("Skeleton names and parents have invalid types.")
    fps = arrays["fps"]
    if fps.shape != () or fps.dtype.kind not in "fiu" or not np.isfinite(fps) or not 1 <= float(fps) <= 240:
        raise ValueError("FPS must be explicit, finite and between 1 and 240.")
    shapes = {
        "posed_joints": (frames, joints, 3),
        "local_rot_mats": (frames, joints, 3, 3),
        "global_rot_mats": (frames, joints, 3, 3),
        "root_positions": (frames, 3),
        "smooth_root_pos": (frames, 3),
        "global_root_heading": (frames, 2),
    }
    for key, shape in shapes.items():
        values = arrays[key]
        if values.shape != shape or values.dtype.kind != "f" or not np.isfinite(values).all():
            raise ValueError(f"{key} must contain finite floating-point values with shape {shape}.")
    for key in ("posed_joints", "root_positions", "smooth_root_pos"):
        if np.max(np.abs(arrays[key])) > 1_000_000:
            raise ValueError("Motion coordinates exceed the supported range.")
    for key in ("local_rot_mats", "global_rot_mats"):
        rotations = arrays[key].astype(np.float64)
        if (
            np.max(np.abs(rotations.swapaxes(-1, -2) @ rotations - np.eye(3))) > 1e-3
            or np.max(np.abs(np.linalg.det(rotations) - 1)) > 1e-3
        ):
            raise ValueError("Motion must contain proper rotation matrices.")
    if np.max(np.abs(arrays["root_positions"] - positions[:, 0])) > 1e-4:
        raise ValueError("Root position channel disagrees with the posed root.")
    local, global_ = arrays["local_rot_mats"], arrays["global_rot_mats"]
    if np.max(np.abs(local[:, 0] - global_[:, 0])) > 1e-3:
        raise ValueError("Root local/global rotations disagree.")
    if any(
        np.max(np.abs(global_[:, joint] - global_[:, parent] @ local[:, joint])) > 1e-3
        for joint, parent in enumerate(parents)
        if parent >= 0
    ):
        raise ValueError("Local/global rotations disagree along the hierarchy.")
    if np.max(np.abs(np.linalg.norm(arrays["global_root_heading"], axis=-1) - 1)) > 1e-3:
        raise ValueError("Heading vectors must have unit length.")
    if "foot_contacts" in arrays:
        contacts = arrays["foot_contacts"]
        if (
            contacts.shape != (frames, 4 if joints == 30 else 6)
            or contacts.dtype.kind not in "bfu"
            or not np.isfinite(contacts).all()
            or np.any((contacts < 0) | (contacts > 1))
        ):
            raise ValueError("Foot contacts must match the SOMA channels and contain values in [0,1].")


def _normalized(edit: VariantEdit, frames: int) -> VariantEdit:
    values = edit.model_dump()
    values["end_frame_exclusive"] = frames if edit.end_frame_exclusive is None else edit.end_frame_exclusive
    result = VariantEdit.model_validate(values)
    if result.end_frame_exclusive > frames:
        raise ValueError("Trim exceeds the source frame count.")
    return result


def _xyz_rotation(angles: np.ndarray) -> np.ndarray:
    """Intrinsic local XYZ: right-compose Rx(x) @ Ry(y) @ Rz(z)."""
    result = np.broadcast_to(np.eye(3), (len(angles), 3, 3)).copy()
    for axis in range(3):
        c, s = np.cos(angles[:, axis]), np.sin(angles[:, axis])
        rotation = np.broadcast_to(np.eye(3), result.shape).copy()
        a, b = (axis + 1) % 3, (axis + 2) % 3
        rotation[:, a, a] = rotation[:, b, b] = c
        rotation[:, a, b], rotation[:, b, a] = -s, s
        result = result @ rotation
    return result


def _offsets(arrays: dict) -> np.ndarray:
    positions = arrays["posed_joints"].astype(np.float64)
    global_ = arrays["global_rot_mats"].astype(np.float64)
    parents = arrays["joint_parents"]
    offsets = np.zeros_like(positions[0])
    for joint, parent in enumerate(parents):
        if parent < 0:
            continue
        observed = np.einsum("tba,tb->ta", global_[:, parent], positions[:, joint] - positions[:, parent])
        offsets[joint] = observed[0]
        if np.max(np.linalg.norm(observed - offsets[joint], axis=-1)) > 1e-4:
            raise ValueError(
                "Joint adjustment requires static parent-local bone offsets within 0.0001 meters."
            )
    return offsets


def apply_edit(arrays: dict[str, np.ndarray], edit: VariantEdit) -> tuple[dict, dict]:
    """Return independent arrays and exact edit metadata; never mutate inputs."""
    _validate(arrays)
    edit = _normalized(edit, len(arrays["posed_joints"]))
    start, stop = edit.start_frame, edit.end_frame_exclusive
    out = {key: value[start:stop].copy() if key in NATIVE else value.copy() for key, value in arrays.items()}
    names, parents = out["joint_names"].tolist(), out["joint_parents"].tolist()
    active = np.zeros(stop - start, dtype=bool)
    for adjustment in edit.joint_adjustments:
        if adjustment.joint_name not in names or names.index(adjustment.joint_name) == 0:
            raise ValueError(
                "Adjustments require named nonroot SOMA joints; use planar placement for the root."
            )
    if edit.joint_adjustments:
        offsets = _offsets(arrays)
        for adjustment in edit.joint_adjustments:
            a, b = adjustment.start_frame - start, adjustment.end_frame_exclusive - start
            weights = np.ones(b - a)
            if adjustment.blend_frames:
                edge = np.minimum(np.arange(b - a), np.arange(b - a)[::-1]) / adjustment.blend_frames
                edge = np.clip(edge, 0, 1)
                weights = edge * edge * (3 - 2 * edge)
            if not any(adjustment.local_xyz_radians):
                continue
            selected = np.flatnonzero(weights > 0) + a
            active[selected] = True
            joint = names.index(adjustment.joint_name)
            delta = _xyz_rotation(weights[weights > 0, None] * np.asarray(adjustment.local_xyz_radians))
            out["local_rot_mats"][selected, joint] = (
                out["local_rot_mats"][selected, joint].astype(np.float64) @ delta
            )
        if active.any():
            global_ = out["global_rot_mats"][active].astype(np.float64)
            positions = out["posed_joints"][active].astype(np.float64)
            local = out["local_rot_mats"][active].astype(np.float64)
            for joint, parent in enumerate(parents):
                if parent < 0:
                    continue
                global_[:, joint] = global_[:, parent] @ local[:, joint]
                positions[:, joint] = positions[:, parent] + np.einsum(
                    "tab,b->ta", global_[:, parent], offsets[joint]
                )
            out["global_rot_mats"][active] = global_
            out["posed_joints"][active] = positions
            del out["foot_contacts"]
    c, s = math.cos(edit.yaw_radians), math.sin(edit.yaw_radians)
    yaw = np.array([[c, 0, s], [0, 1, 0], [-s, 0, c]])
    pivot = (
        out["root_positions"][0].astype(np.float64) * [1, 0, 1]
        if edit.root_origin == "first_frame"
        else np.zeros(3)
    )
    shift = np.array([edit.translation_xz[0], 0, edit.translation_xz[1]]) - yaw @ pivot
    if edit.yaw_radians != 0 or np.any(shift != 0):
        for key in ("posed_joints", "root_positions", "smooth_root_pos"):
            before = out[key]
            result = (before.astype(np.float64) @ yaw.T + shift).astype(before.dtype)
            result[..., 1] = before[..., 1]
            out[key] = result
        if edit.yaw_radians != 0:
            out["global_rot_mats"] = (yaw @ out["global_rot_mats"].astype(np.float64)).astype(
                out["global_rot_mats"].dtype
            )
            out["local_rot_mats"][:, 0] = yaw @ out["local_rot_mats"][:, 0].astype(np.float64)
            out["global_root_heading"] = (
                out["global_root_heading"].astype(np.float64) @ np.array([[c, s], [-s, c]])
            ).astype(out["global_root_heading"].dtype)
    transform = np.eye(4)
    transform[:3, :3], transform[:3, 3] = yaw, shift
    _validate(out, contacts_required=not active.any())
    return out, {
        "edit": edit.model_dump(mode="json"),
        "root_transform_matrix": transform.tolist(),
        "contact_status": "invalidated_by_joint_adjustment"
        if active.any()
        else "preserved_source_predictions",
        "adjusted_frame_count": int(active.sum()),
    }


def _source(request: VariantRequest) -> dict:
    source, _ = _paths(request)
    if not source.is_file() or _digest(source) != request.source_sha256:
        raise ValueError("Original source differs from its completed-job digest.")
    return _read(source)


def _report(request: VariantRequest, arrays: dict, details: dict, artifact: Path) -> dict:
    return {
        "schema_version": 1,
        "kind": "motion-variant",
        "status": "completed",
        "variant_id": variant_id(request),
        "transform_version": VERSION,
        "source_depth": 1,
        "job_id": request.job_id,
        "sample_index": request.sample_index,
        "source_path": Path(request.source_path).name,
        "source_sha256": request.source_sha256,
        "model": str(arrays["model"]),
        "skeleton": str(arrays["skeleton"]),
        "fps": float(arrays["fps"]),
        "frames": len(arrays["posed_joints"]),
        **details,
        "coordinates": {"units": "meters", "up_axis": "Y", "forward_axis": "Z"},
        "artifact": {
            "path": artifact.name,
            "format": "npz",
            "bytes": artifact.stat().st_size,
            "sha256": _digest(artifact),
        },
        "notes": [
            "Trim and adjustment indices refer to the original source; end frames are exclusive.",
            "Local corrections compose Rx @ Ry @ Rz with smoothstep-scaled Euler angles; zero blend gives hard edges.",
            "Joint corrections do not guarantee anatomical limits, contacts, collision avoidance or natural transitions.",
            "Foot-contact labels are omitted after effective joint correction; no replacement contacts are inferred.",
            "Source frame rate is unchanged; no resampling, loop repair or inference was performed.",
        ],
    }


def verify_motion_variant(directory: Path, expected_request: VariantRequest | None = None) -> dict:
    directory = directory.resolve()
    path = directory / "request.json"
    if not path.is_file() or path.stat().st_size > 32768:
        raise ValueError("Variant request is missing or oversized.")
    request = VariantRequest.model_validate_json(path.read_text(encoding="utf-8"))
    if expected_request is not None and request != expected_request:
        raise ValueError("Stored variant request differs from the expected request.")
    source, assigned = _paths(request)
    if directory != assigned:
        raise ValueError("Variant receipt belongs to a different directory.")
    report_path = directory / "variant-report.json"
    artifact = (directory / f"motion_{request.sample_index:02d}.npz").resolve()
    if artifact.parent != directory or not report_path.is_file() or report_path.stat().st_size > 65536:
        raise ValueError("Variant report or output path is invalid.")
    expected, details = apply_edit(_source(request), request.edit)
    actual = _read(artifact, contacts_required=details["contact_status"] == "preserved_source_predictions")
    if actual.keys() != expected.keys() or any(
        actual[key].dtype != expected[key].dtype or not np.array_equal(actual[key], expected[key])
        for key in actual
    ):
        raise ValueError("Variant arrays differ from the recorded source edit.")
    report = json.loads(report_path.read_text(encoding="utf-8"))
    if report != _report(request, actual, details, artifact) or _digest(source) != request.source_sha256:
        raise ValueError("Variant receipt or source digest changed.")
    return {
        **report,
        "output_directory": str(directory),
        "report_path": str(report_path),
        "artifact": {**report["artifact"], "absolute_path": str(artifact)},
    }


def create_motion_variant(
    source_path: Path,
    *,
    source_sha256: str,
    job_id: str,
    sample_index: int = 0,
    edit: VariantEdit | None = None,
) -> dict:
    source = source_path.resolve()
    request = VariantRequest(
        job_id=job_id,
        sample_index=sample_index,
        source_path=str(source),
        source_sha256=source_sha256,
        edit=edit or VariantEdit(),
        output_directory="",
    )
    if source.name != f"motion_{sample_index:02d}.npz" or source.parent.name != job_id:
        raise ValueError("Variants require an original managed job sample; source chains are not supported.")
    if _digest(source) != source_sha256:
        raise ValueError("Original source differs from its completed-job digest.")
    arrays = _read(source)
    request.edit = _normalized(request.edit, len(arrays["posed_joints"]))
    directory = source.parent / "variants" / variant_id(request)
    request.output_directory = str(directory)
    _paths(request)
    try:
        with FileLock(str(source.parent / f".variant-{variant_id(request)}.lock"), timeout=0):
            if directory.exists():
                if (directory / "variant-report.json").is_file():
                    return verify_motion_variant(directory, request)
                raise ValueError("An incomplete variant attempt exists; inspect it before retrying.")
            output, details = apply_edit(arrays, request.edit)
            directory.mkdir(parents=True)
            atomic_json(directory / "request.json", request.model_dump(mode="json"))
            atomic_json(directory / "attempt.json", {"status": "started", "variant_id": variant_id(request)})
            artifact = directory / source.name
            temporary = None
            try:
                with tempfile.NamedTemporaryFile(dir=directory, suffix=".npz", delete=False) as stream:
                    temporary = Path(stream.name)
                    np.savez(stream, **output)
                os.replace(temporary, artifact)
                if _digest(source) != source_sha256:
                    raise ValueError("Source changed while creating the variant.")
                atomic_json(directory / "variant-report.json", _report(request, output, details, artifact))
            finally:
                if temporary is not None:
                    temporary.unlink(missing_ok=True)
            return verify_motion_variant(directory, request)
    except Timeout as error:
        raise ValueError("This variant is already being created.") from error


def list_motion_variants(job_directory: Path) -> list[dict]:
    job = job_directory.resolve()
    root = (job / "variants").resolve()
    if not root.is_relative_to(job) or not root.is_dir():
        return []
    result = []
    for directory in sorted(root.iterdir()):
        if len(directory.name) != 64 or any(c not in "0123456789abcdef" for c in directory.name):
            continue
        try:
            report = verify_motion_variant(directory)
            if report["job_id"] == job.name and Path(report["output_directory"]).parent == root:
                result.append(report)
        except (ValueError, OSError, KeyError, TypeError):
            continue
    return result
