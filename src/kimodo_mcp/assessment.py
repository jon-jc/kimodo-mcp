"""Explicit, reproducible numerical acceptance checks for completed motion.

Passing these checks establishes only the supplied numerical limits. It cannot
approve acting, prompt adherence, a fitted character, or engine gameplay.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Annotated, Any, Literal

import numpy as np
from pydantic import Field, StrictBool, StrictInt, model_validator

from .artifacts import MAX_ARCHIVE_BYTES, _load_motion
from .quality import _frame_hashes
from .schemas import Finite, StrictModel

ALGORITHM_VERSION = "numerical-policy-v1"
MAX_WINDOWS = 16
MAX_REVIEW_FRAMES = 24
MetricName = Literal[
    "speed", "acceleration", "jerk", "angular_speed", "bone_length_drift",
    "contact_sliding", "foot_floor_proxy", "loop_world_pose_gap",
    "loop_root_relative_pose_gap", "loop_root_displacement", "loop_velocity_gap",
]


class QualityCheck(StrictModel):
    metric: MetricName
    max_value: Annotated[Finite, Field(ge=0, le=10000)]


class QualityPolicy(StrictModel):
    """Every requested check is required; no automatic or universal limits."""

    checks: Annotated[list[QualityCheck], Field(min_length=1, max_length=11)]
    contact_probability: Annotated[Finite, Field(ge=0, le=1)] = 0.5
    floor_y_m: Annotated[Finite, Field(ge=-10000, le=10000)] = 0.0
    loop_intent: StrictBool = False

    @model_validator(mode="after")
    def explicit_unique_checks(self):
        metrics = [check.metric for check in self.checks]
        if len(set(metrics)) != len(metrics):
            raise ValueError("Policy checks must name unique metrics")
        if not self.loop_intent and any(metric.startswith("loop_") for metric in metrics):
            raise ValueError("Loop checks require explicit loop_intent=true")
        return self


class CandidateReference(StrictModel):
    job_id: Annotated[str, Field(pattern=r"^[0-9a-f]{32}$", min_length=32, max_length=32, strict=True)]
    sample_index: Annotated[StrictInt, Field(ge=0, le=3)] = 0
    variant_id: Annotated[str, Field(pattern=r"^[0-9a-f]{64}$", strict=True)] | None = None


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


@dataclass
class _Series:
    values: np.ndarray | None
    names: list[str]
    unit: str
    order: int = 0
    reason: str | None = None
    loop_boundary: bool = False
    reference_frame_index: int | None = None


def _series(motion: dict[str, Any], metric: str, policy: QualityPolicy) -> _Series:
    p, fps, names, parents = (motion[key] for key in ("positions", "fps", "names", "parents"))
    frames = len(p)
    if metric in {"speed", "acceleration", "jerk"}:
        order, unit = {"speed": (1, "m/s"), "acceleration": (2, "m/s2"), "jerk": (3, "m/s3")}[metric]
        if fps is None:
            return _Series(None, names, unit, order, "Missing FPS metadata")
        return _Series(np.linalg.norm(np.diff(p, n=order, axis=0), axis=-1) * fps**order,
                       names, unit, order)
    if metric == "bone_length_drift":
        if parents is None:
            return _Series(None, names, "m", reason="Missing joint hierarchy")
        children = [index for index, parent in enumerate(parents) if parent != -1]
        lengths = np.linalg.norm(p[:, children] - p[:, [parents[index] for index in children]], axis=-1)
        return _Series(abs(lengths - lengths[:1]), [names[index] for index in children], "m",
                       reference_frame_index=0)
    if metric == "angular_speed":
        rotations = motion["rotations"].get("global_rot_mats")
        if fps is None or rotations is None:
            return _Series(None, names, "rad/s", 1, "Missing FPS metadata or global rotations")
        if (np.max(abs(rotations @ rotations.swapaxes(-1, -2) - np.eye(3))) > 0.01
                or np.max(abs(np.linalg.det(rotations) - 1)) > 0.01):
            return _Series(None, names, "rad/s", 1, "Matrices are not proper rotations within tolerance")
        # The same diagnostic-only SO(3) projection used by analyze_motion.
        u, _, vt = np.linalg.svd(rotations)
        proper = u @ vt
        cosine = (np.einsum("tjab,tjab->tj", proper[1:], proper[:-1]) - 1) / 2
        return _Series(np.arccos(np.clip(cosine, -1, 1)) * fps, names, "rad/s", 1)
    if metric in {"contact_sliding", "foot_floor_proxy"}:
        feet = [side + joint for side in ("Left", "Right") for joint in ("Foot", "ToeBase", "ToeEnd")]
        if motion["skeleton"] == "somaskel30":
            feet = [name for name in feet if not name.endswith("End")]
        unit = "m/s" if metric == "contact_sliding" else "m"
        if (motion["skeleton"] not in {"somaskel30", "somaskel77"}
                or "joint_names" not in motion["channels"] or any(name not in names for name in feet)):
            return _Series(None, feet, unit, reason="Unsupported exact named foot-joint metadata")
        positions = p[:, [names.index(name) for name in feet]]
        if metric == "foot_floor_proxy":
            return _Series(np.maximum(0, policy.floor_y_m - positions[:, :, 1]), feet, unit)
        contacts = motion["contacts"]
        if (fps is None or contacts is None or contacts.shape != (frames, len(feet))
                or not np.all((contacts >= 0) & (contacts <= 1))):
            return _Series(None, feet, unit, 1, "Missing or unsupported FPS/contact-channel metadata")
        active = ((contacts[:-1] >= policy.contact_probability)
                  & (contacts[1:] >= policy.contact_probability))
        speed = np.linalg.norm(np.diff(positions[:, :, [0, 2]], axis=0), axis=-1) * fps
        return _Series(np.where(active, speed, np.nan), feet, unit, 1)
    unit = "m/s" if metric == "loop_velocity_gap" else "m"
    if frames < 2 or (metric == "loop_velocity_gap" and frames < 3):
        return _Series(None, names, unit, reason="Not enough distinct endpoint samples for this loop check")
    if metric == "loop_velocity_gap":
        if fps is None:
            return _Series(None, names, unit, reason="Missing FPS metadata")
        values = np.linalg.norm((p[-1] - p[-2]) - (p[1] - p[0]), axis=-1) * fps
    elif metric == "loop_world_pose_gap":
        values = np.linalg.norm(p[-1] - p[0], axis=-1)
    else:
        if parents is None:
            return _Series(None, names, unit, reason="Missing joint hierarchy")
        root = parents.index(-1)
        if metric == "loop_root_displacement":
            values = np.array([np.linalg.norm(p[-1, root] - p[0, root])])
            names = [names[root]]
        else:
            relative = p - p[:, root:root + 1]
            values = np.linalg.norm(relative[-1] - relative[0], axis=-1)
    return _Series(values[None], names, unit, 1 if metric == "loop_velocity_gap" else 0,
                   loop_boundary=True)


def _ranges(indices: np.ndarray) -> list[tuple[int, int]]:
    if not len(indices):
        return []
    chunks = np.split(indices, np.flatnonzero(np.diff(indices) != 1) + 1)
    return [(int(chunk[0]), int(chunk[-1])) for chunk in chunks]


def _evaluate(check: QualityCheck, series: _Series, frames: int) -> dict[str, Any]:
    values = series.values
    result: dict[str, Any] = {
        "metric": check.metric, "status": "unavailable", "unit": series.unit,
        "max_value": check.max_value, "comparison": "observed <= max_value",
        "difference_order": series.order,
    }
    if values is None or not values.size or not np.isfinite(values).any():
        result["reason"] = series.reason or (
            "No eligible intervals" if values is not None and values.size else "Not enough samples for this metric"
        )
        return result
    eligible = np.isfinite(values)
    violations = eligible & (values > check.max_value)
    rows = np.flatnonzero(violations.any(axis=1))
    peak_row, peak_joint = np.unravel_index(np.nanargmax(values), values.shape)
    result.update(
        status="fail" if len(rows) else "pass", observed_max=float(values[peak_row, peak_joint]),
        peak_frame_index=frames - 1 if series.loop_boundary else int(peak_row + series.order),
        peak_joint=series.names[peak_joint], eligible_values=int(eligible.sum()),
        violating_values=int(violations.sum()), violating_frame_count=len(rows),
        window_kind="loop_boundary" if series.loop_boundary else "contiguous_metric_end_frames",
    )
    if series.reference_frame_index is not None:
        result["reference_frame_index"] = series.reference_frame_index
    affected = np.zeros(frames, dtype=bool)
    windows = []
    for first, last in _ranges(rows):
        window_values = values[first:last + 1]
        local_row, joint = np.unravel_index(np.nanargmax(window_values), window_values.shape)
        peak_frame = frames - 1 if series.loop_boundary else int(first + local_row + series.order)
        if series.loop_boundary:
            # These are disjoint boundary samples, never a claim that the entire
            # clip is defective. Velocity uses two samples at each boundary.
            source_ranges = [(0, series.order), (frames - 1 - series.order, frames - 1)]
            source_ranges = _ranges(np.unique(np.concatenate(
                [np.arange(start, end + 1) for start, end in source_ranges]
            )))
            start_frame = end_frame = frames - 1
        else:
            start_frame, end_frame = first + series.order, last + series.order
            source_ranges = [(first, last + series.order)]
        for start, end in source_ranges:
            affected[start:end + 1] = True
        windows.append({
            "start_frame": start_frame, "end_frame": end_frame,
            "source_frame_ranges": [{"start_frame": start, "end_frame": end} for start, end in source_ranges],
            "peak_frame_index": peak_frame, "peak_joint": series.names[joint],
            "peak_value": float(window_values[local_row, joint]),
            "violating_values": int(violations[first:last + 1].sum()),
            "violating_frame_count": last - first + 1,
        })
    if len(rows) and series.reference_frame_index is not None:
        affected[series.reference_frame_index] = True
    result.update(affected_frame_count=int(affected.sum()), issue_window_count=len(windows),
                  windows_truncated=len(windows) > MAX_WINDOWS,
                  window_selection="largest peaks, then chronological order")
    selected = sorted(windows, key=lambda window: (-window["peak_value"], window["start_frame"]))[:MAX_WINDOWS]
    result["issue_windows"] = sorted(selected, key=lambda window: window["start_frame"])
    return result


def assess_motion(
    path: str | Path, *, policy: QualityPolicy, expected_source_sha256: str, sample_index: int = 0,
) -> dict[str, Any]:
    """Read one archive sample and check exactly the supplied required limits.

    The caller binds the archive to its managed job. sample_index indexes the
    archive itself, which is normally zero for a single-sample managed NPZ.
    """
    if not isinstance(policy, QualityPolicy):
        raise ValueError("policy must be a validated QualityPolicy")
    # Revalidate copied data in case a caller has modified a Pydantic instance.
    policy = QualityPolicy.model_validate(policy.model_dump(mode="json"))
    if not isinstance(expected_source_sha256, str) or not re.fullmatch(r"[0-9a-f]{64}", expected_source_sha256):
        raise ValueError("expected_source_sha256 must be a lowercase SHA-256 digest")
    source = Path(path).resolve()
    if source.stat().st_size > MAX_ARCHIVE_BYTES:
        raise ValueError("Motion archive exceeds the 128 MiB limit")
    if _sha256(source) != expected_source_sha256:
        raise ValueError("Motion source differs from its expected checksum")
    motion = _load_motion(source)
    if type(sample_index) is not int or not 0 <= sample_index < len(motion["positions"]):
        raise ValueError("sample_index is outside this archive")
    if len(set(motion["names"])) != len(motion["names"]):
        raise ValueError("Joint names must be unique")
    motion["positions"] = motion["positions"][sample_index]
    motion["rotations"] = {key: value[sample_index].astype(np.float64)
                           for key, value in motion["rotations"].items()}
    if motion["contacts"] is not None:
        motion["contacts"] = motion["contacts"][sample_index]
    frames, fps = len(motion["positions"]), motion["fps"]
    normalized_policy = policy.model_dump(mode="json")
    normalized_policy["checks"].sort(key=lambda check: check["metric"])
    policy_sha256 = hashlib.sha256(json.dumps(
        normalized_policy, sort_keys=True, separators=(",", ":"), allow_nan=False,
    ).encode()).hexdigest()
    checks = [_evaluate(check, _series(motion, check.metric, policy), frames)
              for check in sorted(policy.checks, key=lambda check: check.metric)]
    states = [check["status"] for check in checks]
    status = "fail" if "fail" in states else "unavailable" if "unavailable" in states else "pass"
    # Put whole-clip context and every metric peak before extra window bounds.
    candidates = np.linspace(0, frames - 1, min(frames, 5), dtype=int).tolist()
    candidates += [check["peak_frame_index"] for check in checks if "peak_frame_index" in check]
    for check in checks:
        for window in check.get("issue_windows", []):
            for interval in window["source_frame_ranges"]:
                candidates.extend([interval["start_frame"], interval["end_frame"]])
        if "reference_frame_index" in check:
            candidates.append(check["reference_frame_index"])
    unique = list(dict.fromkeys(candidates))
    hashes = _frame_hashes(motion)
    if _sha256(source) != expected_source_sha256:
        raise ValueError("Motion source changed during assessment")
    return {
        "schema_version": 1, "algorithm_version": ALGORITHM_VERSION,
        "source_sha256": expected_source_sha256, "archive_sample_index": sample_index,
        "content_sha256": hashlib.sha256("".join(hashes).encode()).hexdigest(),
        "policy": normalized_policy, "policy_sha256": policy_sha256, "status": status,
        "checks": checks, "frames": frames, "fps": fps,
        "duration_seconds": frames / fps if fps is not None else None,
        "last_key_seconds": (frames - 1) / fps if fps is not None else None,
        "skeleton": motion["skeleton"], "model": motion["model"],
        "coordinate_convention": "meters,+Y up,XZ floor",
        "frame_range_convention": "zero_based_inclusive",
        "review_frames": sorted(unique[:MAX_REVIEW_FRAMES]),
        "review_frames_truncated": len(unique) > MAX_REVIEW_FRAMES,
        "human_review_required": True,
        "assessment_scope": "Compliance with explicit numerical limits; no artistic or production approval.",
        "limitations": [
            "Every requested check is required. Unavailable evidence never counts as a pass.",
            "Limits are supplied by the caller; there is no universal quality score or automatic winner.",
            "Jerk and acceleration use unsmoothed finite differences and can amplify numerical noise.",
            "Derivative frame indices identify window endpoints; source_frame_ranges include contributing samples.",
            "Bone drift compares each bone with frame zero; the reference frame is not itself a detected defect.",
            "Contact labels may be predictions; floor checks concern joint pivots, not sole geometry or mesh collision.",
            "Loop boundaries compare endpoints only; root-relative pose checks remove translation, not heading.",
            "Loop velocity needs at least three samples so its endpoint intervals are distinct.",
            "Near-valid matrices are projected onto SO(3) for diagnostics; original motion is unchanged.",
            "Prompt adherence, acting, skinning, fingers, facial motion, cloth, collisions and gameplay need separate review.",
        ],
    }
