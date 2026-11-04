"""Studio/MCP asset library, attachment recipes and verified prop exports."""

from __future__ import annotations

import secrets
import uuid
from pathlib import Path
from typing import Annotated, Any, Literal

import anyio
from mcp.types import ToolAnnotations
from pydantic import Field
from starlette.responses import FileResponse, JSONResponse
from starlette.routing import Route

from . import props
from . import studio_assets as assets

SHA = Annotated[str, Field(pattern=r"^[0-9a-f]{64}$", strict=True)]
ATTEMPT = Annotated[int, Field(ge=0, le=99, strict=True)]
READ = ToolAnnotations(readOnlyHint=True, destructiveHint=False, idempotentHint=True, openWorldHint=False)
SAVE = ToolAnnotations(readOnlyHint=False, destructiveHint=False, idempotentHint=True, openWorldHint=False)
RUN = ToolAnnotations(readOnlyHint=False, destructiveHint=False, idempotentHint=False, openWorldHint=False)
TOOL_NAMES = {
    "import_studio_asset", "list_studio_assets", "get_studio_asset", "build_studio_asset_preview",
    "get_studio_asset_preview", "save_prop_scene", "get_prop_scene", "list_prop_scenes",
    "prepare_prop_scene_handoff", "execute_prop_scene_handoff", "get_prop_scene_handoff",
    "get_prop_scene_artifact", "list_prop_scene_handoffs",
}


def register_asset_tools(mcp, settings):
    @mcp.tool(annotations=SAVE)
    async def import_studio_asset(source_path: str, label: Annotated[str, Field(min_length=1, max_length=160)],
                                  kind: Literal["prop", "character"] = "prop") -> dict[str, Any]:
        """Copy a local GLB/FBX/OBJ/VRM into the immutable shared library; import does not run Blender.

        Prefer self-contained GLB or embedded FBX textures; external sidecars are not copied.
        This operation does not replace the registered animation character.
        """
        return await anyio.to_thread.run_sync(assets.import_studio_asset, settings, source_path, label, kind)

    @mcp.tool(annotations=READ)
    async def list_studio_assets(limit: Annotated[int, Field(ge=1, le=100)] = 20,
                                 offset: Annotated[int, Field(ge=0, le=10000)] = 0) -> dict[str, Any]:
        """Browse imported props and character models shared by Studio and agents."""
        return await anyio.to_thread.run_sync(assets.list_studio_assets, settings, limit, offset)

    @mcp.tool(annotations=READ)
    async def get_studio_asset(asset_id: SHA) -> dict[str, Any]:
        """Verify an immutable library asset and return its source metadata."""
        return await anyio.to_thread.run_sync(assets.resolve_asset, settings, asset_id)

    @mcp.tool(annotations=RUN)
    async def build_studio_asset_preview(asset_id: SHA, attempt: ATTEMPT = 0) -> dict[str, Any]:
        """Explicitly convert a registered asset into a self-contained GLB in a fresh background Blender process."""
        return await anyio.to_thread.run_sync(assets.build_studio_asset_preview, settings, asset_id, attempt)

    @mcp.tool(annotations=READ)
    async def get_studio_asset_preview(asset_id: SHA, attempt: ATTEMPT = 0) -> dict[str, Any]:
        """Read an existing verified model preview without running Blender."""
        return await anyio.to_thread.run_sync(assets.get_studio_asset_preview, settings, asset_id, attempt)

    @mcp.tool(annotations=SAVE)
    async def save_prop_scene(request: props.PropSceneRequest) -> dict[str, Any]:
        """Save up to 16 prop placements or character-bone attachments as an immutable scene recipe.

        World placement uses meters and Blender Z-up. Bone attachments use the posed bone-head axes.
        Rotations are local XYZ degrees; scales must be positive. This does not modify animation keys.
        """
        return await anyio.to_thread.run_sync(props.save_prop_scene, settings, request)

    @mcp.tool(annotations=READ)
    async def get_prop_scene(scene_id: SHA) -> dict[str, Any]:
        """Verify a saved attachment recipe and every referenced prop asset."""
        return await anyio.to_thread.run_sync(props.get_prop_scene, settings, scene_id)

    @mcp.tool(annotations=READ)
    async def list_prop_scenes(limit: Annotated[int, Field(ge=1, le=100)] = 20,
                               offset: Annotated[int, Field(ge=0, le=10000)] = 0) -> dict[str, Any]:
        """Find saved prop placements across browser and agent sessions."""
        return await anyio.to_thread.run_sync(props.list_prop_scenes, settings, limit, offset)

    @mcp.tool(annotations=SAVE)
    async def prepare_prop_scene_handoff(scene_id: SHA, source: props.PropMotionSource,
                                         attempt: ATTEMPT = 0) -> dict[str, Any]:
        """Bind prop attachments to a verified Kimodo or capture character animation without executing Blender."""
        return await anyio.to_thread.run_sync(props.prepare_prop_scene_handoff, settings, scene_id, source, attempt)

    @mcp.tool(annotations=RUN)
    async def execute_prop_scene_handoff(handoff_id: SHA) -> dict[str, Any]:
        """Build the animated prop scene in fresh background Blender, preserving the source character animation."""
        return await anyio.to_thread.run_sync(props.execute_prop_scene_handoff, settings, handoff_id)

    @mcp.tool(annotations=READ)
    async def get_prop_scene_handoff(handoff_id: SHA) -> dict[str, Any]:
        """Verify prop-scene outputs, source motion identity and unchanged character animation."""
        return await anyio.to_thread.run_sync(props.get_prop_scene_handoff, settings, handoff_id)

    @mcp.tool(annotations=READ)
    async def list_prop_scene_handoffs(limit: Annotated[int, Field(ge=1, le=100)] = 20,
                                       offset: Annotated[int, Field(ge=0, le=10000)] = 0) -> dict[str, Any]:
        """Find saved prop-scene export attempts; open an exact ID to verify its files."""
        return await anyio.to_thread.run_sync(props.list_prop_scene_handoffs, settings, limit, offset)

    @mcp.tool(annotations=READ)
    async def get_prop_scene_artifact(handoff_id: SHA, path: str) -> dict[str, Any]:
        """Locate a verified animated scene, separate static prop FBX or attachment recipe."""
        return await anyio.to_thread.run_sync(props.get_prop_scene_artifact, settings, handoff_id, path)


def asset_routes(settings, csrf):
    async def upload(request):
        if not secrets.compare_digest(request.headers.get("x-studio-token", "").encode(), csrf.encode()):
            return JSONResponse({"error": "Refresh Studio before importing a model"}, status_code=403)
        temporary = None
        try:
            pairs = list(request.query_params.multi_items())
            query = dict(pairs)
            if len(query) != len(pairs) or set(query) != {"extension", "label", "kind"}:
                raise ValueError("Choose one format, label and asset kind")
            if query["extension"] not in assets.FORMATS or query["kind"] not in {"prop", "character"}:
                raise ValueError("Choose a GLB, FBX, OBJ or VRM model")
            root = settings.data_dir.resolve() / "asset-uploads"
            if root.resolve() != root:
                raise ValueError("Upload directory cannot redirect")
            root.mkdir(parents=True, exist_ok=True)
            temporary = root / (uuid.uuid4().hex + "." + query["extension"])
            total = 0
            with temporary.open("xb") as stream:
                async for chunk in request.stream():
                    total += len(chunk)
                    if total > assets.MAX_BYTES:
                        raise ValueError("Model exceeds the 512 MiB import limit")
                    await anyio.to_thread.run_sync(stream.write, chunk)
            value = await anyio.to_thread.run_sync(assets.import_studio_asset, settings, str(temporary),
                                                    query["label"], query["kind"])
            return JSONResponse(value)
        except (ValueError, OSError, KeyError, TypeError) as error:
            return JSONResponse({"error": str(error)[:2000]}, status_code=400)
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)

    async def source(request):
        try:
            value = await anyio.to_thread.run_sync(assets.resolve_asset, settings, request.path_params["asset_id"])
            preview = value["format"] == "glb"
            if preview:
                await anyio.to_thread.run_sync(assets.inspect_glb, Path(value["absolute_path"]))
            return FileResponse(value["absolute_path"], media_type="model/gltf-binary" if preview else None,
                                filename=None if preview else "source." + value["format"],
                                headers={"Cache-Control": "no-store", "X-Content-Type-Options": "nosniff"})
        except (ValueError, OSError, KeyError, TypeError):
            return JSONResponse({"error": "Verified asset not found"}, status_code=404)

    async def preview(request):
        try:
            value = await anyio.to_thread.run_sync(assets.get_studio_asset_preview, settings,
                                                  request.path_params["asset_id"], int(request.path_params["attempt"]))
            return FileResponse(value["absolute_path"], media_type="model/gltf-binary",
                                headers={"Cache-Control": "no-store", "X-Content-Type-Options": "nosniff"})
        except (ValueError, OSError, KeyError, TypeError):
            return JSONResponse({"error": "Verified model preview not found"}, status_code=404)

    async def prop_artifact(request):
        try:
            value = await anyio.to_thread.run_sync(props.get_prop_scene_artifact, settings,
                                                  request.path_params["handoff_id"], request.path_params["path"])
            path = Path(value["absolute_path"])
            preview = path.suffix == ".glb"
            return FileResponse(path, media_type="model/gltf-binary" if preview else None,
                                filename=None if preview else path.name,
                                headers={"Cache-Control": "no-store", "X-Content-Type-Options": "nosniff"})
        except (ValueError, OSError, KeyError, TypeError):
            return JSONResponse({"error": "Verified prop scene artifact not found"}, status_code=404)
    return [Route("/api/assets/import", upload, methods=["POST"]),
            Route("/api/assets/{asset_id}/source", source),
            Route("/api/assets/{asset_id}/preview/{attempt}", preview),
            Route("/api/prop-scenes/{handoff_id}/files/{path:path}", prop_artifact)]
