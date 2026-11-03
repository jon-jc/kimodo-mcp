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
    r"exports/[0-9a-f]{64}/(?:motion_\d{2}\.(?:bvh|csv)|motion_\d{2}_amass\.npz|export-report\.json)|"
    r"variants/[0-9a-f]{64}/(?:motion_\d{2}\.npz|variant-report\.json)|"
    r"reviews/(?:motion|comparison)-[0-9a-f]{20}\.(?:html|json)|reviews/preview-[0-9a-f]{20}\.html|"
    r"handoffs/(?:character|skeleton)-motion_\d{2}(?:-variant[0-9a-f]{64})?(?:-attempt\d{2})?/"
    r"(?:character-preview\.glb|(?:character|animation)\.(?:fbx|blend)|blender-report\.json|"
    r"unreal-[0-9a-f]{16}/unreal-report\.json))"
)
_CHARACTER_DIRECTORY = re.compile(r"character-motion_(\d{2})(?:-variant([0-9a-f]{64}))?(?:-attempt(\d{2}))?")
_SKELETON_DIRECTORY = re.compile(r"skeleton-motion_(\d{2})(?:-variant([0-9a-f]{64}))?(?:-attempt(\d{2}))?")
_NATIVE_FILE = re.compile(r"motion_(\d{2})(_amass)?\.(npz|bvh|csv|html)")
_EXPORTS = (
    ("preview_glb", "character-preview.glb", "Animated GLB", "character_preview"),
    ("character_fbx", "character.fbx", "Character FBX", "character_mesh"),
    ("fbx", "animation.fbx", "Animation FBX", "animation"),
    ("blend", "character.blend", "Blender project", "blender_project"),
)


def handoff_directory_name(source_kind: str, sample_index: int, attempt: int = 0,
                           variant_id: str | None = None) -> str:
    """Keep native directories stable and isolate each immutable variant's attempts."""
    if source_kind not in {"character", "skeleton"}:
        raise ValueError("Unknown handoff source kind")
    if type(sample_index) is not int or not 0 <= sample_index <= 3:
        raise ValueError("Sample index must be 0..3")
    if type(attempt) is not int or not 0 <= attempt <= 99:
        raise ValueError("Attempt must be 0..99")
    if variant_id is not None and (not isinstance(variant_id, str) or not re.fullmatch(r"[0-9a-f]{64}", variant_id)):
        raise ValueError("Expected a managed motion variant ID")
    return (f"{source_kind}-motion_{sample_index:02d}"
            + (f"-variant{variant_id}" if variant_id is not None else "")
            + (f"-attempt{attempt:02d}" if attempt else ""))


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
            "url": self.file_url(path), "variant_id": None,
        }

    def _motion_dependencies(self, job_id: str, sample_index: int, variant_id: str | None) -> list[Path]:
        source = self.store.directory(job_id)
        paths = [source / "manifest.json", source / f"motion_{sample_index:02d}.npz"]
        if variant_id is not None:
            directory = source / "variants" / variant_id
            paths.extend([directory / "request.json", directory / "variant-report.json",
                          directory / f"motion_{sample_index:02d}.npz"])
        return paths

    def verified_motion_variant(self, job_id: str, variant_id: str,
                                sample_index: int | None = None) -> dict[str, Any]:
        """Bind an immutable edit receipt to its original completed-job manifest."""
        from .motion_variants import VariantRequest, verify_motion_variant
        from .motion_variants import variant_id as request_id

        if not isinstance(variant_id, str) or not re.fullmatch(r"[0-9a-f]{64}", variant_id):
            raise ValueError("Expected a managed motion variant ID")
        if sample_index is not None and (type(sample_index) is not int or not 0 <= sample_index <= 3):
            raise ValueError("Sample index must be 0..3")
        job = self.store.get(job_id)
        if job["status"] != "completed":
            raise ValueError("Motion variants require a completed generation job")
        source = self.store.directory(job_id)
        directory = source / "variants" / variant_id
        if any(path.is_symlink() for path in (source, source / "variants", directory)):
            raise ValueError("Variant directories cannot be symbolic links")
        request_path = contained_file(source, f"variants/{variant_id}/request.json")
        if request_path.parent != directory.resolve() or request_path.stat().st_size > 32768:
            raise ValueError("Variant request is outside its assigned directory or oversized")
        prepared = VariantRequest.model_validate_json(request_path.read_text(encoding="utf-8"))
        index = prepared.sample_index
        if (prepared.job_id != job_id or (sample_index is not None and sample_index != index)
                or request_id(prepared) != variant_id or Path(prepared.output_directory) != directory.resolve()):
            raise ValueError("Variant does not belong to this job's selected sample")
        original = self.verified_native_output(job_id, f"motion_{index:02d}.npz")
        if Path(prepared.source_path) != Path(original["absolute_path"]) or prepared.source_sha256 != original["sha256"]:
            raise ValueError("Variant source differs from the selected job's manifest")
        report_path = contained_file(source, f"variants/{variant_id}/variant-report.json")
        artifact = contained_file(source, f"variants/{variant_id}/motion_{index:02d}.npz")
        watched = self._motion_dependencies(job_id, index, variant_id)

        def validate():
            from .artifacts import _load_motion

            report = verify_motion_variant(directory, prepared)
            manifest = job.get("manifest", {})
            original_motion = _load_motion(prepared.source_path)
            if (original_motion["positions"].shape[:2] != (1, manifest.get("frames"))
                    or original_motion["fps"] != manifest.get("fps")):
                raise ValueError("Original variant source timing differs from its completed-job manifest")
            for key in ("fps", "skeleton", "model"):
                if manifest.get(key) is not None and (
                    report[key] != manifest[key] or original_motion[key] != manifest[key]
                ):
                    raise ValueError("Variant metadata differs from the selected job's manifest")
            edit = report["edit"]
            if (type(manifest.get("frames")) is not int or edit["end_frame_exclusive"] > manifest["frames"]
                    or report["frames"] != edit["end_frame_exclusive"] - edit["start_frame"]):
                raise ValueError("Variant timing differs from the original motion range")
            output = self._output_record(artifact, source, report["artifact"]["sha256"], "motion_variant", index)
            return {**report, **output, "variant_id": variant_id, "source_kind": "motion-variant",
                    "report_path": str(report_path), "report_url": self.file_url(report_path),
                    "source_range": {"start_frame": edit["start_frame"],
                                     "end_frame_exclusive": edit["end_frame_exclusive"]}}

        return self._cached_validation(report_path, watched, validate)

    def resolve_motion(self, job_id: str, sample_index: int = 0,
                       variant_id: str | None = None) -> dict[str, Any]:
        """Resolve a selected NPZ; source_sha256 always means the selected archive.

        original_source_sha256 identifies the native sample for either source kind.
        Sample indices remain job sample indices; each selected NPZ has one sample.
        """
        if type(sample_index) is not int or not 0 <= sample_index <= 3:
            raise ValueError("Sample index must be 0..3")
        if variant_id is not None:
            result = self.verified_motion_variant(job_id, variant_id, sample_index)
            return {**result, "job_id": job_id, "original_source_sha256": result["source_sha256"],
                    "source_sha256": result["sha256"]}
        job = self.store.get(job_id)
        if job["status"] != "completed":
            raise ValueError("Motion sources require a completed generation job")
        output = self.verified_native_output(job_id, f"motion_{sample_index:02d}.npz")
        manifest = job.get("manifest", {})
        return {**output, "job_id": job_id, "source_kind": "motion", "source_sha256": output["sha256"],
                "original_source_sha256": output["sha256"],
                **{key: manifest.get(key) for key in ("fps", "frames", "skeleton", "model")},
                "source_range": {"start_frame": 0, "end_frame_exclusive": manifest.get("frames")}, "edit": None}

    def list_motion_variants(self, job_id: str) -> list[dict[str, Any]]:
        job = self.store.get(job_id)
        if job["status"] != "completed":
            return []
        results = []
        for directory in sorted((self.store.directory(job_id) / "variants").glob("*")):
            if not re.fullmatch(r"[0-9a-f]{64}", directory.name) or not directory.is_dir():
                continue
            try:
                results.append(self.verified_motion_variant(job_id, directory.name))
            except (OSError, ValueError, KeyError, TypeError, RuntimeError):
                continue
        return results

    def motion_preview_data(self, job_id: str, sample_index: int = 0,
                            variant_id: str | None = None) -> dict[str, Any]:
        """Bounded source-skeleton data shared by the studio and MCP inspectors."""
        from .artifacts import _load_motion
        from .motion_variants import VariantEdit, _read

        selected = self.resolve_motion(job_id, sample_index, variant_id)
        path = Path(selected["absolute_path"])
        before = path.stat()
        motion = _load_motion(path)
        samples, frames, joints, _ = motion["positions"].shape
        if (samples != 1 or not 1 <= frames <= 2400 or not 1 <= joints <= 77
                or motion["parents"] is None or "joint_names" not in motion["channels"]
                or motion["fps"] is None or motion["fps"] != selected["fps"] or frames != selected["frames"]):
            raise ValueError("Rig preview requires one bounded sample with explicit matching hierarchy and timing")
        for key in ("skeleton", "model"):
            if selected.get(key) is not None and motion[key] != selected[key]:
                raise ValueError("Rig preview metadata differs from its selected source")
        editable = {
            "supported": True, "operations": ["trim", "world_placement", "local_joint_adjustment"],
            "source_depth_limit": 1, "edits_use_original_frames": True,
            "max_joint_adjustments": 16, "max_blend_frames": 30,
            "local_rotation_limit_radians": 1.5707963267948966,
        }
        try:
            original = self.store.directory(job_id) / f"motion_{sample_index:02d}.npz"
            _read(original)
            if frames < 2:
                raise ValueError("Editing requires at least two source frames")
        except (ValueError, OSError, KeyError, TypeError) as error:
            editable.update(supported=False, operations=[], reason=str(error)[:500])
        edit = selected.get("edit") or {**VariantEdit().model_dump(mode="json"), "end_frame_exclusive": frames}
        with path.open("rb") as stream:
            checksum = hashlib.file_digest(stream, "sha256").hexdigest()
        after = path.stat()
        if checksum != selected["sha256"] or (
            before.st_size, before.st_mtime_ns, before.st_ctime_ns, before.st_ino
        ) != (after.st_size, after.st_mtime_ns, after.st_ctime_ns, after.st_ino):
            raise ValueError("Motion changed while loading rig preview data")
        if variant_id is not None:
            refreshed = self.resolve_motion(job_id, sample_index, variant_id)
            if (refreshed["sha256"] != selected["sha256"]
                    or refreshed["original_source_sha256"] != selected["original_source_sha256"]):
                raise ValueError("Variant provenance changed while loading rig preview data")
        return {
            "job_id": job_id, "sample_index": sample_index, "variant_id": variant_id,
            "source_sha256": checksum, "artifact_sha256": checksum,
            "original_source_sha256": selected["original_source_sha256"],
            "fps": motion["fps"], "frames": frames, "model": motion["model"], "skeleton": motion["skeleton"],
            "joint_names": motion["names"], "joint_parents": motion["parents"],
            "positions": motion["positions"][0].tolist(),
            "source_range": selected["source_range"], "edit": edit,
            "contact_status": selected.get("contact_status", "preserved_source_predictions"
                                           if motion["contacts"] is not None else "unavailable"),
            "editable": editable, "frame_range_convention": "zero_based_half_open",
            "coordinates": {"units": "meters", "up_axis": "Y", "forward_axis": "Z"},
        }

    def _verified_character(self, report_path: Path, source: Path, job: dict) -> dict[str, Any]:
        from .blender_bridge import _read_json, _validate_report, _validate_request

        match = _CHARACTER_DIRECTORY.fullmatch(report_path.parent.name)
        if match is None:
            raise ValueError("Unexpected character handoff directory")
        index, variant_id, attempt = int(match[1]), match[2], int(match[3] or 0)
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
        selected = self.resolve_motion(job["id"], index, variant_id)
        motion = Path(selected["absolute_path"])
        if (
            prepared.get("kind") != "character"
            or prepared.get("schema_version") != 1 or type(prepared.get("sample_index")) is not int
            or prepared["sample_index"] != index or prepared.get("source_sha256") != selected["sha256"]
            or prepared.get("variant_id") != variant_id
            or not isinstance(prepared.get("source_motion"), str) or Path(prepared["source_motion"]) != motion
            or prepared.get("fps") != selected["fps"] or prepared.get("frames") != selected["frames"]
        ):
            raise ValueError("Preview does not belong to this job's selected motion sample")
        character = Path(prepared["character_fbx"])
        watched = [*self._motion_dependencies(job["id"], index, variant_id), report_path, request_path, character,
                   *(directory / filename for _, filename, _, _ in _EXPORTS)]

        def validate():
            bones = _validate_request(prepared, "character")
            report = _validate_report(directory.resolve(), prepared, bones)
            exports, outputs = [], []
            for field, _, label, role in _EXPORTS:
                path = Path(report[field + "_path"])
                output = self._output_record(path, source, report[field + "_sha256"], role, index, attempt)
                output["source_kind"] = "character"
                output["variant_id"] = variant_id
                if output["url"] is None:
                    raise ValueError("Preview export is not a managed studio artifact")
                outputs.append(output)
                exports.append({"label": label, "url": output["url"]})
            return {
                "preview": {"sample_index": index, "url": exports[0]["url"], "attempt": attempt, "exports": exports,
                            **({"variant_id": variant_id} if variant_id is not None else {})},
                "outputs": outputs,
            }
        return self._cached_validation(report_path, watched, validate)

    def _verified_skeleton(self, report_path: Path, source: Path, job: dict) -> dict[str, Any]:
        from .blender_bridge import _read_json, _validate_report, _validate_request

        match = _SKELETON_DIRECTORY.fullmatch(report_path.parent.name)
        if match is None:
            raise ValueError("Unexpected skeleton handoff directory")
        index, variant_id, attempt = int(match[1]), match[2], int(match[3] or 0)
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
        selected = self.resolve_motion(job["id"], index, variant_id) if variant_id is not None else manifest
        if (prepared.get("kind") != "blender" or prepared.get("schema_version") != 1
                or not isinstance(prepared.get("source_bvh"), str)
                or prepared.get("variant_id") != variant_id
                or prepared.get("fps") != selected.get("fps") or prepared.get("frames") != selected.get("frames")
                or prepared.get("skeleton") != selected.get("skeleton")):
            raise ValueError("Skeleton export does not belong to this job's selected motion sample")
        motion = Path(prepared["source_bvh"])
        name = f"motion_{index:02d}.bvh"
        dependencies = []
        if motion == source / name:
            if variant_id is not None:
                raise ValueError("A variant skeleton handoff requires a converted variant BVH")
            entries = [item for item in manifest.get("artifacts", []) if item.get("path") == name]
            motion = contained_file(source, name)
            if len(entries) != 1 or prepared.get("source_sha256") != entries[0].get("sha256"):
                raise ValueError("Skeleton export differs from its native BVH artifact")
        else:
            relative = motion.relative_to(source).as_posix()
            match = re.fullmatch(r"exports/([0-9a-f]{64})/motion_\d{2}\.bvh", relative)
            if match is None:
                raise ValueError("Skeleton export requires a native or managed converted BVH")
            # Validate the conversion before entering this handoff's cache lock.
            # Its immutable input/receipt files also invalidate the handoff cache.
            converted = self.verified_motion_export(job["id"], match[1], sample_index=index)
            if (converted["format"] != "bvh" or Path(converted["absolute_path"]) != motion
                    or prepared.get("source_sha256") != converted["sha256"]
                    or converted.get("variant_id") != variant_id):
                raise ValueError("Skeleton export differs from its verified converted BVH")
            export_report = Path(converted["report_path"])
            dependencies = [*self._motion_dependencies(job["id"], index, variant_id), export_report,
                            export_report.with_name("request.json")]
        watched = [source / "manifest.json", report_path, request_path, motion,
                   directory / "animation.fbx", directory / "animation.blend", *dependencies]

        def validate():
            bones = _validate_request(prepared, "blender")
            report = _validate_report(directory.resolve(), prepared, bones)
            outputs = []
            for key, role in (("fbx", "animation"), ("blend", "blender_project")):
                output = self._output_record(Path(report[key + "_path"]), source, report[key + "_sha256"],
                                             role, index, attempt)
                output["source_kind"] = "skeleton"
                output["variant_id"] = variant_id
                outputs.append(output)
            return {"sample_index": index, "attempt": attempt, "variant_id": variant_id, "outputs": outputs}

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
                key = (preview["sample_index"], preview.get("variant_id"))
                previous = latest.get(key)
                if previous is None or preview["attempt"] > previous["preview"]["attempt"]:
                    latest[key] = result
        return list(latest.values())

    def job_details(self, job_id: str) -> dict[str, Any]:
        job = self.store.get(job_id)
        previews = [result["preview"] for result in self._latest_character(job)]
        job["previews"] = [preview for preview in previews if preview.get("variant_id") is None]
        job["variant_previews"] = [preview for preview in previews if preview.get("variant_id") is not None]
        job["variants"] = self.list_motion_variants(job_id)
        if job["status"] == "completed":
            source = self.store.directory(job_id)
            job["downloads"] = [
                {**item, "url": self.file_url(source / item["path"]), "variant_id": None}
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

    def verified_motion_export(
        self, job_id: str, export_id: str, sample_index: int | None = None, variant_id: str | None = None,
    ) -> dict[str, Any]:
        """Bind a conversion receipt to this completed job's current source manifest."""
        from .export import ExportRequest, verify_export
        from .export import export_id as request_id

        if not isinstance(export_id, str) or not re.fullmatch(r"[0-9a-f]{64}", export_id):
            raise ValueError("Expected a managed motion export ID")
        if sample_index is not None and (type(sample_index) is not int or not 0 <= sample_index <= 3):
            raise ValueError("Sample index must be 0..3")
        if variant_id is not None and (not isinstance(variant_id, str) or not re.fullmatch(r"[0-9a-f]{64}", variant_id)):
            raise ValueError("Expected a managed motion variant ID")
        job = self.store.get(job_id)
        if job["status"] != "completed":
            raise ValueError("Motion exports require a completed generation job")
        source = self.store.directory(job_id)
        root, directory = source / "exports", source / "exports" / export_id
        if source.is_symlink() or root.is_symlink() or directory.is_symlink():
            raise ValueError("Motion export directories cannot be symbolic links")
        request_path = contained_file(source, f"exports/{export_id}/request.json")
        if request_path.parent != directory.resolve() or request_path.stat().st_size > 16384:
            raise ValueError("Motion export request is outside its assigned directory or oversized")
        prepared = ExportRequest.model_validate_json(request_path.read_text(encoding="utf-8"))
        manifest = job.get("manifest", {})
        index = prepared.sample_index
        if (prepared.job_id != job_id or type(manifest.get("samples")) is not int
                or not 0 <= index < manifest["samples"] or (sample_index is not None and sample_index != index)
                or (variant_id is not None and prepared.variant_id != variant_id)
                or request_id(prepared) != export_id or Path(prepared.output_directory) != directory.resolve()):
            raise ValueError("Motion export does not belong to this job's selected sample")
        selected = self.resolve_motion(job_id, index, prepared.variant_id)
        motion = Path(selected["absolute_path"])
        if Path(prepared.source_path) != motion or prepared.source_sha256 != selected["sha256"]:
            raise ValueError("Motion export source differs from the selected job's manifest")
        artifact = directory / prepared.basename
        report_path = directory / "export-report.json"
        watched = [*self._motion_dependencies(job_id, index, prepared.variant_id),
                   directory / "request.json", report_path, artifact]

        def validate():
            report = verify_export(directory, prepared)
            if (report["frames"] != selected.get("frames") or report["fps"] != selected.get("fps")
                    or report["skeleton"] != selected.get("skeleton")
                    or (selected.get("model") is not None and report["model"] != selected["model"])):
                raise ValueError("Motion export timing or skeleton differs from the selected job's manifest")
            output = self._output_record(artifact, source, report["artifact"]["sha256"],
                                         "amass" if prepared.format == "amass" else "motion", index)
            output.update(
                export_id=export_id, source_kind="motion-export", format=prepared.format,
                source_sha256=prepared.source_sha256, fps=report["fps"], frames=report["frames"],
                skeleton=report["skeleton"], model=report["model"], options=report["options"],
                coordinates=report["coordinates"], report_path=str(report_path), report_url=self.file_url(report_path),
                variant_id=prepared.variant_id,
            )
            return output

        return self._cached_validation(report_path, watched, validate)

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
            outputs.extend(self.list_motion_variants(job_id))
            source = self.store.directory(job_id)
            latest_skeleton = {}
            for report_path in sorted((source / "handoffs").glob("skeleton-motion_*/blender-report.json")):
                try:
                    result = self._verified_skeleton(report_path, source, job)
                except (OSError, ValueError, KeyError, TypeError, RuntimeError):
                    continue
                key = (result["sample_index"], result.get("variant_id"))
                previous = latest_skeleton.get(key)
                if previous is None or result["attempt"] > previous["attempt"]:
                    latest_skeleton[key] = result
            for result in latest_skeleton.values():
                outputs.extend(result["outputs"])
            for directory in sorted((source / "exports").glob("*")):
                if not re.fullmatch(r"[0-9a-f]{64}", directory.name) or not directory.is_dir():
                    continue
                try:
                    outputs.append(self.verified_motion_export(job_id, directory.name))
                except (OSError, ValueError, KeyError, TypeError, RuntimeError):
                    continue
        return {"job_id": job_id, "outputs": outputs}

    def build_character_preview(
        self, job_id: str, sample_index: int = 0, attempt: int | None = None, *,
        prepare: Callable[..., Any], execute: Callable[..., Any], variant_id: str | None = None,
    ) -> dict[str, Any]:
        if type(sample_index) is not int or not 0 <= sample_index <= 3:
            raise ValueError("Sample index must be 0..3")
        if attempt is not None and (type(attempt) is not int or not 0 <= attempt <= 99):
            raise ValueError("Attempt must be 0..99")
        job = self.store.get(job_id)
        if job["status"] != "completed" or sample_index >= job.get("manifest", {}).get("samples", 0):
            raise ValueError("Choose a sample from a completed generation job")
        source = self.store.directory(job_id)
        stem = handoff_directory_name("character", sample_index, variant_id=variant_id)
        if variant_id is not None:
            self.resolve_motion(job_id, sample_index, variant_id)
        lock_suffix = f"-variant{variant_id}" if variant_id is not None else ""
        lock = FileLock(str(source / f".character-preview-{sample_index:02d}{lock_suffix}.lock"), timeout=0)
        try:
            with lock:
                base = source / "handoffs"
                if attempt is None:
                    matches = [item["preview"] for item in self._latest_character(job)
                               if item["preview"]["sample_index"] == sample_index
                               and item["preview"].get("variant_id") == variant_id]
                    if matches:
                        return {**matches[-1], "reused": True}
                    for pending in base.glob(stem + "*"):
                        match = _CHARACTER_DIRECTORY.fullmatch(pending.name)
                        if match is None or match[2] != variant_id:
                            continue
                        if any((pending / marker).exists() for marker in ("bridge-dispatch.json", "execution.lock")) and not (
                            pending / "blender-report.json"
                        ).exists():
                            raise ValueError("A character export may still be running. Inspect the pending handoff before retrying.")
                    for candidate in range(100):
                        name = handoff_directory_name("character", sample_index, candidate, variant_id)
                        if not (base / name).exists():
                            attempt = candidate
                            break
                    else:
                        raise ValueError("All preview attempts are occupied")
                report_path = base / handoff_directory_name("character", sample_index, attempt, variant_id) / "blender-report.json"
                if report_path.exists():
                    return {**self.verified_preview(report_path, source, job), "reused": True}
                arguments = {"job_id": job_id, "sample_index": sample_index, "attempt": attempt}
                if variant_id is not None:
                    arguments["variant_id"] = variant_id
                prepare(**arguments)
                execute(**arguments, source_kind="character")
                return {**self.verified_preview(report_path, source, self.store.get(job_id)), "reused": False}
        except Timeout as exc:
            raise RuntimeError("Another client is building this character preview; inspect its handoff before retrying") from exc
