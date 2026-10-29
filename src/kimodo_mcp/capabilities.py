"""Runtime-free capability discovery and declarative recipes for MCP clients.

Recipes are documentation. Reference strings describe where the assistant obtains
arguments; this module never resolves them or dispatches a tool.
"""

from __future__ import annotations

from copy import deepcopy
from typing import Any, Literal, get_args

from .catalog import UPSTREAM_REVISION, list_models
from .handoff import UnrealTarget
from .planning import QUALITY_STEPS, AnimationBrief, AnimationRevision, PoseReference
from .schemas import GenerateRequest, QualityThresholds

WorkflowName = Literal[
    "generate", "revise", "reuse-pose", "inspect", "compare", "export-motion", "export-skeleton",
    "export-character", "import-unreal", "recover-job",
]
SchemaName = Literal[
    "generate_request", "animation_brief", "animation_revision", "pose_reference",
    "quality_thresholds", "unreal_target",
]
WORKFLOW_IDS = get_args(WorkflowName)
SCHEMA_IDS = get_args(SchemaName)

_SCHEMAS = {
    "generate_request": GenerateRequest,
    "animation_brief": AnimationBrief,
    "animation_revision": AnimationRevision,
    "pose_reference": PoseReference,
    "quality_thresholds": QualityThresholds,
    "unreal_target": UnrealTarget,
}

_GROUPS = {
    "discovery": (
        "Discover capabilities and workspace readiness",
        ("get_capabilities", "get_workflow", "get_input_schema", "list_models", "health_check",
         "get_workspace_state", "get_character_profile", "get_unreal_target"),
    ),
    "generation": (
        "Plan, generate and revise motion",
        ("plan_animation", "generate_animation", "revise_animation", "validate_motion", "generate_motion"),
    ),
    "jobs": ("Track and recover jobs", ("get_job", "list_jobs", "cancel_job", "get_job_log")),
    "inspection": (
        "Inspect, constrain and compare actual motion",
        ("inspect_motion", "inspect_motion_frames", "extract_pose_constraint", "extract_motion_constraints",
         "preview_motion", "analyze_motion_quality", "create_motion_review", "compare_motions"),
    ),
    "outputs": ("Export and locate verified outputs", ("get_artifact", "list_job_outputs", "export_motion")),
    "blender": (
        "Export skeletons and the registered fitted character",
        ("prepare_blender_handoff", "prepare_character_handoff", "execute_blender_handoff",
         "get_handoff_report", "build_character_preview"),
    ),
    "unreal": (
        "Save an explicit target and import matching animation",
        ("save_unreal_target", "prepare_unreal_handoff", "execute_unreal_handoff", "get_unreal_report"),
    ),
}


def _step(
    step_id: str, tool: str, expected: str, *, arguments: dict | None = None,
    arguments_from: dict | None = None, on_failure: str = "Inspect the error before continuing.",
    when: str | None = None,
) -> dict:
    value = {
        "id": step_id, "tool": tool, "arguments": arguments or {},
        "arguments_from": arguments_from or {}, "expected": expected, "on_failure": on_failure,
    }
    if when:
        value["when"] = when
    return value


def _completed_job() -> dict:
    return _step(
        "job", "get_job", "status is completed and the requested sample exists in the manifest.",
        arguments_from={"job_id": "input.job_id"},
        on_failure="For queued/running/cancelling status, poll. For terminal failure, use recover-job.",
    )


def _handoff_args(source_kind: str) -> tuple[dict, dict]:
    return ({"source_kind": source_kind}, {
        "job_id": "input.job_id", "sample_index": "input.sample_index", "attempt": "input.attempt",
    })


_RECIPES = {
    "generate": {
        "title": "Describe, plan, generate and review",
        "inputs": {"brief": "animation_brief", "start_from": "Optional pose_reference; default null."},
        "requirements": ["Independent worker available; model access and memory sufficient before submission."],
        "steps": [
            _step("workspace", "get_workspace_state", "Worker, memory advisory and configured assets are visible.",
                  arguments={"probe_runtime": True, "recent_jobs": 0},
                  on_failure="Operator resolves runtime, worker, model access or capacity before generation."),
            _step("models", "list_models", "The brief uses an exact supported model and compatible formats."),
            _step("plan", "plan_animation", "Explicit request, quantized timing and notes are reviewed; no job exists.",
                  arguments_from={"brief": "input.brief", "start_from": "input.start_from"}),
            _step("submit", "generate_animation", "A durable job id is returned; this is not completed animation.",
                  arguments_from={"brief": "input.brief", "start_from": "input.start_from"},
                  on_failure="Do not blindly resubmit after an uncertain response; inspect recent jobs first."),
            _step("job", "get_job", "Poll until completed; retain the exact request and manifest.",
                  arguments_from={"job_id": "steps.submit.id"}, on_failure="Use recover-job on terminal failure."),
            _step("review", "create_motion_review", "Inspect the real motion before selecting; repeat for each sample.",
                  arguments={"name": "motion_00.npz"}, arguments_from={"job_id": "steps.submit.id"}),
            _step("outputs", "list_job_outputs", "Verified files and their provenance are available.",
                  arguments_from={"job_id": "steps.submit.id"}),
        ],
        "completion": "A completed job plus inspected candidate; a plan or queued job is not an animation.",
        "notes": [
            "Provide explicit timed beats for actions longer than ten seconds; the assistant interprets intent.",
            "For advanced controls, edit the planned GenerateRequest, call validate_motion, then generate_motion.",
            "Changing seed produces an alternative; effort presets do not guarantee better realism.",
        ],
    },
    "revise": {
        "title": "Revise while preserving unmentioned settings",
        "inputs": {"job_id": "Original job id.", "revision": "animation_revision"},
        "requirements": ["Supply a rewritten action or explicit replacement beats, not a vague editing instruction."],
        "steps": [
            _step("original", "get_job", "Original request, constraints and lineage are known.",
                  arguments_from={"job_id": "input.job_id"}),
            _step("plan", "revise_animation", "Changed fields and preserved settings are visible; no new job.",
                  arguments={"submit": False},
                  arguments_from={"job_id": "input.job_id", "revision": "input.revision"}),
            _step("submit", "revise_animation", "New job records parent lineage; original outputs remain intact.",
                  arguments={"submit": True},
                  arguments_from={"job_id": "input.job_id", "revision": "input.revision"}),
            _step("job", "get_job", "New job completes before review.", arguments_from={"job_id": "steps.submit.id"}),
            _step("review", "create_motion_review", "Review the revised performance; use compare for compatible clips.",
                  arguments={"name": "motion_00.npz"}, arguments_from={"job_id": "steps.submit.id"}),
        ],
        "completion": "Completed revision with its parent id, explicit changes and actual motion review.",
        "notes": ["Timing edits never silently retime constraints; clear them explicitly or build a new request.",
                  "For an exact retry, use the saved request unchanged with generate_motion."],
    },
    "reuse-pose": {
        "title": "Anchor a new animation to a verified source pose",
        "inputs": {"job_id": "Completed SOMA source job.", "sample_index": "0..3, actual existing sample.",
                   "source_frame": "Zero-based source frame.", "brief": "New animation_brief.",
                   "start_from": "pose_reference using the same job_id, sample_index and source_frame as frame."},
        "requirements": ["Source is a canonical SOMA30 or SOMA77 NPZ with local rotations."],
        "steps": [
            _completed_job(),
            _step("source", "inspect_motion_frames", "Inspect the exact selected source frame and joint values.",
                  arguments={"include_rotations": True},
                  arguments_from={"job_id": "input.job_id", "sample_index": "input.sample_index",
                                  "frame_indices": "[input.source_frame]"}),
            _step("plan", "plan_animation", "Source pose, hand/foot orientations and heading anchor destination frame 0.",
                  arguments_from={"brief": "input.brief", "start_from": "input.start_from"}),
            _step("submit", "generate_animation", "New job retains source reference and checksum.",
                  arguments_from={"brief": "input.brief", "start_from": "input.start_from"}),
            _step("job_result", "get_job", "Destination job completes before checking its starting pose.",
                  arguments_from={"job_id": "steps.submit.id"}),
            _step("review", "create_motion_review", "Review the start and nearby motion for the intended transition.",
                  arguments={"name": "motion_00.npz"}, arguments_from={"job_id": "steps.submit.id"}),
        ],
        "completion": "Completed destination motion reviewed against the intended source pose.",
        "notes": [
            "World root position is retained; velocity continuity, terminal twist and seamless blending are not promised.",
            "Use extract_pose_constraint for one key, or extract_motion_constraints for explicit source/target frame lists; "
            "append returned constraints to GenerateRequest, validate_motion, then generate_motion.",
        ],
    },
    "inspect": {
        "title": "Inspect actual data, selected frames and diagnostics",
        "inputs": {"job_id": "Completed job id.", "name": "Exact NPZ manifest name, e.g. motion_00.npz.",
                   "sample_index": "The sample identified by name.", "frame_indices": "1..16 source frames."},
        "requirements": ["Use an existing completed sample; do not infer motion from its prompt alone."],
        "steps": [
            _completed_job(),
            _step("outputs", "list_job_outputs", "Verified output identities and checksums are visible.",
                  arguments_from={"job_id": "input.job_id"}),
            _step("summary", "inspect_motion", "Timing, skeleton, bounds and root travel are known.",
                  arguments_from={"job_id": "input.job_id", "name": "input.name"}),
            _step("frames", "inspect_motion_frames", "Bounded exact-frame numeric data is returned without resampling.",
                  arguments={"include_rotations": False},
                  arguments_from={"job_id": "input.job_id", "sample_index": "input.sample_index",
                                  "frame_indices": "input.frame_indices"}),
            _step("quality", "analyze_motion_quality", "Metric values, availability and worst frames inform review.",
                  arguments_from={"job_id": "input.job_id", "name": "input.name"}),
            _step("review", "create_motion_review", "Open the offline player and labeled contact sheet.",
                  arguments_from={"job_id": "input.job_id", "name": "input.name"}),
        ],
        "completion": "Numeric findings and visual observations are distinguished from assumptions.",
        "notes": ["Metrics do not rank realism or prompt adherence and never repair the source.",
                  "The skeleton review cannot establish skinning, cloth, collision or gameplay quality."],
    },
    "compare": {
        "title": "Compare candidates with explicit time alignment",
        "inputs": {"job_id": "Completed first job.", "other_job_id": "Completed second job, possibly the same job.",
                   "name": "First sample NPZ name.", "other_name": "Second sample NPZ name.",
                   "offset_frames": "Integer offset, default 0.", "space": "world or root_relative."},
        "requirements": ["Both samples have matching FPS, skeleton, joint names and hierarchy, and overlapping frames."],
        "steps": [
            _completed_job(),
            _step("other", "get_job", "Second job is completed and requested sample exists.",
                  arguments_from={"job_id": "input.other_job_id"}),
            _step("comparison", "compare_motions", "Synchronized views and measured differences use the requested alignment.",
                  arguments_from={key: f"input.{key}" for key in
                                  ("job_id", "other_job_id", "name", "other_name", "offset_frames", "space")}),
        ],
        "completion": "Both candidates are visually reviewed; selection records a reason, not an automatic score.",
        "notes": ["B frame = A frame + offset_frames. Root-relative removes translation only, retaining heading and scale.",
                  "Start in world space to see drift; comparison does not retarget, resample or blend either source."],
    },
    "export-motion": {
        "title": "Export completed motion without inference",
        "inputs": {
            "job_id": "Completed job containing the selected native NPZ.",
            "sample_index": "Existing source sample, default 0.",
            "name": "Exact NPZ manifest name matching sample_index, e.g. motion_00.npz.",
            "format": "bvh for SOMA (default), amass for SMPL-X, or csv for G1.",
            "bvh_standard_tpose": "Boolean; default false. Change only for BVH.",
            "amass_z_up": "Boolean; default true. Change only for AMASS.",
            "mujoco_rest_zero": "Boolean; default false. Change only for CSV.",
            "attempt": "Explicit unused handoff attempt, only for the optional Blender branch.",
        },
        "requirements": [
            "Completed compatible managed NPZ with explicit model, FPS, canonical hierarchy and proper local rotations.",
            "Configured Kimodo/PyTorch export runtime and canonical skeleton assets; inference weights and generation worker are unnecessary.",
        ],
        "steps": [
            _completed_job(),
            _step("source", "inspect_motion", "Confirm source timing and skeleton before choosing the compatible format.",
                  arguments_from={"job_id": "input.job_id", "name": "input.name"}),
            _step("export", "export_motion", "A completed receipt verifies source, options, timing and exported bytes.",
                  arguments_from={key: f"input.{key}" for key in
                                  ("job_id", "sample_index", "format", "bvh_standard_tpose",
                                   "amass_z_up", "mujoco_rest_zero")},
                  on_failure="Inspect the managed export error/log. Incomplete attempts are preserved; do not blindly rerun."),
            _step("outputs", "list_job_outputs", "Find the verified derived file matching the returned export_id and sample.",
                  arguments_from={"job_id": "input.job_id"}),
            _step("prepare", "prepare_blender_handoff", "The exact derived BVH is bound to a managed skeleton handoff.",
                  arguments_from={"job_id": "input.job_id", "sample_index": "input.sample_index",
                                  "attempt": "input.attempt", "export_id": "steps.export.export_id"},
                  when="Format is bvh and the user intends a Blender skeleton handoff in a compatible open session."),
            _step("execute", "execute_blender_handoff", "Separate skeleton FBX and .blend exports are verified.",
                  arguments={"source_kind": "skeleton"}, arguments_from=_handoff_args("skeleton")[1],
                  when="The optional derived-BVH preparation succeeded and editor execution is intended.",
                  on_failure="Read the matching handoff report and inspect Blender before choosing another attempt."),
            _step("report", "get_handoff_report", "The optional skeleton handoff completed for the same sample and attempt.",
                  arguments={"source_kind": "skeleton"}, arguments_from=_handoff_args("skeleton")[1],
                  when="The optional Blender execution was dispatched."),
        ],
        "completion": "Verified interchange output; optionally a verified Blender receipt. No new motion was generated.",
        "notes": [
            "Defaults differ from natural-brief generation: standalone BVH export uses BONES-SEED rest unless standard T-pose is requested.",
            "Unchanged non-applicable option defaults are allowed; changing an option for another format is rejected.",
            "Identical source/settings reuse a reverified receipt. Changed options get a distinct export; originals stay unchanged.",
            "SOMA30 hand expansion, AMASS default parameters and G1 joint projection are representation changes, not a lossless claim.",
            "Export runs offline on CPU without inference, resampling, postprocessing or arbitrary file import; CSV is not robot safety validation.",
            "The optional handoff attempt is distinct from export_id; an occupied attempt with another source is not overwritten.",
        ],
    },
    "export-skeleton": {
        "title": "Export a SOMA skeleton through Blender",
        "inputs": {"job_id": "Completed SOMA job with BVH export.", "sample_index": "Existing sample, default 0.",
                   "attempt": "Explicit matching preparation/execution/report attempt, default 0."},
        "requirements": ["Compatible local Blender addon is running; use a fresh session without an existing KimodoRig."],
        "steps": [
            _completed_job(),
            _step("prepare", "prepare_blender_handoff", "Fixed managed script is prepared; no editor export yet.",
                  arguments_from=_handoff_args("skeleton")[1]),
            _step("execute", "execute_blender_handoff", "Verified skeleton .blend and FBX exports are returned.",
                  arguments=_handoff_args("skeleton")[0], arguments_from=_handoff_args("skeleton")[1],
                  on_failure="Read get_handoff_report for the same job/sample/source_kind/attempt before retrying."),
            _step("report", "get_handoff_report", "Completed receipt agrees with the prepared sample and exports.",
                  arguments=_handoff_args("skeleton")[0], arguments_from=_handoff_args("skeleton")[1]),
        ],
        "completion": "A verified Blender execution receipt, not just a generated script.",
        "notes": ["BVH is centimeters; the fixed importer applies scale 0.01. Native NPZ is meters.",
                  "SOMA skeleton output does not create a skinned character or retarget an unrelated rig."],
    },
    "export-character": {
        "title": "Build or reuse the registered fitted-character preview",
        "inputs": {"job_id": "Completed SOMA job.", "sample_index": "Existing sample, default 0."},
        "requirements": ["Supported source FBX is operator-registered with its exact checksum; Blender is connected."],
        "steps": [
            _completed_job(),
            _step("profile", "get_character_profile", "The configured source matches the supported fitted profile."),
            _step("build", "build_character_preview", "A valid completed preview is reused or an unused attempt is exported.",
                  arguments_from={"job_id": "input.job_id", "sample_index": "input.sample_index"},
                  on_failure="If dispatch is uncertain, inspect the reported attempt receipt; do not force a fresh attempt."),
            _step("report", "get_handoff_report", "The character receipt verifies actual exported assets.",
                  arguments={"source_kind": "character"},
                  arguments_from={"job_id": "input.job_id", "sample_index": "input.sample_index",
                                  "attempt": "steps.build.attempt"}),
            _step("outputs", "list_job_outputs", "Choose the matching sample and attempt when opening/downloading exports.",
                  arguments_from={"job_id": "input.job_id"}),
        ],
        "completion": "Verified animated GLB and character exports; inspect the rendered deformation.",
        "notes": ["This is an asset-specific 24-bone fit; arbitrary auto-rigging, face/finger animation and cloth physics are unsupported.",
                  "Manual control remains available with prepare_character_handoff and execute_blender_handoff.",
                  "A registered opening preview is an existing asset, not evidence of new generation."],
    },
    "import-unreal": {
        "title": "Import verified animation into an explicit matching Unreal rig",
        "inputs": {"job_id": "Completed source job.", "sample_index": "Existing sample index.",
                   "source_kind": "skeleton or character; use the exact completed Blender source.",
                   "attempt": "The same completed Blender attempt.", "target": "unreal_target"},
        "requirements": [
            "The matching skeletal mesh and Skeleton are already imported; importing the initial mesh is a separate editor task.",
            "Intended Unreal project is open, engine is registered, and loopback Python remote execution is enabled.",
        ],
        "steps": [
            _step("saved_target", "get_unreal_target", "Inspect existing target; use it only if it matches the intended project and rig."),
            _step("save_target", "save_unreal_target", "Explicit target fields are saved; no project is opened or asset imported.",
                  arguments_from={"target": "input.target"}, when="The operator intends to replace the saved workspace target."),
            _step("source_report", "get_handoff_report", "Completed Blender receipt provides the correct verified FBX.",
                  arguments_from={key: f"input.{key}" for key in ("job_id", "sample_index", "source_kind", "attempt")}),
            _step("prepare", "prepare_unreal_handoff", "Fixed import script is prepared for the explicit target.",
                  arguments_from={key: f"input.{key}" for key in
                                  ("job_id", "target", "sample_index", "source_kind", "attempt")}),
            _step("execute", "execute_unreal_handoff", "Exact project/rig checks pass and a new animation asset is saved.",
                  arguments_from={key: f"input.{key}" for key in
                                  ("job_id", "target", "sample_index", "source_kind", "attempt")},
                  on_failure="Inspect get_unreal_report for identical target and source arguments before retrying."),
            _step("report", "get_unreal_report", "Saved asset, sample count, timing, bones and animation tracks are verified.",
                  arguments_from={key: f"input.{key}" for key in
                                  ("job_id", "target", "sample_index", "source_kind", "attempt")}),
        ],
        "completion": "Completed managed receipt and editor inspection of the saved animation in the intended project.",
        "notes": ["Never attach SOMA motion directly to Manny or another unrelated skeleton; use explicit retargeting first.",
                  "TCP reachability alone does not identify the open project. Existing destination assets are not overwritten."],
    },
    "recover-job": {
        "title": "Diagnose, cancel or deliberately retry a job",
        "inputs": {"job_id": "Existing job id."},
        "requirements": ["Determine current state before cancelling or submitting another request."],
        "steps": [
            _step("job", "get_job", "Status, stage, saved request and context identify what actually happened.",
                  arguments_from={"job_id": "input.job_id"}),
            _step("log", "get_job_log", "Read bounded diagnostics; avoid guessing from elapsed time alone.",
                  arguments={"lines": 40}, arguments_from={"job_id": "input.job_id"}),
            _step("workspace", "get_workspace_state", "Worker and capacity are checked before another attempt.",
                  arguments={"recent_jobs": 10}),
            _step("cancel", "cancel_job", "Cancellation is requested; poll until terminal, without deleting completed artifacts.",
                  arguments_from={"job_id": "input.job_id"},
                  when="The user wants to stop a queued or running job."),
            _step("cancel_status", "get_job", "Poll until cancellation reaches a terminal state.",
                  arguments_from={"job_id": "input.job_id"}, when="Cancellation was requested."),
            _step("validate", "validate_motion", "Saved request still validates before an exact retry.",
                  arguments_from={"request": "steps.job.request"},
                  when="Failure cause is resolved and the user wants an exact retry."),
            _step("retry", "generate_motion", "New job id is recorded; previous job and outputs remain available.",
                  arguments_from={"request": "steps.job.request"},
                  when="The exact retry is intended and validate succeeded.",
                  on_failure="Inspect recent jobs after an uncertain submission; avoid duplicate work."),
            _step("retry_status", "get_job", "Poll the new job to completion or explain its terminal failure.",
                  arguments_from={"job_id": "steps.retry.id"}, when="The retry returned a job id."),
        ],
        "completion": "Failure is explained, cancellation reaches a terminal state, or the deliberate retry is tracked to completion.",
        "notes": ["Service startup, credentials, dependency installation and download permission are operator setup tasks.",
                  "Use revise_animation for an intentional change, rather than reconstructing a request from remembered defaults."],
    },
}


def get_schema_definition(name: str) -> dict[str, Any]:
    """Return a fresh domain JSON Schema; reject arbitrary resource or file names."""
    if name not in _SCHEMAS:
        raise ValueError(f"Unknown input schema. Choose one of: {', '.join(SCHEMA_IDS)}")
    return _SCHEMAS[name].model_json_schema()


def get_workflow_definition(workflow: str) -> dict[str, Any]:
    """Return guidance only; reference strings are never evaluated or dispatched."""
    if workflow not in _RECIPES:
        raise ValueError(f"Unknown workflow. Choose one of: {', '.join(WORKFLOW_IDS)}")
    return {
        "schema_version": 1, "id": workflow, "executes_actions": False,
        "argument_convention": (
            "arguments contains literal tool arguments. arguments_from maps argument names to input or prior-step "
            "result references. Brackets mean construct a list. These are instructions for the client, not executable code. "
            "Omit absent optional inputs; use one consistent sample, source_kind and attempt throughout a handoff."
        ),
        **deepcopy(_RECIPES[workflow]),
    }


def build_capabilities(tool_metadata: list[dict]) -> dict[str, Any]:
    """Describe exactly the registered tools supplied by the server, without probing the host."""
    by_name = {}
    for tool in tool_metadata:
        name = tool["name"]
        if name in by_name:
            raise ValueError(f"Duplicate tool metadata: {name}")
        by_name[name] = {
            "name": name, "description": str(tool.get("description") or "").strip().split("\n\n", 1)[0],
            "annotations": deepcopy(tool.get("annotations") or {}),
        }
    groups, assigned = [], set()
    for group_id, (title, names) in _GROUPS.items():
        present = [name for name in names if name in by_name]
        if present:
            groups.append({"id": group_id, "title": title, "tools": present})
            assigned.update(present)
    if extra := sorted(by_name.keys() - assigned):
        groups.append({"id": "other", "title": "Additional registered operations", "tools": extra})
    return {
        "schema_version": 1, "reviewed_revision": UPSTREAM_REVISION,
        "tool_count": len(by_name), "tools": [by_name[name] for name in sorted(by_name)], "tool_groups": groups,
        "models": list_models(),
        "schemas": [{"id": name, "title": model.__name__, "uri": f"kimodo://schemas/{name}"}
                    for name, model in _SCHEMAS.items()],
        "workflows": [{"id": name, "title": recipe["title"], "uri": f"kimodo://workflows/{name}"}
                      for name, recipe in _RECIPES.items()],
        "entry_points": {"capabilities": "get_capabilities", "readiness": "get_workspace_state",
                         "natural_brief": "plan_animation", "advanced_request": "validate_motion",
                         "schema": "get_input_schema", "workflow": "get_workflow"},
        "coordinates": {"native_units": "meters", "up": "+Y", "forward": "+Z", "ground_plane": "XZ",
                        "frame_index_base": 0, "nominal_fps": 30, "bvh_units": "centimeters",
                        "timing": "N samples play for N/fps seconds; the first-to-last key span is (N-1)/fps."},
        "limits": {"segments": 8, "segment_duration_seconds": [0.2, 10.0], "total_frames": 2400,
                   "samples": 4, "sampling_steps": 100, "constraint_objects": 32,
                   "constrained_frame_entries": 512, "inspection_frames": 16,
                   "inspection_named_joints": 32, "inspection_joint_poses": 2048,
                   "inspection_joint_poses_with_rotations": 256,
                   "extracted_constraint_frames": 128, "sample_index": [0, 3], "handoff_attempt": [0, 99]},
        "defaults": {
            "natural_brief": {"duration_seconds": 4, "model": "kimodo-soma-rp-v1.2", "candidates": 1,
                              "seed": 0, "quality": "balanced", "effort_steps": dict(QUALITY_STEPS),
                              "formats": "Selected model's supported formats", "bvh_standard_tpose": True},
            "advanced_request": {"seed": 0, "samples": 1, "steps": 50, "formats": ["npz"],
                                 "bvh_standard_tpose": False},
            "completed_motion_export": {"format": "bvh", "sample_index": 0, "bvh_standard_tpose": False,
                                        "amass_z_up": True, "mujoco_rest_zero": False},
        },
        "completed_motion_exports": {
            "tool": "export_motion", "requires_completed_managed_npz": True,
            "formats": {"SOMA": "bvh", "SMPLX": "amass", "G1": "csv"},
            "options": {"bvh": ["bvh_standard_tpose"], "amass": ["amass_z_up"], "csv": ["mujoco_rest_zero"]},
            "new_inference": False, "downloads": False, "resampling": False,
            "reuse": "Source, options, receipt and exported file are verified before reusing the same export_id.",
            "blender": "Pass a returned BVH export_id to prepare_blender_handoff to select that derived source explicitly.",
        },
        "runtime_requirements": [
            "Discovery, schema access and planning require no model loading or downloads.",
            "Generation requires the separately installed runtime, model access and sufficient memory; default service mode needs an independent worker.",
            "Downloads default offline; only operator configuration enables them. MCP does not install dependencies, authenticate or start services.",
            "Editor reachability is only a local TCP check; it does not verify scene, project, rig or available memory.",
            "The Blender and Unreal fixed-script execution paths require their configured local editor integrations.",
            "Completed-motion conversion needs the export runtime and skeleton assets, but no inference weights, generation worker or editor; downloads are disabled.",
        ],
        "behavior": [
            "A returned job id means submission. Poll get_job until completed before claiming animation exists.",
            "Inspect actual frames and reviews; diagnostic values are not a realism score or automatic winner.",
            "Use manifest names and verified output metadata, not invented paths. Native files, derived previews and operator-registered previews have different provenance.",
            "Save_unreal_target changes workspace configuration; editor execution writes new assets. Check receipts before retrying uncertain dispatch.",
            "The studio and MCP share job state, target settings and character-preview execution. Viewer camera/playback controls belong to the browser interface.",
        ],
        "unsupported": [
            "Mesh generation or arbitrary character auto-rigging; the supplied character fit is asset-specific.",
            "Facial animation, independent finger animation, cloth/hair physics or collision simulation in the fitted-character workflow.",
            "Automatic cross-rig retargeting, seamless stitching, velocity matching, arbitrary resampling or motion repair.",
            "Initial Unreal skeletal-mesh import through this server; use the editor's import tools first.",
            "Arbitrary external file import/conversion, training, benchmark execution, video/audio rendering; compatible completed managed NPZ export is supported.",
            "Arbitrary code execution or file read/write, dependency installation or worker/editor startup; explicit Unreal target paths are a bounded import exception.",
            "Public remote hosting or multi-user authorization.",
        ],
    }
