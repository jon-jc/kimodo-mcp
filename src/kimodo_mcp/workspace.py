"""Shared local workspace discovery and editor workflow for MCP and the studio.

This module has no HTTP or MCP dependency. Both transports use the same receipt
checks and locks; relative browser URLs are optional presentation metadata.
"""

from __future__ import annotations

import copy
import hashlib
import json
import re
import threading
from collections.abc import Callable
from pathlib import Path
from typing import Any
from urllib.parse import quote

from filelock import FileLock, Timeout

from .config import Settings
from .handoff import UnrealTarget
from .jobs import JobStore, atomic_json, contained_file

DOWNLOAD_PATH = re.compile(
    r"(?:motion_\d{2}(?:_amass)?\.(?:npz|bvh|csv)|motion_\d{2}_preview\.html|"
    r"reviews/(?:motion|comparison)-[0-9a-f]{20}\.(?:html|json)|"
    r"handoffs/(?:character|skeleton)-motion_\d{2}(?:-attempt\d{2})?/"
    r"(?:character-preview\.glb|(?:character|animation)\.(?:fbx|blend)|blender-report\.json|"
    r"unreal-[0-9a-f]{16}/unreal-report\.json))"
)
_CHARACTER_DIRECTORY = re.compile(r"character-motion_(\d{2})(?:-attempt(\d{2}))?")
_SKELETON_DIRECTORY = re.compile(r"skeleton-motion_(\d{2})(?:-attempt(\d{2}))?")
_NATIVE_FILE = re.compile(r"motion_(\d{2})(_amass)?\.(npz|bvh|csv|html)")
_EXPORTS = (
    ("preview_glb", "character-preview.glb", "Animated GLB", "character_preview"),
    ("character_fbx", "character.fbx", "Character FBX", "character_mesh"),
    ("fbx", "animation.fbx", "Animation FBX", "animation"),
    ("blend", "character.blend", "Blender project", "blender_project"),
)


class Workspace:
    def __init__(self, settings: Settings, store: JobStore | None = None):
        self.settings = settings
        self.store = store if store is not None else JobStore(settings)
        self._preview_cache: dict[Path, tuple[tuple, dict[str, Any]]] = {}
        self._preview_cache_lock = threading.Lock()

    def _cached_validation(self, report_path: Path, watched: list[Path], validate: Callable[[], dict]) -> dict:
        def signature():
            result = []
            for path in watched:
                if path.is_symlink() or not path.is_file():
                    raise ValueError("Handoff provenance files must be regular files")
                stat = path.stat()
                result.append((str(path), stat.st_size, stat.st_mtime_ns, stat.st_ctime_ns, stat.st_ino))
            return tuple(result)

        # Polls reuse hash validation only while every receipt/input/export stat
        # matches. Return copies so callers cannot change subsequent results.
        with self._preview_cache_lock:
            before = signature()
            cached = self._preview_cache.get(report_path)
            if cached and cached[0] == before:
                return copy.deepcopy(cached[1])
            result = validate()
            if signature() != before:
                raise ValueError("Handoff files changed during validation; inspect the handoff again")
            if len(self._preview_cache) >= 512:
                self._preview_cache.clear()
            self._preview_cache[report_path] = before, result
            return copy.deepcopy(result)

    def file_url(self, path_value: str | Path) -> str | None:
        if not isinstance(path_value, (str, Path)):
            return None
        try:
            parts = Path(path_value).resolve().relative_to((self.settings.data_dir / "jobs").resolve()).parts
            if len(parts) >= 2 and DOWNLOAD_PATH.fullmatch("/".join(parts[1:])):
                return "/api/files/" + "/".join(quote(part, safe="") for part in parts)
        except (ValueError, OSError):
            pass
        return None

    def project_target(self) -> dict[str, Any] | None:
        path = self.settings.data_dir / "studio.json"
        try:
            if path.is_symlink() or not path.is_file() or path.stat().st_size > 16384:
                return None
            return UnrealTarget.model_validate(json.loads(path.read_text(encoding="utf-8"))["unreal_target"]).model_dump()
        except (ValueError, KeyError, TypeError, OSError):
            return None

    def save_unreal_target(self, target: UnrealTarget) -> dict[str, Any]:
        target = UnrealTarget.model_validate(target)
        path = self.settings.data_dir / "studio.json"
        try:
            with FileLock(str(self.settings.data_dir / "studio-config.lock"), timeout=0):
                value = {}
                if path.exists():
                    if path.is_symlink() or not path.is_file() or path.stat().st_size > 16384:
                        raise ValueError("Studio configuration must be a bounded regular JSON file")
                    value = json.loads(path.read_text(encoding="utf-8"))
                    if not isinstance(value, dict):
                        raise ValueError("Studio configuration must be an object")
                value["unreal_target"] = target.model_dump()
                if len(json.dumps(value, indent=2, allow_nan=False).encode("utf-8")) > 16384:
                    raise ValueError("Studio configuration exceeds its size limit")
                atomic_json(path, value)
        except Timeout as exc:
            raise RuntimeError("Another client is saving the studio configuration") from exc
        return {"target": target.model_dump()}

    def character_preview(self) -> Path | None:
        registration = self.settings.data_dir / "character" / "target.json"
        try:
            value = {}
            if registration.is_file():
                if registration.stat().st_size > 8192:
                    return None
                value = json.loads(registration.read_text(encoding="utf-8"))
                if not isinstance(value, dict):
                    return None
            path = Path(value.get("preview_glb", self.settings.data_dir / "character/final/anime-girl-wave-preview.glb"))
            if not path.is_absolute() or path.suffix.lower() != ".glb" or not path.is_file():
                return None
            return path.resolve()
        except (OSError, ValueError, TypeError):
            return None

    def _output_record(self, path: Path, source: Path, checksum: str, role: str,
                       sample_index: int, attempt: int | None = None) -> dict[str, Any]:
        return {
            "path": path.relative_to(source).as_posix(), "absolute_path": str(path), "uri": path.as_uri(),
            "sha256": checksum, "bytes": path.stat().st_size, "format": path.suffix[1:].lower(),
            "role": role, "sample_index": sample_index, "attempt": attempt, "verified": True,
            "url": self.file_url(path),
        }

    def _verified_character(self, report_path: Path, source: Path, job: dict) -> dict[str, Any]:
        from .blender_bridge import _read_json, _validate_report, _validate_request

        match = _CHARACTER_DIRECTORY.fullmatch(report_path.parent.name)
        if match is None:
            raise ValueError("Unexpected character handoff directory")
        index, attempt = int(match[1]), int(match[2] or 0)
        manifest = job.get("manifest", {})
        if job["status"] != "completed" or not 0 <= index < manifest.get("samples", 0):
            raise ValueError("Preview sample is outside this completed job")
        if source.is_symlink() or source.resolve() != self.store.directory(job["id"]).resolve():
            raise ValueError("Preview source differs from its selected job")
        directory = source / "handoffs" / report_path.parent.name
        report_path = contained_file(source, str(report_path.relative_to(source)))
        request_path = contained_file(source, str((directory / "handoff.json").relative_to(source)))
        if report_path.parent != directory.resolve() or directory.is_symlink():
            raise ValueError("Preview report must stay in its own handoff")
        prepared = _read_json(request_path)
        name = f"motion_{index:02d}.npz"
        entries = [item for item in manifest.get("artifacts", []) if item.get("path") == name]
        motion = contained_file(source, name)
        if (
            len(entries) != 1 or prepared.get("kind") != "character"
            or prepared.get("schema_version") != 1 or type(prepared.get("sample_index")) is not int
            or prepared["sample_index"] != index or prepared.get("source_sha256") != entries[0].get("sha256")
            or not isinstance(prepared.get("source_motion"), str) or Path(prepared["source_motion"]) != motion
            or prepared.get("fps") != manifest.get("fps") or prepared.get("frames") != manifest.get("frames")
        ):
            raise ValueError("Preview does not belong to this job's selected motion sample")
        character = Path(prepared["character_fbx"])
        watched = [source / "manifest.json", report_path, request_path, motion, character,
                   *(directory / filename for _, filename, _, _ in _EXPORTS)]

        def validate():
            bones = _validate_request(prepared, "character")
            report = _validate_report(directory.resolve(), prepared, bones)
            exports, outputs = [], []
            for field, _, label, role in _EXPORTS:
                path = Path(report[field + "_path"])
                output = self._output_record(path, source, report[field + "_sha256"], role, index, attempt)
                output["source_kind"] = "character"
                if output["url"] is None:
                    raise ValueError("Preview export is not a managed studio artifact")
                outputs.append(output)
                exports.append({"label": label, "url": output["url"]})
            return {
                "preview": {"sample_index": index, "url": exports[0]["url"], "attempt": attempt, "exports": exports},
                "outputs": outputs,
            }
        return self._cached_validation(report_path, watched, validate)

    def _verified_skeleton(self, report_path: Path, source: Path, job: dict) -> dict[str, Any]:
        from .blender_bridge import _read_json, _validate_report, _validate_request

        match = _SKELETON_DIRECTORY.fullmatch(report_path.parent.name)
        if match is None:
            raise ValueError("Unexpected skeleton handoff directory")
        index, attempt = int(match[1]), int(match[2] or 0)
        manifest = job.get("manifest", {})
        if job["status"] != "completed" or not 0 <= index < manifest.get("samples", 0):
            raise ValueError("Skeleton sample is outside this completed job")
        if source.is_symlink() or source.resolve() != self.store.directory(job["id"]).resolve():
            raise ValueError("Skeleton source differs from its selected job")
        directory = source / "handoffs" / report_path.parent.name
        report_path = contained_file(source, str(report_path.relative_to(source)))
        request_path = contained_file(source, str((directory / "handoff.json").relative_to(source)))
        if report_path.parent != directory.resolve() or directory.is_symlink():
            raise ValueError("Skeleton report must stay in its own handoff")
        prepared = _read_json(request_path)
        name = f"motion_{index:02d}.bvh"
        entries = [item for item in manifest.get("artifacts", []) if item.get("path") == name]
        motion = contained_file(source, name)
        if (len(entries) != 1 or prepared.get("kind") != "blender" or prepared.get("schema_version") != 1
                or prepared.get("source_sha256") != entries[0].get("sha256")
                or not isinstance(prepared.get("source_bvh"), str) or Path(prepared["source_bvh"]) != motion
                or prepared.get("fps") != manifest.get("fps") or prepared.get("frames") != manifest.get("frames")
                or prepared.get("skeleton") != manifest.get("skeleton")):
            raise ValueError("Skeleton export does not belong to this job's selected motion sample")
        watched = [source / "manifest.json", report_path, request_path, motion,
                   directory / "animation.fbx", directory / "animation.blend"]

        def validate():
            bones = _validate_request(prepared, "blender")
            report = _validate_report(directory.resolve(), prepared, bones)
            outputs = []
            for key, role in (("fbx", "animation"), ("blend", "blender_project")):
                output = self._output_record(Path(report[key + "_path"]), source, report[key + "_sha256"],
                                             role, index, attempt)
                output["source_kind"] = "skeleton"
                outputs.append(output)
            return {"sample_index": index, "attempt": attempt, "outputs": outputs}

        return self._cached_validation(report_path, watched, validate)

    def verified_preview(self, report_path: Path, source: Path, job: dict) -> dict[str, Any]:
        return self._verified_character(report_path, source, job)["preview"]

    def verified_skeleton_outputs(self, report_path: Path, source: Path, job: dict) -> list[dict[str, Any]]:
        return self._verified_skeleton(report_path, source, job)["outputs"]

    def _latest_character(self, job: dict) -> list[dict[str, Any]]:
        latest = {}
        if job["status"] == "completed":
            source = self.store.directory(job["id"])
            for report_path in sorted((source / "handoffs").glob("character-motion_*/blender-report.json")):
                try:
                    result = self._verified_character(report_path, source, job)
                except (OSError, ValueError, KeyError, TypeError, RuntimeError):
                    continue
                preview = result["preview"]
                previous = latest.get(preview["sample_index"])
                if previous is None or preview["attempt"] > previous["preview"]["attempt"]:
                    latest[preview["sample_index"]] = result
        return list(latest.values())

    def job_details(self, job_id: str) -> dict[str, Any]:
        job = self.store.get(job_id)
        job["previews"] = [result["preview"] for result in self._latest_character(job)]
        if job["status"] == "completed":
            source = self.store.directory(job_id)
            job["downloads"] = [
                {**item, "url": self.file_url(source / item["path"])}
                for item in job.get("manifest", {}).get("artifacts", [])
            ]
        return job

    def verified_native_output(self, job_id: str, name: str) -> dict[str, Any]:
        match = _NATIVE_FILE.fullmatch(name)
        if match is None:
            raise ValueError("Expected a native motion artifact name")
        item = self.store.artifact(job_id, name)
        source = self.store.directory(job_id)
        manifest = self.store.get(job_id).get("manifest", {})
        if (source.is_symlink() or (source / name).is_symlink()
                or type(manifest.get("samples")) is not int or not 0 <= int(match[1]) < manifest["samples"]
                or ("sample_index" in item and item["sample_index"] != int(match[1]))):
            raise ValueError("Motion artifact does not belong to the selected job sample")
        path = Path(item["absolute_path"])
        checksum = item.get("sha256")
        before = path.stat()
        if (path.is_symlink() or type(item.get("bytes")) is not int or item["bytes"] <= 0 or before.st_size != item["bytes"]
                or not isinstance(checksum, str) or not re.fullmatch(r"[0-9a-f]{64}", checksum)):
            raise ValueError("Motion artifact metadata differs from its recorded file")
        with path.open("rb") as stream:
            if hashlib.file_digest(stream, "sha256").hexdigest() != checksum:
                raise ValueError("Motion artifact changed after the job completed")
        after = path.stat()
        if (before.st_size, before.st_mtime_ns, before.st_ctime_ns, before.st_ino) != (
            after.st_size, after.st_mtime_ns, after.st_ctime_ns, after.st_ino
        ):
            raise ValueError("Motion artifact changed during validation")
        return self._output_record(path, source, checksum,
                                   "amass" if match[2] else "motion", int(match[1]))

    def list_job_outputs(self, job_id: str) -> dict[str, Any]:
        job = self.store.get(job_id)
        outputs = []
        if job["status"] == "completed":
            names = [item["path"] for item in job.get("manifest", {}).get("artifacts", [])]
            if len(names) != len(set(names)):
                raise ValueError("Motion artifact manifest contains duplicate paths")
            outputs = [self.verified_native_output(job_id, item["path"])
                       for item in job.get("manifest", {}).get("artifacts", [])]
            for result in self._latest_character(job):
                outputs.extend(result["outputs"])
            source = self.store.directory(job_id)
            latest_skeleton = {}
            for report_path in sorted((source / "handoffs").glob("skeleton-motion_*/blender-report.json")):
                try:
                    result = self._verified_skeleton(report_path, source, job)
                except (OSError, ValueError, KeyError, TypeError, RuntimeError):
                    continue
                previous = latest_skeleton.get(result["sample_index"])
                if previous is None or result["attempt"] > previous["attempt"]:
                    latest_skeleton[result["sample_index"]] = result
            for result in latest_skeleton.values():
                outputs.extend(result["outputs"])
        return {"job_id": job_id, "outputs": outputs}

    def build_character_preview(
        self, job_id: str, sample_index: int = 0, attempt: int | None = None, *,
        prepare: Callable[..., Any], execute: Callable[..., Any],
    ) -> dict[str, Any]:
        if type(sample_index) is not int or not 0 <= sample_index <= 3:
            raise ValueError("Sample index must be 0..3")
        if attempt is not None and (type(attempt) is not int or not 0 <= attempt <= 99):
            raise ValueError("Attempt must be 0..99")
        job = self.store.get(job_id)
        if job["status"] != "completed" or sample_index >= job.get("manifest", {}).get("samples", 0):
            raise ValueError("Choose a sample from a completed generation job")
        source = self.store.directory(job_id)
        lock = FileLock(str(source / f".character-preview-{sample_index:02d}.lock"), timeout=0)
        try:
            with lock:
                base = source / "handoffs"
                if attempt is None:
                    matches = [item["preview"] for item in self._latest_character(job)
                               if item["preview"]["sample_index"] == sample_index]
                    if matches:
                        return {**matches[-1], "reused": True}
                    for pending in base.glob(f"character-motion_{sample_index:02d}*"):
                        if any((pending / marker).exists() for marker in ("bridge-dispatch.json", "execution.lock")) and not (
                            pending / "blender-report.json"
                        ).exists():
                            raise ValueError("A character export may still be running. Inspect the pending handoff before retrying.")
                    for candidate in range(100):
                        suffix = f"-attempt{candidate:02d}" if candidate else ""
                        if not (base / f"character-motion_{sample_index:02d}{suffix}").exists():
                            attempt = candidate
                            break
                    else:
                        raise ValueError("All preview attempts are occupied")
                suffix = f"-attempt{attempt:02d}" if attempt else ""
                report_path = base / f"character-motion_{sample_index:02d}{suffix}" / "blender-report.json"
                if report_path.exists():
                    return {**self.verified_preview(report_path, source, job), "reused": True}
                arguments = {"job_id": job_id, "sample_index": sample_index, "attempt": attempt}
                prepare(**arguments)
                execute(**arguments, source_kind="character")
                return {**self.verified_preview(report_path, source, self.store.get(job_id)), "reused": False}
        except Timeout as exc:
            raise RuntimeError("Another client is building this character preview; inspect its handoff before retrying") from exc
