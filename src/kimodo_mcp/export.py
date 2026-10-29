"""Verified, weight-free exports of completed managed motion samples."""

from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
from pathlib import Path
from typing import Annotated, Literal

import numpy as np
from filelock import FileLock, Timeout
from pydantic import BaseModel, ConfigDict, Field, model_validator

from .artifacts import _check_archive, _load_motion
from .catalog import UPSTREAM_REVISION, model_info
from .config import Settings
from .jobs import atomic_json

EXPORT_VERSION = 1
EXPORT_TIMEOUT = 120
MAX_EXPORT_BYTES = 128 * 1024 * 1024
SHA = Annotated[str, Field(pattern=r"^[0-9a-f]{64}$", strict=True)]


class ExportRequest(BaseModel):
    """Internal request: paths are assigned by the server, never by a tool caller."""

    model_config = ConfigDict(extra="forbid", strict=True)
    schema_version: Literal[1] = 1
    job_id: Annotated[str, Field(pattern=r"^[0-9a-f]{32}$")]
    sample_index: Annotated[int, Field(ge=0, le=3)] = 0
    source_path: str
    source_sha256: SHA
    output_directory: str
    format: Literal["bvh", "amass", "csv"]
    bvh_standard_tpose: bool = False
    amass_z_up: bool = True
    mujoco_rest_zero: bool = False

    @model_validator(mode="after")
    def compatible_options(self):
        if self.format != "bvh" and self.bvh_standard_tpose:
            raise ValueError("bvh_standard_tpose applies only to BVH exports.")
        if self.format != "amass" and not self.amass_z_up:
            raise ValueError("amass_z_up applies only to AMASS exports.")
        if self.format != "csv" and self.mujoco_rest_zero:
            raise ValueError("mujoco_rest_zero applies only to CSV exports.")
        return self

    @property
    def basename(self) -> str:
        suffix = {"bvh": ".bvh", "amass": "_amass.npz", "csv": ".csv"}[self.format]
        return f"motion_{self.sample_index:02d}{suffix}"

    def options(self) -> dict:
        return self.model_dump(include={"format", "bvh_standard_tpose", "amass_z_up", "mujoco_rest_zero"})


def digest(path: Path) -> str:
    value = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            value.update(block)
    return value.hexdigest()


def export_id(request: ExportRequest) -> str:
    payload = {
        "version": EXPORT_VERSION,
        "reviewed_upstream_revision": UPSTREAM_REVISION,
        "source_sha256": request.source_sha256,
        "sample_index": request.sample_index,
        **request.options(),
    }
    return hashlib.sha256(json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def _paths(request: ExportRequest) -> tuple[Path, Path]:
    source = Path(request.source_path).resolve()
    output = Path(request.output_directory).resolve()
    expected = source.parent / "exports" / export_id(request)
    if (
        source.name != f"motion_{request.sample_index:02d}.npz"
        or source.parent.name != request.job_id
        or output != expected
        or not output.is_relative_to(source.parent)
    ):
        raise ValueError("Export paths must be assigned inside the source job directory.")
    return source, output


def load_source(request: ExportRequest) -> dict:
    source, _ = _paths(request)
    if (
        not source.is_file()
        or source.stat().st_size > MAX_EXPORT_BYTES
        or digest(source) != request.source_sha256
    ):
        raise ValueError("Source motion is missing, oversized or differs from its completed-job digest.")
    motion = _load_motion(source)
    samples, frames, joints, _ = motion["positions"].shape
    if samples != 1 or not 1 <= frames <= 2400:
        raise ValueError("Export requires one sample with at most 2400 frames.")
    if motion["fps"] is None or motion["parents"] is None or "joint_names" not in motion["channels"]:
        raise ValueError("Export requires explicit FPS, joint names and hierarchy metadata.")
    if "local_rot_mats" not in motion["rotations"]:
        raise ValueError("Export requires local_rot_mats.")
    info = model_info(motion["model"])
    allowed = {
        "bvh": {"somaskel30": 30, "somaskel77": 77},
        "amass": {"smplx22": 22},
        "csv": {"g1skel34": 34},
    }[request.format]
    if request.format not in info["formats"] or allowed.get(motion["skeleton"]) != joints:
        raise ValueError("Requested format is incompatible with the source model/skeleton.")
    matrices = motion["rotations"]["local_rot_mats"][0].astype(np.float64)
    if (
        np.max(np.abs(matrices.swapaxes(-1, -2) @ matrices - np.eye(3))) > 1e-3
        or np.max(np.abs(np.linalg.det(matrices) - 1)) > 1e-3
    ):
        raise ValueError("Export requires proper local rotation matrices without reflection or scaling.")
    return motion


def validate_artifact(path: Path, request: ExportRequest, frames: int, fps: float) -> None:
    if not path.is_file() or not 0 < path.stat().st_size <= MAX_EXPORT_BYTES:
        raise ValueError("Export artifact is missing, empty or oversized.")
    if request.format == "bvh":
        text = path.read_text(encoding="utf-8")
        header, separator, body = text.partition("MOTION")
        timing = re.match(r"\s*Frames:\s*(\d+)\s*Frame Time:\s*([\d.eE+-]+)\s*\n", body)
        if not separator or not header.startswith("HIERARCHY") or timing is None:
            raise ValueError("BVH has an invalid hierarchy or motion header.")
        offsets = [line.split()[1:] for line in header.splitlines() if line.strip().startswith("OFFSET ")]
        if (
            not offsets
            or any(len(row) != 3 for row in offsets)
            or not np.isfinite(np.array(offsets, dtype=float)).all()
        ):
            raise ValueError("BVH hierarchy offsets must be finite triples.")
        count, interval = int(timing[1]), float(timing[2])
        channels = sum(int(value) for value in re.findall(r"\bCHANNELS\s+(\d+)", header))
        if count != frames or not np.isfinite(interval) or not np.isclose(interval, 1 / fps, rtol=1e-5):
            raise ValueError("BVH frame count or frame rate differs from its source.")
        lines = body[timing.end() :].splitlines()
        if len(lines) != frames or not 1 <= channels <= 500:
            raise ValueError("BVH motion rows are incomplete or malformed.")
        tokens = [line.split() for line in lines]
        if any(len(row) != channels for row in tokens):
            raise ValueError("BVH motion channel count is inconsistent.")
        values = np.array([[float(value) for value in row] for row in tokens])
        if values.shape != (frames, channels) or not np.isfinite(values).all():
            raise ValueError("BVH motion contains invalid channel values.")
    elif request.format == "csv":
        lines = path.read_text(encoding="utf-8").splitlines()
        if len(lines) != frames or any(line.count(",") != 35 for line in lines):
            raise ValueError("G1 CSV must contain 36 values per source frame.")
        values = np.array([[float(value) for value in line.split(",")] for line in lines])
        if values.shape != (frames, 36) or not np.isfinite(values).all():
            raise ValueError("G1 CSV must contain 36 finite values per source frame.")
        if not np.allclose(np.linalg.norm(values[:, 3:7], axis=1), 1, atol=1e-3):
            raise ValueError("G1 CSV root quaternions must have unit length.")
    else:
        _check_archive(path)
        with np.load(path, allow_pickle=False) as arrays:
            for key, shape in (
                ("trans", (frames, 3)),
                ("root_orient", (frames, 3)),
                ("pose_body", (frames, 63)),
            ):
                if key not in arrays or arrays[key].shape != shape or not np.isfinite(arrays[key]).all():
                    raise ValueError(f"AMASS output has invalid {key}.")
            rate = arrays["mocap_frame_rate"]
            if rate.size != 1 or float(rate.reshape(())) != fps:
                raise ValueError("AMASS frame rate differs from its source.")
            for key in arrays.files:
                if arrays[key].dtype.kind in "biuf" and not np.isfinite(arrays[key]).all():
                    raise ValueError("AMASS output contains nonfinite values.")


def make_report(request: ExportRequest, motion: dict, artifact: Path) -> dict:
    convention = {
        "bvh": {
            "units": "centimeters",
            "up_axis": "Y",
            "rest_pose": "standard T-pose" if request.bvh_standard_tpose else "BONES-SEED",
        },
        "amass": {
            "units": "meters",
            "up_axis": "Z" if request.amass_z_up else "Y",
            "body_parameters": "SMPL-X; default neutral shape, hands, eyes and jaw",
        },
        "csv": {
            "units": "meters",
            "up_axis": "Z",
            "forward_axis": "X",
            "root_quaternion_order": "wxyz",
            "columns": 36,
            "joint_angles": "MuJoCo rest-relative" if request.mujoco_rest_zero else "raw joint DOFs",
        },
    }[request.format]
    return {
        "schema_version": 1,
        "kind": "motion-export",
        "status": "completed",
        "export_id": export_id(request),
        "reviewed_upstream_revision": UPSTREAM_REVISION,
        "job_id": request.job_id,
        "sample_index": request.sample_index,
        "source_path": Path(request.source_path).name,
        "source_sha256": request.source_sha256,
        "model": motion["model"],
        "skeleton": motion["skeleton"],
        "fps": motion["fps"],
        "frames": motion["positions"].shape[1],
        "options": request.options(),
        "coordinates": convention,
        "artifact": {
            "path": request.basename,
            "format": request.format,
            "bytes": artifact.stat().st_size,
            "sha256": digest(artifact),
        },
        "notes": [
            "Converted existing motion without inference, resampling or postprocessing.",
            "G1 joint projection and AMASS default parameters may omit source detail; this is not robot safety validation.",
        ],
    }


def verify_export(directory: Path, request: ExportRequest | None = None) -> dict:
    directory = directory.resolve()
    path = directory / "request.json"
    if not path.is_file() or path.stat().st_size > 16384:
        raise ValueError("Export request is missing or oversized.")
    stored = ExportRequest.model_validate_json(path.read_text(encoding="utf-8"))
    if request is not None and stored != request:
        raise ValueError("Stored export request differs from the expected request.")
    request = stored
    source, expected_directory = _paths(request)
    if directory != expected_directory:
        raise ValueError("Export receipt does not belong to this directory.")
    report_path = directory / "export-report.json"
    artifact = (directory / request.basename).resolve()
    if artifact.parent != directory or not report_path.is_file() or report_path.stat().st_size > 65536:
        raise ValueError("Export report or artifact path is invalid.")
    report = json.loads(report_path.read_text(encoding="utf-8"))
    motion = load_source(request)
    validate_artifact(artifact, request, motion["positions"].shape[1], motion["fps"])
    if report != make_report(request, motion, artifact):
        raise ValueError("Export receipt does not match the source, settings or artifact digest.")
    if digest(source) != request.source_sha256:
        raise ValueError("Source changed during receipt verification.")
    return {
        **report,
        "output_directory": str(directory),
        "report_path": str(report_path),
        "artifact": {**report["artifact"], "absolute_path": str(artifact)},
    }


def list_exports(job_directory: Path) -> list[dict]:
    """Return only receipts whose source, assigned paths and output bytes verify."""
    directory = job_directory.resolve()
    root = (directory / "exports").resolve()
    if not root.is_relative_to(directory) or not root.is_dir():
        return []
    results = []
    for child in sorted(root.iterdir()):
        if not re.fullmatch(r"[0-9a-f]{64}", child.name) or not child.is_dir():
            continue
        try:
            report = verify_export(child)
            if report["job_id"] == directory.name and Path(report["output_directory"]).parent == root:
                results.append(report)
        except (ValueError, OSError, KeyError, TypeError):
            continue
    return results


def export_motion(
    settings: Settings,
    *,
    source_path: Path,
    source_sha256: str,
    job_id: str,
    format: Literal["bvh", "amass", "csv"],
    sample_index: int = 0,
    bvh_standard_tpose: bool = False,
    amass_z_up: bool = True,
    mujoco_rest_zero: bool = False,
) -> dict:
    """Run one fixed converter process, or reuse its fully verified receipt.

    Failed/incomplete attempts are retained and never retried automatically.
    The caller must resolve a completed source artifact through its job manifest.
    """
    source = source_path.resolve()
    request = ExportRequest(
        source_path=str(source),
        output_directory="",
        source_sha256=source_sha256,
        job_id=job_id,
        sample_index=sample_index,
        format=format,
        bvh_standard_tpose=bvh_standard_tpose,
        amass_z_up=amass_z_up,
        mujoco_rest_zero=mujoco_rest_zero,
    )
    directory = source.parent / "exports" / export_id(request)
    request = request.model_copy(update={"output_directory": str(directory)})
    _paths(request)
    lock = FileLock(str(source.parent / f".export-{export_id(request)}.lock"), timeout=0)
    try:
        with lock:
            if directory.exists():
                if (directory / "export-report.json").is_file():
                    return verify_export(directory, request)
                raise ValueError("An incomplete export attempt exists; inspect it before retrying.")
            load_source(request)
            directory.mkdir(parents=True)
            atomic_json(directory / "request.json", request.model_dump())
            atomic_json(
                directory / "attempt.json",
                {"status": "started", "export_id": export_id(request)},
            )
            env = settings.worker_env()
            env.update(
                HF_HUB_OFFLINE="1",
                TRANSFORMERS_OFFLINE="1",
                HF_HUB_DISABLE_TELEMETRY="1",
                KIMODO_ALLOW_DOWNLOADS="0",
                CUDA_VISIBLE_DEVICES="",
                TEXT_ENCODER_DEVICE="cpu",
            )
            try:
                with (directory / "export.log").open("w", encoding="utf-8") as stream:
                    process = subprocess.run(
                        [
                            settings.python,
                            "-m",
                            "kimodo_mcp.export_worker",
                            "--request",
                            str(directory / "request.json"),
                        ],
                        env=env,
                        stdin=subprocess.DEVNULL,
                        stdout=stream,
                        stderr=subprocess.STDOUT,
                        timeout=EXPORT_TIMEOUT,
                        check=False,
                        creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
                    )
                if process.returncode:
                    raise ValueError("Export worker failed; inspect its managed export.log.")
                return verify_export(directory, request)
            except Exception as error:
                atomic_json(
                    directory / "failure.json",
                    {"status": "failed", "error_type": type(error).__name__},
                )
                raise
    except Timeout as error:
        raise ValueError("An export with these settings is already running.") from error
