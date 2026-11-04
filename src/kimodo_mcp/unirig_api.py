"""Shared typed rigging operations and verified Studio artifact delivery."""

from __future__ import annotations

from typing import Annotated, Any, Literal

import anyio
from mcp.types import ToolAnnotations
from pydantic import Field
from starlette.responses import FileResponse, JSONResponse
from starlette.routing import Route

from . import unirig
from .studio_assets import digest, import_studio_asset, inspect_glb

JOB = Annotated[str, Field(pattern=r"^[0-9a-f]{32}$", strict=True)]
READ = ToolAnnotations(readOnlyHint=True, destructiveHint=False, idempotentHint=True, openWorldHint=False)
START = ToolAnnotations(readOnlyHint=False, destructiveHint=False, idempotentHint=False, openWorldHint=False)
CANCEL = ToolAnnotations(readOnlyHint=False, destructiveHint=True, idempotentHint=True, openWorldHint=False)
SAVE = ToolAnnotations(readOnlyHint=False, destructiveHint=False, idempotentHint=True, openWorldHint=False)
TOOL_NAMES = {
    "get_unirig_capabilities", "plan_unirig", "start_unirig", "get_unirig_job", "list_unirig_jobs",
    "cancel_unirig_job", "get_unirig_log", "get_unirig_artifact", "register_unirig_output",
}


def artifact(settings, job_id, name):
    path = unirig.get_unirig_artifact(settings, job_id, name)
    return {"job_id": job_id, "name": name, "absolute_path": str(path),
            "bytes": path.stat().st_size, "sha256": digest(path), "source_kind": "unirig"}


def register_output(settings, job_id, name, label, kind="character"):
    """Keep a verified rigged result available in the shared library without selecting it as the character."""
    path = unirig.get_unirig_artifact(settings, job_id, name)
    result = import_studio_asset(settings, str(path), label, kind)
    # Recheck the producer after the immutable library copy has been made.
    unirig.get_unirig_artifact(settings, job_id, name)
    return result | {"unirig_source": {"job_id": job_id, "name": name}, "registered_character_changed": False}


def register_unirig_tools(mcp, settings):
    @mcp.tool(annotations=READ)
    async def get_unirig_capabilities() -> dict[str, Any]:
        """Read released rigging stages, setup requirements and limits without starting a model."""
        return await anyio.to_thread.run_sync(unirig.get_unirig_capabilities, settings)

    @mcp.tool(annotations=READ)
    async def plan_unirig(request: unirig.UniRigRequest) -> dict[str, Any]:
        """Verify registered mesh/rig inputs and runtime configuration before an explicit UniRig start."""
        return await anyio.to_thread.run_sync(unirig.plan_unirig, settings, request)

    @mcp.tool(annotations=START)
    async def start_unirig(request: unirig.UniRigRequest) -> dict[str, Any]:
        """Start a new immutable preprocessing, skeleton, skinning, merge or complete rigging job.

        Uses separately installed UniRig and its released models. Submission is not completion;
        poll the returned job ID and review the rig before retargeting or importing it into an engine.
        """
        return await anyio.to_thread.run_sync(unirig.start_unirig, settings, request)

    @mcp.tool(annotations=READ)
    async def get_unirig_job(job_id: JOB) -> dict[str, Any]:
        """Recover rigging progress or verify completed source/output receipts."""
        return await anyio.to_thread.run_sync(unirig.get_unirig_job, settings, job_id)

    @mcp.tool(annotations=READ)
    async def list_unirig_jobs(limit: Annotated[int, Field(ge=1, le=100)] = 20) -> dict[str, Any]:
        """Find saved rigging jobs shared with Studio and other agents."""
        return await anyio.to_thread.run_sync(unirig.list_unirig_jobs, settings, limit)

    @mcp.tool(annotations=CANCEL)
    async def cancel_unirig_job(job_id: JOB) -> dict[str, Any]:
        """Request cancellation of this managed rigging job while retaining its evidence."""
        return await anyio.to_thread.run_sync(unirig.cancel_unirig_job, settings, job_id)

    @mcp.tool(annotations=READ)
    async def get_unirig_log(job_id: JOB, lines: Annotated[int, Field(ge=1, le=200)] = 80) -> dict[str, Any]:
        """Read a bounded tail of the selected rigging job's diagnostic log."""
        return await anyio.to_thread.run_sync(unirig.get_unirig_log, settings, job_id, lines)

    @mcp.tool(annotations=READ)
    async def get_unirig_artifact(job_id: JOB, name: str) -> dict[str, Any]:
        """Locate an exact manifest-listed completed rigging output with its checksum."""
        return await anyio.to_thread.run_sync(artifact, settings, job_id, name)

    @mcp.tool(annotations=SAVE)
    async def register_unirig_output(job_id: JOB, name: str,
                                     label: Annotated[str, Field(min_length=1, max_length=160)],
                                     kind: Literal["character", "prop"] = "character") -> dict[str, Any]:
        """Copy a verified rigged GLB/FBX into the shared asset library; preserve the current character registration."""
        return await anyio.to_thread.run_sync(register_output, settings, job_id, name, label, kind)


def unirig_routes(settings):
    async def download(request):
        try:
            path = await anyio.to_thread.run_sync(unirig.get_unirig_artifact, settings,
                                                  request.path_params["job_id"], request.path_params["name"])
            preview = path.suffix.lower() == ".glb"
            if preview:
                await anyio.to_thread.run_sync(inspect_glb, path)
            return FileResponse(path, media_type="model/gltf-binary" if preview else None,
                                filename=None if preview else path.name,
                                headers={"Cache-Control": "no-store", "X-Content-Type-Options": "nosniff"})
        except (ValueError, OSError, KeyError, TypeError):
            return JSONResponse({"error": "Verified rigging artifact not found"}, status_code=404)
    return [Route("/api/unirig/{job_id}/files/{name:path}", download)]
