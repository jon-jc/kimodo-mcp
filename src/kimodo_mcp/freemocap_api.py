"""Typed MCP operations for the separately installed loopback FreeMoCap service."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Annotated, Any, Literal

from mcp.types import ToolAnnotations
from pydantic import Field, StrictInt
from starlette.responses import FileResponse, JSONResponse
from starlette.routing import Route

from . import freemocap as capture
from . import freemocap_review as review
from .jobs import redact

TOOL_NAMES = (
    "get_freemocap_capabilities",
    "list_freemocap_recordings",
    "inspect_freemocap_recording",
    "plan_freemocap_capture",
    "start_freemocap_capture",
    "get_freemocap_job",
    "cancel_freemocap_job",
    "inspect_freemocap_frames",
    "analyze_freemocap_quality",
    "get_freemocap_artifact",
)
READ = ToolAnnotations(readOnlyHint=True, destructiveHint=False, idempotentHint=True, openWorldHint=False)
# Calibration updates the upstream global calibration library; explicit export
# invokes Blender. Processing may download tracker weights in the external service.
START = ToolAnnotations(readOnlyHint=False, destructiveHint=True, idempotentHint=False, openWorldHint=True)
CANCEL = ToolAnnotations(readOnlyHint=False, destructiveHint=True, idempotentHint=True, openWorldHint=False)


def register_freemocap_tools(mcp, settings):
    @mcp.tool(annotations=READ)
    async def get_freemocap_capabilities() -> dict[str, Any]:
        """Inspect the configured loopback FreeMoCap version, route compatibility and limits without starting it.

        This probes an independently installed service; it neither starts cameras nor downloads models.
        Compatibility is a version/route check, not proof of calibration, motion accuracy or completed work.
        """
        return await asyncio.to_thread(capture.get_freemocap_capabilities, settings)

    @mcp.tool(annotations=READ)
    async def list_freemocap_recordings(
        limit: Annotated[StrictInt, Field(ge=1, le=100)] = 20,
    ) -> dict[str, Any]:
        """List recordings contained in the operator's configured FreeMoCap recording root."""
        return await asyncio.to_thread(capture.list_freemocap_recordings, settings, limit)

    @mcp.tool(annotations=READ)
    async def inspect_freemocap_recording(recording_id: capture.Identifier) -> dict[str, Any]:
        """Inspect observed output readiness and contained file checksums for a local recording.

        File presence does not prove pipeline completion. FreeMoCap landmarks are not SOMA local rotations
        or an automatically compatible Unreal skeleton. Single-camera output is planar pixel space.
        """
        return await asyncio.to_thread(capture.inspect_freemocap_recording, settings, recording_id)

    @mcp.tool(annotations=READ)
    async def plan_freemocap_capture(request: capture.FreeMocapRequest) -> dict[str, Any]:
        """Validate one explicit FreeMoCap stage and inspect its fixed payload without submitting it.

        Select check_sync, synchronize, import, calibrate, process or export. Video/calibration references
        are relative to operator roots. Equal frame counts do not prove synchronization. Processing one
        camera requires allow_planar=true; metric multi-camera work needs an explicit matching calibration.
        """
        return await asyncio.to_thread(capture.plan_freemocap_capture, settings, request)

    @mcp.tool(annotations=START)
    async def start_freemocap_capture(request: capture.FreeMocapRequest) -> dict[str, Any]:
        """Explicitly submit one reviewed stage to the separate FreeMoCap service and retain its receipt.

        Import creates a new recording; existing recordings are not reprocessed in place. Processing does
        not export or open Blender. Explicit export starts background Blender, supports MediaPipe only,
        and cannot independently verify upstream rig/cleanup options. Calibration also updates the global
        calibration library. A submitted or submission_unknown receipt is not successful completion;
        inspect it before retrying. The external tracker may download its own model assets.
        """
        return await asyncio.to_thread(capture.start_freemocap_capture, settings, request)

    @mcp.tool(annotations=READ)
    async def get_freemocap_job(job_id: capture.JobIdentifier) -> dict[str, Any]:
        """Read this adapter's receipt, synchronization result and separately observed output readiness.

        Calibration/processing success requires the pinned status extension's matching terminal event,
        stopped workers and verified outputs. Do not infer completion from files or retry uncertain submission.
        """
        return await asyncio.to_thread(capture.get_freemocap_job, settings, job_id)

    @mcp.tool(annotations=CANCEL)
    async def cancel_freemocap_job(job_id: capture.JobIdentifier) -> dict[str, Any]:
        """Cancel only the exact upstream calibration/processing pipeline bound to this adapter receipt.

        Synchronization has no supported cancel endpoint. This never calls a stop-all route, shuts down
        the service, stops cameras or kills an editor. Existing partial outputs remain for inspection.
        """
        return await asyncio.to_thread(capture.cancel_freemocap_job, settings, job_id)


    @mcp.tool(annotations=READ)
    async def inspect_freemocap_frames(
        job_id: capture.JobIdentifier,
        frame_indices: Annotated[list[Annotated[StrictInt, Field(ge=0, le=143999)]], Field(max_length=16)] = [],
        aspect: Literal["body", "left_hand", "right_hand", "face"] = "body",
    ) -> dict[str, Any]:
        """Inspect original landmark frames from a verified completed process/export, without the runtime.

        Missing positions are null with a validity mask. Coordinates retain native units; planar pixels
        are not meters. Names follow the reviewed output profile, not the upstream tracker-schema label.
        """
        return await asyncio.to_thread(review.inspect_freemocap_frames, settings, job_id, frame_indices, aspect)

    @mcp.tool(annotations=READ)
    async def analyze_freemocap_quality(job_id: capture.JobIdentifier) -> dict[str, Any]:
        """Review body landmark gaps and adjacent-frame movement offline, with original problem frames.

        Valid output after interpolation is not detector confidence. Rates use persisted nominal FPS only;
        legacy results report displacement per frame. No realism score, contacts or automatic repair.
        """
        return await asyncio.to_thread(review.analyze_freemocap_quality, settings, job_id)

    @mcp.tool(annotations=READ)
    async def get_freemocap_artifact(
        job_id: capture.JobIdentifier, path: Annotated[str, Field(min_length=1, max_length=512)],
    ) -> dict[str, Any]:
        """Verify and locate an exact output path in a completed process/export receipt.

        This reads managed output evidence; it never exports, opens Blender, or accepts arbitrary files.
        """
        return await asyncio.to_thread(review.get_freemocap_artifact, settings, job_id, path)


def freemocap_routes(settings):
    """Read-only verified review routes, behind Studio's host/origin guards."""
    async def preview(request):
        try:
            pairs = list(request.query_params.multi_items())
            if len({key for key, _ in pairs}) != len(pairs) or any(key not in {"start_frame", "count"} for key, _ in pairs):
                raise ValueError("Preview accepts start_frame and count once each")
            options = {}
            for key, value in pairs:
                if not value.isascii() or not value.isdecimal() or len(value) > 7:
                    raise ValueError("Preview window controls must be nonnegative integers")
                options[key] = int(value)
            result = await asyncio.to_thread(review.preview_freemocap, settings, request.path_params["job_id"], **options)
            return JSONResponse(result)
        except (ValueError, OSError, KeyError, TypeError) as error:
            return JSONResponse({"error": redact(str(error))[:2000]}, status_code=400)

    async def download(request):
        try:
            result = await asyncio.to_thread(review.get_freemocap_artifact, settings,
                                            request.path_params["job_id"], request.path_params["path"])
            return FileResponse(result["absolute_path"], filename=Path(result["path"]).name,
                                headers={"X-Content-Type-Options": "nosniff"})
        except (ValueError, OSError, KeyError, TypeError):
            return JSONResponse({"error": "Verified capture artifact not found"}, status_code=404)

    return [Route("/api/freemocap/{job_id}/preview", preview),
            Route("/api/freemocap/{job_id}/files/{path:path}", download)]
