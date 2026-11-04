"""Offline, receipt-bound landmark review. No runtime imports or inferred rig rotations."""
from __future__ import annotations

import math
from contextlib import contextmanager
from pathlib import Path

import numpy as np

from . import freemocap as capture
from .freemocap_profiles import get_landmark_profile

MAX_NPY_BYTES = 4 * 1024**3
ASPECTS = {"body", "left_hand", "right_hand", "face"}


def verified_receipt(settings, job_id):
    """Verify saved evidence without consulting the external service."""
    client = capture.FreeMoCapClient(settings)
    path, receipt = client.read_job(job_id)
    if (receipt.get("schema_version") != 1 or receipt.get("operation") not in {"process", "export"}
            or receipt.get("status") != "completed" or receipt.get("pipeline_completion_verified") is not True):
        raise ValueError("Review requires a verified completed process or export receipt")
    capture.FreeMocapRequest.model_validate(receipt["request"])
    entries = receipt.get("outputs")
    if (not isinstance(entries, list) or not 1 <= len(entries) <= 512
            or len({entry["path"] for entry in entries}) != len(entries)):
        raise ValueError("Invalid receipt output inventory")
    capture._refresh_pipeline(client, path, receipt)
    return client, path, receipt


def _process_receipt(client, receipt):
    if receipt["operation"] == "process":
        return receipt
    matches = []
    paths = list((client.directory / "jobs").glob("*.json"))
    if len(paths) > 10000:
        raise ValueError("Capture receipt inventory exceeds the review limit")
    for path in paths:
        value = capture._json(path)
        if (value.get("runtime_key") == client.runtime_key and value.get("recording_id") == receipt["recording_id"]
                and value.get("operation") == "process" and value.get("status") == "completed"
                and value.get("pipeline_completion_verified") is True):
            capture._refresh_pipeline(client, path, value)
            matches.append(value)
    if len(matches) != 1:
        raise ValueError("Export review requires one unambiguous verified processing receipt")
    return matches[0]


def _timing(receipt, count):
    videos = receipt.get("video_metadata", {}).get("videos", [])
    if not videos:
        return None, "unavailable: legacy receipt has no persisted video timing"
    if any(type(v.get("frame_count")) is not int or v["frame_count"] != count for v in videos):
        raise ValueError("Landmark frame count differs from submitted video metadata")
    fps = [v.get("fps") for v in videos]
    if any(type(f) not in (int, float) or not math.isfinite(f) or not 1 <= f <= 240 for f in fps):
        raise ValueError("Invalid persisted video frame rate")
    if max(fps) - min(fps) > .01:
        raise ValueError("Persisted video frame rates disagree")
    return float(fps[0]), "nominal video frame rate; not measured capture timestamps"


def _units(client, process, calibration_evidence):
    videos = [s for s in process["sources"] if Path(s["path"]).suffix.lower() in capture.VIDEO_EXTENSIONS]
    if not 1 <= len(videos) <= 8:
        raise ValueError("Processing receipt has no bounded video source set")
    if len(videos) == 1:
        return {"units": "pixels", "up_axis": "Z", "mode": "planar", "depth_available": False,
                "origin": "detected-image bounds; Y=0", "floor_verified": False}
    # A generic calibration file does not prove the scale of its translations.
    units = "calibration_units"
    calibration_id = process["request"].get("calibration_recording_id")
    if calibration_id:
        paths = list((client.directory / "jobs").glob("*.json"))
        if len(paths) > 10000:
            raise ValueError("Capture receipt inventory exceeds the review limit")
        for path in paths:
            value = capture._json(path)
            if (value.get("runtime_key") == client.runtime_key and value.get("recording_id") == calibration_id
                    and value.get("operation") == "calibrate" and value.get("status") == "completed"
                    and value.get("pipeline_completion_verified") is True):
                controls = capture.FreeMocapRequest.model_validate(value["request"])
                capture._refresh_pipeline(client, path, value)
                folder = client.recording(calibration_id, owned=True)
                matching = any(
                    Path(source["path"]).is_absolute()
                    and Path(source["path"]).resolve() == (folder / output["path"]).resolve()
                    and source["sha256"] == output["sha256"]
                    and Path(output["path"]).suffix.lower() == ".toml"
                    for source in process["sources"] for output in value["outputs"]
                )
                if controls.board is not None and matching:
                    digest = capture._digest(path)
                    if capture._json(path) != value:
                        raise ValueError("Calibration receipt changed during review")
                    calibration_evidence.append((path, digest))
                    units = "millimeters"
    return {"units": units, "up_axis": "Z", "mode": "calibrated", "depth_available": True,
            "origin": "calibration coordinates; upstream may ground-align by feet", "floor_verified": False}


def _array(path, points):
    if not path.is_file() or not 1 <= path.stat().st_size <= MAX_NPY_BYTES:
        raise ValueError("Landmark array exceeds the 4 GiB saved-output budget")
    with path.open("rb") as stream:
        version = np.lib.format.read_magic(stream)
        if version == (1, 0):
            shape, _, dtype = np.lib.format.read_array_header_1_0(stream)
        elif version == (2, 0):
            shape, _, dtype = np.lib.format.read_array_header_2_0(stream)
        else:
            raise ValueError("Unsupported NPY format")
        if (dtype.kind != "f" or dtype.itemsize not in (4, 8) or len(shape) != 3
                or shape[1:] != (points, 3) or not 1 <= shape[0] <= 144000
                or math.prod(shape) * dtype.itemsize != path.stat().st_size - stream.tell()):
            raise ValueError("Landmark array layout does not match the reviewed detector profile")
    return np.load(path, allow_pickle=False, mmap_mode="r")


def _load(settings, job_id, aspect="body"):
    if aspect not in ASPECTS:
        raise ValueError("Unknown landmark aspect")
    client, receipt_path, receipt = verified_receipt(settings, job_id)
    receipt_sha = capture._digest(receipt_path)
    if client.read_job(job_id)[1] != receipt:
        raise ValueError("Capture receipt changed during selection")
    process = _process_receipt(client, receipt)
    process_path, stored_process = client.read_job(process["job_id"])
    if stored_process != process:
        raise ValueError("Processing receipt changed during selection")
    process_sha = capture._digest(process_path)
    if client.read_job(process["job_id"])[1] != process:
        raise ValueError("Processing receipt changed during selection")
    detector = capture.FreeMocapRequest.model_validate(process["request"]).process_options.detector_type
    profile = get_landmark_profile(detector, aspect)
    relative = f"output_data/{detector}_{aspect}_3d_xyz.npy"
    entry = next((e for e in receipt["outputs"] if e["path"] == relative), None)
    native = next((e for e in process["outputs"] if e["path"] == relative), None)
    if entry is None or entry != native:
        raise ValueError("Selected landmark file is not bound to the verified processing output")
    path = capture._relative(client.recording(receipt["recording_id"], owned=True), relative, suffixes={".npy"})
    values = _array(path, len(profile["names"]))
    try:
        fps, timing = _timing(process, len(values))
        calibration_evidence = []
        coordinates = _units(client, process, calibration_evidence)
        metadata = {"job_id": job_id, "processing_job_id": process["job_id"], "recording_id": receipt["recording_id"],
                    "source_kind": "freemocap", "detector": detector, "aspect": aspect,
                    "skeleton": f"freemocap-{detector}-{aspect}{len(profile['names'])}",
                    "frames": len(values), "fps": fps, "timing_basis": timing,
                    "motion_sha256": entry["sha256"], "source_sha256": entry["sha256"],
                    "source_files": process["sources"], "profile": profile,
                    "profile_binding": {"basis": "submitted detector and exact saved-array layout",
                                        "runtime_dependency_revision_verified": False},
                    "joint_names": profile["names"], "joint_parents": profile["parents"],
                    "tracked_mask": profile["tracked_flags"], "coordinates": coordinates,
                    "contact_status": "unavailable", "confidence_status": "unavailable",
                    "editable": {"supported": False, "reason": "Landmarks have no fitted local rotations or SOMA rig"},
                    "notes": ["Validity describes processed output after interpolation/filtering, not observed tracking coverage.",
                              "Tracker schema JSON is ignored because the reviewed upstream can mislabel MediaPipe output.",
                              "Body connections describe landmark relationships, not a rigid animation skeleton."]}
        def verify_after():
            _close(values)
            if capture._digest(receipt_path) != receipt_sha or capture._digest(process_path) != process_sha:
                raise ValueError("Capture receipt changed during review")
            verified_receipt(settings, job_id)
            if process["job_id"] != job_id:
                verified_receipt(settings, process["job_id"])
            if any(capture._digest(p) != digest for p, digest in calibration_evidence):
                raise ValueError("Calibration receipt changed during review")
            if capture._digest(path) != entry["sha256"]:
                raise ValueError("Landmark output changed during review")
        return values, metadata, verify_after
    except BaseException:
        _close(values)
        raise


def _close(values):
    if isinstance(values, np.memmap) and not values._mmap.closed:
        values._mmap.close()


@contextmanager
def _loaded(settings, job_id, aspect="body"):
    values, metadata, verify = _load(settings, job_id, aspect)
    try:
        yield values, metadata, verify
    finally:
        _close(values)


def _frames(values, indices, fps):
    rows = values[indices]
    if np.isinf(rows).any() or np.any(np.abs(rows[np.isfinite(rows)]) > 1e9):
        raise ValueError("Selected landmarks contain infinite or excessive coordinates")
    valid = np.isfinite(rows).all(axis=-1)
    positions = np.where(valid[..., None], rows, np.nan).astype(object)
    positions[~np.isfinite(rows) | ~valid[..., None]] = None
    return positions.tolist(), valid.tolist(), [i / fps if fps else None for i in indices]


def inspect_freemocap_frames(settings, job_id, frame_indices=None, aspect="body"):
    indices = [0] if frame_indices is None or frame_indices == [] else frame_indices
    if (not isinstance(indices, list) or not 1 <= len(indices) <= 16
            or any(type(i) is not int or i < 0 for i in indices) or len(set(indices)) != len(indices)):
        raise ValueError("Select at most 16 unique nonnegative integer frame indices")
    with _loaded(settings, job_id, aspect) as (values, metadata, verify):
        if max(indices) >= len(values):
            raise ValueError("Selected frame is outside this recording")
        positions, valid, times = _frames(values, indices, metadata["fps"])
        result = metadata | {"frames_inspected": [{"frame_index": i, "time_seconds": t,
                "positions_native": p, "valid_mask": v} for i, t, p, v in zip(indices, times, positions, valid)]}
        verify()
        return result

def preview_freemocap(settings, job_id, start_frame=0, count=240):
    if type(start_frame) is not int or start_frame < 0 or type(count) is not int or not 1 <= count <= 2400:
        raise ValueError("Preview requires nonnegative start_frame and count 1..2400")
    with _loaded(settings, job_id) as (values, metadata, verify):
        if start_frame >= len(values):
            raise ValueError("Preview starts outside this recording")
        indices = list(range(start_frame, min(start_frame + count, len(values))))
        positions, valid, times = _frames(values, indices, metadata["fps"])
        result = metadata | {"total_frames": len(values), "frames": len(indices), "start_frame": start_frame,
                             "frame_indices": indices, "positions": positions, "valid_mask": valid,
                             "source_time_seconds": times, "sampling": "contiguous_original_frames"}
        verify()
        return result

def analyze_freemocap_quality(settings, job_id):
    with _loaded(settings, job_id) as (values, metadata, verify):
        if np.isinf(values).any() or np.any(np.abs(values[np.isfinite(values)]) > 1e9):
            raise ValueError("Body landmarks contain infinite or excessive coordinates")
        valid = np.isfinite(values).all(axis=-1)
        missing = ~valid
        runs = np.zeros(values.shape[1], dtype=int)
        longest = np.zeros_like(runs)
        for row in missing:
            runs = (runs + 1) * row
            longest = np.maximum(longest, runs)
        delta = np.linalg.norm(np.diff(values.astype(np.float64), axis=0), axis=-1)
        eligible = valid[1:] & valid[:-1]
        delta[~eligible] = np.nan
        fps = metadata["fps"]
        metric = delta * fps if fps else delta
        finite = metric[np.isfinite(metric)]
        worst = int(np.nanargmax(metric) // metric.shape[1] + 1) if finite.size else None
        units = metadata["coordinates"]["units"]
        metric_report = {"available": bool(finite.size), "unit": units + ("/s" if fps else "/frame"),
                         "maximum": float(finite.max()) if finite.size else None,
                         "p95": float(np.percentile(finite, 95)) if finite.size else None,
                         "worst_frame_index": worst, "eligible_intervals": int(eligible.sum())}
        review = set(np.linspace(0, len(values) - 1, min(8, len(values)), dtype=int).tolist())
        review.add(int(np.argmax(missing.sum(axis=1))))
        if worst is not None:
            review.add(worst)
        report = metadata | {"diagnostic_scope": "body processed-landmark validity and adjacent-frame movement",
                             "coverage": {"valid_fraction": float(valid.mean()),
                             "per_landmark_valid_fraction": valid.mean(axis=0).tolist(),
                             "longest_missing_run_frames": longest.tolist(),
                             "frames_with_missing_landmarks": int(missing.any(axis=1).sum())},
                             "movement": metric_report, "review_frames": sorted(review),
                             "quality_score": None}
        verify()
        return report

def get_freemocap_artifact(settings, job_id, path):
    client, receipt_path, receipt = verified_receipt(settings, job_id)
    before = capture._digest(receipt_path)
    if client.read_job(job_id)[1] != receipt:
        raise ValueError("Capture receipt changed during selection")
    entry = next((item for item in receipt["outputs"] if item["path"] == path), None)
    if entry is None:
        raise ValueError("Choose an exact output path from the verified receipt")
    source = capture._relative(client.recording(receipt["recording_id"], owned=True), path,
                               suffixes=capture.OUTPUT_EXTENSIONS)
    verified_receipt(settings, job_id)
    if capture._digest(receipt_path) != before or capture._digest(source) != entry["sha256"]:
        raise ValueError("Capture artifact changed during verification")
    return {"job_id": job_id, "source_kind": "freemocap", **entry, "absolute_path": str(source)}
