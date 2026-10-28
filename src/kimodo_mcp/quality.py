"""Read-only numerical motion diagnostics; thresholds are review aids, not scores."""

from __future__ import annotations

import hashlib
import json
import math
from dataclasses import asdict, dataclass

import numpy as np

from kimodo_mcp.artifacts import _load_motion


@dataclass(frozen=True)
class ReviewThresholds:
    speed_m_s: float = 12.0
    acceleration_m_s2: float = 150.0
    jerk_m_s3: float = 2000.0
    angular_speed_rad_s: float = 25.0
    bone_drift_m: float = 0.005
    contact_slide_m_s: float = 0.05
    floor_penetration_m: float = 0.01
    contact_probability: float = 0.5
    floor_y_m: float = 0.0

    def __post_init__(self):
        for key, value in asdict(self).items():
            if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
                raise ValueError(f"{key} must be a finite number")
            if not (-10000 <= value <= 10000) or (key != "floor_y_m" and value < 0):
                raise ValueError(f"{key} exceeds its supported bounds")
        if not 0 <= self.contact_probability <= 1:
            raise ValueError("contact_probability must be between 0 and 1")


def _sample(path, sample_index):
    motion = _load_motion(path)
    if (
        isinstance(sample_index, bool)
        or not isinstance(sample_index, int)
        or not 0 <= sample_index < len(motion["positions"])
    ):
        raise ValueError("sample_index is outside this archive")
    if motion["fps"] is None or motion["parents"] is None or "joint_names" not in motion["channels"]:
        raise ValueError("Review requires FPS, joint_names and joint_parents metadata")
    if len(set(motion["names"])) != len(motion["names"]):
        raise ValueError("Joint names must be unique")
    motion["positions"] = motion["positions"][sample_index]
    motion["rotations"] = {k: v[sample_index].astype(np.float64) for k, v in motion["rotations"].items()}
    motion["contacts"] = motion["contacts"][sample_index] if motion["contacts"] is not None else None
    return motion


def _frame_hashes(motion):
    header = json.dumps(
        {k: motion[k] for k in ("names", "parents", "fps", "skeleton")}, sort_keys=True, separators=(",", ":")
    ).encode()
    hashes = []
    for frame in range(len(motion["positions"])):
        digest = hashlib.sha256(header)
        channels = {
            "positions": motion["positions"][frame],
            **{k: v[frame] for k, v in motion["rotations"].items()},
        }
        if motion["contacts"] is not None:
            channels["contacts"] = motion["contacts"][frame]
        for key, array in sorted(channels.items()):
            canonical = np.ascontiguousarray(array, dtype="<f8")
            digest.update(json.dumps([key, "<f8", canonical.shape], separators=(",", ":")).encode())
            digest.update(canonical.tobytes())
        hashes.append(digest.hexdigest())
    return hashes


def _stats(values, names, offset, unit, threshold=None):
    values = np.asarray(values)
    if not values.size:
        return {"available": False, "reason": "Not enough samples for this finite difference"}
    valid = np.isfinite(values)
    if not valid.any():
        return {"available": False, "reason": "No eligible intervals"}
    index = np.unravel_index(np.nanargmax(values), values.shape)
    result = {
        "available": True,
        "unit": unit,
        "mean": float(np.nanmean(values)),
        "p95": float(np.nanpercentile(values, 95)),
        "max": float(values[index]),
        "worst_frame_index": int(index[0] + offset),
        "worst_joint": names[index[-1]],
        "eligible_values": int(valid.sum()),
    }
    if threshold is not None:
        result.update(threshold=threshold, above_threshold=int(np.sum(values > threshold)))
    return result


def analyze_motion(path, *, sample_index=0, thresholds=None):
    """Return reproducible diagnostics for canonical Kimodo metres/Y-up arrays."""
    if thresholds is not None and not isinstance(thresholds, dict):
        raise ValueError("thresholds must be an object of named numeric sensitivities")
    limit = ReviewThresholds(**({} if thresholds is None else thresholds))
    motion = _sample(path, sample_index)
    p, fps, names, parents = (motion[k] for k in ("positions", "fps", "names", "parents"))
    frames, root = len(p), parents.index(-1)
    metrics = {}
    for order, key, unit, threshold in [
        (1, "speed", "m/s", limit.speed_m_s),
        (2, "acceleration", "m/s2", limit.acceleration_m_s2),
        (3, "jerk", "m/s3", limit.jerk_m_s3),
    ]:
        metrics[key] = _stats(
            np.linalg.norm(np.diff(p, n=order, axis=0), axis=-1) * fps**order, names, order, unit, threshold
        )
    children = [i for i, parent in enumerate(parents) if parent != -1]
    lengths = np.linalg.norm(p[:, children] - p[:, [parents[i] for i in children]], axis=-1)
    metrics["bone_length_drift"] = _stats(
        abs(lengths - lengths[:1]), [names[i] for i in children], 0, "m", limit.bone_drift_m
    )
    rotations = motion["rotations"].get("global_rot_mats")
    if rotations is None:
        metrics["angular_speed"] = {"available": False, "reason": "No global rotations"}
    elif (
        np.max(abs(rotations @ rotations.swapaxes(-1, -2) - np.eye(3))) > 0.01
        or np.max(abs(np.linalg.det(rotations) - 1)) > 0.01
    ):
        metrics["angular_speed"] = {
            "available": False,
            "reason": "Matrices are not proper rotations within tolerance",
        }
    else:
        # Small orthogonality errors can otherwise report rotation even between
        # two identical matrices. Project only the diagnostic copy onto SO(3).
        u, _, vt = np.linalg.svd(rotations)
        proper = u @ vt
        cosine = (np.einsum("tjab,tjab->tj", proper[1:], proper[:-1]) - 1) / 2
        metrics["angular_speed"] = _stats(
            np.arccos(np.clip(cosine, -1, 1)) * fps, names, 1, "rad/s", limit.angular_speed_rad_s
        )
    # Upstream SOMA77 is LFoot,LToeBase,LToeEnd,RFoot,RToeBase,RToeEnd;
    # SOMA30-to77 duplicates each toe contact for its newly expanded toe end.
    foot_names = [side + joint for side in ("Left", "Right") for joint in ("Foot", "ToeBase", "ToeEnd")]
    if motion["skeleton"] == "somaskel30":
        foot_names = [name for name in foot_names if not name.endswith("End")]
    known = motion["skeleton"] in ("somaskel30", "somaskel77") and all(n in names for n in foot_names)
    contacts = motion["contacts"]
    metrics["contact_sliding"] = {
        "available": False,
        "reason": "Missing or unsupported exact contact-channel metadata",
    }
    metrics["foot_floor_proxy"] = {"available": False, "reason": "Unsupported named foot-joint metadata"}
    if known:
        feet = p[:, [names.index(n) for n in foot_names]]
        metrics["foot_floor_proxy"] = _stats(
            np.maximum(0, limit.floor_y_m - feet[:, :, 1]), foot_names, 0, "m", limit.floor_penetration_m
        )
        if (
            contacts is not None
            and contacts.shape == (frames, len(foot_names))
            and np.all((contacts >= 0) & (contacts <= 1))
        ):
            active = (contacts[:-1] >= limit.contact_probability) & (
                contacts[1:] >= limit.contact_probability
            )
            sliding = np.linalg.norm(np.diff(feet[:, :, [0, 2]], axis=0), axis=-1) * fps
            metrics["contact_sliding"] = _stats(
                np.where(active, sliding, np.nan), foot_names, 1, "m/s", limit.contact_slide_m_s
            )
    relative = p - p[:, root : root + 1]
    seam = {
        "world_pose_max_gap_m": float(np.linalg.norm(p[-1] - p[0], axis=-1).max()),
        "root_relative_pose_max_gap_m": float(np.linalg.norm(relative[-1] - relative[0], axis=-1).max()),
        "root_displacement_m": float(np.linalg.norm(p[-1, root] - p[0, root])),
        "velocity_max_gap_m_s": float(np.linalg.norm((p[-1] - p[-2]) - (p[1] - p[0]), axis=-1).max() * fps)
        if frames > 1
        else None,
    }
    extrema = [metric["worst_frame_index"] for metric in metrics.values() if metric.get("available")]
    review = sorted(set(extrema + np.linspace(0, frames - 1, min(frames, 5), dtype=int).tolist()))
    hashes = _frame_hashes(motion)
    return {
        "schema_version": 1,
        "sample_index": sample_index,
        "frames": frames,
        "fps": fps,
        "duration_seconds": frames / fps,
        "last_key_seconds": (frames - 1) / fps,
        "coordinate_convention": "meters,+Y up,XZ floor",
        "thresholds": asdict(limit),
        "metrics": metrics,
        "loop_seam": seam,
        "review_frames": review,
        "frame_hashes": hashes,
        "content_sha256": hashlib.sha256("".join(hashes).encode()).hexdigest(),
        "limitations": [
            "Thresholds are configurable review sensitivities, not a quality score.",
            "Jerk uses unsmoothed finite differences and amplifies numerical noise.",
            "Derivative frame indices identify the ending frame of each forward-difference window.",
            "Near-valid matrices are projected onto SO(3) only to measure angular speed; source data is unchanged.",
            "Foot labels may be predictions; joint floor tests do not test mesh collision.",
            "Endpoint differences are expected for nonlooping actions.",
            "Hashes detect exact canonical numeric changes; cross-hardware identity is not promised.",
        ],
    }


def compare_motion(first, second, *, first_sample=0, second_sample=0, second_offset_frames=0, space="world"):
    """Compare exact matching-FPS timelines; never time-warp or resample motion."""
    a, b = _sample(first, first_sample), _sample(second, second_sample)
    if any(a[k] != b[k] for k in ("fps", "names", "parents", "skeleton")):
        raise ValueError("Comparison requires matching FPS, skeleton and joint hierarchy")
    if (
        isinstance(second_offset_frames, bool)
        or not isinstance(second_offset_frames, int)
        or abs(second_offset_frames) > 36000
    ):
        raise ValueError("second_offset_frames must be a bounded integer")
    if space not in ("world", "root_relative"):
        raise ValueError("space must be world or root_relative")
    start_a, start_b = max(0, -second_offset_frames), max(0, second_offset_frames)
    count = min(len(a["positions"]) - start_a, len(b["positions"]) - start_b)
    if count <= 0:
        raise ValueError("Aligned clips have no overlapping frames")
    x, y = a["positions"][start_a : start_a + count], b["positions"][start_b : start_b + count]
    if space == "root_relative":
        root = a["parents"].index(-1)
        x, y = x - x[:, root : root + 1], y - y[:, root : root + 1]
    differences = np.linalg.norm(x - y, axis=-1)
    ha, hb = _frame_hashes(a)[start_a : start_a + count], _frame_hashes(b)[start_b : start_b + count]
    return {
        "space": space,
        "first_start_frame": start_a,
        "second_start_frame": start_b,
        "frames": count,
        "fps": a["fps"],
        "position_difference": _stats(differences, a["names"], start_a, "m"),
        "position_rmse_m": float(np.sqrt(np.mean(differences**2))),
        "exact_numeric_frames_equal": sum(x == y for x, y in zip(ha, hb)),
        "limitations": [
            "Pose difference is not a preference or realism score.",
            "Root-relative comparison removes translation only, not heading or body-size differences.",
        ],
    }
