"""Explicit, local EasyMocap jobs; no model downloads or shared Kimodo worker."""

from __future__ import annotations

import hashlib
import json
import math
import os
import re
import subprocess
import sys
import time
import uuid
import zipfile
from pathlib import Path
from typing import Annotated, Literal

import numpy as np
from filelock import FileLock, Timeout
from pydantic import AliasChoices, BaseModel, ConfigDict, Field, field_validator, model_validator

from .config import Settings
from .jobs import atomic_json, redact

EASYMOCAP_REVISION = "e6006fd3814d5f8ad45a7ce965beb9bcc90767f2"
ACTIVE = {"queued", "running", "cancelling"}
ARTIFACT_NAMES = {
    "reference.mp4", "motion.npz", "motion.bvh", "detections.json", "smpl-parameters.json",
    "timing.json", "diagnostics.json",
}
MODEL_FILES = (
    "data/bodymodels/SMPL_python_v.1.1.0/smpl/models/basicmodel_neutral_lbs_10_207_0_v1.1.0.pkl",
    "data/smplx/J_regressor_body25.npy", "data/models/spin_checkpoint.pt",
    "data/models/smpl_mean_params.npz",
)
PIPELINE_FILES = (
    "apps/preprocess/extract_keypoints.py", "apps/calibration/create_blank_camera.py",
    "apps/fit/fit.py", "config/model/smpl.yml", "config/data/multivideo.yml", "config/fit/1v1p.yml",
)
CAPABILITIES = {
    "supported": ["bounded video extraction", "single visible person BODY25 detection",
                  "monocular SMPL fitting with temporal regularization", "SMPL24 skeleton review",
                  "confidence and source timestamps", "explicit short-gap 2D interpolation", "numerical BVH export"],
    "future": ["interactive annotation correction", "explicit smoothing revisions",
               "calibrated synchronized multiview", "camera calibration", "person tracking selection"],
    "unsupported": ["guaranteed accuracy from arbitrary footage", "moving-camera world trajectory",
                    "automatic SOMA character retargeting", "facial or finger capture", "NeRF training"],
    "license": {"name": "Project Registration License v1.0", "reviewed_revision": EASYMOCAP_REVISION,
                "url": f"https://github.com/zju3dv/EasyMocap/blob/{EASYMOCAP_REVISION}/LICENSE",
                "note": "Organizational project use requires project registration; body models and weights have separate licenses."},
    "limitations": ["Use one fully visible person and a static camera; scale and depth are estimates.",
                    "Every frame needs a usable detection or an explicitly requested bounded 2D gap estimate; ambiguous people stop fitting.",
                    "No model assets are downloaded or distributed by this adapter."],
}


class VideoMocapRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)
    video_path: str = Field(min_length=1, max_length=4096,
                            validation_alias=AliasChoices("video_path", "local_video_path"))
    start_seconds: Annotated[float, Field(ge=0, le=86400, allow_inf_nan=False)] = 0
    duration_seconds: Annotated[float, Field(gt=0, le=80, allow_inf_nan=False)] = 4
    fps: Annotated[float, Field(ge=1, le=60, allow_inf_nan=False)] = 30
    max_frames: Annotated[int, Field(ge=6, le=2400, strict=True)] = 2400
    detector: Literal["mp-pose", "yolo-hrnet"] = "mp-pose"
    max_gap_frames: Annotated[int, Field(ge=0, le=15, strict=True)] = 0

    @field_validator("video_path")
    @classmethod
    def local_path(cls, value: str) -> str:
        if "\x00" in value or "://" in value:
            raise ValueError("Choose an existing local video file")
        return str(Path(value).expanduser().resolve())

    @model_validator(mode="after")
    def frame_budget(self) -> VideoMocapRequest:
        if not 6 <= self.frames <= self.max_frames:
            raise ValueError("The selected duration and frame rate must produce 6 to max_frames frames")
        return self

    @property
    def frames(self) -> int:
        return int(math.floor(self.duration_seconds * self.fps + 1e-8))


class VideoMocapRuntime(BaseModel):
    model_config = ConfigDict(extra="forbid")
    python: str
    repo_path: str
    ffmpeg: str
    ffprobe: str
    device: Literal["cpu", "cuda"] = "cpu"

    @field_validator("python", "repo_path", "ffmpeg", "ffprobe")
    @classmethod
    def absolute_path(cls, value: str) -> str:
        path = Path(value).expanduser()
        if not path.is_absolute() or "\x00" in value:
            raise ValueError("Runtime paths must be absolute local paths")
        return str(path.resolve())


def _hash(path: Path) -> str:
    result = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            result.update(chunk)
    return result.hexdigest()


def _json(path: Path, maximum: int = 2_000_000) -> dict:
    if path.stat().st_size > maximum:
        raise ValueError("Managed record exceeds its size limit")
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError("Managed record must be a JSON object")
    return value


def _canonical(value: dict) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()).hexdigest()


def _base(settings: Settings) -> Path:
    base = settings.data_dir.resolve() / "video-mocap"
    if base.resolve() != base:
        raise ValueError("Video capture storage cannot redirect outside the configured data directory")
    return base


def _directory(settings: Settings, job_id: str) -> Path:
    if not re.fullmatch(r"[a-f0-9]{32}", job_id):
        raise ValueError("Invalid video job ID")
    base = _base(settings)
    directory = base / "jobs" / job_id
    if directory.resolve() != directory or (base / "jobs").resolve() != base / "jobs":
        raise ValueError("Video job directories cannot redirect outside managed storage")
    if not directory.is_dir():
        raise ValueError("Video job does not exist")
    return directory


def _run_read(argv: list[str], *, cwd: Path | None = None, timeout: int = 30) -> str:
    result = subprocess.run(argv, cwd=cwd, stdin=subprocess.DEVNULL, capture_output=True,
                            text=True, encoding="utf-8", errors="replace", timeout=timeout,
                            check=False, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    if result.returncode:
        raise ValueError(f"Tool check failed (exit {result.returncode}): {redact(result.stderr[-1200:])}")
    if len(result.stdout) > 2_000_000:
        raise ValueError("Tool check returned too much data")
    return result.stdout.strip()


def _probe_video(runtime: VideoMocapRuntime, source: Path) -> dict:
    raw = json.loads(_run_read([runtime.ffprobe, "-v", "error", "-select_streams", "v:0",
        "-show_entries", "stream=width,height,r_frame_rate,avg_frame_rate,duration:format=duration",
        "-of", "json", str(source)]))
    streams = raw.get("streams", [])
    if len(streams) != 1:
        raise ValueError("The file must contain a readable video stream")
    stream = streams[0]
    duration = float(stream.get("duration") or raw.get("format", {}).get("duration") or 0)
    width, height = int(stream.get("width", 0)), int(stream.get("height", 0))
    if not math.isfinite(duration) or duration <= 0 or not 16 <= width <= 7680 or not 16 <= height <= 7680:
        raise ValueError("Video duration or dimensions are invalid (maximum dimension 7680)")
    return {"duration_seconds": duration, "width": width, "height": height,
            "average_frame_rate": stream.get("avg_frame_rate"), "nominal_frame_rate": stream.get("r_frame_rate")}


def _runtime_probe(runtime: VideoMocapRuntime, detector: str) -> dict:
    # This fixed probe imports the installed numerical runtime, but never constructs a model.
    code = (
        "import importlib.util,json,pathlib,sys; "
        "names=['torch','torchvision','cv2','numpy','scipy','yacs','tqdm','joblib','chumpy','ipdb','termcolor','tabulate','yaml']; "
        "missing=[n for n in names if importlib.util.find_spec(n) is None]; "
        "spec=importlib.util.find_spec('mediapipe'); "
        "mp=str(pathlib.Path(spec.origin).parent) if spec and spec.origin else None; "
        "import torch,torchvision.models as models; "
        "import torchvision.models.resnet as resnet; "
        "url=models.ResNet50_Weights.IMAGENET1K_V1.url if hasattr(models,'ResNet50_Weights') else resnet.model_urls['resnet50']; "
        "cache=str(pathlib.Path(torch.hub.get_dir())/'checkpoints'/url.rsplit('/',1)[-1]); "
        "print(json.dumps({'python':sys.version.split()[0],'missing':missing,'cuda':torch.cuda.is_available(),'mediapipe_path':mp,'resnet_cache':cache}))"
    )
    result = json.loads(_run_read([runtime.python, "-c", code], cwd=Path(runtime.repo_path), timeout=60))
    modules = ["numpy", "scipy", "chumpy", "yaml", "tabulate", "easymocap.config.baseconfig",
               "easymocap.datasets.base", "easymocap.bodymodel.smpl", "easymocap.multistage.base",
               "easymocap.multistage.init_cnn", "easymocap.estimator.SPIN.models"]
    modules += (["easymocap.estimator.mediapipe_wrapper"] if detector == "mp-pose" else
                ["easymocap.estimator.HRNet", "easymocap.estimator.YOLOv4"])
    import_check = (
        "import importlib,json,socket\n"
        "def offline(*args,**kwargs):\n    raise RuntimeError('Network disabled during runtime check')\n"
        "socket.create_connection=offline\nsocket.socket.connect=offline\nsocket.socket.connect_ex=offline\n"
        "failures=[]\n"
        f"for name in {modules!r}:\n"
        "    try: importlib.import_module(name)\n"
        "    except Exception as error: failures.append(name+': '+str(error))\n"
        "print(json.dumps(failures))\n"
    )
    imported = _run_read([runtime.python, "-c", import_check], cwd=Path(runtime.repo_path), timeout=60)
    result["missing"].extend(json.loads(imported.splitlines()[-1]))
    runtime_assets = []
    cache = Path(result["resnet_cache"])
    if not cache.is_file():
        result["missing"].append("cached torchvision ResNet50 ImageNet V1 weights: " + str(cache))
    else:
        runtime_assets.append(str(cache))
    if detector == "mp-pose":
        mp_path = result.get("mediapipe_path")
        if not mp_path:
            result["missing"].append("mediapipe==0.10.0")
        else:
            supported = _run_read([runtime.python, "-c", "import mediapipe as m; print(bool(hasattr(m,'solutions') and hasattr(m.solutions,'pose')))"],
                                  cwd=Path(runtime.repo_path), timeout=30)
            if supported != "True":
                result["missing"].append("MediaPipe solutions.pose API (reviewed version 0.10.0)")
            for name in ("modules/pose_landmark/pose_landmark_heavy.tflite", "modules/pose_detection/pose_detection.tflite"):
                if not (Path(mp_path) / name).is_file():
                    result["missing"].append("mediapipe/" + name)
                else:
                    runtime_assets.append(str(Path(mp_path) / name))
    result["assets"] = runtime_assets
    return result


def plan_video_mocap(settings: Settings, request: VideoMocapRequest | dict) -> dict:
    request = VideoMocapRequest.model_validate(request)
    source_path = Path(request.video_path)
    checks: list[dict] = []
    source: dict = {"path": request.video_path}
    runtime = None
    assets: list[dict] = []
    runtime_assets: list[dict] = []

    def check(name: str, action) -> object | None:
        try:
            result = action()
            checks.append({"name": name, "ready": True, "detail": "Available"})
            return result
        except (OSError, ValueError, subprocess.SubprocessError, KeyError, TypeError) as error:
            checks.append({"name": name, "ready": False, "detail": redact(str(error))[:1500]})
            return None

    def inspect_source() -> dict:
        if not source_path.is_file() or not 0 < source_path.stat().st_size <= 2 * 1024**3:
            raise ValueError("Choose an existing video no larger than 2 GiB")
        return {"path": str(source_path), "bytes": source_path.stat().st_size, "sha256": _hash(source_path)}

    source.update(check("source_video", inspect_source) or {})
    def configured_runtime() -> VideoMocapRuntime:
        path = _base(settings) / "runtime.json"
        if not path.is_file():
            raise ValueError(f"Set up the separate EasyMocap environment and model files, then create {path}. See the EasyMocap setup guide.")
        return VideoMocapRuntime.model_validate(_json(path))

    runtime_value = check("runtime_configuration", configured_runtime)
    if isinstance(runtime_value, VideoMocapRuntime):
        runtime = runtime_value
        repo = Path(runtime.repo_path)

        def revision() -> str:
            actual = _run_read(["git", "-C", str(repo), "rev-parse", "HEAD"])
            if actual != EASYMOCAP_REVISION:
                raise ValueError(f"Use the reviewed EasyMocap revision {EASYMOCAP_REVISION}")
            if _run_read(["git", "-C", str(repo), "status", "--porcelain", "--untracked-files=no"]):
                raise ValueError("The EasyMocap tracked source must match the reviewed revision without local edits")
            for name in PIPELINE_FILES:
                if not (repo / name).is_file():
                    raise ValueError(f"Missing reviewed pipeline file: {name}")
            return actual

        check("reviewed_source", revision)

        def model_assets() -> list[dict]:
            names = list(MODEL_FILES)
            if request.detector == "yolo-hrnet":
                names += ["data/models/yolov4.weights", "data/models/pose_hrnet_w48_384x288.pth"]
            missing = [name for name in names if not (repo / name).is_file()]
            if missing:
                raise ValueError("Install separately licensed assets locally: " + ", ".join(missing))
            return [{"path": name, "bytes": (repo / name).stat().st_size, "sha256": _hash(repo / name)} for name in names]

        assets = check("model_assets", model_assets) or []

        def dependencies() -> dict:
            value = _runtime_probe(runtime, request.detector)
            if value.get("missing"):
                raise ValueError("Missing runtime dependencies/assets: " + ", ".join(value["missing"]))
            if runtime.device == "cuda" and not value.get("cuda"):
                raise ValueError("The configured runtime has no available CUDA device")
            return value

        dependencies_result = check("python_dependencies", dependencies)
        if dependencies_result:
            runtime_assets = [{"path": name, "bytes": Path(name).stat().st_size, "sha256": _hash(Path(name))}
                              for name in dependencies_result["assets"]]
        def encoder() -> bool:
            available = _run_read([runtime.ffmpeg, "-hide_banner", "-encoders"])
            if not re.search(r"\blibx264\b", available):
                raise ValueError("FFmpeg must include the libx264 encoder for the reviewed reference clip")
            return True

        check("ffmpeg", encoder)
        metadata = check("video_metadata", lambda: _probe_video(runtime, source_path))
        if metadata:
            source.update(metadata)

            def timing() -> bool:
                if request.start_seconds + request.frames / request.fps > metadata["duration_seconds"] + 0.001:
                    raise ValueError("The selected time range extends beyond the video")
                return True

            check("selected_time_range", timing)
    ready = bool(checks) and all(item["ready"] for item in checks)
    return {"request": request.model_dump(), "source": source, "frames": request.frames,
            "ready": ready, "preflight": {"ready": ready, "checks": checks},
            "runtime": runtime.model_dump() if runtime else None, "assets": assets, "runtime_assets": runtime_assets,
            "reviewed_revision": EASYMOCAP_REVISION, "capabilities": CAPABILITIES,
            "stages": ["extract", "detect", "validate_detections", "fit", "normalize", "verify"]}


def _process_identity(pid: object) -> str | None:
    """Read process start identity; never use os.kill(pid, 0) on Windows."""
    if type(pid) is not int or pid <= 0:
        return None
    if os.name == "nt":
        import ctypes
        from ctypes import wintypes

        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        kernel.OpenProcess.restype = wintypes.HANDLE
        kernel.GetExitCodeProcess.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
        kernel.GetProcessTimes.argtypes = [wintypes.HANDLE] + [ctypes.POINTER(wintypes.FILETIME)] * 4
        kernel.CloseHandle.argtypes = [wintypes.HANDLE]
        handle = kernel.OpenProcess(0x1000, False, pid)
        if not handle:
            return None
        try:
            exit_code = wintypes.DWORD()
            times = [wintypes.FILETIME() for _ in range(4)]
            if not kernel.GetExitCodeProcess(handle, ctypes.byref(exit_code)) or exit_code.value != 259:
                return None
            if not kernel.GetProcessTimes(handle, *(ctypes.byref(value) for value in times)):
                return None
            return str((times[0].dwHighDateTime << 32) | times[0].dwLowDateTime)
        finally:
            kernel.CloseHandle(handle)
    try:
        # Linux start ticks distinguish an unrelated process reusing the same PID.
        fields = Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()
        return fields[19] if fields[0] != "Z" else None
    except OSError:
        return None


def _alive(pid: object, identity: str | None = None) -> bool:
    actual = _process_identity(pid)
    return actual is not None and (identity is None or actual == identity)


def _live_child(directory: Path) -> bool:
    try:
        child = _json(directory / "stage-process.json")
    except (OSError, ValueError):
        return False
    return _alive(child.get("pid"), child.get("identity"))


def _record(directory: Path) -> dict:
    job = _json(directory / "job.json")
    if job.get("id") != directory.name:
        raise ValueError("Video job identity does not match its directory")
    return job


def get_video_mocap_job(settings: Settings, job_id: str) -> dict:
    directory = _directory(settings, job_id)
    job = _record(directory)
    if job.get("status") in ACTIVE and time.time() - job.get("created_at", 0) > 10:
        try:
            launcher = _json(directory / "launcher.json")
        except OSError:
            launcher = {}
        if not _alive(launcher.get("pid"), launcher.get("identity")):
            with FileLock(str(directory / ".state.lock"), timeout=5):
                job = _record(directory)
                if job.get("status") in ACTIVE:
                    message = "The video worker stopped before publishing a result."
                    if _live_child(directory):
                        message += " Its processing stage is still running; new capture is blocked until it exits."
                    else:
                        message += " Start a new job to retry."
                    job.update(status="interrupted", updated_at=time.time(), error=message)
                    atomic_json(directory / "job.json", job)
    result = dict(job)
    if job.get("status") == "completed":
        try:
            result["output"] = _verified_manifest(directory)
        except (OSError, ValueError, KeyError, TypeError) as error:
            result["output"] = None
            result["verification_error"] = str(error)
    return result


def list_video_mocap_jobs(settings: Settings, limit: int = 20) -> dict:
    if type(limit) is not int or not 1 <= limit <= 100:
        raise ValueError("limit must be between 1 and 100")
    base = _base(settings) / "jobs"
    paths = sorted((p for p in base.glob("*") if re.fullmatch(r"[a-f0-9]{32}", p.name)),
                   key=lambda p: p.stat().st_mtime, reverse=True)
    jobs = []
    for path in paths[:limit]:
        try:
            jobs.append(get_video_mocap_job(settings, path.name))
        except (ValueError, OSError):
            continue
    return {"jobs": jobs, "total": len(paths)}


def start_video_mocap(settings: Settings, request: VideoMocapRequest | dict, *, expected_source_sha256: str | None = None) -> dict:
    if expected_source_sha256 is not None and not re.fullmatch(r"[a-f0-9]{64}", expected_source_sha256):
        raise ValueError("The expected source checksum must be a lowercase SHA256 digest")
    base = _base(settings)
    base.mkdir(parents=True, exist_ok=True)
    if (base / "jobs").resolve() != base / "jobs":
        raise ValueError("Video job storage cannot redirect outside its managed directory")
    try:
        with FileLock(str(base / ".dispatch.lock"), timeout=0):
            for path in (base / "jobs").glob("*"):
                if re.fullmatch(r"[a-f0-9]{32}", path.name):
                    job = get_video_mocap_job(settings, path.name)
                    if job.get("status") in ACTIVE or _live_child(path):
                        raise ValueError("A video capture job is already active; wait or cancel it first")
            plan = plan_video_mocap(settings, request)
            if not plan["ready"]:
                problems = "; ".join(c["detail"] for c in plan["preflight"]["checks"] if not c["ready"])
                raise ValueError("Video capture is not ready: " + problems)
            if expected_source_sha256 is not None and plan["source"]["sha256"] != expected_source_sha256:
                raise ValueError("The video changed since the setup check. Check the source again before capturing.")
            identifier = uuid.uuid4().hex
            directory = base / "jobs" / identifier
            directory.mkdir(parents=True)
            internal = {"id": identifier, "request": plan["request"], "source": plan["source"],
                        "runtime": plan["runtime"], "assets": plan["assets"], "runtime_assets": plan["runtime_assets"],
                        "reviewed_revision": EASYMOCAP_REVISION, "timeout": min(max(settings.timeout, 30), 86400)}
            atomic_json(directory / "request.json", internal)
            now = time.time()
            job = {"id": identifier, "status": "queued", "stage": "queued", "created_at": now,
                   "updated_at": now, "request": plan["request"], "source": plan["source"],
                   "request_sha256": _canonical(internal), "error": None, "diagnostics": None, "output": None}
            atomic_json(directory / "job.json", job)
            env = os.environ.copy()
            env["PYTHONPATH"] = str(Path(__file__).resolve().parent.parent)
            env["PYTHONUNBUFFERED"] = "1"
            try:
                with (directory / "worker.log").open("wb") as log:
                    process = subprocess.Popen([sys.executable, "-m", "kimodo_mcp.video_mocap_worker", str(directory)],
                        stdin=subprocess.DEVNULL, stdout=log, stderr=subprocess.STDOUT, env=env,
                        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
                atomic_json(directory / "launcher.json", {"pid": process.pid, "identity": _process_identity(process.pid)})
            except OSError as error:
                job.update(status="failed", stage="launch", error=str(error), updated_at=time.time())
                atomic_json(directory / "job.json", job)
                raise ValueError("The separate video worker could not be started") from error
            return get_video_mocap_job(settings, identifier)
    except Timeout as error:
        raise ValueError("Another client is starting a video capture job") from error


def cancel_video_mocap_job(settings: Settings, job_id: str) -> dict:
    directory = _directory(settings, job_id)
    with FileLock(str(directory / ".state.lock"), timeout=5):
        job = _record(directory)
        if job.get("status") in ACTIVE:
            atomic_json(directory / "cancel.json", {"requested_at": time.time()})
            job.update(status="cancelling", updated_at=time.time())
            atomic_json(directory / "job.json", job)
    return get_video_mocap_job(settings, job_id)


def get_video_mocap_log(settings: Settings, job_id: str, lines: int = 80) -> dict:
    if type(lines) is not int or not 1 <= lines <= 500:
        raise ValueError("lines must be between 1 and 500")
    path = _directory(settings, job_id) / "worker.log"
    if not path.exists():
        return {"id": job_id, "text": ""}
    with path.open("rb") as stream:
        stream.seek(max(0, path.stat().st_size - 128_000))
        value = stream.read().decode("utf-8", errors="replace")
    return {"id": job_id, "text": redact("\n".join(value.splitlines()[-lines:]))}


def _verified_manifest(directory: Path) -> dict:
    request = _json(directory / "request.json")
    job = _record(directory)
    manifest = _json(directory / "manifest.json")
    if manifest.get("job_id") != directory.name or manifest.get("request_sha256") != _canonical(request):
        raise ValueError("Video output is not bound to the selected request")
    if job.get("request_sha256") != manifest["request_sha256"] or manifest.get("source_sha256") != request["source"]["sha256"]:
        raise ValueError("Video output source identity does not match")
    controls = VideoMocapRequest.model_validate(request["request"])
    if (request.get("id") != directory.name or job.get("request") != request["request"]
            or job.get("source") != request["source"] or manifest.get("frames") != controls.frames
            or manifest.get("fps") != controls.fps or manifest.get("skeleton") != "smpl24"
            or manifest.get("model") != "easymocap-smpl" or manifest.get("reviewed_revision") != EASYMOCAP_REVISION):
        raise ValueError("Video output timing, rig, or source does not match its request")
    entries = manifest.get("artifacts")
    if (not isinstance(entries, list) or not all(isinstance(e, dict) for e in entries)
            or {e.get("name") for e in entries} != ARTIFACT_NAMES or len(entries) != len(ARTIFACT_NAMES)):
        raise ValueError("Video output artifact list is incomplete or invalid")
    for entry in entries:
        name = entry["name"]
        if entry.get("path") != name:
            raise ValueError("Video artifact paths must be fixed managed filenames")
        path = directory / name
        if path.resolve() != path or not path.is_file():
            raise ValueError("Video artifact path escaped its job")
        if path.stat().st_size != entry.get("bytes") or _hash(path) != entry.get("sha256"):
            raise ValueError("Video artifact changed after verification")
    return manifest


def get_video_mocap_artifact(settings: Settings, job_id: str, name: str) -> Path:
    if name not in ARTIFACT_NAMES:
        raise ValueError("Unknown video capture artifact")
    directory = _directory(settings, job_id)
    if _record(directory).get("status") != "completed":
        raise ValueError("Video output is available only after successful verification")
    _verified_manifest(directory)
    return directory / name


def get_video_mocap_preview_data(settings: Settings, job_id: str) -> dict:
    path = get_video_mocap_artifact(settings, job_id, "motion.npz")
    manifest = _json(path.parent / "manifest.json")
    with zipfile.ZipFile(path) as archive:
        if len(archive.infolist()) > 32 or sum(entry.file_size for entry in archive.infolist()) > 64 * 1024**2:
            raise ValueError("The skeleton archive exceeds its bounded review size")
    with np.load(path, allow_pickle=False) as motion:
        positions = motion["posed_joints"]
        if positions.shape != (manifest["frames"], 24, 3) or not np.isfinite(positions).all():
            raise ValueError("The reviewed skeleton has invalid dimensions or coordinates")
        result = {"job_id": job_id, "source_kind": "video-mocap", "source_sha256": manifest["source_sha256"],
                "motion_sha256": next(entry["sha256"] for entry in manifest["artifacts"] if entry["name"] == "motion.npz"),
                "skeleton": "smpl24", "model": "easymocap-smpl", "fps": manifest["fps"], "frames": manifest["frames"],
                "joint_names": motion["joint_names"].tolist(), "joint_parents": motion["joint_parents"].tolist(),
                "positions": positions.tolist(), "source_time_seconds": motion["source_time_seconds"].tolist(),
                "keypoints2d": motion["keypoints2d"].tolist(), "coordinates": manifest["coordinates"],
                "estimated_frames": motion["estimated_frames"].tolist() if "estimated_frames" in motion.files else [],
                "editable": {"supported": False, "reason": "This capture uses SMPL24, not the SOMA correction rig."},
                "contact_status": "unavailable"}
    expected = next(entry["sha256"] for entry in manifest["artifacts"] if entry["name"] == "motion.npz")
    if _hash(path) != expected:
        raise ValueError("The reviewed motion changed while it was being read")
    return result
