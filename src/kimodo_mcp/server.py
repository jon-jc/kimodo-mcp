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
from pydantic import Field, StrictInt

from .artifacts import create_preview
from .artifacts import inspect_motion as inspect_archive
from .capabilities import (
    SchemaName,
    WorkflowName,
    build_capabilities,
    get_schema_definition,
    get_workflow_definition,
)
from .capacity import capacity_snapshot, editor_connections
from .catalog import list_models
from .config import UPSTREAM_REVISION, Settings
from .handoff import UnrealTarget
from .handoff import prepare_blender_handoff as prepare_blender_files
from .handoff import prepare_character_handoff as prepare_character_files
from .handoff import prepare_unreal_handoff as prepare_unreal_files
from .jobs import contained_file, redact
from .planning import AnimationBrief, AnimationRevision, PoseReference, plan_revision
from .planning import plan_animation as compile_animation_plan
from .quality import analyze_motion, compare_motion
from .review import compare_motion_reviews
from .review import create_motion_review as create_review_file
from .schemas import GenerateRequest, QualityThresholds
from .workspace import Workspace

READ_ONLY = ToolAnnotations(readOnlyHint=True, destructiveHint=False, idempotentHint=True, openWorldHint=False)
LOCAL_WRITE = ToolAnnotations(readOnlyHint=False, destructiveHint=False, openWorldHint=False)
GENERATION = ToolAnnotations(readOnlyHint=False, destructiveHint=False, idempotentHint=False, openWorldHint=True)
CONFIG_WRITE = ToolAnnotations(readOnlyHint=False, destructiveHint=True, idempotentHint=True, openWorldHint=False)
CANCEL = ToolAnnotations(readOnlyHint=False, destructiveHint=True, idempotentHint=True, openWorldHint=False)


def create_server(
    settings: Settings | None = None, port: int = 8767, workspace: Workspace | None = None,
) -> FastMCP:
    settings = settings or Settings.from_env()
    workspace = workspace or Workspace(settings)
    store = workspace.store
    store.recover_stale()
    mcp = FastMCP(
        "Kimodo Motion Studio",
        instructions=(
            "Create and review skeletal animation with a local Kimodo runtime. Start with get_capabilities "
            "and get_workspace_state; get_workflow provides ordered recipes and get_input_schema exposes "
            "the complete typed controls. Use plan_animation then generate_animation for natural briefs; "
            "validate_motion then generate_motion for explicit constraints. Use revise_animation for "
            "preserved revisions and inspect_motion_frames/extract_motion_constraints for actual source poses. "
            "Poll get_job; submission is not completion. Inspect diagnostics and frame reviews before selecting. "
            "Use build_character_preview for the registered character, list_job_outputs for verified exports, "
            "and explicit matching project/rig targets for Unreal. Never blindly retry uncertain editor dispatch. "
            "Discovery does not establish runtime, memory, model access or editor readiness. Independent-worker "
            "jobs survive client disconnects. NPZ coordinates are meters, Y-up, +Z forward, frames zero-based. "
            "This server does not create meshes or automatically fit arbitrary characters."
        ),
        host="127.0.0.1", port=port,
    )

    @mcp.tool(annotations=READ_ONLY)
    async def get_capabilities() -> dict[str, Any]:
        """Discover every registered operation, workflow, input schema, model and implementation boundary."""
        tools = await mcp.list_tools()
        return build_capabilities([
            {"name": tool.name, "description": tool.description or "",
             "annotations": tool.annotations.model_dump(exclude_none=True) if tool.annotations else {}}
            for tool in tools
        ])

    @mcp.tool(annotations=READ_ONLY)
    def get_workflow(workflow: WorkflowName) -> dict[str, Any]:
        """Get an ordered workflow recipe, required inputs, completion evidence and recovery guidance; run nothing."""
        return get_workflow_definition(workflow)

    @mcp.tool(annotations=READ_ONLY)
    def get_input_schema(schema_name: SchemaName) -> dict[str, Any]:
        """Read a complete JSON input schema, including advanced generation and constraint controls."""
        return get_schema_definition(schema_name)

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
            "capacity": capacity_snapshot(),
            "editors": editor_connections(),
        }
        if settings.repo:
            try:
                revision = subprocess.run(
                    ["git", "-C", str(settings.repo), "rev-parse", "HEAD"],
                    stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=5, check=True,
                    creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
                ).stdout.strip()
                result["installed_revision"] = revision
                result["reviewed_revision_matches"] = revision == UPSTREAM_REVISION
            except (OSError, subprocess.SubprocessError):
                result["installed_revision"] = None
        if probe_runtime:
            try:
                probe = subprocess.run(
                    [settings.python, "-m", "kimodo_mcp.worker", "--probe"],
                    env=settings.worker_env(), stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=60,
                    creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
                )
                result["runtime"] = json.loads(redact(probe.stdout))
                result["runtime_exit_code"] = probe.returncode
            except (OSError, subprocess.SubprocessError, ValueError) as exc:
                result["runtime"] = {"ready": False, "error": redact(str(exc))}
        return result

    @mcp.tool(annotations=READ_ONLY)
    def get_workspace_state(
        probe_runtime: bool = False, recent_jobs: Annotated[StrictInt, Field(ge=0, le=20)] = 10,
    ) -> dict[str, Any]:
        """Read shared worker/memory/editor status, character, saved Unreal target and recent job history.

        Editor reachability checks only local TCP listeners. This does not start services, load models,
        verify an open project, or claim that the configured machine has sufficient inference capacity.
        """
        try:
            character = get_character_profile()
        except (ValueError, OSError, KeyError, TypeError) as exc:
            character = {"configured": False, "error": redact(str(exc))[:2000]}
        preview = workspace.character_preview()
        return {
            "health": health_check(probe_runtime), "character": character,
            "unreal_target": workspace.project_target(),
            "jobs": store.list(recent_jobs) if recent_jobs else [],
            "opening_preview": {"path": str(preview), "uri": preview.as_uri(),
                                "source": "operator_registered_preview", "generated_for_current_request": False}
            if preview else None,
            "capabilities_uri": "kimodo://capabilities",
        }

    @mcp.tool(annotations=READ_ONLY)
    def get_unreal_target() -> dict[str, Any]:
        """Read the same locally saved Unreal project, skeleton, mesh and destination used by the studio."""
        target = workspace.project_target()
        return {"configured": target is not None, "target": target}

    @mcp.tool(annotations=CONFIG_WRITE)
    def save_unreal_target(target: UnrealTarget) -> dict[str, Any]:
        """Save explicit Unreal target fields for this workspace; this does not open a project or import assets."""
        return workspace.save_unreal_target(target)

    @mcp.tool(annotations=READ_ONLY)
    def list_job_outputs(job_id: str) -> dict[str, Any]:
        """List verified native, skeleton and fitted-character files with checksums, paths and sample/attempt identity."""
        return workspace.list_job_outputs(job_id)

    @mcp.tool(annotations=LOCAL_WRITE)
    def build_character_preview(
        job_id: str, sample_index: Annotated[StrictInt, Field(ge=0, le=3)] = 0,
        attempt: Annotated[StrictInt, Field(ge=0, le=99)] | None = None,
    ) -> dict[str, Any]:
        """Prepare, execute and verify a registered character preview through the same workflow as the studio.

        Reuses a valid completed preview or chooses an unused attempt under a shared lock. Uncertain
        editor dispatch blocks automatic retries. Requires the supported registered source and Blender.
        """
        return workspace.build_character_preview(
            job_id, sample_index, attempt, prepare=prepare_character_handoff, execute=execute_blender_handoff,
        )

    @mcp.tool(annotations=READ_ONLY)
    def validate_motion(request: GenerateRequest) -> dict[str, Any]:
        """Validate text segments, model compatibility, frame ranges and kinematic constraints offline."""
        return {"valid": True, "plan": request.validated_plan(), "request": request.model_dump(mode="json")}

    @mcp.tool(annotations=GENERATION)
    def generate_motion(request: GenerateRequest) -> dict[str, Any]:
        """Queue motion generation and return a job ID immediately. May download weights if operator enabled it."""
        return store.submit(request.model_dump(mode="json"))

    def compile_brief(brief: AnimationBrief, start_from: PoseReference | None = None) -> dict[str, Any]:
        plan = compile_animation_plan(brief)
        if start_from is not None:
            from .pose import anchor_start

            artifact = motion_artifact(start_from.job_id, f"motion_{start_from.sample_index:02d}.npz")
            request = anchor_start(GenerateRequest.model_validate(plan["request"]),
                                   artifact["absolute_path"], source_frame=start_from.frame)
            plan.update(request=request.model_dump(mode="json"), plan=request.validated_plan(),
                        start_from={**start_from.model_dump(), "source_sha256": artifact["sha256"]})
            plan["notes"].append("Starting pose, hand/foot orientations and heading are anchored to the selected sample. "
                                 "World root position is retained; velocity continuity and seamless blending are not guaranteed.")
        return plan

    @mcp.tool(annotations=READ_ONLY)
    def plan_animation(brief: AnimationBrief, start_from: PoseReference | None = None) -> dict[str, Any]:
        """Turn a natural action description and optional timed beats into an explicit generation plan.

        Choose draft, balanced or detailed effort. Defaults: four seconds, one candidate, seed zero,
        current SOMA model, native postprocessing, NPZ and standard-T-pose BVH. No generation is started.
        """
        return compile_brief(brief, start_from)

    @mcp.tool(annotations=GENERATION)
    def generate_animation(brief: AnimationBrief, start_from: PoseReference | None = None) -> dict[str, Any]:
        """Generate from a plain-language brief with transparent presets; return a durable job immediately."""
        plan = compile_brief(brief, start_from)
        job = store.submit(plan["request"], context={"brief": plan["brief"], "notes": plan["notes"],
                                                    "start_from": plan.get("start_from")})
        return {**job, "plan": plan["plan"], "notes": plan["notes"]}

    @mcp.tool(annotations=GENERATION)
    def revise_animation(job_id: str, revision: AnimationRevision, submit: bool = False) -> dict[str, Any]:
        """Plan or submit a new version while preserving every setting not explicitly changed.

        The assistant supplies the rewritten action, not a vague editing instruction. Timing edits
        never silently retime existing constraints. The original job and outputs remain unchanged.
        """
        original = store.get(job_id)
        plan = plan_revision(original["request"], revision)
        plan["parent_job_id"] = job_id
        if not submit:
            return plan
        context = {"parent_job_id": job_id, "revision": revision.model_dump(mode="json", exclude_none=True),
                   "changes": plan["changes"], "parent_context": {
                       key: value for key, value in original.get("context", {}).items() if key == "brief"
                   }}
        job = store.submit(plan["request"], context=context)
        return {**job, "plan": plan["plan"], "changes": plan["changes"], "notes": plan["notes"]}

    @mcp.tool(annotations=READ_ONLY)
    def get_job(job_id: str) -> dict[str, Any]:
        """Get durable status, current stage, request, and final export manifest."""
        return store.get(job_id)

    @mcp.tool(annotations=READ_ONLY)
    def list_jobs(limit: Annotated[int, Field(ge=1, le=100)] = 20) -> dict[str, Any]:
        """List recent jobs shared by clients configured with the same data directory."""
        return {"jobs": store.list(limit)}

    @mcp.tool(annotations=CANCEL)
    def cancel_job(job_id: str) -> dict[str, Any]:
        """Request cancellation of a queued or running job; completed outputs remain available."""
        return store.cancel(job_id)

    @mcp.tool(annotations=READ_ONLY)
    def get_job_log(job_id: str, lines: Annotated[int, Field(ge=1, le=100)] = 40) -> dict[str, Any]:
        """Read a bounded tail of generation diagnostics with Hugging Face tokens redacted."""
        return {"job_id": job_id, "log": store.log(job_id, lines)}

    @mcp.tool(annotations=READ_ONLY)
    def get_artifact(job_id: str, name: str) -> dict[str, Any]:
        """Resolve and verify an exported file by its exact manifest path, including checksum and local URI."""
        artifact = store.artifact(job_id, name)
        workspace.verified_native_output(job_id, name)
        return artifact

    @mcp.tool(annotations=READ_ONLY)
    def inspect_motion(job_id: str, name: str = "motion_00.npz") -> dict[str, Any]:
        """Measure a completed NPZ: timing, skeleton, root travel, bounds and finite-value checks."""
        artifact = motion_artifact(job_id, name)
        if artifact["format"] != "npz":
            raise ValueError("Inspection requires a native Kimodo NPZ artifact")
        return inspect_archive(artifact["absolute_path"])

    @mcp.tool(annotations=LOCAL_WRITE)
    def preview_motion(job_id: str, name: str = "motion_00.npz") -> dict[str, Any]:
        """Create an offline HTML animation viewer with playback, scrubber and camera controls."""
        artifact = motion_artifact(job_id, name)
        if artifact["format"] != "npz":
            raise ValueError("Preview requires a native Kimodo NPZ artifact")
        target = store.directory(job_id) / (name.removesuffix(".npz") + "_preview.html")
        return create_preview(artifact["absolute_path"], target)

    def motion_artifact(job_id: str, name: str) -> dict[str, Any]:
        artifact = store.artifact(job_id, name)
        if artifact["format"] != "npz":
            raise ValueError("Motion review requires a native Kimodo NPZ artifact")
        workspace.verified_native_output(job_id, name)
        return artifact

    @mcp.tool(annotations=READ_ONLY)
    def inspect_motion_frames(
        job_id: str,
        frame_indices: Annotated[list[Annotated[StrictInt, Field(ge=0, le=2399)]],
                                 Field(min_length=1, max_length=16)],
        sample_index: Annotated[StrictInt, Field(ge=0, le=3)] = 0,
        joint_names: Annotated[list[Annotated[str, Field(min_length=1, max_length=256, strict=True)]],
                               Field(min_length=1, max_length=32)] | None = None,
        include_rotations: Annotated[bool, Field(strict=True)] = False,
    ) -> dict[str, Any]:
        """Read selected source frames as exact joint positions, hierarchy, timing and optional rotation matrices.

        Uses checksum-verified completed motion, not rendered pixels or invented poses. Select up to 16
        ordered frames and optionally named joints. Missing metadata remains explicitly unavailable.
        """
        from .motion_access import inspect_motion_frames as inspect_frames

        artifact = motion_artifact(job_id, f"motion_{sample_index:02d}.npz")
        result = inspect_frames(artifact["absolute_path"], frame_indices=frame_indices,
                                joint_names=joint_names, include_rotations=include_rotations)
        return {**result, "job_id": job_id, "sample_index": sample_index, "source_sha256": artifact["sha256"]}

    @mcp.tool(annotations=READ_ONLY)
    def extract_motion_constraints(
        job_id: str,
        source_frames: Annotated[list[Annotated[StrictInt, Field(ge=0, le=2399)]],
                                 Field(min_length=1, max_length=128)],
        target_frames: Annotated[list[Annotated[StrictInt, Field(ge=0, le=2399)]],
                                 Field(min_length=1, max_length=128)],
        sample_index: Annotated[StrictInt, Field(ge=0, le=3)] = 0,
        constraint_type: Literal["fullbody", "left-hand", "right-hand", "left-foot", "right-foot"] = "fullbody",
    ) -> dict[str, Any]:
        """Map several actual SOMA poses to destination frames as one reusable full-body or limb constraint.

        Both frame lists must be ordered, unique and equally sized. World root coordinates are retained.
        Add the returned constraint to a new request, then validate its duration and all constraint conflicts.
        This reads poses; it does not smooth, resample or splice clips or guarantee seamless transitions.
        """
        from .motion_access import extract_motion_constraints as extract_constraints

        artifact = motion_artifact(job_id, f"motion_{sample_index:02d}.npz")
        model = store.get(job_id)["request"]["model"]
        result = extract_constraints(artifact["absolute_path"], source_frames=source_frames,
                                     target_frames=target_frames, target_model=model, constraint_type=constraint_type)
        return {**result, "source_job_id": job_id, "source_sample": sample_index,
                "source_sha256": artifact["sha256"], "model": model}

    @mcp.tool(annotations=READ_ONLY)
    def extract_pose_constraint(
        job_id: str, source_frame: Annotated[int, Field(ge=0, le=2399)],
        target_frame: Annotated[int, Field(ge=0, le=2399)] = 0,
        sample_index: Annotated[int, Field(ge=0, le=3)] = 0,
        constraint_type: Literal["fullbody", "left-hand", "right-hand", "left-foot", "right-foot"] = "fullbody",
    ) -> dict[str, Any]:
        """Reuse an actual SOMA pose as a constraint for a new generation, preserving its world position.

        Returns a constraint to add to GenerateRequest.constraints. Validate the full new request;
        pose anchoring does not guarantee terminal-joint twists, velocity continuity or a seamless loop.
        """
        from .pose import extract_pose_constraint as extract_pose

        artifact = motion_artifact(job_id, f"motion_{sample_index:02d}.npz")
        model = store.get(job_id)["request"]["model"]
        constraint = extract_pose(artifact["absolute_path"], source_frame=source_frame,
                                  target_frame=target_frame, target_model=model, constraint_type=constraint_type)
        return {"constraint": constraint.model_dump(mode="json"), "model": model,
                "source_job_id": job_id, "source_sample": sample_index, "source_frame": source_frame,
                "source_sha256": artifact["sha256"],
                "notes": ["World root position is retained; coordinates are meters with Y up.",
                          "Validate destination duration and other constraints before generating."]}

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

    @mcp.tool(annotations=LOCAL_WRITE)
    def execute_blender_handoff(
        job_id: str, source_kind: Literal["skeleton", "character"] = "character",
        sample_index: Annotated[int, Field(ge=0, le=3)] = 0,
        attempt: Annotated[int, Field(ge=0, le=99)] = 0,
    ) -> dict[str, Any]:
        """Run only the prepared fixed script in the connected local Blender and verify its exports.

        A completed receipt is reused. After an uncertain dispatch inspect the receipt before a new attempt.
        Character handoffs include an animated GLB for the local studio viewer.
        """
        from .blender_bridge import execute_handoff

        directory = handoff_directory(job_id, source_kind, sample_index, attempt)
        script = contained_file(directory, "character_handoff.py" if source_kind == "character" else "blender_handoff.py")
        return execute_handoff(script, source_kind)

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
            status = "not_completed" if any((directory / name).exists() for name in
                                            ("execution.lock", "bridge-dispatch.json")) else "not_executed"
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

    @mcp.resource("kimodo://models", name="Supported Kimodo models", mime_type="application/json",
                  description="Exact model identifiers, skeletons, native formats and reviewed upstream revision.")
    def model_resource() -> str:
        return json.dumps(list_models(), indent=2)

    @mcp.resource("kimodo://jobs/{job_id}", name="Animation job", mime_type="application/json",
                  description="Saved request, lineage, status, progress and completed manifest for a managed job.")
    def job_resource(job_id: str) -> str:
        return json.dumps(store.get(job_id), indent=2)

    @mcp.resource("kimodo://request-schema", name="Generation request schema", mime_type="application/json",
                  description="Complete structured generation contract, including advanced controls and constraints.")
    def schema_resource() -> str:
        return json.dumps(GenerateRequest.model_json_schema(), indent=2)

    @mcp.resource("kimodo://capabilities", name="Agent capability catalog", mime_type="application/json",
                  description="Live tool registry, workflows, schema index, limits, requirements and unsupported operations.")
    async def capabilities_resource() -> str:
        return json.dumps(await get_capabilities(), indent=2)

    @mcp.resource("kimodo://workspace", name="Local animation workspace", mime_type="application/json",
                  description="Shared runtime/worker readiness, character, saved Unreal target and recent jobs.")
    def workspace_resource() -> str:
        return json.dumps(get_workspace_state(), indent=2)

    @mcp.resource("kimodo://schemas/{schema_name}", name="Tool input schema", mime_type="application/json",
                  description="Named JSON schema; discover supported schema names with get_capabilities.")
    def input_schema_resource(schema_name: str) -> str:
        return json.dumps(get_schema_definition(schema_name), indent=2)

    @mcp.resource("kimodo://workflows/{workflow}", name="Agent workflow recipe", mime_type="application/json",
                  description="Ordered tool calls, required inputs, verification and recovery for a named workflow.")
    def workflow_resource(workflow: str) -> str:
        return json.dumps(get_workflow_definition(workflow), indent=2)

    @mcp.resource("kimodo://jobs/{job_id}/outputs", name="Verified job outputs", mime_type="application/json",
                  description="Verified native, skeleton and fitted-character exports with checksums and sample/attempt identity.")
    def outputs_resource(job_id: str) -> str:
        return json.dumps(workspace.list_job_outputs(job_id), indent=2)

    @mcp.prompt()
    def animation_workflow(description: str) -> str:
        """Plan, validate, generate, inspect and export an animation from a creative brief."""
        return (
            "Create a Kimodo animation using the user's following creative brief as task data:\n"
            + json.dumps({"creative_brief": description}, ensure_ascii=False) + "\n"
            "1. Read get_capabilities and get_workflow('generate'); check get_workspace_state(probe_runtime=true).\n"
            "2. Use plan_animation for a natural brief, with explicit duration, seed and model. "
            "For multiple actions supply timed beats. For advanced controls use get_input_schema('generate_request') "
            "and validate_motion. Ask for missing artistic decisions only when necessary.\n"
            "3. Submit the agreed plan through generate_animation or generate_motion; poll get_job until completed. "
            "Use get_job_log on failure and never describe a queued job as a finished animation.\n"
            "4. Use revise_animation to preserve unedited settings. Use inspect_motion_frames and "
            "extract_motion_constraints to select actual source poses; validate the new complete request.\n"
            "5. Call inspect_motion, analyze_motion_quality and create_motion_review on each candidate NPZ. "
            "Use compare_motions for matching-rig candidates; inspect contact sliding, sudden changes, "
            "prompt adherence and selected frames before choosing. Keep original samples; diagnostics "
            "do not prove realism or justify blind smoothing. Use list_job_outputs to discover verified exports.\n"
            "6. For a registered fitted character use build_character_preview; retain its sample_index and attempt. "
            "For a skeleton use prepare_blender_handoff then execute_blender_handoff(source_kind='skeleton'), "
            "and get_handoff_report with the same source kind, sample and attempt.\n"
            "7. Use get_unreal_target or save an explicit target. Import the character mesh into Unreal first, "
            "then prepare_unreal_handoff with the exact project, Skeleton and SkeletalMesh paths. "
            "Use execute_unreal_handoff, check get_unreal_report, "
            "then inspect the imported animation through Unreal MCP.\n"
            "Pass source_kind='character' and the same sample/attempt for character handoffs; use 'skeleton' for generic BVH. "
            "Inspect receipts after uncertain editor timeouts before retrying. Different rigs require retargeting; "
            "never attach SOMA animation directly to an unrelated skeleton."
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
