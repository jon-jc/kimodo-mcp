"""MCP tools; no model weights are imported into the connection process."""

from __future__ import annotations

import argparse
import json
import subprocess
from typing import Annotated, Any

from mcp.server.fastmcp import FastMCP
from mcp.types import ToolAnnotations
from pydantic import Field

from .artifacts import create_preview
from .artifacts import inspect_motion as inspect_archive
from .catalog import list_models
from .config import UPSTREAM_REVISION, Settings
from .jobs import JobStore, redact
from .schemas import GenerateRequest

READ_ONLY = ToolAnnotations(readOnlyHint=True, destructiveHint=False, openWorldHint=False)
LOCAL_WRITE = ToolAnnotations(readOnlyHint=False, destructiveHint=False, openWorldHint=False)
GENERATION = ToolAnnotations(readOnlyHint=False, destructiveHint=False, openWorldHint=True)


def create_server(settings: Settings | None = None, port: int = 8767) -> FastMCP:
    settings = settings or Settings.from_env()
    store = JobStore(settings)
    store.recover_stale()
    mcp = FastMCP(
        "Kimodo Motion Studio",
        instructions=(
            "Generate skeletal animation with a local Kimodo installation. Start with list_models and "
            "health_check. Validate a request before generate_motion; poll get_job for completion. "
            "Only describe an animation as generated when its job is completed. Jobs survive client "
            "disconnects. NPZ coordinates are meters, Y-up, +Z forward. This server does not create meshes."
        ),
        host="127.0.0.1", port=port,
    )

    @mcp.tool(name="list_models", annotations=READ_ONLY)
    def models() -> dict[str, Any]:
        """List exact supported model IDs, skeletons, export formats and upstream revision."""
        return {"models": list_models(), "default": "kimodo-soma-rp-v1.2"}

    @mcp.tool(annotations=READ_ONLY)
    def health_check(probe_runtime: bool = False) -> dict[str, Any]:
        """Check connection/storage; optionally probe the configured Python runtime without downloads."""
        result = {
            "server": "ready", "data_directory": str(settings.data_dir),
            "runtime_python": settings.python, "upstream_directory": str(settings.repo),
            "reviewed_revision": UPSTREAM_REVISION,
            "runtime_probed": probe_runtime,
            "worker_mode": settings.worker_mode,
            "worker_service": store.service_status(),
        }
        if settings.repo:
            try:
                revision = subprocess.run(
                    ["git", "-C", str(settings.repo), "rev-parse", "HEAD"],
                    capture_output=True, text=True, timeout=5, check=True,
                ).stdout.strip()
                result["installed_revision"] = revision
                result["reviewed_revision_matches"] = revision == UPSTREAM_REVISION
            except (OSError, subprocess.SubprocessError):
                result["installed_revision"] = None
        if probe_runtime:
            try:
                probe = subprocess.run(
                    [settings.python, "-m", "kimodo_mcp.worker", "--probe"],
                    env=settings.worker_env(), capture_output=True, text=True, timeout=60,
                )
                result["runtime"] = json.loads(redact(probe.stdout))
                result["runtime_exit_code"] = probe.returncode
            except (OSError, subprocess.SubprocessError, ValueError) as exc:
                result["runtime"] = {"ready": False, "error": redact(str(exc))}
        return result

    @mcp.tool(annotations=READ_ONLY)
    def validate_motion(request: GenerateRequest) -> dict[str, Any]:
        """Validate text segments, model compatibility, frame ranges and kinematic constraints offline."""
        return {"valid": True, "plan": request.validated_plan(), "request": request.model_dump(mode="json")}

    @mcp.tool(annotations=GENERATION)
    def generate_motion(request: GenerateRequest) -> dict[str, Any]:
        """Queue motion generation and return a job ID immediately. May download weights if operator enabled it."""
        return store.submit(request.model_dump(mode="json"))

    @mcp.tool(annotations=READ_ONLY)
    def get_job(job_id: str) -> dict[str, Any]:
        """Get durable status, current stage, request, and final export manifest."""
        return store.get(job_id)

    @mcp.tool(annotations=READ_ONLY)
    def list_jobs(limit: Annotated[int, Field(ge=1, le=100)] = 20) -> dict[str, Any]:
        """List recent jobs shared by clients configured with the same data directory."""
        return {"jobs": store.list(limit)}

    @mcp.tool(annotations=LOCAL_WRITE)
    def cancel_job(job_id: str) -> dict[str, Any]:
        """Request cancellation of a queued or running job; completed outputs remain available."""
        return store.cancel(job_id)

    @mcp.tool(annotations=READ_ONLY)
    def get_job_log(job_id: str, lines: Annotated[int, Field(ge=1, le=100)] = 40) -> dict[str, Any]:
        """Read a bounded tail of generation diagnostics with Hugging Face tokens redacted."""
        return {"job_id": job_id, "log": store.log(job_id, lines)}

    @mcp.tool(annotations=READ_ONLY)
    def get_artifact(job_id: str, name: str) -> dict[str, Any]:
        """Resolve an exported file by its exact manifest path, including checksum and local URI."""
        return store.artifact(job_id, name)

    @mcp.tool(annotations=READ_ONLY)
    def inspect_motion(job_id: str, name: str = "motion_00.npz") -> dict[str, Any]:
        """Measure a completed NPZ: timing, skeleton, root travel, bounds and finite-value checks."""
        artifact = store.artifact(job_id, name)
        if artifact["format"] != "npz":
            raise ValueError("Inspection requires a native Kimodo NPZ artifact")
        return inspect_archive(artifact["absolute_path"])

    @mcp.tool(annotations=LOCAL_WRITE)
    def preview_motion(job_id: str, name: str = "motion_00.npz") -> dict[str, Any]:
        """Create an offline HTML animation viewer with playback, scrubber and camera controls."""
        artifact = store.artifact(job_id, name)
        if artifact["format"] != "npz":
            raise ValueError("Preview requires a native Kimodo NPZ artifact")
        target = store.directory(job_id) / (name.removesuffix(".npz") + "_preview.html")
        return create_preview(artifact["absolute_path"], target)

    @mcp.resource("kimodo://models")
    def model_resource() -> str:
        return json.dumps(list_models(), indent=2)

    @mcp.resource("kimodo://jobs/{job_id}")
    def job_resource(job_id: str) -> str:
        return json.dumps(store.get(job_id), indent=2)

    @mcp.resource("kimodo://request-schema")
    def schema_resource() -> str:
        return json.dumps(GenerateRequest.model_json_schema(), indent=2)

    @mcp.prompt()
    def animation_workflow(description: str) -> str:
        """Plan, validate, generate, inspect and export an animation from a creative brief."""
        return (
            "Create a Kimodo animation for this brief: " + description + "\n"
            "1. Check health_check(probe_runtime=true) and list_models.\n"
            "2. Translate the brief into short text segments with explicit duration, seed, and model.\n"
            "3. Use validate_motion. Ask for missing artistic decisions only when necessary.\n"
            "4. Call generate_motion and poll get_job; use get_job_log on failure.\n"
            "5. Call inspect_motion and preview_motion on a completed NPZ, then get_artifact for exports.\n"
            "SOMA BVH can animate an imported skeleton in Blender; retargeting a custom mesh is a separate step."
        )

    return mcp


def main() -> None:
    parser = argparse.ArgumentParser(description="Kimodo local MCP server")
    parser.add_argument("--transport", choices=["stdio", "streamable-http"], default="stdio")
    parser.add_argument("--port", type=int, default=8767)
    args = parser.parse_args()
    if not 1024 <= args.port <= 65535:
        parser.error("port must be between 1024 and 65535")
    create_server(port=args.port).run(transport=args.transport)
