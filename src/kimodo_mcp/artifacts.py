"""Inspect numeric Kimodo motion archives and build an offline skeletal viewer.

This module deliberately has no Kimodo, PyTorch, browser, or GPU dependency.
An archive is validated before NumPy allocates arrays, including its NPY headers.
"""

from __future__ import annotations

import hashlib
import html
import json
import math
import os
import tempfile
import zipfile
from pathlib import Path
from typing import Any

import numpy as np

MAX_ARCHIVE_BYTES = 128 * 1024 * 1024
MAX_ARRAY_BYTES = 128 * 1024 * 1024
MAX_UNCOMPRESSED_BYTES = 256 * 1024 * 1024
MAX_MEMBERS = 32
MAX_FRAMES = 36_000
MAX_JOINTS = 256
MAX_SAMPLES = 16
MAX_PREVIEW_FRAMES = 1500
MAX_PREVIEW_VALUES = 2_000_000
_MOTION_KEYS = {
    "posed_joints",
    "global_rot_mats",
    "local_rot_mats",
    "foot_contacts",
    "joint_parents",
    "joint_names",
    "fps",
    "skeleton",
    "model",
}


def _check_archive(path: Path) -> None:
    """Check advertised array sizes before calling np.load (never allow pickle)."""
    if path.suffix.lower() != ".npz" or not path.is_file():
        raise ValueError("Motion must be an existing .npz file.")
    if path.stat().st_size > MAX_ARCHIVE_BYTES:
        raise ValueError("Motion archive exceeds the 128 MiB limit.")
    try:
        with zipfile.ZipFile(path) as archive:
            entries = archive.infolist()
            if not 1 <= len(entries) <= MAX_MEMBERS:
                raise ValueError("Motion archive must contain between 1 and 32 arrays.")
            if sum(entry.file_size for entry in entries) > MAX_UNCOMPRESSED_BYTES:
                raise ValueError("Expanded motion archive exceeds the 256 MiB limit.")
            names: set[str] = set()
            for entry in entries:
                name = entry.filename
                if name in names or not name.endswith(".npy") or "/" in name or "\\" in name:
                    raise ValueError("Motion archive contains duplicate or invalid array names.")
                names.add(name)
                if entry.file_size > MAX_ARRAY_BYTES:
                    raise ValueError("A motion array exceeds the 128 MiB limit.")
                with archive.open(entry) as stream:
                    version = np.lib.format.read_magic(stream)
                    if version == (1, 0):
                        shape, _, dtype = np.lib.format.read_array_header_1_0(stream)
                    elif version == (2, 0):
                        shape, _, dtype = np.lib.format.read_array_header_2_0(stream)
                    else:
                        raise ValueError("Unsupported NPY version; use NumPy's standard numeric NPZ format.")
                    if dtype.hasobject or dtype.kind not in "biufUS":
                        raise ValueError("Motion archives may contain only numeric or plain string arrays.")
                    if len(shape) > 6 or any(not isinstance(n, int) or n < 0 for n in shape):
                        raise ValueError("Motion archive contains an invalid array shape.")
                    byte_count = math.prod(shape) * dtype.itemsize
                    if byte_count > MAX_ARRAY_BYTES or byte_count != entry.file_size - stream.tell():
                        raise ValueError("Motion array size does not match its header or exceeds the limit.")
    except (zipfile.BadZipFile, EOFError, OSError) as exc:
        raise ValueError("Motion archive is incomplete or is not a valid NPZ file.") from exc


def _scalar_text(arrays: dict[str, np.ndarray], key: str) -> str | None:
    value = arrays.get(key)
    if value is None:
        return None
    if value.size != 1 or value.dtype.kind not in "US":
        raise ValueError(f"{key} must be one plain string.")
    result = value.reshape(()).item()
    if isinstance(result, bytes):
        result = result.decode("utf-8", errors="strict")
    if len(result) > 256:
        raise ValueError(f"{key} is too long.")
    return result


def _finite_numeric(array: np.ndarray, name: str, *, allow_bool: bool = False) -> None:
    allowed_kinds = "biuf" if allow_bool else "iuf"
    if array.dtype.kind not in allowed_kinds or not np.isfinite(array).all():
        raise ValueError(f"{name} must contain finite numbers.")


def _load_motion(path: str | Path) -> dict[str, Any]:
    source = Path(path).expanduser().resolve()
    _check_archive(source)
    try:
        with np.load(source, allow_pickle=False) as archive:
            arrays = {key: archive[key] for key in archive.files if key in _MOTION_KEYS}
    except (ValueError, OSError, EOFError) as exc:
        raise ValueError(f"Cannot read numeric motion arrays: {exc}") from exc
    if "posed_joints" not in arrays:
        raise ValueError("Motion archive is missing posed_joints.")
    positions = arrays["posed_joints"]
    _finite_numeric(positions, "posed_joints")
    if positions.ndim == 3:
        positions = positions[None]
    if positions.ndim != 4 or positions.shape[-1] != 3:
        raise ValueError("posed_joints must have shape [frames, joints, 3] or [samples, frames, joints, 3].")
    samples, frames, joints, _ = positions.shape
    if not (1 <= samples <= MAX_SAMPLES and 1 <= frames <= MAX_FRAMES and 1 <= joints <= MAX_JOINTS):
        raise ValueError("Motion exceeds limits of 16 samples, 36000 frames, or 256 joints, or is empty.")
    if np.max(np.abs(positions)) > 1_000_000:
        raise ValueError("Motion coordinates are too large to inspect safely.")
    positions = positions.astype(np.float64, copy=False)

    fps_value = arrays.get("fps")
    fps = None
    if fps_value is not None:
        _finite_numeric(fps_value, "fps")
        if fps_value.size != 1:
            raise ValueError("fps must be a scalar.")
        fps = float(fps_value.reshape(()))
        if not 1 <= fps <= 240:
            raise ValueError("fps must be between 1 and 240.")

    parents_value = arrays.get("joint_parents")
    parents = None
    if parents_value is not None:
        if parents_value.shape != (joints,) or parents_value.dtype.kind not in "iu":
            raise ValueError("joint_parents must contain one integer parent index per joint.")
        if np.any(parents_value < -1) or np.any(parents_value >= joints):
            raise ValueError("joint_parents contains an out-of-range parent index.")
        parents = parents_value.astype(np.int64).tolist()
        if parents.count(-1) != 1:
            raise ValueError("joint_parents must have exactly one root with parent -1.")
        for joint in range(joints):
            visited: set[int] = set()
            ancestor = joint
            while ancestor != -1:
                if ancestor in visited:
                    raise ValueError("joint_parents contains a cycle.")
                visited.add(ancestor)
                ancestor = parents[ancestor]

    names_value = arrays.get("joint_names")
    names = [f"Joint {i}" for i in range(joints)]
    if names_value is not None:
        if names_value.shape != (joints,) or names_value.dtype.kind not in "US":
            raise ValueError("joint_names must contain one plain string per joint.")
        names = [x.decode("utf-8") if isinstance(x, bytes) else str(x) for x in names_value.tolist()]
        if any(len(name) > 256 for name in names):
            raise ValueError("A joint name is too long.")

    rotations: dict[str, np.ndarray] = {}
    for key in ("global_rot_mats", "local_rot_mats"):
        if key not in arrays:
            continue
        rotation = arrays[key]
        _finite_numeric(rotation, key)
        if rotation.ndim == 4:
            rotation = rotation[None]
        if rotation.shape != (samples, frames, joints, 3, 3):
            raise ValueError(f"{key} shape does not match posed_joints.")
        if np.max(np.abs(rotation)) > 10:
            raise ValueError(f"{key} contains invalid rotation matrix values.")
        rotations[key] = rotation

    contacts = arrays.get("foot_contacts")
    if contacts is not None:
        _finite_numeric(contacts, "foot_contacts", allow_bool=True)
        if contacts.ndim == 2:
            contacts = contacts[None]
        if contacts.ndim != 3 or contacts.shape[:2] != (samples, frames) or contacts.shape[-1] > 32:
            raise ValueError("foot_contacts shape does not match the motion.")

    return {
        "path": source,
        "positions": positions,
        "parents": parents,
        "names": names,
        "fps": fps,
        "skeleton": _scalar_text(arrays, "skeleton"),
        "model": _scalar_text(arrays, "model"),
        "rotations": rotations,
        "contacts": contacts,
        "channels": sorted(arrays),
    }


def _summarize(motion: dict[str, Any]) -> dict[str, Any]:
    positions = motion["positions"]
    samples, frames, joints, _ = positions.shape
    fps, parents = motion["fps"], motion["parents"]
    root_index = parents.index(-1) if parents is not None else 0
    warnings: list[str] = []
    if fps is None:
        warnings.append("No FPS metadata: duration and speed are unknown.")
    if parents is None:
        warnings.append(
            "No skeleton hierarchy: joint 0 is used for root statistics; bone lengths are unavailable."
        )
    sample_stats = []
    for sample in positions:
        root = sample[:, root_index, :]
        steps = np.linalg.norm(np.diff(root, axis=0), axis=-1)
        travel = float(steps.sum())
        sample_stats.append(
            {
                "bounds_min_m": sample.min(axis=(0, 1)).round(6).tolist(),
                "bounds_max_m": sample.max(axis=(0, 1)).round(6).tolist(),
                "root_travel_m": round(travel, 6),
                "root_displacement_m": round(float(np.linalg.norm(root[-1] - root[0])), 6),
                "mean_root_speed_m_s": round(travel * fps / (frames - 1), 6) if fps and frames > 1 else None,
                "max_root_speed_m_s": round(float(steps.max()) * fps, 6) if fps and steps.size else None,
            }
        )
    bone_length_variation = None
    if parents is not None and joints > 1:
        children = np.asarray([i for i, parent in enumerate(parents) if parent != -1])
        parent_indices = np.asarray([parents[i] for i in children])
        lengths = np.linalg.norm(positions[:, :, children] - positions[:, :, parent_indices], axis=-1)
        bone_length_variation = float(np.ptp(lengths, axis=1).max())
        if bone_length_variation > 0.02:
            warnings.append("A bone length varies by more than 2 cm within a sample; review the motion.")
    rotation_checks = {}
    for key, matrices in motion["rotations"].items():
        flat = matrices.reshape(-1, 3, 3)
        max_error, min_det, max_det = 0.0, math.inf, -math.inf
        for start in range(0, len(flat), 4096):
            chunk = flat[start : start + 4096].astype(np.float64)
            residual = chunk @ chunk.transpose(0, 2, 1) - np.eye(3)
            max_error = max(max_error, float(np.abs(residual).max()))
            determinants = np.linalg.det(chunk)
            min_det, max_det = (
                min(min_det, float(determinants.min())),
                max(max_det, float(determinants.max())),
            )
        rotation_checks[key] = {
            "max_orthogonality_error": round(max_error, 8),
            "determinant_min": round(min_det, 8),
            "determinant_max": round(max_det, 8),
        }
        if max_error > 0.01 or min_det < 0.99 or max_det > 1.01:
            warnings.append(f"{key} contains matrices that are not close to proper rotations.")
    contacts = motion["contacts"]
    contact_range = (
        [float(contacts.min()), float(contacts.max())] if contacts is not None and contacts.size else None
    )
    if contact_range and (contact_range[0] < 0 or contact_range[1] > 1):
        warnings.append("Foot contact values lie outside the expected 0 to 1 range.")
    return {
        "path": str(motion["path"]),
        "samples": samples,
        "frames": frames,
        "joints": joints,
        "fps": fps,
        "duration_seconds": round(frames / fps, 6) if fps else None,
        "skeleton": motion["skeleton"],
        "model": motion["model"],
        "coordinate_convention": "Kimodo: meters, +Y up, ground plane XZ",
        "root_joint": motion["names"][root_index],
        "channels": motion["channels"],
        "sample_statistics": sample_stats,
        "max_bone_length_variation_m": round(bone_length_variation, 6)
        if bone_length_variation is not None
        else None,
        "rotation_checks": rotation_checks,
        "foot_contact_range": contact_range,
        "warnings": warnings,
        "assessment_scope": "Numerical diagnostics only; these do not establish motion quality or prompt adherence.",
    }


def inspect_motion(path: str | Path) -> dict[str, Any]:
    """Return dimensions, timing, bounds and numerical checks without executing archive content."""
    return _summarize(_load_motion(path))


def create_preview(npz_path: str | Path, output_html: str | Path) -> dict[str, Any]:
    """Write a portable offline HTML skeleton animation; existing output is atomically replaced."""
    motion = _load_motion(npz_path)
    if motion["parents"] is None or motion["fps"] is None:
        raise ValueError("Preview requires joint_parents and fps metadata from a generated motion archive.")
    output = Path(output_html).expanduser().resolve()
    if output.suffix.lower() != ".html":
        raise ValueError("Preview output must end in .html.")
    positions = motion["positions"]
    samples, frames, joints, _ = positions.shape
    max_frames = min(MAX_PREVIEW_FRAMES, max(2, MAX_PREVIEW_VALUES // (samples * joints * 3)))
    frame_indices = np.unique(np.linspace(0, frames - 1, min(frames, max_frames), dtype=np.int64))
    summary = _summarize(motion)
    payload = {
        "title": motion["path"].name,
        "positions": positions[:, frame_indices].round(5).tolist(),
        "parents": motion["parents"],
        "names": motion["names"],
        "frameIndices": frame_indices.tolist(),
        "frames": frames,
        "fps": motion["fps"],
        "skeleton": motion["skeleton"] or "Skeleton",
        "model": motion["model"] or "Kimodo",
        "summary": summary,
    }
    # Escape HTML delimiters even inside an inert JSON script element. Names and
    # source filenames are untrusted display text, never executable markup.
    data = json.dumps(payload, ensure_ascii=True, separators=(",", ":"), allow_nan=False)
    data = data.replace("&", "\\u0026").replace("<", "\\u003c").replace(">", "\\u003e")
    template = Path(__file__).with_name("preview_template.html").read_text(encoding="utf-8")
    rendered = template.replace("__MOTION_TITLE__", html.escape(motion["path"].name, quote=True))
    rendered = rendered.replace("__MOTION_DATA__", data)
    output.parent.mkdir(parents=True, exist_ok=True)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w", encoding="utf-8", dir=output.parent, suffix=".tmp", delete=False
        ) as file:
            temporary = Path(file.name)
            file.write(rendered)
        os.replace(temporary, output)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
    with motion["path"].open("rb") as source:
        digest = hashlib.file_digest(source, "sha256").hexdigest()
    return {
        "path": str(output),
        "source_path": str(motion["path"]),
        "source_sha256": digest,
        "samples": samples,
        "frames": frames,
        "preview_frames": len(frame_indices),
        "fps": motion["fps"],
        "duration_seconds": summary["duration_seconds"],
        "preview_kind": "Skeletal animation; no character mesh or textures.",
        "downsampled": len(frame_indices) < frames,
        "offline": True,
    }
