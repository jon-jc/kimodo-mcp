"""Build portable, deterministic contact sheets from validated numeric motion.

The renderer uses exact source poses and a camera fitted to every source frame.
It never changes motion, interpolates poses, executes archive data or downloads
browser dependencies. Pair comparisons require matching time and rig metadata.
"""

from __future__ import annotations

import hashlib
import html
import json
import math
import os
import tempfile
from pathlib import Path
from typing import Any, Literal

import numpy as np

from .artifacts import _load_motion

MAX_REVIEW_VALUES = 2_000_000
MAX_REVIEW_CARDS = 24
VIEWS = {
    "Three-quarter": [
        [math.cos(-0.55), 0, math.sin(-0.55)],
        [
            math.sin(-0.55) * math.sin(0.22),
            math.cos(0.22),
            -math.cos(-0.55) * math.sin(0.22),
        ],
    ],
    "Front": [[1, 0, 0], [0, 1, 0]],
    "Side": [[0, 0, -1], [0, 1, 0]],
}


def _source(path: str | Path, sample_index: int) -> tuple[dict[str, Any], np.ndarray]:
    motion = _load_motion(path)
    if motion["fps"] is None or motion["parents"] is None or "joint_names" not in motion["channels"]:
        raise ValueError("Review requires FPS, joint_names and joint_parents metadata.")
    if len(set(motion["names"])) != len(motion["names"]):
        raise ValueError("Joint names must be unique.")
    if isinstance(sample_index, bool) or not isinstance(sample_index, int):
        raise ValueError("Sample index must be an integer.")
    if not 0 <= sample_index < len(motion["positions"]):
        raise ValueError("Sample index is outside this motion archive.")
    poses = motion["positions"][sample_index]
    if poses.size > MAX_REVIEW_VALUES:
        raise ValueError("Exact-frame review exceeds the two-million-coordinate limit.")
    return motion, poses


def _hash(path: Path) -> str:
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def _bind_diagnostics(diagnostics: dict[str, Any] | None, expected: dict[str, Any]) -> None:
    """Reject contradictory report metadata while allowing manual frame notes."""
    if diagnostics is None:
        return
    if not isinstance(diagnostics, dict):
        raise ValueError("Diagnostics must be an object or None.")
    for key, value in expected.items():
        if key not in diagnostics:
            continue
        actual = diagnostics[key]
        if (isinstance(value, (int, float)) and isinstance(actual, bool)) or actual != value:
            raise ValueError(f"Diagnostic {key} does not match the rendered motion or alignment.")


def _bind_sample_content(
    diagnostics: dict[str, Any] | None, motion: dict[str, Any], poses: np.ndarray, sample: int
) -> None:
    if not diagnostics or not ({"content_sha256", "frame_hashes"} & diagnostics.keys()):
        return
    # Reuse canonical hashing on the already validated arrays. This binds an
    # actual QA report to its sample even when another clip has identical timing.
    from .quality import _frame_hashes

    selected = {
        **motion,
        "positions": poses,
        "rotations": {key: value[sample] for key, value in motion["rotations"].items()},
        "contacts": motion["contacts"][sample] if motion["contacts"] is not None else None,
    }
    hashes = _frame_hashes(selected)
    _bind_diagnostics(
        diagnostics,
        {
            "frame_hashes": hashes,
            "content_sha256": hashlib.sha256("".join(hashes).encode()).hexdigest(),
        },
    )


def _clip(motion: dict[str, Any], poses: np.ndarray, sample: int) -> dict[str, Any]:
    return {
        "name": motion["path"].name,
        "sourceSha256": _hash(motion["path"]),
        "sample": sample,
        "frames": len(poses),
        "fps": motion["fps"],
        "parents": motion["parents"],
        "names": motion["names"],
        "skeleton": motion["skeleton"],
        # Python's JSON float serialization round-trips IEEE doubles. Exact
        # source samples are retained; drawing does not substitute interpolated
        # or rounded poses for diagnostic frames.
        "positions": poses.tolist(),
    }


def _cameras(poses: list[np.ndarray]) -> list[dict[str, Any]]:
    cameras = []
    for name, basis in VIEWS.items():
        matrix = np.asarray(basis, dtype=np.float64).T
        # Every frame in each clip contributes, including frames outside a
        # paired overlap. One view therefore keeps exactly one shared framing.
        bounds = [item.reshape(-1, 3) @ matrix for item in poses]
        lower = np.min([item.min(axis=0) for item in bounds], axis=0)
        upper = np.max([item.max(axis=0) for item in bounds], axis=0)
        cameras.append(
            {
                "name": name,
                "basis": basis,
                "minimum": lower.tolist(),
                "maximum": upper.tolist(),
            }
        )
    return cameras


def _review_frames(start: int, stop: int, diagnostics: dict[str, Any] | None) -> list[dict[str, Any]]:
    """Use uniform coverage plus explicitly identified diagnostic source frames."""
    reasons: dict[int, set[str]] = {
        int(frame): {"Timeline sample"}
        for frame in np.unique(np.linspace(start, stop, min(8, stop - start + 1), dtype=np.int64))
    }
    reasons.setdefault(start, set()).add("First available frame")
    reasons.setdefault(stop, set()).add("Last available frame")
    if diagnostics:
        candidates = diagnostics.get("review_frames", [])
        if not isinstance(candidates, list):
            raise ValueError("Diagnostic review_frames must be a list.")
        for entry in candidates[:128]:
            if isinstance(entry, dict):
                frame, reason = (
                    entry.get("frame"),
                    entry.get("reason", "Diagnostic sample"),
                )
            else:
                frame, reason = entry, "Diagnostic sample"
            if isinstance(frame, bool) or not isinstance(frame, int) or not start <= frame <= stop:
                continue
            if frame not in reasons and len(reasons) >= MAX_REVIEW_CARDS:
                continue
            reasons.setdefault(frame, set()).add(str(reason)[:160])
        metrics = diagnostics.get("metrics", {})
        if "position_difference" in diagnostics:
            metrics = {
                **metrics,
                "position_difference": diagnostics["position_difference"],
            }
        for name, metric in metrics.items():
            frame = metric.get("worst_frame_index") if isinstance(metric, dict) else None
            if isinstance(frame, int) and not isinstance(frame, bool) and start <= frame <= stop:
                if frame in reasons or len(reasons) < MAX_REVIEW_CARDS:
                    reasons.setdefault(frame, set()).add("Maximum " + str(name).replace("_", " ")[:140])
    return [{"frame": frame, "reasons": sorted(reasons[frame])} for frame in sorted(reasons)]


def _write(payload: dict[str, Any], output_html: str | Path) -> dict[str, Any]:
    output = Path(output_html).expanduser().resolve()
    if output.suffix.lower() != ".html":
        raise ValueError("Review output must end in .html.")
    data = json.dumps(payload, ensure_ascii=True, separators=(",", ":"), allow_nan=False)
    data = data.replace("&", "\\u0026").replace("<", "\\u003c").replace(">", "\\u003e")
    template = Path(__file__).with_name("review_template.html").read_text(encoding="utf-8")
    rendered = template.replace("__REVIEW_TITLE__", html.escape(payload["title"], quote=True))
    rendered = rendered.replace("__REVIEW_DATA__", data)
    output.parent.mkdir(parents=True, exist_ok=True)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w", encoding="utf-8", dir=output.parent, suffix=".tmp", delete=False
        ) as stream:
            temporary = Path(stream.name)
            stream.write(rendered)
        os.replace(temporary, output)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
    return {
        "path": str(output),
        "kind": payload["kind"],
        "offline": True,
        "exact_frames": True,
        "source_sha256": [clip["sourceSha256"] for clip in payload["clips"]],
        "review_frames": [item["frame"] for item in payload["reviewFrames"]],
        "first_frame": payload["firstFrame"],
        "last_frame": payload["lastFrame"],
        "fps": payload["fps"],
        "alignment": payload["alignment"],
        "assessment_scope": "Visual review of skeletal samples; no mesh, automatic repair or quality score.",
    }


def create_motion_review(
    npz_path: str | Path,
    output_html: str | Path,
    *,
    sample_index: int = 0,
    diagnostics: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Create three fixed views and a contact sheet of exact source poses."""
    motion, poses = _source(npz_path, sample_index)
    _bind_diagnostics(
        diagnostics,
        {
            "sample_index": sample_index,
            "frames": len(poses),
            "fps": motion["fps"],
            "duration_seconds": len(poses) / motion["fps"],
            "last_key_seconds": (len(poses) - 1) / motion["fps"],
        },
    )
    _bind_sample_content(diagnostics, motion, poses, sample_index)
    payload = {
        "schemaVersion": 1,
        "kind": "motion-review",
        "title": "Motion review",
        "clips": [_clip(motion, poses, sample_index)],
        "cameras": _cameras([poses]),
        "fps": motion["fps"],
        "firstFrame": 0,
        "lastFrame": len(poses) - 1,
        "alignment": {"mode": "world", "offset_frames": 0},
        "reviewFrames": _review_frames(0, len(poses) - 1, diagnostics),
        "diagnostics": diagnostics,
    }
    return _write(payload, output_html)


def compare_motion_reviews(
    left_npz: str | Path,
    right_npz: str | Path,
    output_html: str | Path,
    *,
    left_sample: int = 0,
    right_sample: int = 0,
    offset_frames: int = 0,
    mode: Literal["world", "root_relative"] = "world",
    diagnostics: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Compare overlapping exact frames: right_frame = left_frame + offset.

    Root-relative display subtracts each pose's root translation only. It does
    not align facing, retarget rigs, alter saved motion or claim physical parity.
    """
    left, a = _source(left_npz, left_sample)
    right, b = _source(right_npz, right_sample)
    if isinstance(offset_frames, bool) or not isinstance(offset_frames, int):
        raise ValueError("Frame offset must be an integer.")
    if mode not in {"world", "root_relative"}:
        raise ValueError("Comparison mode must be world or root_relative.")
    if left["fps"] != right["fps"]:
        raise ValueError("Exact-frame comparison requires matching FPS; no implicit resampling.")
    if any(left[key] != right[key] for key in ("names", "parents", "skeleton")):
        raise ValueError(
            "Comparison requires identical skeleton, joint names and hierarchy; retarget separately."
        )
    if a.size + b.size > MAX_REVIEW_VALUES:
        raise ValueError("Paired exact-frame review exceeds the two-million-coordinate limit.")
    start, stop = max(0, -offset_frames), min(len(a) - 1, len(b) - 1 - offset_frames)
    if start > stop:
        raise ValueError("The requested frame offset leaves no overlapping samples.")
    _bind_diagnostics(
        diagnostics,
        {
            "first_start_frame": start,
            "second_start_frame": start + offset_frames,
            "frames": stop - start + 1,
            "fps": left["fps"],
            "space": mode,
            "first_sample": left_sample,
            "second_sample": right_sample,
        },
    )
    if mode == "root_relative":
        root = left["parents"].index(-1)
        a, b = a - a[:, root : root + 1], b - b[:, root : root + 1]
    payload = {
        "schemaVersion": 1,
        "kind": "motion-comparison",
        "title": "Motion comparison",
        "clips": [_clip(left, a, left_sample), _clip(right, b, right_sample)],
        "cameras": _cameras([a, b]),
        "fps": left["fps"],
        "firstFrame": start,
        "lastFrame": stop,
        "alignment": {"mode": mode, "offset_frames": offset_frames},
        "reviewFrames": _review_frames(start, stop, diagnostics),
        "diagnostics": diagnostics,
    }
    return _write(payload, output_html)
