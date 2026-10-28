"""Local browser studio backed by the same validated operations as MCP clients."""

from __future__ import annotations

import argparse
import asyncio
import json
import re
import secrets
import tempfile
import threading
from pathlib import Path
from urllib.parse import quote, urlsplit

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
from .handoff import UnrealTarget
from .jobs import JobStore, contained_file, redact
from .server import create_server

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
}
_DOWNLOAD = re.compile(
    r"(?:motion_\d{2}(?:_amass)?\.(?:npz|bvh|csv)|motion_\d{2}_preview\.html|"
    r"reviews/(?:motion|comparison)-[0-9a-f]{20}\.(?:html|json)|"
    r"handoffs/(?:character|skeleton)-motion_\d{2}(?:-attempt\d{2})?/"
    r"(?:character-preview\.glb|(?:character|animation)\.(?:fbx|blend)|blender-report\.json|"
    r"unreal-[0-9a-f]{16}/unreal-report\.json))"
)


def create_app(settings: Settings | None = None, web_directory: Path | None = None) -> Starlette:
    settings = settings or Settings.from_env()
    store, mcp = JobStore(settings), create_server(settings)
    packaged = Path(__file__).with_name("studio_web")
    development = Path(__file__).resolve().parents[2] / "web" / "dist"
    static = (web_directory or (packaged if (packaged / "index.html").is_file() else development)).resolve()
    csrf = secrets.token_urlsafe(32)
    preview_cache = {}
    preview_cache_lock = threading.Lock()

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

    def file_url(path_value):
        if not isinstance(path_value, str):
            return None
        try:
            parts = Path(path_value).resolve().relative_to((settings.data_dir / "jobs").resolve()).parts
            if len(parts) >= 2 and _DOWNLOAD.fullmatch("/".join(parts[1:])):
                return "/api/files/" + "/".join(quote(part, safe="") for part in parts)
        except (ValueError, OSError):
            pass
        return None

    def decorate(value):
        if not isinstance(value, dict):
            return value
        result = dict(value)
        for source, destination in (
            ("path", "url"),
            ("preview_glb_path", "url"),
            ("diagnostics_path", "diagnostics_url"),
        ):
            url = file_url(result.get(source))
            if url:
                result[destination] = url
        return result

    def project_target():
        path = settings.data_dir / "studio.json"
        if not path.is_file() or path.stat().st_size > 16384:
            return None
        try:
            return UnrealTarget.model_validate(json.loads(path.read_text())["unreal_target"]).model_dump()
        except (ValueError, KeyError, TypeError):
            return None

    def character_preview():
        registration = settings.data_dir / "character" / "target.json"
        try:
            value = {}
            if registration.is_file():
                if registration.stat().st_size > 8192:
                    return None
                value = json.loads(registration.read_text(encoding="utf-8"))
                if not isinstance(value, dict):
                    return None
            path = Path(
                value.get("preview_glb", settings.data_dir / "character/final/anime-girl-wave-preview.glb")
            )
            if not path.is_absolute() or path.suffix.lower() != ".glb" or not path.is_file():
                return None
            return path.resolve()
        except (OSError, ValueError, TypeError):
            return None

    def verified_preview(report_path, source, job):
        from .blender_bridge import _read_json, _validate_report, _validate_request

        match = re.fullmatch(r"character-motion_(\d{2})(?:-attempt(\d{2}))?", report_path.parent.name)
        if match is None:
            raise ValueError("Unexpected character handoff directory")
        index, attempt = int(match[1]), int(match[2] or 0)
        manifest = job.get("manifest", {})
        if not 0 <= index < manifest.get("samples", 0):
            raise ValueError("Preview sample is outside this completed job")
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
        outputs = [directory / filename for filename in
                   ("character-preview.glb", "character.fbx", "animation.fbx", "character.blend")]
        watched = [source / "manifest.json", report_path, request_path, motion, character, *outputs]

        def signature():
            result = []
            for path in watched:
                if path.is_symlink() or not path.is_file():
                    raise ValueError("Preview provenance files must be regular files")
                stat = path.stat()
                result.append((str(path), stat.st_size, stat.st_mtime_ns, stat.st_ctime_ns, stat.st_ino))
            return tuple(result)

        # A new/changed receipt is fully checked once. Polling then reads only
        # small JSON and stat metadata; changing any input/export invalidates it.
        with preview_cache_lock:
            before = signature()
            cached = preview_cache.get(report_path)
            if cached and cached[0] == before:
                return cached[1]
            bones = _validate_request(prepared, "character")
            report = _validate_report(directory.resolve(), prepared, bones)
            if signature() != before:
                raise ValueError("Preview files changed during validation; inspect the handoff again")
            exports = []
            for field, label in (("preview_glb_path", "Animated GLB"), ("character_fbx_path", "Character FBX"),
                                 ("fbx_path", "Animation FBX"), ("blend_path", "Blender project")):
                url = file_url(report[field])
                if url is None:
                    raise ValueError("Preview export is not a managed studio artifact")
                exports.append({"label": label, "url": url})
            result = {"sample_index": index, "url": exports[0]["url"], "attempt": attempt, "exports": exports}
            if len(preview_cache) >= 512:
                preview_cache.clear()
            preview_cache[report_path] = before, result
            return result

    def job_details(job_id):
        job = store.get(job_id)
        job["previews"] = []
        if job["status"] == "completed":
            source = store.directory(job_id)
            for report_path in sorted((source / "handoffs").glob("character-motion_*/blender-report.json")):
                try:
                    job["previews"].append(verified_preview(report_path, source, job))
                except (OSError, ValueError, KeyError, TypeError, RuntimeError):
                    continue
            latest = {}
            for preview in job["previews"]:
                previous = latest.get(preview["sample_index"])
                if previous is None or preview["attempt"] > previous["attempt"]:
                    latest[preview["sample_index"]] = preview
            job["previews"] = list(latest.values())
            job["downloads"] = [
                {**item, "url": file_url(str(source / item["path"]))}
                for item in job.get("manifest", {}).get("artifacts", [])
            ]
        return job

    async def state(request):
        try:
            health = await invoke("health_check", {})
            health["capacity"] = await anyio.to_thread.run_sync(capacity_snapshot)
            health["editors"] = await anyio.to_thread.run_sync(editor_connections)
            try:
                profile = await invoke("get_character_profile", {})
            except Exception as exc:
                profile = {"configured": False, "error": redact(str(exc))[:2000]}
            preview = character_preview()
            profile.update(
                name="Registered character", preview_url="/api/character/model.glb" if preview else None
            )
            return JSONResponse(
                {
                    "jobs": await anyio.to_thread.run_sync(
                        lambda: [job_details(item["id"]) for item in store.list(40)]
                    ),
                    "character": profile,
                    "targets": {"unreal": project_target()},
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
                await anyio.to_thread.run_sync(lambda: job_details(request.path_params["job_id"]))
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
            if name == "build_character_preview":
                allowed = {"job_id", "sample_index", "attempt"}
                if set(arguments) - allowed or "job_id" not in arguments:
                    raise ValueError("Use a job and sample from the studio")
                job_id = arguments["job_id"]
                index = arguments.get("sample_index", 0)
                if type(index) is not int or not 0 <= index <= 3:
                    raise ValueError("Sample index must be 0..3")
                if "attempt" not in arguments:
                    existing = job_details(job_id)["previews"]
                    matches = [p for p in existing if p["sample_index"] == index]
                    if matches:
                        return JSONResponse({"result": matches[-1]})
                    base = store.directory(job_id) / "handoffs"
                    for pending in base.glob(f"character-motion_{index:02d}*"):
                        if any((pending / marker).exists() for marker in
                               ("bridge-dispatch.json", "execution.lock")) and not (
                            pending / "blender-report.json"
                        ).exists():
                            raise ValueError(
                                "A character export may still be running. Inspect the pending handoff before retrying."
                            )
                    for attempt in range(100):
                        suffix = f"-attempt{attempt:02d}" if attempt else ""
                        if not (base / f"character-motion_{index:02d}{suffix}").exists():
                            arguments["attempt"] = attempt
                            break
                    else:
                        raise ValueError("All preview attempts are occupied")
                await invoke("prepare_character_handoff", arguments)
                report = await invoke("execute_blender_handoff", {**arguments, "source_kind": "character"})
                result = {**decorate(report), "attempt": arguments["attempt"]}
            elif name == "save_unreal_target":
                target = UnrealTarget.model_validate(arguments["target"])
                config = settings.data_dir / "studio.json"
                with tempfile.NamedTemporaryFile(
                    mode="w", dir=settings.data_dir, encoding="utf-8", delete=False
                ) as stream:
                    temporary = Path(stream.name)
                    json.dump({"unreal_target": target.model_dump()}, stream, indent=2)
                temporary.replace(config)
                result = {"target": target.model_dump()}
            elif name in _ACTIONS:
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
                path = Path(store.artifact(job_id, relative)["absolute_path"])
            else:
                path = contained_file(store.directory(job_id), relative)
                if path.parent.name.startswith("character-motion_") and path.suffix in {".glb", ".fbx", ".blend"}:
                    verified_preview(path.with_name("blender-report.json"), store.directory(job_id), job)
            headers = {"X-Content-Type-Options": "nosniff"}
            if path.suffix == ".html":
                headers["Content-Security-Policy"] = (
                    "default-src 'none'; script-src 'unsafe-inline'; style-src 'unsafe-inline'; img-src data:; frame-ancestors 'self'"
                )
            return FileResponse(path, headers=headers)
        except (ValueError, OSError, KeyError, TypeError, RuntimeError):
            return JSONResponse({"error": "Artifact not found"}, status_code=404)

    async def initial_character(request):
        path = character_preview()
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
