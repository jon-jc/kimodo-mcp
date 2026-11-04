"""Shared typed capture operations and local-only Studio transfer routes."""

from __future__ import annotations

import asyncio
import hashlib
import secrets
import uuid
from typing import Annotated, Any, Literal

import anyio
from mcp.types import ToolAnnotations
from pydantic import Field, StrictInt
from starlette.responses import FileResponse, JSONResponse
from starlette.routing import Route

from . import video_mocap as capture
from .jobs import redact
from .motion_revision import FrameSelection

TOOL_NAMES = (
    "plan_video_mocap", "start_video_mocap", "get_video_mocap_job", "list_video_mocap_jobs",
    "get_video_mocap_log", "cancel_video_mocap_job", "inspect_video_mocap_frames", "get_video_mocap_artifact",
)
ArtifactName = Literal["reference.mp4", "motion.npz", "motion.bvh", "detections.json",
                       "smpl-parameters.json", "timing.json", "diagnostics.json"]
READ = ToolAnnotations(readOnlyHint=True, destructiveHint=False, idempotentHint=True, openWorldHint=False)
WRITE = ToolAnnotations(readOnlyHint=False, destructiveHint=False, idempotentHint=False, openWorldHint=False)
CANCEL = ToolAnnotations(readOnlyHint=False, destructiveHint=True, idempotentHint=True, openWorldHint=False)
MAX_UPLOAD = 512 * 1024 * 1024
VIDEO_EXTENSIONS = {"mp4", "mov", "mkv", "webm", "avi", "m4v"}


def register_video_tools(mcp, settings):
    @mcp.tool(annotations=READ)
    async def plan_video_mocap(request: capture.VideoMocapRequest) -> dict[str, Any]:
        """Check a local video, bounded trim, pinned EasyMocap runtime and licensed assets without capturing.

        Returns ready, actionable preflight checks, source checksum and stage plan. One visible person and
        a static camera are assumed; arbitrary footage may fail or yield uncertain scale/depth. No downloads.
        """
        return await asyncio.to_thread(capture.plan_video_mocap, settings, request)

    @mcp.tool(annotations=WRITE)
    async def start_video_mocap(
        request: capture.VideoMocapRequest,
        expected_source_sha256: Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")] | None = None,
    ) -> dict[str, Any]:
        """Explicitly start a separate durable EasyMocap capture after all local readiness checks pass.

        Originals are preserved. Poll get_video_mocap_job; submission is not success. A completed capture
        uses SMPL24 and must be retargeted and reviewed before use on a SOMA character or in Unreal.
        Supply the plan's source.sha256 to reject a video changed since readiness was reviewed.
        """
        options = {"expected_source_sha256": expected_source_sha256} if expected_source_sha256 is not None else {}
        return await asyncio.to_thread(capture.start_video_mocap, settings, request, **options)

    @mcp.tool(annotations=READ)
    async def get_video_mocap_job(job_id: str) -> dict[str, Any]:
        """Read capture status, current stage, detection diagnostics, errors and completed output receipt."""
        return await asyncio.to_thread(capture.get_video_mocap_job, settings, job_id)

    @mcp.tool(annotations=READ)
    async def list_video_mocap_jobs(limit: Annotated[StrictInt, Field(ge=1, le=100)] = 20) -> dict[str, Any]:
        """List video-capture jobs separately from generated Kimodo animations."""
        return await asyncio.to_thread(capture.list_video_mocap_jobs, settings, limit)

    @mcp.tool(annotations=READ)
    async def get_video_mocap_log(job_id: str, lines: Annotated[StrictInt, Field(ge=1, le=500)] = 80) -> dict[str, Any]:
        """Read a bounded tail of a managed capture's stage log."""
        return await asyncio.to_thread(capture.get_video_mocap_log, settings, job_id, lines)

    @mcp.tool(annotations=CANCEL)
    async def cancel_video_mocap_job(job_id: str) -> dict[str, Any]:
        """Request cancellation of this capture. Poll until terminal; no other worker or editor is stopped."""
        return await asyncio.to_thread(capture.cancel_video_mocap_job, settings, job_id)

    @mcp.tool(annotations=READ)
    async def inspect_video_mocap_frames(job_id: str, frame_indices: FrameSelection = []) -> dict[str, Any]:
        """Inspect up to 16 captured SMPL24 frames with source times, world positions and 2D confidences.

        Indices are zero-based within the captured trim; empty selects frame zero. The camera/world scale
        is estimated. No contacts, finger motion, anatomical acceptance or fitted-character quality is implied.
        """
        data = await asyncio.to_thread(capture.get_video_mocap_preview_data, settings, job_id)
        frames = frame_indices or [0]
        if len(set(frames)) != len(frames) or any(frame >= data["frames"] for frame in frames):
            raise ValueError("Choose unique frame indices inside this capture")
        return {key: value for key, value in data.items() if key not in {"positions", "keypoints2d", "source_time_seconds"}} | {
            "frames_inspected": [{"frame_index": index, "source_time_seconds": data["source_time_seconds"][index],
                                  "positions_m": data["positions"][index], "keypoints2d": data["keypoints2d"][index]}
                                 for index in frames]}

    @mcp.tool(annotations=READ)
    async def get_video_mocap_artifact(job_id: str, name: ArtifactName) -> dict[str, Any]:
        """Verify and locate a completed capture file for an agent or Blender handoff.

        motion.bvh preserves SMPL24 bone lengths, meters and capture frame rate. Retarget it explicitly;
        it is not the registered character FBX or a Kimodo SOMA motion variant.
        """
        path = await asyncio.to_thread(capture.get_video_mocap_artifact, settings, job_id, name)
        digest = await asyncio.to_thread(capture._hash, path)
        return {"job_id": job_id, "name": name, "absolute_path": str(path), "bytes": path.stat().st_size,
                "sha256": digest, "source_kind": "video-mocap"}


def video_routes(settings, csrf: str):
    """Routes inherit Studio host/origin guards; uploads additionally require its token."""
    async def reference(request):
        if not secrets.compare_digest(request.headers.get("x-studio-token", "").encode(), csrf.encode()):
            return JSONResponse({"error": "Refresh the studio before copying a reference."}, status_code=403)
        pairs = list(request.query_params.multi_items())
        extension = request.query_params.get("extension", "")
        if len(pairs) != 1 or pairs[0][0] != "extension" or extension not in VIDEO_EXTENSIONS:
            return JSONResponse({"error": "Choose a supported video file extension."}, status_code=400)
        directory = settings.data_dir.resolve() / "video-mocap" / "references"
        temporary = None
        try:
            if directory.resolve() != directory:
                raise ValueError("Reference storage cannot redirect outside the local workspace")
            directory.mkdir(parents=True, exist_ok=True)
            temporary = directory / f"{uuid.uuid4().hex}.upload"
            digest, total = hashlib.sha256(), 0
            with temporary.open("xb") as output:
                async for chunk in request.stream():
                    total += len(chunk)
                    if total > MAX_UPLOAD:
                        raise ValueError("Reference exceeds 512 MB. Enter its local path instead.")
                    digest.update(chunk)
                    await anyio.to_thread.run_sync(output.write, chunk)
            if not total:
                raise ValueError("The selected video is empty")
            target = temporary.with_suffix(f".{extension}")
            temporary.rename(target)
            return JSONResponse({"path": str(target), "bytes": total, "sha256": digest.hexdigest()})
        except (ValueError, OSError) as error:
            return JSONResponse({"error": redact(str(error))[:2000]}, status_code=400)
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)

    async def preview(request):
        try:
            data = await anyio.to_thread.run_sync(capture.get_video_mocap_preview_data, settings, request.path_params["job_id"])
            return JSONResponse(data)
        except (ValueError, OSError, KeyError, TypeError) as error:
            return JSONResponse({"error": redact(str(error))[:2000]}, status_code=400)

    async def download(request):
        try:
            path = await anyio.to_thread.run_sync(capture.get_video_mocap_artifact, settings,
                                                  request.path_params["job_id"], request.path_params["name"])
            return FileResponse(path, headers={"X-Content-Type-Options": "nosniff"})
        except (ValueError, OSError, KeyError, TypeError):
            return JSONResponse({"error": "Verified capture artifact not found"}, status_code=404)

    return [Route("/api/video-mocap/reference", reference, methods=["POST"]),
            Route("/api/video-mocap/{job_id}/preview", preview),
            Route("/api/video-mocap/{job_id}/files/{name}", download)]
