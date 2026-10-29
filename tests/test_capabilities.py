import json
import subprocess
import sys
from typing import get_args

import pytest

from kimodo_mcp.capabilities import (
    SCHEMA_IDS,
    WORKFLOW_IDS,
    SchemaName,
    WorkflowName,
    build_capabilities,
    get_schema_definition,
    get_workflow_definition,
)
from kimodo_mcp.config import Settings
from kimodo_mcp.handoff import UnrealTarget
from kimodo_mcp.planning import AnimationBrief, AnimationRevision, PoseReference
from kimodo_mcp.schemas import GenerateRequest, QualityThresholds
from kimodo_mcp.server import create_server


def test_manifest_uses_actual_registry_without_inventing_or_omitting_tools(tmp_path):
    server = create_server(Settings(tmp_path, sys.executable))
    tools = server._tool_manager.list_tools()
    metadata = [{"name": tool.name, "description": tool.description,
                 "annotations": tool.annotations.model_dump(exclude_none=True)} for tool in tools]
    manifest = build_capabilities(metadata)
    names = {tool.name for tool in tools}
    grouped = [name for group in manifest["tool_groups"] for name in group["tools"]]
    assert manifest["tool_count"] == len(names)
    assert set(grouped) == names == {tool["name"] for tool in manifest["tools"]}
    assert len(grouped) == len(set(grouped))
    assert all(group["id"] != "other" for group in manifest["tool_groups"])
    assert {entry["id"] for entry in manifest["schemas"]} == set(SCHEMA_IDS)
    assert {entry["id"] for entry in manifest["workflows"]} == set(WORKFLOW_IDS)
    json.dumps(manifest, allow_nan=False)


def test_manifest_descriptions_and_annotations_are_copied_not_reinterpreted():
    metadata = [{"name": "future_tool", "description": "First paragraph.\n\nFurther details.",
                 "annotations": {"readOnlyHint": False, "idempotentHint": False}}]
    manifest = build_capabilities(metadata)
    assert manifest["tool_count"] == 1
    assert manifest["tools"][0]["description"] == "First paragraph."
    assert manifest["tool_groups"] == [{"id": "other", "title": "Additional registered operations",
                                        "tools": ["future_tool"]}]
    manifest["tools"][0]["annotations"]["readOnlyHint"] = True
    assert metadata[0]["annotations"]["readOnlyHint"] is False
    with pytest.raises(ValueError, match="Duplicate tool"):
        build_capabilities(metadata + metadata)


@pytest.mark.parametrize(("name", "model", "example"), [
    ("generate_request", GenerateRequest, {"segments": [{"text": "A person walks.", "duration": 2.0}]}),
    ("animation_brief", AnimationBrief, {"description": "A person waves."}),
    ("animation_revision", AnimationRevision, {"quality": "draft"}),
    ("pose_reference", PoseReference, {"job_id": "0" * 32, "frame": 0}),
    ("quality_thresholds", QualityThresholds, {"contact_slide_m_s": 0.08}),
    ("unreal_target", UnrealTarget, None),
])
def test_schemas_are_complete_current_models_and_examples_validate(name, model, example, tmp_path):
    if example is None:
        example = _target(tmp_path)
    value = model.model_validate(example)
    schema = get_schema_definition(name)
    assert schema == model.model_json_schema()
    assert schema["type"] == "object" and schema["additionalProperties"] is False
    assert model.model_validate_json(value.model_dump_json()) == value
    schema["properties"].clear()
    assert get_schema_definition(name)["properties"]


def _target(tmp_path):
    return {"project_file": str(tmp_path / "AnimationLab.uproject"),
            "skeleton_path": "/Game/Character/Skeleton.Skeleton",
            "skeletal_mesh_path": "/Game/Character/Mesh.Mesh",
            "destination_path": "/Game/Animations", "asset_name": "Wave"}


def _example_references(tmp_path):
    return {
        "input": {"job_id": "0" * 32, "other_job_id": "1" * 32, "sample_index": 0,
                  "name": "motion_00.npz", "other_name": "motion_00.npz", "attempt": 0,
                  "source_kind": "character", "source_frame": 10, "frame_indices": [0, 10, 20],
                  "offset_frames": 0, "space": "world", "target": _target(tmp_path),
                  "format": "bvh", "bvh_standard_tpose": True, "amass_z_up": True,
                  "mujoco_rest_zero": False,
                  "brief": {"description": "A person waves.", "duration_seconds": 2.0},
                  "revision": {"description": "A person makes a slower wave."},
                  "start_from": {"job_id": "0" * 32, "sample_index": 0, "frame": 10}},
        "steps": {"submit": {"id": "2" * 32}, "retry": {"id": "3" * 32}, "build": {"attempt": 0},
                  "export": {"export_id": "a" * 64},
                  "job": {"request": {"segments": [{"text": "A person waves.", "duration": 2.0}]}}},
    }


def _example_reference(reference, values):
    if reference.startswith("[") and reference.endswith("]"):
        return [_example_reference(reference[1:-1], values)]
    for component in reference.split("."):
        values = values[component]
    return values


@pytest.mark.parametrize("workflow", WORKFLOW_IDS)
def test_every_recipe_step_has_real_tool_and_valid_complete_example_arguments(workflow, tmp_path):
    server = create_server(Settings(tmp_path, sys.executable))
    recipe = get_workflow_definition(workflow)
    values = _example_references(tmp_path)
    seen = set()
    assert recipe["executes_actions"] is False and recipe["completion"] and recipe["requirements"]
    for step in recipe["steps"]:
        assert step["id"] not in seen
        for reference in step["arguments_from"].values():
            plain = reference.strip("[]")
            if plain.startswith("steps."):
                assert plain.split(".")[1] in seen, "Recipe references a result before its tool call"
        seen.add(step["id"])
        tool = server._tool_manager.get_tool(step["tool"])
        assert tool is not None, f"Recipe advertises unregistered tool {step['tool']}"
        arguments = {**step["arguments"], **{
            key: _example_reference(reference, values) for key, reference in step["arguments_from"].items()
        }}
        # Validate only; generation and editor tools are never executed by this test.
        tool.fn_metadata.arg_model.model_validate(arguments)
        assert step["expected"] and step["on_failure"]


def test_resource_names_are_bounded_and_results_do_not_share_mutable_state():
    assert WORKFLOW_IDS == get_args(WorkflowName)
    assert SCHEMA_IDS == get_args(SchemaName)
    for name in ("../server.py", "generate_motion", "", "https://untrusted.example/schema"):
        with pytest.raises(ValueError, match="Unknown workflow"):
            get_workflow_definition(name)
        with pytest.raises(ValueError, match="Unknown input schema"):
            get_schema_definition(name)
    get_workflow_definition("generate")["steps"].clear()
    assert get_workflow_definition("generate")["steps"]


def test_discovery_import_does_not_load_inference_or_editor_modules():
    script = (
        "import sys; from kimodo_mcp.capabilities import build_capabilities, get_schema_definition, "
        "get_workflow_definition; build_capabilities([]); get_schema_definition('generate_request'); "
        "get_workflow_definition('generate'); "
        "assert not any(n.split('.')[0] in {'torch', 'kimodo', 'bpy', 'unreal'} for n in sys.modules)"
    )
    result = subprocess.run([sys.executable, "-c", script], capture_output=True, text=True, timeout=15)
    assert result.returncode == 0, result.stderr
