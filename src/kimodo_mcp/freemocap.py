"""Typed local HTTP adapter for an independently installed FreeMoCap runtime.

No FreeMoCap implementation is bundled or imported. Processing and exporting
are separate operations; this module never starts cameras, a server or an editor
as a side effect of discovery, planning or motion processing.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
import time
import tomllib
import uuid
from pathlib import Path, PurePosixPath
from typing import Annotated, Literal
from urllib.parse import urlencode, urlsplit

import httpx
from filelock import FileLock
from pydantic import BaseModel, ConfigDict, Field, StrictBool, model_validator

from .config import Settings
from .jobs import atomic_json

UPSTREAM_REVISION = "700c8614d11d886dacfea8e52333d3c0b942ef52"
UPSTREAM_VERSION = "v2.0.0-alpha.25"
Operation = Literal["check_sync", "synchronize", "import", "calibrate", "process", "export"]
Identifier = Annotated[str, Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9_-]{0,79}$")]
JobIdentifier = Annotated[str, Field(pattern=r"^[0-9a-f]{32}$")]
VIDEO_EXTENSIONS = {".mp4", ".mov", ".avi", ".mkv", ".webm"}
OUTPUT_EXTENSIONS = {".npy", ".csv", ".parquet", ".toml", ".json", ".blend", ".fbx", ".bvh"}
MAX_RESPONSE = 8 * 1024 * 1024
PREFIX = "/freemocap"
ROUTES = {
    "check_sync": "/mocap/recording/check_sync",
    "synchronize": "/mocap/recording/synchronize",
    "import": "/mocap/recording/import",
    "calibrate": "/calibration/recording/calibrate",
    "process": "/mocap/recording/process",
    "export": "/blender/export",
}
LIMITATIONS = [
    "One camera produces planar pixel-space XZ positions with Y=0, not depth or metric 3D.",
    "Equal frame counts do not establish synchronization; calibrated 3D needs matching cameras and timing.",
    "Posthoc completion requires the optional pinned status extension; file presence alone is not success.",
    "Calibration also updates FreeMoCap's global calibration library and last-successful calibration.",
    "Only MediaPipe output can use upstream Blender export; animated glTF export is not supported.",
    "The upstream addon may fall back to default export settings; requested rig/cleanup settings are not independently verified.",
    "This upstream task writes an RTMPose tracker schema even after MediaPipe; schema metadata is not a conversion guarantee.",
    "FreeMoCap landmarks are not Kimodo SOMA motion or an automatically compatible Unreal skeleton.",
    "Tracking may download model files through the separately installed runtime; no downloads are performed by planning.",
]


class _Model(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)


class FreeMocapBoard(_Model):
    squares_x: Annotated[int, Field(strict=True, ge=2, le=30)]
    squares_y: Annotated[int, Field(strict=True, ge=2, le=30)]
    square_length_mm: Annotated[float, Field(gt=0, le=1000)]
    marker_length_ratio: Annotated[float, Field(gt=0, lt=1)] = 0.8
    aruco_dictionary_enum: Annotated[int, Field(strict=True, ge=0, le=20)] = 2


class FreeMocapProcessOptions(_Model):
    detector_type: Literal["mediapipe", "rtmpose"] = "mediapipe"
    rtmpose_model_name: Literal["rtmw-x-l_256x192", "rtmw-x-l_384x288", "rtmw-l-m_256x192"] = (
        "rtmw-x-l_256x192"
    )
    rtmpose_confidence_threshold: Annotated[float, Field(ge=0, le=1)] = 0.004
    mediapipe_detection_confidence: Annotated[float, Field(ge=0, le=1)] = 0.5
    mediapipe_presence_confidence: Annotated[float, Field(ge=0, le=1)] = 0.5
    mediapipe_tracking_confidence: Annotated[float, Field(ge=0, le=1)] = 0.5
    cutoff_hz: Annotated[float, Field(gt=0, le=100)] = 6.0
    filter_order: Annotated[int, Field(strict=True, ge=1, le=8)] = 4
    use_outlier_rejection: StrictBool = True
    minimum_cameras: Annotated[int, Field(strict=True, ge=2, le=8)] = 2
    maximum_cameras_to_drop: Annotated[int, Field(strict=True, ge=0, le=3)] = 2
    target_reprojection_error: Annotated[float, Field(gt=0, le=1)] = 0.02


class FreeMocapExportOptions(_Model):
    formats: Annotated[list[Literal["fbx", "bvh"]], Field(max_length=2)] = Field(
        default_factory=lambda: ["fbx", "bvh"]
    )
    rest_pose: Literal["tpose", "apose"] = "tpose"
    apply_foot_locking: StrictBool = False
    limit_hand_markers_range_of_motion: StrictBool = False

    @model_validator(mode="after")
    def unique_formats(self):
        if len(set(self.formats)) != len(self.formats):
            raise ValueError("Export formats must be unique.")
        return self


class FreeMocapRequest(_Model):
    """One explicit posthoc stage; paths are operator-root-relative references."""

    operation: Operation
    video_files: Annotated[list[str], Field(max_length=8)] = Field(default_factory=list)
    recording_id: Identifier | None = None
    sync_job_id: JobIdentifier | None = None
    sync_method: Literal["audio", "brightness"] = "audio"
    brightness_ratio_threshold: Annotated[float, Field(ge=1, le=1_000_000)] = 1000.0
    synchronization_confirmed: StrictBool = False
    allow_planar: StrictBool = False
    calibration_file: str | None = None
    calibration_recording_id: Identifier | None = None
    board: FreeMocapBoard | None = None
    use_groundplane: StrictBool = False
    process_options: FreeMocapProcessOptions = Field(default_factory=FreeMocapProcessOptions)
    export_options: FreeMocapExportOptions = Field(default_factory=FreeMocapExportOptions)

    @model_validator(mode="after")
    def stage_fields(self):
        allowed = {
            "check_sync": {"video_files"},
            "synchronize": {"video_files", "sync_method", "brightness_ratio_threshold"},
            "import": {"video_files", "sync_job_id", "synchronization_confirmed"},
            "calibrate": {"recording_id", "board", "use_groundplane"},
            "process": {
                "recording_id",
                "allow_planar",
                "calibration_file",
                "calibration_recording_id",
                "process_options",
            },
            "export": {"recording_id", "export_options"},
        }[self.operation]
        extra = self.model_fields_set - allowed - {"operation"}
        if extra:
            raise ValueError(f"Fields do not apply to {self.operation}: {', '.join(sorted(extra))}")
        if self.operation in {"check_sync", "synchronize", "import"} and not self.video_files:
            raise ValueError("Select at least one registered reference video.")
        if self.operation in {"calibrate", "process", "export"} and self.recording_id is None:
            raise ValueError("Select a recording ID.")
        if self.operation == "calibrate" and self.board is None:
            raise ValueError("Calibration requires the measured ChArUco board geometry.")
        if self.calibration_file and self.calibration_recording_id:
            raise ValueError("Select a calibration reference or a calibration recording, not both.")
        return self


def _json(path: Path, maximum: int = MAX_RESPONSE):
    if not path.is_file() or path.stat().st_size > maximum:
        raise ValueError(f"Expected a bounded JSON file: {path.name}")
    return json.loads(path.read_text(encoding="utf-8"))


def _digest(path: Path) -> str:
    before = path.stat()
    with path.open("rb") as stream:
        result = hashlib.file_digest(stream, "sha256").hexdigest()
    after = path.stat()
    if (before.st_size, before.st_mtime_ns) != (after.st_size, after.st_mtime_ns):
        raise ValueError("A source file changed while its checksum was read.")
    return result


def _relative(root: Path, name: str, *, suffixes: set[str] | None = None) -> Path:
    if not isinstance(name, str) or not name or "\\" in name or ":" in name or "\x00" in name:
        raise ValueError("Use a relative file reference with forward slashes.")
    parts = PurePosixPath(name).parts
    if PurePosixPath(name).is_absolute() or any(part in {".", ".."} for part in parts):
        raise ValueError("File reference must stay within its configured root.")
    result = (root / name).resolve()
    if not result.is_relative_to(root.resolve()) or not result.is_file():
        raise ValueError("Referenced file is unavailable within its configured root.")
    if suffixes and result.suffix.lower() not in suffixes:
        raise ValueError("Unsupported file format.")
    return result


def _identifier(value: str) -> str:
    if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,79}", value):
        raise ValueError("Invalid recording or pipeline identifier.")
    return value


def _sync_result(result: dict, sources: list[dict]) -> dict:
    """Validate completion structure without trusting remote paths as file access."""
    if result.get("pending") is True:
        return result
    before, after = result.get("videos_before"), result.get("videos_after")
    count = result.get("synchronized_frame_count")
    expected = {Path(item["path"]).stem for item in sources}
    if (
        not isinstance(before, list)
        or not isinstance(after, list)
        or len(before) != len(sources)
        or len(after) != len(sources)
        or not all(isinstance(item, dict) for item in before + after)
        or type(count) is not int
        or not 1 <= count <= 144_000
        or {item.get("video_name") for item in before} != expected
        or {item.get("video_name", "").removeprefix("synced_") for item in after} != expected
        or any(item.get("frame_count") != count for item in after)
    ):
        raise ValueError("Synchronization result does not match this exact reference set and frame budget.")
    return result


class FreeMoCapClient:
    def __init__(self, settings: Settings):
        self.directory = settings.data_dir / "freemocap"
        config_path = self.directory / "runtime.json"
        if not config_path.exists():
            raise ValueError(
                f"Configure the separate FreeMoCap service in {config_path.resolve()}. "
                "See the FreeMoCap setup guide."
            )
        config = _json(config_path, 16_384)
        if not isinstance(config, dict) or set(config) != {"base_url", "recordings_root", "references_root"}:
            raise ValueError("runtime.json requires exactly base_url, recordings_root and references_root.")
        if not all(isinstance(value, str) and value for value in config.values()):
            raise ValueError("Runtime settings must be nonempty strings.")
        parsed = urlsplit(config["base_url"])
        if (
            parsed.scheme != "http"
            or parsed.hostname not in {"127.0.0.1", "::1", "localhost"}
            or parsed.username
            or parsed.password
            or parsed.path not in {"", "/"}
            or parsed.query
            or parsed.fragment
        ):
            raise ValueError("FreeMoCap must use an operator-configured loopback HTTP origin.")
        self.base_url = config["base_url"].rstrip("/")
        # Resolve localhost once to a literal loopback address; never use DNS or environment proxies.
        if parsed.hostname == "localhost":
            self.base_url = f"http://127.0.0.1:{parsed.port or 80}"
        self.recordings = Path(config["recordings_root"]).expanduser()
        self.references = Path(config["references_root"]).expanduser()
        if not self.recordings.is_absolute() or not self.references.is_absolute():
            raise ValueError("Operator roots must be absolute paths.")
        self.recordings = self.recordings.resolve()
        self.references = self.references.resolve()
        if not self.recordings.is_dir() or not self.references.is_dir():
            raise ValueError("Operator recording and reference roots must already exist.")
        self.runtime_key = hashlib.sha256(
            json.dumps({**config, "base_url": self.base_url}, sort_keys=True).encode()
        ).hexdigest()

    def _request(self, method: str, route: str, payload=None, *, query=None, timeout: float = 20):
        url = self.base_url + route + ("?" + urlencode(query) if query else "")
        with httpx.Client(trust_env=False, follow_redirects=False, timeout=timeout) as client:
            with client.stream(method, url, json=payload) as response:
                raw = bytearray()
                for chunk in response.iter_bytes():
                    raw.extend(chunk)
                    if len(raw) > MAX_RESPONSE:
                        raise ValueError("FreeMoCap returned an oversized response.")
                if response.status_code == 425:
                    return {"pending": True}
                if not 200 <= response.status_code < 300:
                    raise RuntimeError(
                        f"FreeMoCap HTTP {response.status_code}: {bytes(raw[:2000]).decode(errors='replace')}"
                    )
                return json.loads(raw) if raw else {}

    def capabilities(self) -> dict:
        health = self._request("GET", "/health")
        schema = self._request("GET", "/openapi.json")
        version = schema.get("info", {}).get("version")
        paths = schema.get("paths", {})
        missing = [PREFIX + route for route in ROUTES.values() if "post" not in paths.get(PREFIX + route, {})]
        compatible = health.get("alive") is True and version == UPSTREAM_VERSION and not missing
        return {
            "configured": True,
            "reachable": health.get("alive") is True,
            "compatible": compatible,
            "upstream_version": version,
            "reviewed_version": UPSTREAM_VERSION,
            "reviewed_revision": UPSTREAM_REVISION,
            "license": "AGPL-3.0",
            "base_url": self.base_url,
            "missing_routes": missing,
            "status_extension": self.status_extension(),
            "operations": list(ROUTES),
            "limitations": LIMITATIONS,
            "unsupported": [
                "camera discovery or recording",
                "realtime pipelines",
                "arbitrary HTTP or Python",
                "pyceres experimental solver",
                "automatic SOMA conversion",
                "RTMPose Blender export",
                "glTF export",
            ],
        }

    def status_extension(self, *, required: bool = False) -> dict:
        try:
            value = self._request("GET", "/kimodo-status/v1")
            if (value.get("schema_version") != 1 or value.get("reviewed_revision") != UPSTREAM_REVISION
                    or value.get("terminal_publication_observer") is not True
                    or not re.fullmatch(r"[0-9a-f]{32}", str(value.get("instance_id", "")))):
                raise ValueError("Invalid FreeMoCap status extension contract")
            return {**value, "available": True}
        except (OSError, ValueError, RuntimeError, httpx.HTTPError) as error:
            if required:
                raise ValueError("Start FreeMoCap with the pinned status extension before asynchronous processing") from error
            return {"available": False, "error": str(error)}

    def recording(self, recording_id: str, *, owned: bool = False) -> Path:
        path = (self.recordings / _identifier(recording_id)).resolve()
        if path.parent != self.recordings or not path.is_dir():
            raise ValueError("Recording is not inside the configured recording root.")
        if owned:
            marker = _json(path / ".kimodo-freemocap.json", 100_000)
            if marker.get("recording_id") != recording_id or marker.get("runtime_key") != self.runtime_key:
                raise ValueError("Mutation requires an imported recording owned by this adapter/runtime.")
            for source in marker["videos"]:
                if _digest(_relative(path, source["path"], suffixes=VIDEO_EXTENSIONS)) != source["sha256"]:
                    raise ValueError("Imported reference video changed after registration.")
        return path

    def source_videos(self, names: list[str]) -> list[dict]:
        paths = [_relative(self.references, name, suffixes=VIDEO_EXTENSIONS) for name in names]
        if len({path.stem.casefold() for path in paths}) != len(paths):
            raise ValueError(
                "Video basenames must have unique stems, including case, before synchronization/import."
            )
        if any(not 0 < path.stat().st_size <= 4 * 1024**3 for path in paths):
            raise ValueError("Each reference video must contain 1 byte to 4 GiB.")
        if sum(path.stat().st_size for path in paths) > 16 * 1024**3:
            raise ValueError("Selected reference videos exceed the 16 GiB budget.")
        return [
            {"reference": name, "path": str(path), "bytes": path.stat().st_size, "sha256": _digest(path)}
            for name, path in zip(names, paths)
        ]

    def video_metadata(self, paths: list[str]) -> dict:
        report = self._request("POST", PREFIX + ROUTES["check_sync"], {"videoPaths": paths})
        videos = report.get("videos", [])
        if len(videos) != len(paths) or not videos:
            raise ValueError("FreeMoCap returned mismatched video metadata.")
        for item in videos:
            fps, frames = item.get("fps"), item.get("frame_count")
            if (
                type(frames) is not int
                or not 1 <= frames <= 144_000
                or type(fps) not in {int, float}
                or not math.isfinite(fps)
                or not 1 <= fps <= 240
                or frames / fps > 600
            ):
                raise ValueError(
                    "Capture limits require videos of at most 10 minutes, 144000 frames and 240 fps."
                )
        if max(item["fps"] for item in videos) - min(item["fps"] for item in videos) > 0.01:
            raise ValueError(
                "Reference videos need matching frame rates; resample explicitly outside this adapter."
            )
        return {
            **report,
            "synchronization_verified": False,
            "note": "Matching frame counts only; temporal alignment must be verified separately.",
        }

    def read_job(self, job_id: str) -> tuple[Path, dict]:
        if not re.fullmatch(r"[0-9a-f]{32}", job_id):
            raise ValueError("Invalid FreeMoCap job ID.")
        path = self.directory / "jobs" / f"{job_id}.json"
        value = _json(path)
        if value.get("job_id") != job_id or value.get("runtime_key") != self.runtime_key:
            raise ValueError("FreeMoCap receipt belongs to a different configured runtime.")
        return path, value

    def outputs(self, recording_id: str) -> list[dict]:
        folder = self.recording(recording_id)
        files = []
        for parent in [folder, folder / "output_data", folder / "3d_models"]:
            if not parent.is_dir():
                continue
            for path in sorted(parent.rglob("*") if parent != folder else parent.iterdir()):
                if (
                    not path.is_file()
                    or path.name.startswith(".")
                    or path.suffix.lower() not in OUTPUT_EXTENSIONS
                ):
                    continue
                relative = path.relative_to(folder).as_posix()
                checked = _relative(folder, relative, suffixes=OUTPUT_EXTENSIONS)
                if len(files) >= 512 or checked.stat().st_size > 4 * 1024**3:
                    raise ValueError("FreeMoCap outputs exceed the bounded inspection budget.")
                files.append({"path": relative, "bytes": checked.stat().st_size, "sha256": _digest(checked)})
        return files


def get_freemocap_capabilities(settings: Settings) -> dict:
    try:
        return FreeMoCapClient(settings).capabilities()
    except (OSError, ValueError, RuntimeError, httpx.HTTPError) as error:
        return {
            "configured": (settings.data_dir / "freemocap/runtime.json").is_file(),
            "reachable": False,
            "compatible": False,
            "error": str(error),
            "reviewed_revision": UPSTREAM_REVISION,
            "reviewed_version": UPSTREAM_VERSION,
            "license": "AGPL-3.0",
            "limitations": LIMITATIONS,
        }


def list_freemocap_recordings(settings: Settings, limit: int = 20) -> dict:
    if type(limit) is not int or not 1 <= limit <= 100:
        raise ValueError("Recording limit must be from 1 to 100.")
    client = FreeMoCapClient(settings)
    records = client._request(
        "GET", PREFIX + "/playback/recordings", query={"recording_parent_directory": str(client.recordings)}
    )
    if not isinstance(records, list):
        raise ValueError("Invalid recording list.")
    safe = []
    for entry in records:
        path = client.recording(entry["name"])
        if Path(entry["path"]).resolve() != path:
            raise ValueError("FreeMoCap returned a recording outside the configured root.")
        safe.append(entry)
    return {"recordings": safe[:limit], "total": len(safe), "limit": limit}


def inspect_freemocap_recording(settings: Settings, recording_id: str) -> dict:
    client = FreeMoCapClient(settings)
    client.recording(recording_id)
    query = {"recording_parent_directory": str(client.recordings)}
    status = client._request("GET", PREFIX + f"/playback/{recording_id}/status", query=query)
    return {
        "recording_id": recording_id,
        "upstream_readiness": status,
        "outputs": client.outputs(recording_id),
        "pipeline_completion_verified": False,
        "limitations": LIMITATIONS,
    }


def _plan(client: FreeMoCapClient, request: FreeMocapRequest) -> dict:
    operation = request.operation
    extension = client.status_extension(required=True) if operation in {"calibrate", "process", "export"} else None
    videos = client.source_videos(request.video_files) if request.video_files else []
    sources = list(videos)
    payload: dict = {}
    notes = list(LIMITATIONS)
    metadata = None
    if videos:
        payload["videoPaths"] = [item["path"] for item in videos]
        metadata = client.video_metadata(payload["videoPaths"])
    if operation == "synchronize":
        if len(videos) < 2:
            raise ValueError("Synchronization requires at least two videos.")
        method = {"audio": "audio cross-correlation", "brightness": "brightness change detection"}[
            request.sync_method
        ]
        payload.update(method=method, brightnessRatioThreshold=request.brightness_ratio_threshold)
    if operation == "import":
        if request.sync_job_id:
            _, sync = client.read_job(request.sync_job_id)
            if sync["request"]["operation"] != "synchronize" or sync["sources"] != sources:
                raise ValueError("Synchronization receipt must match these exact reference videos.")
            response = client._request(
                "GET", PREFIX + ROUTES["synchronize"] + "/" + _identifier(sync["upstream_job_id"])
            )
            response = _sync_result(response, sources)
            if response.get("pending"):
                raise ValueError("Synchronization is still running.")
            payload["syncJobId"] = sync["upstream_job_id"]
        elif len(videos) > 1 and (
            not request.synchronization_confirmed or metadata.get("synchronized") is not True
        ):
            raise ValueError(
                "Confirm temporal synchronization of matching-frame videos, or use a completed sync job."
            )
        payload["baseDirectory"] = str(client.recordings)
    if request.recording_id:
        folder = client.recording(request.recording_id, owned=True)
        marker = _json(folder / ".kimodo-freemocap.json")
        sources.extend(marker["videos"])
        if operation in {"calibrate", "process"}:
            if (folder / "output_data").exists() or any(folder.glob("*calibration*.toml")):
                raise ValueError(
                    "This recording already has outputs. Import a new take to preserve the previous results."
                )
            paths = [str(_relative(folder, item["path"])) for item in marker["videos"]]
            metadata = client.video_metadata(paths)
            if metadata.get("synchronized") is not True:
                raise ValueError("Imported frame counts no longer match.")
        if operation == "calibrate":
            if len(marker["videos"]) < 2:
                raise ValueError("Calibration requires at least two camera videos.")
            payload = {
                "calibrationRecordingDirectory": str(folder),
                "calibrationTaskConfig": {
                    "charucoBoard": request.board.model_dump(),
                    "solverMethod": "anipose",
                    "useGroundplane": request.use_groundplane,
                },
            }
        if operation == "process":
            count = len(marker["videos"])
            options = request.process_options
            if count == 1 and not request.allow_planar:
                raise ValueError(
                    "Single-camera processing is only planar pixel-space output; explicitly set allow_planar."
                )
            calibration = None
            if count > 1:
                if request.calibration_file:
                    calibration = _relative(client.references, request.calibration_file, suffixes={".toml"})
                elif request.calibration_recording_id:
                    calibration_folder = client.recording(request.calibration_recording_id)
                    candidates = list(calibration_folder.glob("*calibration*.toml"))
                    if len(candidates) == 1:
                        calibration = _relative(calibration_folder, candidates[0].name, suffixes={".toml"})
                if calibration is None:
                    raise ValueError("Multi-camera processing requires an explicit calibration reference.")
                if calibration.stat().st_size > 2_000_000:
                    raise ValueError("Calibration TOML exceeds the size limit.")
                parsed = tomllib.loads(calibration.read_text(encoding="utf-8"))
                cameras = [
                    value for key, value in parsed.items() if key != "metadata" and isinstance(value, dict)
                ]
                camera_ids = {item.get("id", item.get("name")) for item in cameras}
                if not {item["camera_id"] for item in metadata["videos"]} <= camera_ids:
                    raise ValueError("Calibration camera IDs do not match the imported video IDs.")
                sources.append(
                    {
                        "path": str(calibration),
                        "bytes": calibration.stat().st_size,
                        "sha256": _digest(calibration),
                    }
                )
                if options.minimum_cameras > count:
                    raise ValueError("Minimum triangulation cameras exceeds the recording's camera count.")
            elif request.calibration_file or request.calibration_recording_id:
                raise ValueError(
                    "Calibration does not turn one camera into metric 3D; omit it for planar processing."
                )
            fps = metadata["videos"][0]["fps"]
            if options.cutoff_hz >= fps / 2:
                raise ValueError("Low-pass cutoff must be below half the actual recording frame rate.")
            config = options.model_dump(
                exclude={
                    "cutoff_hz",
                    "filter_order",
                    "use_outlier_rejection",
                    "minimum_cameras",
                    "maximum_cameras_to_drop",
                    "target_reprojection_error",
                }
            )
            config.update(
                videoFps=fps,
                calibrationTomlPath=str(calibration) if calibration else None,
                exportToBlender=False,
                autoOpenBlendFile=False,
                filterConfig={
                    "method": "butter_low_pass",
                    "cutoff": options.cutoff_hz,
                    "order": options.filter_order,
                    "sampling_rate": fps,
                },
                triangulationConfig={
                    "useOutlierRejection": options.use_outlier_rejection,
                    "minimumCamerasForTriangulation": options.minimum_cameras,
                    "maximumCamerasToDrop": options.maximum_cameras_to_drop,
                    "targetReprojectionError": options.target_reprojection_error,
                },
            )
            payload = {"mocapRecordingDirectory": str(folder), "mocapTaskConfig": config}
        if operation == "export":
            if (folder / f"{folder.name}.blend").exists() or (folder / "3d_models").exists():
                raise ValueError("Export output already exists; it will not be overwritten.")
            data = folder / "output_data"
            if (data / "rtmpose_body_3d_xyz.npy").exists():
                raise ValueError("This upstream version does not export RTMPose data to Blender.")
            required = [
                "body_3d_xyz",
                "right_hand_3d_xyz",
                "left_hand_3d_xyz",
                "face_3d_xyz",
                "body_total_body_center_of_mass",
                "body_segment_center_of_mass",
            ]
            for suffix in required:
                path = _relative(folder, f"output_data/mediapipe_{suffix}.npy", suffixes={".npy"})
                if not path.stat().st_size:
                    raise ValueError("Blender input is empty.")
                sources.append({"path": str(path), "bytes": path.stat().st_size, "sha256": _digest(path)})
            payload = {
                "recordingFolderPath": str(folder),
                "autoOpenBlendFile": False,
                "blenderExportConfig": request.export_options.model_dump(),
            }
            notes.append(
                "This explicit export starts Blender in background through FreeMoCap; it does not open an editor window."
            )
    return {
        "request": request.model_dump(mode="json", exclude_unset=True),
        "operation": operation,
        "method": "POST",
        "route": PREFIX + ROUTES[operation],
        "payload": payload,
        "sources": sources,
        "video_metadata": metadata,
        "notes": notes,
        "starts_processing": operation in {"calibrate", "process"},
        "starts_blender": operation == "export",
        "creates_recording": operation == "import",
        "status_extension": extension,
    }


def plan_freemocap_capture(settings: Settings, request: FreeMocapRequest) -> dict:
    return _plan(FreeMoCapClient(settings), request)


def start_freemocap_capture(settings: Settings, request: FreeMocapRequest) -> dict:
    client = FreeMoCapClient(settings)
    if not client.capabilities()["compatible"]:
        raise ValueError(
            "The configured FreeMoCap version/routes do not match the reviewed adapter contract."
        )
    jobs = client.directory / "jobs"
    jobs.mkdir(parents=True, exist_ok=True)
    with FileLock(str(client.directory / "operations.lock"), timeout=1):
        plan = _plan(client, request)
        extension = client.status_extension(required=True) if request.operation in {"calibrate", "process", "export"} else None
        completed_processing = False
        completed_calibration = False
        if request.recording_id and request.operation in {"calibrate", "process", "export"}:
            for previous in jobs.glob("*.json"):
                old = _json(previous)
                if (request.calibration_recording_id and old.get("runtime_key") == client.runtime_key
                        and old.get("recording_id") == request.calibration_recording_id
                        and old.get("operation") == "calibrate"):
                    old = _refresh_pipeline(client, previous, old)
                    if old.get("status") != "completed" or old.get("pipeline_completion_verified") is not True:
                        raise ValueError("The selected calibration recording has no verified successful terminal result")
                    calibration_folder = client.recording(request.calibration_recording_id, owned=True)
                    calibration_path = Path(plan["payload"]["mocapTaskConfig"]["calibrationTomlPath"]).resolve()
                    if not any(
                        (calibration_folder / output["path"]).resolve() == calibration_path
                        for output in old["outputs"]
                    ):
                        raise ValueError("The selected calibration file is not a verified calibration output")
                    completed_calibration = True
                same_recording = (old.get("runtime_key") == client.runtime_key
                                  and old.get("recording_id") == request.recording_id)
                if same_recording and old.get("operation") in {"calibrate", "process"}:
                    old = _refresh_pipeline(client, previous, old)
                    if old.get("status") not in {"completed", "cancelled", "failed"}:
                        raise ValueError("Another stage for this recording has no verified terminal result; inspect its receipt first")
                    completed_processing |= (old.get("operation") == "process"
                                              and old.get("pipeline_completion_verified") is True
                                              and old.get("status") == "completed")
                if (
                    same_recording
                    and old.get("operation") == request.operation
                    and old.get("status") not in {"cancelled", "failed"}
                ):
                    raise ValueError(
                        "This stage already has a receipt. Inspect it before importing a new take; uncertain submissions are never retried automatically."
                    )
        if request.calibration_recording_id and not completed_calibration:
            raise ValueError("The selected calibration recording requires an owned, verified successful calibration receipt")
        if request.operation == "export" and not completed_processing:
            raise ValueError("Export requires this recording's verified successful processing receipt")
        job_id = uuid.uuid4().hex
        path = jobs / f"{job_id}.json"
        if request.operation == "import":
            recording_id = "kimodo-" + job_id
            if (client.recordings / recording_id).exists():
                raise ValueError("The new recording destination already exists.")
            plan["payload"]["recordingName"] = recording_id
        else:
            recording_id = request.recording_id
        receipt = {
            "schema_version": 1,
            "job_id": job_id,
            "runtime_key": client.runtime_key,
            "operation": request.operation,
            "recording_id": recording_id,
            "created_at": time.time(),
            "request": plan["request"],
            "sources": plan["sources"],
            "status": "submitting",
            "pipeline_completion_verified": False,
            "notes": plan["notes"],
            "status_instance_id": extension["instance_id"] if extension else None,
        }
        if request.operation == "process":
            receipt["video_metadata"] = plan["video_metadata"]
        atomic_json(path, receipt)
        try:
            response = client._request(
                "POST", plan["route"], plan["payload"], timeout=180 if request.operation == "export" else 30
            )
            receipt["response"] = response
            if request.operation == "import":
                folder = client.recording(recording_id)
                if (
                    response.get("success") is not True
                    or Path(response.get("recording_path", "")).resolve() != folder
                ):
                    raise ValueError("Import response does not match the managed destination.")
                imported = []
                for source in plan["sources"]:
                    filename = (
                        Path(source["path"]).stem + ".mp4"
                        if request.sync_job_id
                        else Path(source["path"]).name
                    )
                    relative = "synchronized_videos/" + filename
                    video = _relative(folder, relative, suffixes=VIDEO_EXTENSIONS)
                    digest = _digest(video)
                    if not request.sync_job_id and digest != source["sha256"]:
                        raise ValueError("Imported video checksum does not match its original reference.")
                    imported.append({"path": relative, "sha256": digest, "bytes": video.stat().st_size})
                atomic_json(
                    folder / ".kimodo-freemocap.json",
                    {
                        "recording_id": recording_id,
                        "runtime_key": client.runtime_key,
                        "videos": imported,
                        "import_job_id": job_id,
                    },
                )
                receipt["status"] = "completed"
                receipt["pipeline_completion_verified"] = True
            elif request.operation in {"calibrate", "process"}:
                if response.get("success") is not True:
                    raise ValueError("FreeMoCap did not accept the processing stage.")
                receipt["upstream_pipeline_id"] = _identifier(response["pipeline_id"])
                receipt["status"] = "submitted"
            elif request.operation == "synchronize":
                receipt["upstream_job_id"] = _identifier(response["job_id"])
                receipt["status"] = "submitted"
            elif request.operation == "check_sync":
                receipt["status"] = "completed"
                receipt["pipeline_completion_verified"] = True
            else:
                if response.get("success") is not True:
                    raise ValueError("FreeMoCap did not confirm Blender export.")
                output = _relative(
                    client.recording(recording_id), recording_id + ".blend", suffixes={".blend"}
                )
                if (
                    Path(response.get("blender_file_path", "")).resolve() != output
                    or not output.stat().st_size
                ):
                    raise ValueError("Blender export response/output does not match the managed recording.")
                receipt["outputs"] = client.outputs(recording_id)
                formats_found = {
                    Path(item["path"]).suffix.removeprefix(".")
                    for item in receipt["outputs"]
                    if item["path"].startswith("3d_models/") and item["bytes"] > 0
                }
                if not set(request.export_options.formats) <= formats_found:
                    raise ValueError("The requested FBX/BVH export files are missing or empty.")
                receipt["export_settings_verified"] = False
                receipt["status"] = "completed"
                receipt["pipeline_completion_verified"] = True
        except Exception as error:
            # A timeout can arrive after upstream accepted the mutation. Keep that
            # ambiguity durable and never encourage duplicate processing.
            receipt["status"] = "submission_unknown"
            receipt["error"] = str(error)
            atomic_json(path, receipt)
            raise RuntimeError(
                f"FreeMoCap outcome is unverified; inspect job {job_id} before retrying: {error}"
            ) from error
        atomic_json(path, receipt)
        return receipt


def _refresh_pipeline(client: FreeMoCapClient, path: Path, receipt: dict) -> dict:
    if receipt.get("operation") not in {"calibrate", "process", "export"} or receipt.get("status") in {"cancelled", "failed"}:
        return receipt
    folder = client.recording(receipt["recording_id"], owned=True)
    if receipt.get("status") == "completed":
        if receipt.get("pipeline_completion_verified") is not True or not receipt.get("outputs"):
            raise ValueError("Completed pipeline lacks verified output evidence")
        for output in receipt["outputs"]:
            candidate = _relative(folder, output["path"], suffixes=OUTPUT_EXTENSIONS)
            if candidate.stat().st_size != output["bytes"] or _digest(candidate) != output["sha256"]:
                raise ValueError("Completed pipeline output changed after verification")
        for source in receipt["sources"]:
            raw = Path(source["path"])
            candidate = raw.resolve() if raw.is_absolute() else _relative(folder, source["path"])
            if (not any(candidate.is_relative_to(root) for root in (client.references, client.recordings))
                    or _digest(candidate) != source["sha256"]):
                raise ValueError("Processing source changed since its submission")
        return receipt
    pipeline_id = receipt.get("upstream_pipeline_id")
    if receipt.get("operation") == "export" or not pipeline_id:
        return receipt
    extension = client.status_extension(required=True)
    if extension["instance_id"] != receipt.get("status_instance_id"):
        raise ValueError("FreeMoCap restarted; this pipeline has no bound terminal observation in the current instance")
    observed = client._request("GET", "/kimodo-status/v1/pipelines/" + _identifier(pipeline_id))
    expected_type = "calibration" if receipt["operation"] == "calibrate" else "mocap"
    if (observed.get("instance_id") != receipt["status_instance_id"]
            or observed.get("pipeline_id") != pipeline_id or observed.get("pipeline_type") != expected_type
            or Path(observed.get("recording_path", "")).resolve() != folder):
        raise ValueError("Pipeline observation does not match its receipt and recording")
    receipt["pipeline_observation"] = observed
    if observed.get("terminal_verified") is True and observed.get("workers_alive") is False:
        if observed.get("phase") == "failed":
            receipt.update(status="failed", error=str(observed.get("detail", "Processing failed"))[:2000],
                           pipeline_completion_verified=True)
        elif observed.get("phase") == "complete":
            for source in receipt["sources"]:
                raw = Path(source["path"])
                candidate = raw.resolve() if raw.is_absolute() else _relative(folder, source["path"])
                if (not any(candidate.is_relative_to(root) for root in (client.references, client.recordings))
                        or _digest(candidate) != source["sha256"]):
                    raise ValueError("Processing source changed since its submission")
            outputs = client.outputs(receipt["recording_id"])
            if not outputs:
                raise ValueError("A terminal success was observed but no bounded result files exist")
            receipt.update(status="completed", pipeline_completion_verified=True, outputs=outputs)
    atomic_json(path, receipt)
    return receipt


def get_freemocap_job(settings: Settings, job_id: str) -> dict:
    client = FreeMoCapClient(settings)
    path, receipt = client.read_job(job_id)
    if receipt.get("operation") in {"calibrate", "process", "export"}:
        with FileLock(str(client.directory / "operations.lock"), timeout=1):
            path, receipt = client.read_job(job_id)
            receipt = _refresh_pipeline(client, path, receipt)
    if (
        receipt["operation"] == "synchronize"
        and receipt.get("upstream_job_id")
        and receipt["status"] != "completed"
    ):
        result = client._request(
            "GET", PREFIX + ROUTES["synchronize"] + "/" + _identifier(receipt["upstream_job_id"])
        )
        result = _sync_result(result, receipt["sources"])
        receipt["synchronization"] = result
        if not result.get("pending"):
            receipt["status"] = "completed"
            receipt["pipeline_completion_verified"] = True
        atomic_json(path, receipt)
    if receipt.get("recording_id") and (client.recordings / receipt["recording_id"]).is_dir():
        if receipt.get("status") == "completed" and receipt.get("pipeline_completion_verified") is True:
            receipt["recording"] = {
                "recording_id": receipt["recording_id"],
                "outputs": receipt.get("outputs", []),
                "upstream_readiness": {"available": False, "reason": "Not queried for verified saved results"},
            }
        else:
            receipt["recording"] = inspect_freemocap_recording(settings, receipt["recording_id"])
    return receipt


def cancel_freemocap_job(settings: Settings, job_id: str) -> dict:
    client = FreeMoCapClient(settings)
    with FileLock(str(client.directory / "operations.lock"), timeout=1):
        path, receipt = client.read_job(job_id)
        if receipt["status"] == "cancelled":
            return receipt
        pipeline_id = receipt.get("upstream_pipeline_id")
        if not pipeline_id:
            raise ValueError(
                "Only this adapter's submitted calibration/processing pipelines support cancellation; sync has no upstream cancel route."
            )
        if receipt["status"] in {"completed", "failed"}:
            return receipt
        if client.status_extension(required=True)["instance_id"] != receipt.get("status_instance_id"):
            raise ValueError("Cannot cancel a pipeline belonging to a previous FreeMoCap service instance")
        response = client._request("DELETE", PREFIX + "/posthoc/pipeline/" + _identifier(pipeline_id))
        if response.get("success") is not True or response.get("pipeline_id") != pipeline_id:
            raise ValueError("FreeMoCap did not confirm cancellation of this exact pipeline.")
        receipt["status"] = "cancelled"
        receipt["cancellation"] = response
        atomic_json(path, receipt)
        return receipt
