"""Bounded, registered-asset access to the released UniRig rigging stages."""

from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
import sys
import time
import uuid
from pathlib import Path
from typing import Annotated, Literal

from filelock import FileLock
from pydantic import Field, StrictInt, model_validator

from .config import Settings
from .jobs import atomic_json
from .schemas import Finite, StrictModel
from .video_mocap import _alive, _process_identity

UNIRIG_REVISION = "6793c6640ff01c8fb389f3993434124bb43d2933"
AssetId = Annotated[str, Field(pattern=r"^[0-9a-f]{64}$", strict=True)]
JobId = Annotated[str, Field(pattern=r"^[0-9a-f]{32}$", strict=True)]
ACTIVE = {"queued", "running", "cancelling"}
INPUT_SUFFIXES = {".obj", ".fbx", ".glb", ".vrm"}
MAX_ASSET_BYTES = 512 * 1024**2
MAX_TOTAL_BYTES = 1024**3
MODULES = ("torch", "numpy", "bpy", "yaml", "box", "trimesh", "fast_simplification", "scipy", "tqdm",
           "lightning", "transformers", "huggingface_hub", "open3d", "pyrender", "spconv",
           "torch_scatter", "torch_cluster", "flash_attn")


class UniRigInput(StrictModel):
    asset_id: AssetId
    rig_asset_id: AssetId | None = None
    source_job_id: JobId | None = None
    source_item_index: Annotated[StrictInt, Field(ge=0, le=3)] = 0
    rig_output: Literal["skeleton", "skin"] = "skeleton"

    @model_validator(mode="after")
    def one_rig_source(self):
        if self.rig_asset_id and self.source_job_id:
            raise ValueError("Choose a registered rig asset or a completed UniRig output, not both")
        if self.source_job_id is None and (self.source_item_index != 0 or self.rig_output != "skeleton"):
            raise ValueError("source_item_index and rig_output apply only to a prior UniRig job")
        return self


class UniRigRequest(StrictModel):
    operation: Literal["preprocess", "skeleton", "skin", "merge", "pipeline"] = "pipeline"
    assets: Annotated[list[UniRigInput], Field(min_length=1, max_length=4)]
    seed: Annotated[StrictInt, Field(ge=0, le=2**32 - 1)] = 12345
    faces_target_count: Annotated[StrictInt, Field(ge=100, le=100000)] = 50000
    do_sample: Annotated[bool, Field(strict=True)] = True
    max_new_tokens: Annotated[StrictInt, Field(ge=1, le=2048)] = 2048
    num_beams: Annotated[StrictInt, Field(ge=1, le=30)] = 15
    top_k: Annotated[StrictInt, Field(ge=1, le=100)] = 5
    top_p: Annotated[Finite, Field(gt=0, le=1)] = 0.95
    temperature: Annotated[Finite, Field(gt=0, le=5)] = 1.5
    repetition_penalty: Annotated[Finite, Field(ge=0.1, le=10)] = 3.0
    skin_backend: Literal["pyrender", "open3d"] = "pyrender"
    output_format: Literal["glb", "fbx"] = "glb"
    add_root: Annotated[bool, Field(strict=True)] = False

    @model_validator(mode="after")
    def applicable_sources(self):
        if len({item.asset_id for item in self.assets}) != len(self.assets):
            raise ValueError("Each batch item must identify a distinct registered mesh asset")
        for item in self.assets:
            rig = item.rig_asset_id is not None or item.source_job_id is not None
            if self.operation == "merge" and not rig:
                raise ValueError("Merge requires an explicit registered or completed rig source")
            if self.operation not in {"skin", "merge"} and rig:
                raise ValueError("An existing rig source applies only to skin or merge")
        defaults = type(self).model_fields
        groups = [
            ({"skeleton", "pipeline"}, ("do_sample", "max_new_tokens", "num_beams", "top_k", "top_p", "temperature", "repetition_penalty")),
            ({"skin", "pipeline"}, ("skin_backend",)),
            ({"merge", "pipeline"}, ("output_format", "add_root")),
        ]
        for operations, fields in groups:
            if self.operation not in operations:
                for name in fields:
                    if getattr(self, name) != defaults[name].default:
                        raise ValueError(f"{name} does not apply to {self.operation}")
        return self


def _hash(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _canonical(value: dict) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()).hexdigest()


def _json(path: Path, maximum: int = 4 * 1024**2) -> dict:
    with path.open("rb") as stream:
        data = stream.read(maximum + 1)
    if len(data) > maximum:
        raise ValueError("UniRig receipt exceeds its bounded size")
    value = json.loads(data)
    if not isinstance(value, dict):
        raise ValueError("UniRig receipt must be an object")
    return value


def _contained(root: Path, relative: str) -> Path:
    path = root / relative
    if path.resolve() != path or not path.is_relative_to(root):
        raise ValueError("UniRig paths must remain inside their assigned directory")
    return path


def _base(settings: Settings) -> Path:
    return _contained(settings.data_dir.resolve(), "unirig")


def _directory(settings: Settings, job_id: str) -> Path:
    if not isinstance(job_id, str) or not re.fullmatch(r"[0-9a-f]{32}", job_id):
        raise ValueError("Invalid UniRig job id")
    return _contained(_base(settings), f"jobs/{job_id}")


def _read_command(argv, timeout=15):
    return subprocess.run(argv, stdin=subprocess.DEVNULL, capture_output=True, text=True,
                          check=True, timeout=timeout,
                          creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0)).stdout.strip()


def _runtime(*, probe=False, operation="pipeline") -> dict:
    repo_value = os.environ.get("KIMODO_UNIRIG_REPO", "")
    python_value = os.environ.get("KIMODO_UNIRIG_PYTHON", "")
    result = {"repo": str(Path(repo_value).resolve()) if repo_value else None,
              "python": str(Path(python_value).resolve()) if python_value else None,
              "reviewed_revision": UNIRIG_REVISION, "checks": [], "modules": {},
              "downloads_allowed": os.environ.get("KIMODO_ALLOW_DOWNLOADS", "").lower() in {"1", "true", "yes"},
              "gpu_verified": False, "weights_verified": False}
    checks = result["checks"]
    checks.append({"name": "python", "ready": bool(python_value and Path(python_value).is_file()),
                   "detail": "Set KIMODO_UNIRIG_PYTHON to a separate installed UniRig Python 3.11 runtime."})
    valid_repo = bool(repo_value and (Path(repo_value) / "run.py").is_file())
    revision_ok = False
    if valid_repo:
        repo = result["repo"]
        prefix = ["git", "-c", f"safe.directory={repo}", "-C", repo]
        try:
            revision = _read_command([*prefix, "rev-parse", "HEAD"])
            dirty = _read_command([*prefix, "status", "--porcelain", "--untracked-files=all"])
            revision_ok = revision == UNIRIG_REVISION and not dirty
        except (OSError, subprocess.SubprocessError):
            revision_ok = False
    checks.append({"name": "pinned_checkout", "ready": revision_ok,
                   "detail": "KIMODO_UNIRIG_REPO must be the clean reviewed checkout; generated files belong in managed jobs."})
    if probe and checks[0]["ready"]:
        required = MODULES if operation not in {"preprocess", "merge"} else (
            "numpy", "bpy", "yaml", "box", "trimesh", "fast_simplification", "scipy", "tqdm", "open3d")
        result["required_modules"] = list(required)
        script = ("import importlib.util,json,sys; names=" + repr(required)
                  + "; print(json.dumps({'python_version':list(sys.version_info[:2]),"
                  + "'modules':{name:importlib.util.find_spec(name) is not None for name in names}}))")
        try:
            values = json.loads(_read_command([result["python"], "-c", script]))
            result.update(modules=values["modules"], python_version=values["python_version"])
            deps = values["python_version"] == [3, 11] and all(values["modules"].get(name) is True for name in required)
            detail = "Module discovery only; CUDA execution, checkpoint access and model quality remain unverified."
        except (OSError, ValueError, KeyError, subprocess.SubprocessError) as error:
            deps, detail = False, f"Runtime package discovery failed: {str(error)[:300]}"
        checks.append({"name": "dependencies", "ready": deps, "detail": detail})
    result["configuration_ready"] = all(check["ready"] for check in checks)
    result["dependency_probe_performed"] = probe and checks[0]["ready"]
    return result


def get_unirig_capabilities(settings: Settings) -> dict:
    return {"source_kind": "unirig", "reviewed_revision": UNIRIG_REVISION, "license": "MIT",
            "runtime": _runtime(), "operations": ["preprocess", "skeleton", "skin", "merge", "pipeline"],
            "input_formats": sorted(suffix[1:] for suffix in INPUT_SUFFIXES),
            "merged_formats": ["glb", "fbx"], "max_batch_assets": 4,
            "schema": UniRigRequest.model_json_schema(),
            "boundaries": [
                "An explicit start runs separate preprocessing/rigging processes; status and planning never run inference.",
                "Skeleton and skinning use the released Articulation-XL checkpoints, not the unreleased paper checkpoints.",
                "Bone physics attributes and Rig-XL/VRoid model checkpoints are not available in the reviewed release.",
                "Training and RigNet benchmark code are operator workflows; no arbitrary task/config path is exposed.",
                "UniRig creates a static rig. It does not animate, automatically map it to SOMA/Manny, or guarantee deformation quality.",
                "UniRig accepts geometry-only OBJ without mtllib/usemtl references. External FBX texture sidecars are not copied or fingerprinted; prefer self-contained GLB/embedded textures.",
                "VRM requires the operator-installed modified upstream addon. Blender package/runtime compatibility is separate from Studio's editor bridge.",
            ]}


def resolve_registered_asset(settings: Settings, asset_id: str) -> dict:
    from .studio_assets import resolve_asset

    value = resolve_asset(settings, asset_id)
    path = Path(value["absolute_path"])
    if not path.is_file() or path.suffix.lower() not in INPUT_SUFFIXES:
        raise ValueError("UniRig requires a registered OBJ, FBX, GLB or VRM asset")
    if not 0 < path.stat().st_size <= MAX_ASSET_BYTES:
        raise ValueError("Each UniRig asset must be nonempty and at most 512 MiB")
    if path.stat().st_size != value["bytes"] or _hash(path) != value["sha256"]:
        raise ValueError("Registered UniRig asset changed after registration")
    validate_obj_source(path)
    return {"asset_id": asset_id, "absolute_path": str(path), "sha256": value["sha256"],
            "bytes": value["bytes"], "format": path.suffix.lower()[1:]}


def validate_obj_source(path: Path) -> None:
    """Do not let upstream OBJ import follow unrecorded material sidecars.

    Preview conversion can deliberately strip materials; UniRig instead rejects
    those sources so its immutable staged bytes remain identical to the input.
    FBX is passed to the trusted native importer and retains its own behavior.
    """
    if path.suffix.lower() != ".obj":
        return
    with path.open("rb") as stream:
        while line := stream.readline(65537):
            if len(line) > 65536:
                raise ValueError("UniRig OBJ lines must be at most 64 KiB; export a self-contained GLB instead")
            directive = line.lstrip().removeprefix(b"\xef\xbb\xbf").lstrip().split(None, 1)
            if directive and directive[0].lower() in {b"mtllib", b"usemtl"}:
                raise ValueError(
                    "UniRig requires a geometry-only OBJ without mtllib/usemtl material references. "
                    "Export a self-contained GLB or a geometry-only OBJ and register that file."
                )


def plan_unirig(settings: Settings, request: UniRigRequest | dict) -> dict:
    request = UniRigRequest.model_validate(request)
    assets = []
    total_bytes = 0
    for index, item in enumerate(request.assets):
        mesh = resolve_registered_asset(settings, item.asset_id)
        rig = None
        if item.rig_asset_id:
            rig = resolve_registered_asset(settings, item.rig_asset_id)
        elif item.source_job_id:
            name = f"outputs/item_{item.source_item_index:02d}/{item.rig_output}.fbx"
            path = get_unirig_artifact(settings, item.source_job_id, name)
            rig = {"source_job_id": item.source_job_id, "source_artifact": name,
                   "absolute_path": str(path), "sha256": _hash(path), "bytes": path.stat().st_size, "format": "fbx"}
        total_bytes += mesh["bytes"] + (rig["bytes"] if rig else 0)
        assets.append({"item_index": index, "mesh": mesh, "rig": rig})
    if total_bytes > MAX_TOTAL_BYTES:
        raise ValueError("UniRig batch source files exceed the 1 GiB total limit")
    runtime = _runtime(probe=True, operation=request.operation)
    operations = {
        "preprocess": ["extract_mesh"], "skeleton": ["extract_mesh", "predict_skeleton"],
        "skin": ["extract_rig", "predict_skin"], "merge": ["merge_rig"],
        "pipeline": ["extract_mesh", "predict_skeleton", "extract_rig", "predict_skin", "merge_rig"],
    }
    return {"ready": runtime["configuration_ready"], "request": request.model_dump(mode="json"),
            "assets": assets, "runtime": runtime, "stages": operations[request.operation],
            "notes": ["No stage was executed by this plan. Starting remains explicit.",
                      "Runtime dependency presence is not proof of usable CUDA, cached weights, or sufficient memory.",
                      "Inputs are copied and checksummed; each retry creates a new job and preserves earlier files.",
                      "max_new_tokens limits decoder tokens, not bones; a lower cap can truncate or fail skeleton prediction.",
                      "With do_sample=false, native generation does not use top_k, top_p or temperature; their requested values are retained.",
                      "Skeleton-only merge has no predicted skin weights. Use a completed skin output for a deforming asset.",
                      "Output rest frames, scale, bone naming and deformation must be reviewed before retargeting or engine import."]}


def _record(directory: Path) -> dict:
    value = _json(_contained(directory, "job.json"))
    if value.get("id") != directory.name:
        raise ValueError("UniRig job identity does not match its directory")
    return value


def _live_child(directory: Path) -> bool:
    try:
        child = _json(_contained(directory, "stage-process.json"))
        return _alive(child.get("pid"), child.get("identity"))
    except (ValueError, OSError):
        return False


def _verified_manifest(directory: Path) -> dict:
    job = _record(directory)
    request = _json(_contained(directory, "request.json"))
    manifest = _json(_contained(directory, "manifest.json"))
    if request.get("id") != directory.name or _canonical(request) != job.get("request_sha256"):
        raise ValueError("UniRig request identity or checksum changed")
    if manifest.get("job_id") != directory.name or manifest.get("request_sha256") != job["request_sha256"]:
        raise ValueError("UniRig output is not bound to this request")
    if _canonical(manifest) != job.get("manifest_sha256"):
        raise ValueError("UniRig output receipt checksum changed")
    if manifest.get("reviewed_revision") != UNIRIG_REVISION:
        raise ValueError("UniRig output has an unreviewed source revision")
    for entry in manifest.get("sources", []) + manifest.get("artifacts", []) + manifest.get("configuration", []):
        path = _contained(directory, entry["path"])
        if not path.is_file() or path.stat().st_size != entry["bytes"] or _hash(path) != entry["sha256"]:
            raise ValueError("UniRig source or output checksum changed")
    if not manifest.get("artifacts") or not manifest.get("sources"):
        raise ValueError("UniRig receipt does not contain verified sources and outputs")
    return manifest


def get_unirig_job(settings: Settings, job_id: str) -> dict:
    directory = _directory(settings, job_id)
    job = _record(directory)
    if job.get("status") in ACTIVE and time.time() - job.get("created_at", 0) > 10:
        try:
            launcher = _json(_contained(directory, "launcher.json"))
        except OSError:
            launcher = {}
        if not _alive(launcher.get("pid"), launcher.get("identity")):
            with FileLock(_contained(directory, ".state.lock"), timeout=5):
                job = _record(directory)
                if job.get("status") in ACTIVE:
                    job.update(status="interrupted", updated_at=time.time(),
                               error="UniRig supervisor stopped before verification. A surviving owned stage blocks new jobs until it exits.")
                    atomic_json(directory / "job.json", job)
    if job.get("status") == "completed":
        try:
            return {**job, "output": _verified_manifest(directory)}
        except (ValueError, OSError, KeyError, TypeError) as error:
            return {**job, "output": None, "verification_error": str(error)}
    return job


def list_unirig_jobs(settings: Settings, limit: int = 20) -> dict:
    if type(limit) is not int or not 1 <= limit <= 100:
        raise ValueError("limit must be between 1 and 100")
    root = _contained(_base(settings), "jobs")
    paths = sorted((path for path in root.glob("*") if re.fullmatch(r"[0-9a-f]{32}", path.name)),
                   key=lambda path: path.stat().st_mtime, reverse=True)
    values, invalid = [], 0
    for path in paths[:limit]:
        try:
            values.append(get_unirig_job(settings, path.name))
        except (ValueError, OSError):
            invalid += 1
    return {"jobs": values, "total": len(paths), "invalid_count": invalid}


def start_unirig(settings: Settings, request: UniRigRequest | dict) -> dict:
    base = _base(settings)
    base.mkdir(parents=True, exist_ok=True)
    with FileLock(_contained(base, ".dispatch.lock"), timeout=5):
        for path in _contained(base, "jobs").glob("*"):
            if re.fullmatch(r"[0-9a-f]{32}", path.name):
                if get_unirig_job(settings, path.name).get("status") in ACTIVE or _live_child(path):
                    raise ValueError("A UniRig job or its owned stage is already active; wait or cancel it first")
        plan = plan_unirig(settings, request)
        if not plan["ready"]:
            problems = "; ".join(check["detail"] for check in plan["runtime"]["checks"] if not check["ready"])
            raise ValueError("UniRig runtime is not ready: " + problems)
        identifier = uuid.uuid4().hex
        directory = _directory(settings, identifier)
        directory.mkdir(parents=True, exist_ok=False)
        internal = {"id": identifier, "request": plan["request"], "assets": plan["assets"],
                    "runtime": plan["runtime"], "reviewed_revision": UNIRIG_REVISION,
                    "timeout": min(max(settings.timeout, 30), 86400)}
        atomic_json(directory / "request.json", internal)
        now = time.time()
        job = {"id": identifier, "status": "queued", "stage": "queued", "created_at": now,
               "updated_at": now, "request": plan["request"], "request_sha256": _canonical(internal),
               "error": None, "output": None}
        atomic_json(directory / "job.json", job)
        env = os.environ.copy()
        env["PYTHONPATH"] = str(Path(__file__).resolve().parent.parent)
        env["PYTHONUNBUFFERED"] = "1"
        try:
            process = subprocess.Popen([sys.executable, "-m", "kimodo_mcp.unirig_worker", str(directory)],
                stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, env=env,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
            atomic_json(directory / "launcher.json", {"pid": process.pid, "identity": _process_identity(process.pid)})
        except OSError as error:
            job.update(status="failed", stage="launch", error=str(error)[:2000], updated_at=time.time())
            atomic_json(directory / "job.json", job)
            raise ValueError("The UniRig supervisor could not start") from error
        return get_unirig_job(settings, identifier)


def cancel_unirig_job(settings: Settings, job_id: str) -> dict:
    directory = _directory(settings, job_id)
    with FileLock(_contained(directory, ".state.lock"), timeout=5):
        job = _record(directory)
        if job.get("status") in ACTIVE:
            atomic_json(directory / "cancel.json", {"job_id": job_id, "requested_at": time.time()})
            job.update(status="cancelling", updated_at=time.time())
            atomic_json(directory / "job.json", job)
    return get_unirig_job(settings, job_id)


def get_unirig_log(settings: Settings, job_id: str, lines: int = 80) -> dict:
    if type(lines) is not int or not 1 <= lines <= 500:
        raise ValueError("lines must be between 1 and 500")
    directory = _directory(settings, job_id)
    _record(directory)
    path = _contained(directory, "worker.log")
    if not path.exists():
        return {"job_id": job_id, "text": ""}
    with path.open("rb") as stream:
        stream.seek(max(0, path.stat().st_size - 65536))
        text = stream.read(65536).decode("utf-8", errors="replace")
    return {"job_id": job_id, "text": "\n".join(text.splitlines()[-lines:])}


def get_unirig_artifact(settings: Settings, job_id: str, name: str) -> Path:
    directory = _directory(settings, job_id)
    if _record(directory).get("status") != "completed":
        raise ValueError("UniRig artifacts are available only after successful completion")
    manifest = _verified_manifest(directory)
    if name not in {entry["path"] for entry in manifest["artifacts"]}:
        raise ValueError("Artifact is not listed in the verified UniRig manifest")
    return _contained(directory, name)
