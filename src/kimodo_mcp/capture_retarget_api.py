"""Shared MCP/Studio controls for verified capture-to-character handoffs."""
from __future__ import annotations

import hashlib
import json
import os
import re
from pathlib import Path
from typing import Annotated, Any

from filelock import FileLock
from mcp.types import ToolAnnotations
from pydantic import Field
from starlette.responses import FileResponse, JSONResponse
from starlette.routing import Route

from .capture_retarget import (
    CaptureRetargetRequest,
    get_capture_character_report,
    inspect_capture_retarget_source,
    prepare_capture_character_handoff,
    verify_capture_preparation,
)
from .handoff import UnrealTarget, _digest, _prepare
from .jobs import contained_file

SHA = Annotated[str, Field(pattern=r"^[0-9a-f]{64}$", strict=True)]
JOB = Annotated[str, Field(pattern=r"^[0-9a-f]{32}$", strict=True)]
READ = ToolAnnotations(readOnlyHint=True, destructiveHint=False, idempotentHint=True, openWorldHint=False)
PREPARE = ToolAnnotations(readOnlyHint=False, destructiveHint=False, idempotentHint=True, openWorldHint=False)
EXECUTE = ToolAnnotations(readOnlyHint=False, destructiveHint=True, idempotentHint=False, openWorldHint=False)
TOOL_NAMES = {
    "list_freemocap_character_handoffs",
    "inspect_freemocap_retarget_source", "prepare_freemocap_character", "execute_freemocap_character",
    "get_freemocap_character", "get_freemocap_character_artifact", "prepare_freemocap_unreal",
    "execute_freemocap_unreal", "get_freemocap_unreal",
}


def list_handoffs(settings, job_id=None, limit=30):
    if (job_id is not None and not re.fullmatch(r"[0-9a-f]{32}", job_id)) or type(limit) is not int or not 1 <= limit <= 100:
        raise ValueError("Use a valid source job ID and a history limit from 1 to 100")
    from .blender_bridge import _read_json
    root = settings.data_dir.resolve() / "freemocap" / "retargets"
    if not root.is_dir():
        return {"handoffs": [], "metadata_only": True, "partial": False}
    if root.resolve() != root:
        raise ValueError("Capture handoff history redirects outside its managed directory")
    entries, partial = [], False
    for index, directory in enumerate(root.iterdir()):
        if index >= 10000:
            partial = True
            break
        if not re.fullmatch(r"[0-9a-f]{64}", directory.name) or directory.resolve() != directory:
            continue
        try:
            request = _read_json(directory / "handoff.json")
            source_id = request.get("job_id", request.get("capture_job_id"))
            if job_id is not None and source_id != job_id:
                continue
            stamp = (directory / "handoff.json").stat().st_mtime
            status = "recorded_completed" if (directory / "blender-report.json").is_file() else "prepared"
            if status == "prepared" and any((directory / p).exists() for p in ("execution.lock", "bridge-dispatch.json")):
                status = "dispatch_recorded"
            entries.append({"handoff_id": directory.name, "job_id": source_id,
                            "recorded_status": status, "created_at": stamp, "verification": "not_checked"})
        except (OSError, ValueError, KeyError):
            partial = True
    entries.sort(key=lambda value: (value["created_at"], value["handoff_id"]), reverse=True)
    return {"handoffs": entries[:limit], "metadata_only": True, "partial": partial or len(entries) > limit}


def _character_files(settings, handoff_id):
    directory, request, _ = verify_capture_preparation(settings, handoff_id)
    report = get_capture_character_report(settings, handoff_id)
    if report.get("status") != "completed":
        raise ValueError("A verified completed character handoff is required")
    return directory, request, report


def character_artifact(settings, handoff_id, path):
    directory, _, report = _character_files(settings, handoff_id)
    allowed = {
        "character.blend": "blend", "animation.fbx": "fbx",
        "character.fbx": "character_fbx", "character-preview.glb": "preview_glb",
        "cleanup-report.json": "cleanup", "cleaned-motion.npz": "cleaned_motion",
    }
    if path not in allowed:
        raise ValueError("Choose a listed completed character artifact")
    key = allowed[path]
    output = contained_file(directory, path)
    if Path(report[key + "_path"]) != output or _digest(output) != report[key + "_sha256"]:
        raise ValueError("Character artifact differs from its verified report")
    return {"handoff_id": handoff_id, "path": path, "absolute_path": str(output),
            "bytes": output.stat().st_size, "sha256": report[key + "_sha256"]}


def prepare_unreal(settings, handoff_id, target):
    directory, _, report = _character_files(settings, handoff_id)
    fps, frames = report["fps"], report["frames"]
    if (type(fps) not in (int, float) or not 1 <= fps <= 120 or int(fps) != fps
            or type(frames) is not int or not 2 <= frames <= 2400):
        raise ValueError("Capture character timing is outside the verified Unreal handoff limits")
    artifact = character_artifact(settings, handoff_id, "animation.fbx")
    report_path = contained_file(directory, "blender-report.json")
    request = {
        "schema_version": 1, "kind": "unreal", "capture_handoff_id": handoff_id,
        "source_fbx": artifact["absolute_path"], "source_sha256": artifact["sha256"],
        "blender_report": str(report_path), "blender_report_sha256": _digest(report_path),
        "fps": float(fps), "frames": frames, "expected_bones": report["fbx_bones"],
        "target": target.model_dump(mode="json"),
    }
    key = hashlib.sha256(target.model_dump_json().encode()).hexdigest()[:16]
    destination = directory / f"unreal-{key}"
    result = _prepare("unreal", destination, request).model_dump()
    _character_files(settings, handoff_id)
    return result | {"handoff_id": handoff_id, "target": request["target"], "fps": fps, "frames": frames}


def _unreal_preparation(settings, handoff_id, target):
    directory, _, report = _character_files(settings, handoff_id)
    key = hashlib.sha256(target.model_dump_json().encode()).hexdigest()[:16]
    destination = directory / f"unreal-{key}"
    request_path = contained_file(destination, "handoff.json")
    script = contained_file(destination, "unreal_handoff.py")
    from .blender_bridge import _read_json
    request = _read_json(request_path)
    template = Path(__file__).with_name("handoff_scripts") / "unreal.py"
    if (_digest(script) != hashlib.sha256(template.read_text(encoding="utf-8").encode()).hexdigest()
            or request.get("schema_version") != 1 or request.get("kind") != "unreal"
            or request.get("capture_handoff_id") != handoff_id
            or request.get("target") != target.model_dump(mode="json")
            or request.get("source_fbx") != report["fbx_path"]
            or request.get("source_sha256") != report["fbx_sha256"]
            or request.get("blender_report") != str(directory / "blender-report.json")
            or request.get("blender_report_sha256") != _digest(directory / "blender-report.json")
            or request.get("fps") != report["fps"] or request.get("frames") != report["frames"]
            or request.get("expected_bones") != report["fbx_bones"]):
        raise ValueError("Unreal preparation differs from the selected capture character and target")
    return destination, request, script


def unreal_report(settings, handoff_id, target):
    directory, request, _ = _unreal_preparation(settings, handoff_id, target)
    path = directory / "unreal-report.json"
    if not path.exists():
        dispatched = any((directory / name).exists() for name in ("bridge-dispatch.json", "execution.lock"))
        return {"status": "not_completed" if dispatched else "not_executed", "handoff_id": handoff_id,
                "report_path": str(path), "target": target.model_dump(mode="json"),
                "recovery": "Inspect the editor and report after an uncertain dispatch; do not automatically retry."}
    from .unreal_bridge import _validate_report
    result = _validate_report(contained_file(directory, "unreal-report.json"), request, Path(target.project_file))
    _character_files(settings, handoff_id)
    return result | {"handoff_id": handoff_id}


def execute_unreal(settings, handoff_id, target):
    directory, request, script = _unreal_preparation(settings, handoff_id, target)
    from .blender_bridge import _read_json
    from .unreal_bridge import execute_handoff
    engine = os.environ.get("KIMODO_UNREAL_ENGINE")
    if not engine:
        registration = settings.data_dir / "Unreal" / "engine.json"
        engine = _read_json(registration).get("engine_directory")
    if not isinstance(engine, str) or not Path(engine).is_absolute():
        raise ValueError("Register the installed Unreal engine before importing")
    with FileLock(str(directory / ".capture-import.lock"), timeout=0):
        current = unreal_report(settings, handoff_id, target)
        if current["status"] == "completed":
            return current
        marker = directory / "bridge-dispatch.json"
        if marker.exists() or (directory / "execution.lock").exists():
            raise RuntimeError("Unreal import was already dispatched; inspect the editor and report before a new attempt")
        with marker.open("x", encoding="utf-8") as stream:
            json.dump({"schema_version": 1, "handoff_id": handoff_id,
                       "request_sha256": _digest(directory / "handoff.json")}, stream)
        execute_handoff(script, Path(engine), Path(request["target"]["project_file"]))
        return unreal_report(settings, handoff_id, target)


def register_capture_retarget_tools(mcp, settings):
    @mcp.tool(annotations=READ)
    def list_freemocap_character_handoffs(job_id: JOB | None = None,
                                         limit: Annotated[int, Field(ge=1, le=100)] = 30) -> dict[str, Any]:
        """Find saved capture retarget attempts shared by Studio and agents; open an ID to verify its actual result."""
        return list_handoffs(settings, job_id, limit)

    @mcp.tool(annotations=READ)
    def inspect_freemocap_retarget_source(job_id: JOB, artifact_path: str) -> dict[str, Any]:
        """Inspect a verified exported BVH's timing, hierarchy and required retarget settings; run no editor."""
        return inspect_capture_retarget_source(settings, job_id, artifact_path)

    @mcp.tool(annotations=PREPARE)
    def prepare_freemocap_character(job_id: JOB, artifact_path: str, options: CaptureRetargetRequest,
                                   attempt: Annotated[int, Field(ge=0, le=99)] = 0) -> dict[str, Any]:
        """Prepare immutable capture cleanup and retargeting onto the registered fitted character.

        Select an exact verified FreeMoCap export BVH; choose source scale/rest pose explicitly.
        Trimming, bounded smoothing and local corrections preserve the original. No editor runs.
        """
        return prepare_capture_character_handoff(settings, job_id, artifact_path, options, attempt)

    @mcp.tool(annotations=EXECUTE)
    def execute_freemocap_character(handoff_id: SHA) -> dict[str, Any]:
        """Run the prepared fixed capture-retarget script in connected Blender and verify its character outputs."""
        from .blender_bridge import execute_handoff
        directory, _, _ = verify_capture_preparation(settings, handoff_id)
        execute_handoff(contained_file(directory, "capture_character_handoff.py"), "capture_character")
        return get_capture_character_report(settings, handoff_id)

    @mcp.tool(annotations=READ)
    def get_freemocap_character(handoff_id: SHA) -> dict[str, Any]:
        """Recover capture-retarget status and verify completed rig, timing, source evidence and output hashes."""
        return get_capture_character_report(settings, handoff_id)

    @mcp.tool(annotations=READ)
    def get_freemocap_character_artifact(handoff_id: SHA, path: str) -> dict[str, Any]:
        """Locate a verified character export, cleaned motion or cleanup report from this exact handoff."""
        return character_artifact(settings, handoff_id, path)

    @mcp.tool(annotations=PREPARE)
    def prepare_freemocap_unreal(handoff_id: SHA, target: UnrealTarget) -> dict[str, Any]:
        """Prepare a separate animation-only UE5 import from the verified retargeted character FBX."""
        return prepare_unreal(settings, handoff_id, target)

    @mcp.tool(annotations=EXECUTE)
    def execute_freemocap_unreal(handoff_id: SHA, target: UnrealTarget) -> dict[str, Any]:
        """Import the prepared capture into the exact open Unreal project and matching registered character rig."""
        return execute_unreal(settings, handoff_id, target)

    @mcp.tool(annotations=READ)
    def get_freemocap_unreal(handoff_id: SHA, target: UnrealTarget) -> dict[str, Any]:
        """Read verified capture import evidence or explicit uncertain-dispatch recovery status."""
        return unreal_report(settings, handoff_id, target)


def capture_retarget_routes(settings):
    async def artifact(request):
        try:
            value = character_artifact(settings, request.path_params["handoff_id"], request.path_params["path"])
            # GLB is a self-contained preview with no external URL references.
            preview = value["path"] == "character-preview.glb"
            return FileResponse(value["absolute_path"], media_type="model/gltf-binary" if preview else None,
                                filename=None if preview else value["path"],
                                headers={"X-Content-Type-Options": "nosniff", "Cache-Control": "no-store"})
        except (ValueError, OSError, KeyError, RuntimeError) as exc:
            return JSONResponse({"error": str(exc)}, status_code=400)
    return [Route("/api/capture-character/{handoff_id}/files/{path:path}", artifact)]
