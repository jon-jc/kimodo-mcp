"""Immutable, verified FreeMoCap BVH-to-registered-character preparation."""

from __future__ import annotations

import hashlib
import json
import math
import os
import re
import sys
from pathlib import Path
from typing import Annotated, Literal

from filelock import FileLock
from pydantic import BaseModel, ConfigDict, Field, StrictBool, StrictInt, model_validator

from . import freemocap as capture
from . import freemocap_review as review
from .config import Settings
from .handoff_scripts.character import SOURCE_SHA256, landmarks, validate_target
from .jobs import atomic_json

# Reviewed anatomical correspondence, not the addon's automatic roll-changing retargeter.
SOURCE_MAPPING = {
    "Hips": "pelvis",
    "Spine1": "spine",
    "Spine2": "spine",
    "Chest": "spine.001",
    "Neck1": "neck",
    "Neck2": "neck",
    "Head": "face",
}
for _side, _suffix in (("Left", "L"), ("Right", "R")):
    SOURCE_MAPPING.update(
        {
            _side + target: source + "." + _suffix
            for target, source in (
                ("Shoulder", "shoulder"),
                ("Arm", "upper_arm"),
                ("ForeArm", "forearm"),
                ("Hand", "hand"),
                ("Leg", "thigh"),
                ("Shin", "shin"),
                ("Foot", "foot"),
                ("ToeBase", "foot"),
            )
        }
    )
MAX_BVH_BYTES = 128 * 1024**2


class JointCorrection(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)
    bone: Annotated[str, Field(min_length=1, max_length=64)]
    rotation_degrees: tuple[
        Annotated[float, Field(strict=True, ge=-45, le=45)],
        Annotated[float, Field(strict=True, ge=-45, le=45)],
        Annotated[float, Field(strict=True, ge=-45, le=45)],
    ]
    start_frame: Annotated[StrictInt, Field(ge=0, le=143999)]
    end_frame: Annotated[StrictInt, Field(ge=1, le=144000)]
    blend_frames: Annotated[StrictInt, Field(ge=0, le=30)] = 3

    @model_validator(mode="after")
    def valid(self):
        if self.bone not in SOURCE_MAPPING or self.end_frame <= self.start_frame:
            raise ValueError("Choose a fitted non-root bone and increasing original BVH frame window")
        return self


class CaptureRetargetRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)
    start_frame: Annotated[StrictInt, Field(ge=0, le=143998)] = 0
    end_frame: Annotated[StrictInt, Field(ge=2, le=144000)] | None = None
    source_scale_to_meters: Annotated[float, Field(strict=True, ge=1e-6, le=10)]
    source_rest_pose: Literal["tpose", "apose"]
    root_motion: StrictBool = True
    rotation_smoothing_radius: Annotated[StrictInt, Field(ge=0, le=5)] = 0
    root_smoothing_radius: Annotated[StrictInt, Field(ge=0, le=5)] = 0
    smoothing_start_frame: Annotated[StrictInt, Field(ge=0, le=143999)] | None = None
    smoothing_end_frame: Annotated[StrictInt, Field(ge=1, le=144000)] | None = None
    blend_frames: Annotated[StrictInt, Field(ge=0, le=30)] = 3
    joint_corrections: Annotated[list[JointCorrection], Field(max_length=16)] = Field(default_factory=list)

    @model_validator(mode="after")
    def valid(self):
        if self.end_frame is not None and self.end_frame <= self.start_frame + 1:
            raise ValueError("Retarget trim must retain at least two frames")
        if len({c.bone for c in self.joint_corrections}) != len(self.joint_corrections):
            raise ValueError("Only one correction per named joint is supported")
        return self


def _json(path, maximum=1024**2):
    if path.is_symlink() or not path.is_file() or path.stat().st_size > maximum:
        raise ValueError("Expected a bounded regular handoff JSON file")
    value = json.loads(
        path.read_text(encoding="utf-8"), parse_constant=lambda v: (_ for _ in ()).throw(ValueError(v))
    )
    if not isinstance(value, dict):
        raise ValueError("Expected a handoff JSON object")
    return value


def _sha(path):
    return capture._digest(path)


def _canonical(value):
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()
    ).hexdigest()


def _templates():
    base = Path(__file__).with_name("handoff_scripts")
    return {
        "capture_character_handoff.py": base / "capture_character.py",
        "character_helper.py": base / "character.py",
    }


def _read_bvh(path):
    """Bounded numeric BVH parsing, retaining authored channels and rest offsets."""
    if not path.is_file() or path.is_symlink() or not 1 <= path.stat().st_size <= MAX_BVH_BYTES:
        raise ValueError("Retarget requires a regular BVH no larger than 128 MiB")
    text = path.read_text(encoding="utf-8")
    parts = re.split(r"(?m)^\s*MOTION\s*$", text)
    if len(parts) != 2:
        raise ValueError("Expected one BVH hierarchy and motion section")
    tokens = re.findall(r"[{}]|[^\s{}]+", parts[0])
    index, bones, channels = 0, [], 0

    def take(expected=None):
        nonlocal index
        if index >= len(tokens):
            raise ValueError("Incomplete BVH hierarchy")
        value = tokens[index]
        index += 1
        if expected is not None and value != expected:
            raise ValueError("Unexpected BVH hierarchy token")
        return value

    def vector():
        values = [float(take()) for _ in range(3)]
        if any(not math.isfinite(v) or abs(v) > 1e7 for v in values):
            raise ValueError("Invalid BVH rest offset")
        return values

    def joint(parent=None, depth=0):
        nonlocal channels
        if depth > 128 or len(bones) >= 256:
            raise ValueError("BVH hierarchy exceeds limits")
        take("ROOT" if parent is None else "JOINT")
        name = take()
        if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_.-]{0,127}", name) or any(b["name"] == name for b in bones):
            raise ValueError("BVH bone names must be unique bounded identifiers")
        take("{")
        take("OFFSET")
        offset = vector()
        take("CHANNELS")
        count = int(take())
        if count not in (3, 6):
            raise ValueError("BVH bones require rotation or position-and-rotation channels")
        names = [take() for _ in range(count)]
        rotations = [n for n in names if n.endswith("rotation")]
        if (
            set(rotations) != {"Xrotation", "Yrotation", "Zrotation"}
            or len(set(names)) != count
            or any(
                n not in {axis + kind for axis in "XYZ" for kind in ("position", "rotation")} for n in names
            )
        ):
            raise ValueError("Unsupported or duplicate BVH channels")
        bones.append({"name": name, "parent": parent, "offset": offset, "channels": names})
        channels += count
        while tokens[index] != "}":
            if tokens[index] == "JOINT":
                joint(name, depth + 1)
            else:
                take("End")
                take("Site")
                take("{")
                take("OFFSET")
                vector()
                take("}")
        take("}")

    try:
        take("HIERARCHY")
        joint()
    except (IndexError, OverflowError) as exc:
        raise ValueError("Invalid BVH hierarchy") from exc
    if index != len(tokens):
        raise ValueError("Unexpected content after BVH hierarchy")
    lines = [line.strip() for line in parts[1].splitlines() if line.strip()]
    if len(lines) < 3 or not re.fullmatch(r"Frames:\s*[0-9]+", lines[0]):
        raise ValueError("Invalid BVH motion header")
    frames = int(lines[0].split(":")[1])
    match = re.fullmatch(r"Frame Time:\s*([0-9.eE+-]+)", lines[1])
    interval = float(match[1]) if match else 0
    if not math.isfinite(interval) or interval <= 0:
        raise ValueError("Invalid BVH frame interval")
    fps = round(1 / interval)
    if not 1 <= fps <= 120 or abs(1 / interval - fps) > 0.001:
        raise ValueError(
            "Retarget preserves integer frame rates from 1 to 120; resample fractional FPS explicitly"
        )
    if not 2 <= frames <= 144000 or len(lines) != frames + 2:
        raise ValueError("BVH frame count differs from its motion rows")
    for row in lines[2:]:
        values = [float(v) for v in row.split()]
        if len(values) != channels or any(not math.isfinite(v) or abs(v) > 1e9 for v in values):
            raise ValueError("BVH contains invalid or incomplete numeric channels")
    missing = sorted(set(SOURCE_MAPPING.values()) - {b["name"] for b in bones})
    if missing:
        raise ValueError("BVH does not match the reviewed FreeMoCap rig: " + ", ".join(missing))
    parents = {b["name"]: b["parent"] for b in bones}
    for suffix in ("L", "R"):
        for child, parent in (
            ("upper_arm", "shoulder"),
            ("forearm", "upper_arm"),
            ("hand", "forearm"),
            ("shin", "thigh"),
            ("foot", "shin"),
        ):
            if parents[child + "." + suffix] != parent + "." + suffix:
                raise ValueError("Source limb hierarchy differs from the reviewed mapping")
    return {
        "frames": frames,
        "fps": fps,
        "bones": bones,
        "channels": channels,
        "hierarchy": parts[0].rstrip(),
        "rows": lines[2:],
    }


def _source(settings, job_id, artifact_path):
    client, receipt_path, receipt = review.verified_receipt(settings, job_id)
    if receipt["operation"] != "export":
        raise ValueError("Retargeting requires a verified FreeMoCap export receipt")
    artifact = review.get_freemocap_artifact(settings, job_id, artifact_path)
    if Path(artifact_path).suffix.lower() != ".bvh":
        raise ValueError("Select an exact BVH output from the export receipt")
    process = review._process_receipt(client, receipt)
    if (
        capture.FreeMocapRequest.model_validate(process["request"]).process_options.detector_type
        != "mediapipe"
    ):
        raise ValueError("Reviewed FreeMoCap BVH export supports MediaPipe only")
    source = _read_bvh(Path(artifact["absolute_path"]))
    videos = process.get("video_metadata", {}).get("videos", [])
    if not videos or any(abs(v.get("fps", 0) - source["fps"]) > 0.01 for v in videos):
        raise ValueError("BVH FPS must match persisted processing video timing")
    process_path, _ = client.read_job(process["job_id"])
    binding = {
        "job_id": job_id,
        "artifact_path": artifact_path,
        "source_bvh": artifact["absolute_path"],
        "source_sha256": artifact["sha256"],
        "export_receipt_sha256": _sha(receipt_path),
        "process_job_id": process["job_id"],
        "process_receipt_sha256": _sha(process_path),
        "process_frame_count": videos[0]["frame_count"],
        "source_frames": source["frames"],
        "fps": source["fps"],
    }
    review.verified_receipt(settings, job_id)
    review.verified_receipt(settings, process["job_id"])
    if _sha(Path(binding["source_bvh"])) != binding["source_sha256"]:
        raise ValueError("BVH changed while it was inspected")
    return source, binding


def inspect_capture_retarget_source(settings, job_id, artifact_path):
    source, binding = _source(settings, job_id, artifact_path)
    return binding | {
        "bones": source["bones"],
        "mapping": SOURCE_MAPPING,
        "target_profile_sha256": SOURCE_SHA256,
        "requires_source_scale": True,
        "requires_rest_pose": True,
        "frame_count_matches_process": source["frames"] == binding["process_frame_count"],
        "notes": [
            "Source BVH indices are authoritative; they may differ from landmark frame numbering.",
            "Source non-root translations are discarded when preserving target bone lengths.",
            "ToeBase inherits foot rotation; fingers, face and cloth are not transferred.",
            "Cleanup smooths selected rotations/root positions; it does not detect or repair foot contacts.",
        ],
    }


def _registered_character(settings):
    path = settings.data_dir / "character" / "target.json"
    if os.environ.get("KIMODO_CHARACTER_FBX"):
        source = Path(os.environ["KIMODO_CHARACTER_FBX"])
    else:
        value = _json(path, 8192)
        source = Path(value["source_fbx"])
    if not source.is_absolute():
        raise ValueError("Registered character path must be absolute")
    return validate_target(source)


def _resolved(options, frames):
    options = CaptureRetargetRequest.model_validate(options)
    value = options.model_dump(mode="json")
    start, end = options.start_frame, options.end_frame or frames
    if not 0 <= start < end <= frames or not 2 <= end - start <= 2400:
        raise ValueError("Select a source trim containing 2..2400 original BVH frames")
    value.update(
        end_frame=end,
        smoothing_start_frame=options.smoothing_start_frame
        if options.smoothing_start_frame is not None
        else start,
        smoothing_end_frame=options.smoothing_end_frame if options.smoothing_end_frame is not None else end,
    )
    if not start <= value["smoothing_start_frame"] < value["smoothing_end_frame"] <= end:
        raise ValueError("Smoothing window must lie inside the selected original-frame trim")
    for correction in value["joint_corrections"]:
        if not start <= correction["start_frame"] < correction["end_frame"] <= end:
            raise ValueError("Joint correction windows must lie inside the selected original-frame trim")
    return value


def _directory(settings, handoff_id):
    if not isinstance(handoff_id, str) or not re.fullmatch(r"[0-9a-f]{64}", handoff_id):
        raise ValueError("Invalid capture handoff ID")
    base = settings.data_dir.resolve() / "freemocap" / "retargets"
    path = base / handoff_id
    if path.resolve() != path:
        raise ValueError("Capture handoff storage cannot redirect")
    return path


def prepare_capture_character_handoff(settings, job_id, artifact_path, options, attempt=0):
    if type(attempt) is not int or not 0 <= attempt <= 99:
        raise ValueError("Attempt must be an integer from 0 to 99")
    source, binding = _source(settings, job_id, artifact_path)
    options = _resolved(options, source["frames"])
    character = _registered_character(settings)
    scripts = {
        name: hashlib.sha256(path.read_text(encoding="utf-8").encode()).hexdigest()
        for name, path in _templates().items()
    }
    identity = {
        "schema_version": 1,
        "kind": "capture_character",
        **binding,
        "options": options,
        "character_fbx": str(character),
        "character_sha256": SOURCE_SHA256,
        "scripts": scripts,
        "attempt": attempt,
        "mapping": SOURCE_MAPPING,
    }
    handoff_id = _canonical(identity)
    directory = _directory(settings, handoff_id)
    directory.parent.mkdir(parents=True, exist_ok=True)
    with FileLock(str(directory.parent / (handoff_id + ".lock")), timeout=1):
        if directory.exists():
            verify_capture_preparation(settings, handoff_id)
            return get_capture_character_report(settings, handoff_id) | {"reused": True}
        directory.mkdir()
        trimmed = (
            source["hierarchy"]
            + f"\nMOTION\nFrames: {options['end_frame'] - options['start_frame']}\nFrame Time: {1 / source['fps']:.12g}\n"
        )
        trimmed += "\n".join(source["rows"][options["start_frame"] : options["end_frame"]]) + "\n"
        (directory / "input.bvh").write_text(trimmed, encoding="utf-8", newline="\n")
        for name, path in _templates().items():
            (directory / name).write_text(path.read_text(encoding="utf-8"), encoding="utf-8", newline="\n")
        request = identity | {
            "handoff_id": handoff_id,
            "frames": options["end_frame"] - options["start_frame"],
            "input_bvh": str(directory / "input.bvh"),
            "input_sha256": _sha(directory / "input.bvh"),
        }
        atomic_json(directory / "handoff.json", request)
        verify_capture_preparation(settings, handoff_id)
    return get_capture_character_report(settings, handoff_id) | {"reused": False}


def verify_capture_preparation(settings, handoff_id):
    directory = _directory(settings, handoff_id)
    request = _json(directory / "handoff.json")
    identity = {
        key: value
        for key, value in request.items()
        if key not in {"handoff_id", "frames", "input_bvh", "input_sha256"}
    }
    if (
        request.get("handoff_id") != handoff_id
        or _canonical(identity) != handoff_id
        or request.get("kind") != "capture_character"
    ):
        raise ValueError("Capture preparation identity changed")
    source, binding = _source(settings, request["job_id"], request["artifact_path"])
    if any(request.get(key) != value for key, value in binding.items()):
        raise ValueError("Capture source provenance changed after preparation")
    if request["options"] != _resolved(request["options"], source["frames"]):
        raise ValueError("Capture cleanup options changed")
    if request.get("mapping") != SOURCE_MAPPING:
        raise ValueError("Source mapping differs from the reviewed anatomical profile")
    validate_target(request["character_fbx"])
    if (
        request["character_sha256"] != SOURCE_SHA256
        or request["frames"] != request["options"]["end_frame"] - request["options"]["start_frame"]
    ):
        raise ValueError("Capture target or trim changed")
    for name, trusted in _templates().items():
        path = directory / name
        checksum = hashlib.sha256(trusted.read_text(encoding="utf-8").encode()).hexdigest()
        if path.is_symlink() or _sha(path) != checksum or request["scripts"].get(name) != checksum:
            raise ValueError("Prepared capture script differs from the bundled implementation")
    path = directory / "input.bvh"
    if request["input_bvh"] != str(path) or path.is_symlink() or _sha(path) != request["input_sha256"]:
        raise ValueError("Prepared trimmed BVH changed")
    expected = (
        source["hierarchy"]
        + f"\nMOTION\nFrames: {request['frames']}\nFrame Time: {1 / request['fps']:.12g}\n"
    )
    expected += (
        "\n".join(source["rows"][request["options"]["start_frame"] : request["options"]["end_frame"]]) + "\n"
    )
    if path.read_text(encoding="utf-8") != expected:
        raise ValueError("Prepared BVH is not the exact selected original trim")
    bones = [{"name": n, "parent": values[2]} for n, values in landmarks().items()]
    return directory, request, bones


def _validate_capture_request(request, directory):
    settings = Settings(data_dir=directory.parents[2], python=sys.executable)
    actual, stored, bones = verify_capture_preparation(settings, directory.name)
    if actual != directory or stored != request:
        raise ValueError("Capture request is outside managed storage")
    return bones


def _validate_capture_report(directory, request, bones):
    from .blender_bridge import _output, _read_json, _same_hierarchy, _validate_glb

    report = _read_json(directory / "blender-report.json")
    if (
        report.get("kind") != "blender"
        or report.get("schema_version") != 1
        or report.get("status") != "completed"
        or report.get("source_kind") != "capture_character"
        or report.get("handoff_id") != request["handoff_id"]
        or report.get("source_sha256") != request["source_sha256"]
        or report.get("source_bvh") != request["source_bvh"]
        or report.get("character_sha256") != SOURCE_SHA256
        or report.get("fps") != request["fps"]
        or report.get("frames") != request["frames"]
        or report.get("frame_range") != [1, request["frames"]]
        or not _same_hierarchy(report.get("bones"), bones)
        or not _same_hierarchy(report.get("fbx_bones"), bones)
    ):
        raise ValueError("Capture completion report differs from its preparation")
    validation = report.get("validation", {})
    if (
        validation.get("finite") is not True
        or validation.get("frames") != request["frames"]
        or validation.get("unweighted_vertices") != 0
    ):
        raise ValueError("Capture character validation failed")
    for key, limit in (("max_weight_sum_error", 1e-5), ("max_bone_length_error_m", 1e-4)):
        value = validation.get(key)
        if type(value) not in (int, float) or not math.isfinite(value) or not 0 <= value <= limit:
            raise ValueError("Capture character deformation validation failed")
    outputs = []
    for key, filename in (
        ("fbx", "animation.fbx"),
        ("character_fbx", "character.fbx"),
        ("blend", "character.blend"),
        ("preview_glb", "character-preview.glb"),
        ("cleanup", "cleanup-report.json"),
        ("cleaned_motion", "cleaned-motion.npz"),
    ):
        path = _output(report, directory, key, filename)
        outputs.append(
            {"path": filename, "absolute_path": str(path), "sha256": _sha(path), "bytes": path.stat().st_size}
        )
    _validate_glb(directory / "character-preview.glb", request, bones)
    return report | {"outputs": outputs, "report_path": str(directory / "blender-report.json")}


def get_capture_character_report(settings, handoff_id):
    directory, request, bones = verify_capture_preparation(settings, handoff_id)
    base = {
        "handoff_id": handoff_id,
        "kind": "capture_character",
        "directory": str(directory),
        "script_path": str(directory / "capture_character_handoff.py"),
        "request_path": str(directory / "handoff.json"),
        "report_path": str(directory / "blender-report.json"),
        "request": request,
        "status": "prepared",
        "outputs": [],
    }
    if (directory / "blender-report.json").exists():
        return base | _validate_capture_report(directory, request, bones)
    if (directory / "failure.json").exists():
        return base | {
            "status": "failed",
            "error": _json(directory / "failure.json").get("error", "Capture retarget failed"),
        }
    if (directory / "bridge-dispatch.json").exists() or (directory / "execution.lock").exists():
        return base | {
            "status": "outcome_unknown",
            "note": "Execution was dispatched; inspect its existing result before a new attempt",
        }
    return base
