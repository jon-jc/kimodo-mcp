"""MCP tools; no model weights are imported into the connection process."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
import tempfile
from pathlib import Path
from typing import Annotated, Any, Literal

from mcp.server.fastmcp import FastMCP
from mcp.types import ToolAnnotations
from pydantic import Field

from .artifacts import create_preview
from .artifacts import inspect_motion as inspect_archive
from .catalog import list_models
from .config import UPSTREAM_REVISION, Settings
from .handoff import UnrealTarget
from .handoff import prepare_blender_handoff as prepare_blender_files
from .handoff import prepare_character_handoff as prepare_character_files
from .handoff import prepare_unreal_handoff as prepare_unreal_files
from .jobs import JobStore, contained_file, redact
from .quality import analyze_motion, compare_motion
from .review import compare_motion_reviews
from .review import create_motion_review as create_review_file
from .schemas import GenerateRequest, QualityThresholds

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

    def motion_artifact(job_id: str, name: str) -> dict[str, Any]:
        artifact = store.artifact(job_id, name)
        if artifact["format"] != "npz":
            raise ValueError("Motion review requires a native Kimodo NPZ artifact")
        with Path(artifact["absolute_path"]).open("rb") as stream:
            if hashlib.file_digest(stream, "sha256").hexdigest() != artifact["sha256"]:
                raise ValueError("Motion changed after the job completed")
        return artifact

    def review_path(job_id: str, key: str, suffix: str) -> Path:
        base = store.directory(job_id).resolve()
        path = (base / "reviews" / (key + suffix)).resolve()
        if not path.is_relative_to(base):
            raise ValueError("Review output escapes its job storage")
        return path

    def write_review_report(path: Path, report: dict[str, Any]) -> None:
        temporary = None
        try:
            with tempfile.NamedTemporaryFile(
                mode="w", encoding="utf-8", dir=path.parent, suffix=".tmp", delete=False,
            ) as stream:
                temporary = Path(stream.name)
                json.dump(report, stream, indent=2, allow_nan=False)
            temporary.replace(path)
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)

    @mcp.tool(annotations=READ_ONLY)
    def analyze_motion_quality(
        job_id: str, name: str = "motion_00.npz", thresholds: QualityThresholds | None = None,
    ) -> dict[str, Any]:
        """Measure speed, jerk, bone drift, contact sliding, floor proxies and endpoint seams.

        These are review diagnostics, not a realism score or automatic repair. Original motion is unchanged.
        """
        artifact = motion_artifact(job_id, name)
        return analyze_motion(artifact["absolute_path"], thresholds=thresholds.model_dump() if thresholds else None)

    @mcp.tool(annotations=LOCAL_WRITE)
    def create_motion_review(
        job_id: str, name: str = "motion_00.npz", thresholds: QualityThresholds | None = None,
    ) -> dict[str, Any]:
        """Create an offline three-view player, exact-frame contact sheet and numerical diagnostic file.

        Highlights uniform timeline samples and metric extrema with fixed whole-clip camera bounds.
        """
        artifact = motion_artifact(job_id, name)
        report = analyze_motion(artifact["absolute_path"], thresholds=thresholds.model_dump() if thresholds else None)
        key = "motion-" + hashlib.sha256(json.dumps(
            [artifact["sha256"], report["thresholds"]], sort_keys=True,
        ).encode()).hexdigest()[:20]
        result = create_review_file(artifact["absolute_path"], review_path(job_id, key, ".html"), diagnostics=report)
        report_path = review_path(job_id, key, ".json")
        write_review_report(report_path, report)
        return {**result, "diagnostics_path": str(report_path), "content_sha256": report["content_sha256"]}

    @mcp.tool(annotations=LOCAL_WRITE)
    def compare_motions(
        job_id: str, other_job_id: str, name: str = "motion_00.npz", other_name: str = "motion_00.npz",
        offset_frames: Annotated[int, Field(ge=-2400, le=2400)] = 0,
        space: Literal["world", "root_relative"] = "world",
    ) -> dict[str, Any]:
        """Compare two completed motion samples with synchronized exact-frame views and measured differences.

        Requires matching FPS and skeletons. B frame = A frame + offset. Root-relative removes translation only.
        Differences do not rank realism or prompt adherence; inspect both candidates before choosing.
        """
        first, second = motion_artifact(job_id, name), motion_artifact(other_job_id, other_name)
        report = compare_motion(first["absolute_path"], second["absolute_path"],
                                second_offset_frames=offset_frames, space=space)
        key = "comparison-" + hashlib.sha256(json.dumps(
            [first["sha256"], second["sha256"], offset_frames, space],
        ).encode()).hexdigest()[:20]
        result = compare_motion_reviews(
            first["absolute_path"], second["absolute_path"], review_path(job_id, key, ".html"),
            offset_frames=offset_frames, mode=space, diagnostics=report,
        )
        report_path = review_path(job_id, key, ".json")
        write_review_report(report_path, report)
        return {**result, "diagnostics_path": str(report_path), "comparison": report}

    def handoff_directory(job_id: str, source_kind: str, sample_index: int, attempt: int = 0) -> Path:
        job = store.get(job_id)
        if job["status"] != "completed" or sample_index >= job["manifest"]["samples"]:
            raise ValueError("Choose a sample from a completed generation job")
        base = store.directory(job_id)
        suffix = f"-attempt{attempt:02d}" if attempt else ""
        directory = (base / "handoffs" / f"{source_kind}-motion_{sample_index:02d}{suffix}").resolve()
        if not directory.is_relative_to(base.resolve()):
            raise ValueError("Handoff directory escapes its job storage")
        return directory

    def configured_character() -> Path | None:
        explicit = os.environ.get("KIMODO_CHARACTER_FBX")
        if explicit:
            return Path(explicit).resolve()
        registration = settings.data_dir / "character" / "target.json"
        if not registration.is_file():
            return None
        if registration.stat().st_size > 8192:
            raise ValueError("Character registration exceeds its size limit")
        value = json.loads(registration.read_text(encoding="utf-8"))
        path = Path(value["source_fbx"])
        if not path.is_absolute():
            raise ValueError("Character registration must use an absolute FBX path")
        return path.resolve()

    @mcp.tool(annotations=READ_ONLY)
    def get_character_profile() -> dict[str, Any]:
        """Inspect the operator-configured fitted-character profile without loading Blender."""
        from .handoff_scripts.character import SOURCE_SHA256, validate_target

        path = configured_character()
        if path is None:
            return {"configured": False, "setup": "Set KIMODO_CHARACTER_FBX to the supported source FBX."}
        validate_target(path)
        return {
            "configured": True, "source_fbx": str(path), "source_sha256": SOURCE_SHA256,
            "profile": "Fitted humanoid rig for the supplied static character", "bones": 24,
            "limitations": ["Asset-specific fit", "No facial or finger animation", "No cloth or hair physics"],
        }

    @mcp.tool(annotations=LOCAL_WRITE)
    def prepare_character_handoff(
        job_id: str, sample_index: Annotated[int, Field(ge=0, le=3)] = 0,
        attempt: Annotated[int, Field(ge=0, le=99)] = 0,
    ) -> dict[str, Any]:
        """Prepare Blender rigging, skinning and motion retargeting for the configured character.

        Uses the inspected source's exact checksum. Saves a separate rigged mesh, animation FBX,
        .blend, and verification report. Execute the returned script through Blender MCP.
        """
        character = configured_character()
        if character is None:
            raise ValueError("Configure a supported character first; see get_character_profile")
        artifact = store.artifact(job_id, f"motion_{sample_index:02d}.npz")
        manifest = store.get(job_id)["manifest"]
        result = prepare_character_files(
            character, Path(artifact["absolute_path"]),
            handoff_directory(job_id, "character", sample_index, attempt),
            source_sha256=artifact["sha256"], fps=manifest["fps"], frames=manifest["frames"],
            sample_index=sample_index,
        )
        return result.model_dump()

    @mcp.tool(annotations=LOCAL_WRITE)
    def prepare_blender_handoff(
        job_id: str, sample_index: Annotated[int, Field(ge=0, le=3)] = 0,
        attempt: Annotated[int, Field(ge=0, le=99)] = 0,
    ) -> dict[str, Any]:
        """Prepare a trusted Blender script to import a generated SOMA BVH and save .blend/FBX."""
        artifact = store.artifact(job_id, f"motion_{sample_index:02d}.bvh")
        manifest = store.get(job_id)["manifest"]
        result = prepare_blender_files(
            Path(artifact["absolute_path"]), handoff_directory(job_id, "skeleton", sample_index, attempt),
            fps=manifest["fps"], frames=manifest["frames"],
            source_sha256=artifact["sha256"], skeleton=manifest["skeleton"],
        )
        return result.model_dump()

    @mcp.tool(annotations=READ_ONLY)
    def get_handoff_report(
        job_id: str, source_kind: Literal["skeleton", "character"] = "skeleton",
        sample_index: Annotated[int, Field(ge=0, le=3)] = 0,
        attempt: Annotated[int, Field(ge=0, le=99)] = 0,
    ) -> dict[str, Any]:
        """Read the Blender execution report for a managed skeleton or fitted-character handoff."""
        directory = handoff_directory(job_id, source_kind, sample_index, attempt)
        path = directory / "blender-report.json"
        if not path.is_file():
            status = "not_completed" if (directory / "execution.lock").exists() else "not_executed"
            return {"status": status, "report_path": str(path)}
        path = contained_file(directory, "blender-report.json")
        if path.stat().st_size > 1_000_000:
            raise ValueError("Handoff report exceeds its size limit")
        return json.loads(path.read_text(encoding="utf-8"))

    @mcp.tool(annotations=LOCAL_WRITE)
    def prepare_unreal_handoff(
        job_id: str, target: UnrealTarget,
        source_kind: Literal["skeleton", "character"] = "skeleton",
        sample_index: Annotated[int, Field(ge=0, le=3)] = 0,
        attempt: Annotated[int, Field(ge=0, le=99)] = 0,
    ) -> dict[str, Any]:
        """Prepare animation-only Unreal import after Blender reports a verified FBX export.

        The editor checks the exact project and matching rig hierarchy before creating an asset.
        Import the rigged character mesh first, then use execute_unreal_handoff.
        """
        source = handoff_directory(job_id, source_kind, sample_index, attempt)
        key = hashlib.sha256(target.model_dump_json().encode()).hexdigest()[:16]
        report = contained_file(source, "blender-report.json")
        output = (source / f"unreal-{key}").resolve()
        if not output.is_relative_to(source.resolve()):
            raise ValueError("Unreal handoff directory escapes its managed source")
        result = prepare_unreal_files(report, output, target=target)
        return result.model_dump()

    @mcp.tool(annotations=LOCAL_WRITE)
    def execute_unreal_handoff(
        job_id: str, target: UnrealTarget,
        source_kind: Literal["skeleton", "character"] = "skeleton",
        sample_index: Annotated[int, Field(ge=0, le=3)] = 0,
        attempt: Annotated[int, Field(ge=0, le=99)] = 0,
    ) -> dict[str, Any]:
        """Import a prepared animation into its exact open Unreal project and verify the saved asset.

        Requires the operator's engine registration and loopback Python remote execution.
        Executes only the bundled handoff script. A completed matching receipt is reused.
        """
        from .unreal_bridge import execute_handoff

        engine = os.environ.get("KIMODO_UNREAL_ENGINE")
        if not engine:
            registration = settings.data_dir / "Unreal" / "engine.json"
            if not registration.is_file() or registration.stat().st_size > 8192:
                raise ValueError("Set KIMODO_UNREAL_ENGINE to the Unreal installation directory")
            engine = json.loads(registration.read_text(encoding="utf-8"))["engine_directory"]
        engine_path = Path(engine)
        if not engine_path.is_absolute():
            raise ValueError("Unreal engine registration must use an absolute path")
        source = handoff_directory(job_id, source_kind, sample_index, attempt)
        key = hashlib.sha256(target.model_dump_json().encode()).hexdigest()[:16]
        script = contained_file(source, f"unreal-{key}/unreal_handoff.py")
        return execute_handoff(script, engine_path, Path(target.project_file))

    @mcp.tool(annotations=READ_ONLY)
    def get_unreal_report(
        job_id: str, target: UnrealTarget,
        source_kind: Literal["skeleton", "character"] = "skeleton",
        sample_index: Annotated[int, Field(ge=0, le=3)] = 0,
        attempt: Annotated[int, Field(ge=0, le=99)] = 0,
    ) -> dict[str, Any]:
        """Read a managed Unreal import's final timing, rig and asset-path verification report."""
        source = handoff_directory(job_id, source_kind, sample_index, attempt)
        key = hashlib.sha256(target.model_dump_json().encode()).hexdigest()[:16]
        directory = (source / f"unreal-{key}").resolve()
        if not directory.is_relative_to(source.resolve()):
            raise ValueError("Unreal handoff directory escapes its managed source")
        path = directory / "unreal-report.json"
        if not path.is_file():
            status = "not_completed" if (directory / "execution.lock").exists() else "not_executed"
            return {"status": status, "report_path": str(path)}
        path = contained_file(directory, "unreal-report.json")
        if path.stat().st_size > 1_000_000:
            raise ValueError("Handoff report exceeds its size limit")
        return json.loads(path.read_text(encoding="utf-8"))

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
            "5. Call inspect_motion, analyze_motion_quality and create_motion_review on each candidate NPZ. "
            "Use compare_motions for matching-rig candidates; inspect contact sliding, sudden changes, "
            "prompt adherence and selected frames before choosing. Keep original samples; diagnostics "
            "do not prove realism or justify blind smoothing. Then get_artifact for exports.\n"
            "6. For a skeleton use prepare_blender_handoff; for a configured fitted character use "
            "prepare_character_handoff. Run the returned trusted script through Blender MCP and check get_handoff_report.\n"
            "7. Import the character mesh into Unreal first, then prepare_unreal_handoff with its exact project, "
            "Skeleton and SkeletalMesh paths. Use execute_unreal_handoff, check get_unreal_report, "
            "then inspect the imported animation through Unreal MCP.\n"
            "Different rigs require retargeting; never attach SOMA animation directly to an unrelated skeleton."
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
