"""Local browser studio backed by the same validated operations as MCP clients."""

from __future__ import annotations

import argparse
import asyncio
import json
import secrets
from pathlib import Path
from urllib.parse import urlsplit

import anyio
import uvicorn
from starlette.applications import Starlette
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.middleware.trustedhost import TrustedHostMiddleware
from starlette.requests import Request
from starlette.responses import FileResponse, JSONResponse
from starlette.routing import Route

from .capacity import capacity_snapshot, editor_connections
from .config import Settings
from .jobs import JobStore, contained_file, redact
from .server import create_server
from .workspace import DOWNLOAD_PATH as _DOWNLOAD
from .workspace import Workspace

_MAX_BODY = 1024 * 1024
_ACTIONS = {
    "plan_animation",
    "generate_animation",
    "revise_animation",
    "generate_motion",
    "validate_motion",
    "cancel_job",
    "get_job_log",
    "inspect_motion",
    "analyze_motion_quality",
    "create_motion_review",
    "compare_motions",
    "preview_motion",
    "extract_pose_constraint",
    "get_character_profile",
    "prepare_character_handoff",
    "prepare_blender_handoff",
    "get_handoff_report",
    "execute_blender_handoff",
    "prepare_unreal_handoff",
    "execute_unreal_handoff",
    "get_unreal_report",
    "health_check",
    "list_models",
    "build_character_preview",
    "save_unreal_target",
    "get_capabilities",
    "get_workflow",
    "get_input_schema",
    "get_workspace_state",
    "get_unreal_target",
    "list_job_outputs",
    "inspect_motion_frames",
    "extract_motion_constraints",
    "export_motion",
}


def create_app(settings: Settings | None = None, web_directory: Path | None = None) -> Starlette:
    settings = settings or Settings.from_env()
    store = JobStore(settings)
    workspace = Workspace(settings, store)
    mcp = create_server(settings, workspace=workspace)
    packaged = Path(__file__).with_name("studio_web")
    development = Path(__file__).resolve().parents[2] / "web" / "dist"
    static = (web_directory or (packaged if (packaged / "index.html").is_file() else development)).resolve()
    csrf = secrets.token_urlsafe(32)

    async def invoke(name: str, arguments: dict):
        # Synchronous generation/editor wrappers must not block polling or the
        # viewer. Each call uses the MCP tool's existing Pydantic validation.
        def run():
            value = asyncio.run(mcp.call_tool(name, arguments))
            if isinstance(value, tuple):
                return value[1]
            if isinstance(value, dict):
                return value
            raise ValueError("Tool did not return structured data")

        return await anyio.to_thread.run_sync(run)

    def decorate(value):
        if not isinstance(value, dict):
            return value
        result = dict(value)
        for source, destination in (
            ("path", "url"),
            ("preview_glb_path", "url"),
            ("diagnostics_path", "diagnostics_url"),
        ):
            url = workspace.file_url(result.get(source))
            if url:
                result[destination] = url
        return result

    async def state(request):
        try:
            health = await invoke("health_check", {})
            health["capacity"] = await anyio.to_thread.run_sync(capacity_snapshot)
            health["editors"] = await anyio.to_thread.run_sync(editor_connections)
            try:
                profile = await invoke("get_character_profile", {})
            except Exception as exc:
                profile = {"configured": False, "error": redact(str(exc))[:2000]}
            preview = workspace.character_preview()
            profile.update(
                name="Registered character", preview_url="/api/character/model.glb" if preview else None
            )
            return JSONResponse(
                {
                    "jobs": await anyio.to_thread.run_sync(
                        lambda: [workspace.job_details(item["id"]) for item in store.list(40)]
                    ),
                    "character": profile,
                    "targets": {"unreal": workspace.project_target()},
                    "health": health,
                    "csrf_token": csrf,
                    "presets": [
                        {
                            "label": "Friendly wave",
                            "description": "A person stands in place and slowly waves with their right hand.",
                            "duration_seconds": 4,
                        },
                        {
                            "label": "Confident walk",
                            "description": "A person walks forward at a relaxed, confident pace with natural arm swing.",
                            "duration_seconds": 5,
                        },
                        {
                            "label": "Look around",
                            "description": "A person stands comfortably, turns their head to look left, then looks forward again.",
                            "duration_seconds": 4,
                        },
                        {
                            "label": "Careful landing",
                            "description": "A person makes a small jump forward, lands softly with bent knees, and settles into standing.",
                            "duration_seconds": 4,
                        },
                    ],
                }
            )
        except Exception as exc:
            return JSONResponse({"error": redact(str(exc))[:2000]}, status_code=400)

    async def job(request):
        try:
            return JSONResponse(
                await anyio.to_thread.run_sync(lambda: workspace.job_details(request.path_params["job_id"]))
            )
        except (ValueError, OSError) as exc:
            return JSONResponse({"error": str(exc)}, status_code=404)

    async def action(request: Request):
        if not secrets.compare_digest(
            request.headers.get("x-studio-token", "").encode("utf-8"), csrf.encode("utf-8")
        ):
            return JSONResponse({"error": "Refresh the studio before sending this action."}, status_code=403)
        try:
            body = bytearray()
            async for chunk in request.stream():
                body.extend(chunk)
                if len(body) > _MAX_BODY:
                    return JSONResponse({"error": "Request exceeds the studio size limit."}, status_code=413)
            arguments = json.loads(body)
            if not isinstance(arguments, dict):
                raise ValueError("Action arguments must be an object")
            name = request.path_params["tool"]
            if name in _ACTIONS:
                result = decorate(await invoke(name, arguments))
            else:
                return JSONResponse({"error": "Unknown studio action"}, status_code=404)
            return JSONResponse({"result": result})
        except Exception as exc:
            return JSONResponse({"error": redact(str(exc))[:4000]}, status_code=400)

    async def download(request):
        try:
            relative = request.path_params["path"]
            if not _DOWNLOAD.fullmatch(relative):
                raise ValueError("This file is not a studio artifact")
            job_id = request.path_params["job_id"]
            job = store.get(job_id)
            if job["status"] != "completed":
                raise ValueError("Artifacts are available after successful completion")
            if "/" not in relative and not relative.endswith("_preview.html"):
                output = await anyio.to_thread.run_sync(lambda: workspace.verified_native_output(job_id, relative))
                path = Path(output["absolute_path"])
            else:
                path = contained_file(store.directory(job_id), relative)
                if relative.startswith("exports/"):
                    output = await anyio.to_thread.run_sync(
                        lambda: workspace.verified_motion_export(job_id, relative.split("/")[1])
                    )
                    if path not in {Path(output["absolute_path"]), Path(output["report_path"])}:
                        raise ValueError("File is not the verified motion export or its report")
                elif path.parent.name.startswith("character-motion_") and path.suffix in {".glb", ".fbx", ".blend"}:
                    await anyio.to_thread.run_sync(
                        lambda: workspace.verified_preview(path.with_name("blender-report.json"), store.directory(job_id), job)
                    )
                elif path.parent.name.startswith("skeleton-motion_") and path.suffix in {".fbx", ".blend"}:
                    await anyio.to_thread.run_sync(
                        lambda: workspace.verified_skeleton_outputs(
                            path.with_name("blender-report.json"), store.directory(job_id), job
                        )
                    )
            headers = {"X-Content-Type-Options": "nosniff"}
            if path.suffix == ".html":
                headers["Content-Security-Policy"] = (
                    "default-src 'none'; script-src 'unsafe-inline'; style-src 'unsafe-inline'; img-src data:; frame-ancestors 'self'"
                )
            return FileResponse(path, headers=headers)
        except (ValueError, OSError, KeyError, TypeError, RuntimeError):
            return JSONResponse({"error": "Artifact not found"}, status_code=404)

    async def initial_character(request):
        path = workspace.character_preview()
        if path is None:
            return JSONResponse(
                {"error": "Build a character preview from a completed animation first."}, status_code=404
            )
        return FileResponse(path, media_type="model/gltf-binary")

    async def web(request):
        relative = request.path_params.get("path") or "index.html"
        try:
            path = contained_file(static, relative)
        except (ValueError, OSError):
            return JSONResponse(
                {"error": "Studio assets are missing. Build the web app first."}, status_code=404
            )
        return FileResponse(
            path,
            headers={
                "Content-Security-Policy": "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data: blob:; connect-src 'self' blob:; object-src 'none'; frame-ancestors 'none'"
            },
        )

    app = Starlette(
        routes=[
            Route("/api/state", state),
            Route("/api/jobs/{job_id}", job),
            Route("/api/actions/{tool}", action, methods=["POST"]),
            Route("/api/character/model.glb", initial_character),
            Route("/api/files/{job_id}/{path:path}", download),
            Route("/{path:path}", web),
        ]
    )

    async def local_origin(request, call_next):
        origin = request.headers.get("origin")
        try:
            parsed_origin = urlsplit(origin) if origin else None
        except ValueError:
            return JSONResponse({"error": "Invalid browser origin."}, status_code=403)
        if parsed_origin and (
            parsed_origin.netloc != request.headers.get("host") or parsed_origin.scheme != "http"
        ):
            return JSONResponse({"error": "Only the local studio can access this endpoint."}, status_code=403)
        if request.headers.get("sec-fetch-site") == "cross-site":
            return JSONResponse({"error": "Cross-site access is disabled."}, status_code=403)
        response = await call_next(request)
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Cache-Control"] = "no-store"
        return response

    app.add_middleware(BaseHTTPMiddleware, dispatch=local_origin)
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=["127.0.0.1", "localhost", "[::1]"])
    return app


def main():
    parser = argparse.ArgumentParser(description="Kimodo local animation studio")
    parser.add_argument("--port", type=int, default=8768)
    parser.add_argument("--web-directory", type=Path)
    args = parser.parse_args()
    if not 1024 <= args.port <= 65535:
        parser.error("port must be 1024..65535")
    uvicorn.run(create_app(web_directory=args.web_directory), host="127.0.0.1", port=args.port)


if __name__ == "__main__":
    main()
