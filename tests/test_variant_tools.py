"""Exercise agent rig correction, verification and downstream selectors together."""

import hashlib
import json
import sys
from pathlib import Path

import numpy as np
import pytest
from test_motion_variants import fixture

from kimodo_mcp.assessment import CandidateReference, QualityPolicy
from kimodo_mcp.config import Settings
from kimodo_mcp.jobs import JobStore
from kimodo_mcp.motion_variants import JointAdjustment, VariantEdit
from kimodo_mcp.planning import AnimationBrief, PoseReference
from kimodo_mcp.server import create_server


@pytest.fixture
def setup(tmp_path):
    settings = Settings(tmp_path, sys.executable)
    store = JobStore(settings)
    job = store.submit({"model": "kimodo-soma-rp-v1.2", "seed": 12}, launch=False)
    source = store.directory(job["id"]) / "motion_00.npz"
    np.savez(source, **fixture())
    (source.parent / "manifest.json").write_text(json.dumps({
        "samples": 1, "frames": 12, "fps": 30, "skeleton": "somaskel77",
        "model": "kimodo-soma-rp-v1.2", "artifacts": [{"path": source.name, "format": "npz",
        "bytes": source.stat().st_size, "sha256": hashlib.sha256(source.read_bytes()).hexdigest()}],
    }))
    store.update(job["id"], "completed")
    server = create_server(settings)
    return server, job["id"], source


@pytest.mark.asyncio
async def test_agent_can_inspect_correct_compare_assess_and_reuse_pose(setup):
    server, job, source = setup
    def call(name):
        return server._tool_manager.get_tool(name).fn
    before = source.read_bytes()
    rig = call("inspect_motion_rig")(job)
    assert rig["rig_kind"] == "source_animation_skeleton"
    assert len(rig["joints"]) == 77 and rig["editing"]["supported"]
    assert not rig["joints"][0]["rotation_adjustable"]
    shin = next(joint for joint in rig["joints"] if joint["name"] == "LeftShin")
    assert shin["body_group"] == "left_leg"
    edit = VariantEdit(start_frame=2, end_frame_exclusive=12, joint_adjustments=[JointAdjustment(
        joint_name="LeftArm", local_xyz_radians=[0.0, 0.0, 0.1], start_frame=3, end_frame_exclusive=11,
    )])
    saved = await call("create_motion_variant")(job, edit)
    variant = saved["variant_id"]
    assert saved["frames"] == 10
    assert saved["contact_status"] == "invalidated_by_joint_adjustment"
    assert call("get_motion_variant")(job, variant)["artifact"]["sha256"] == saved["artifact"]["sha256"]
    assert variant in [v["variant_id"] for v in call("get_job")(job)["variants"]]
    selected = call("inspect_motion_rig")(job, variant_id=variant)
    assert selected["frames"] == 10 and selected["source_range"]["start_frame"] == 2
    frames = call("inspect_motion_frames")(job, [0, 9], variant_id=variant)
    assert frames["frame_count"] == 10
    assert frames["source_sha256"] == saved["artifact"]["sha256"]
    policy = QualityPolicy(checks=[{"metric": "bone_length_drift", "max_value": .001},
                                  {"metric": "contact_sliding", "max_value": 1}])
    assessment = await call("assess_motion_candidates")(
        [CandidateReference(job_id=job), CandidateReference(job_id=job, variant_id=variant)], policy,
    )
    assert assessment["candidates"][1]["status"] == "unavailable"
    assert assessment["candidates"][1]["source"]["variant_id"] == variant
    comparison = call("compare_motions")(job, job, other_variant_id=variant, offset_frames=-2)
    assert comparison["comparison"]["frames"] == 10
    assert comparison["comparison"]["position_rmse_m"] > 0
    review = call("create_motion_review")(job, variant_id=variant)
    assert json.loads(Path(review["diagnostics_path"]).read_text())["frames"] == 10
    pose = call("extract_pose_constraint")(job, 1, variant_id=variant)
    assert pose["source_sha256"] == saved["artifact"]["sha256"]
    plan = call("plan_animation")(AnimationBrief(description="A person waves."),
                                  PoseReference(job_id=job, frame=1, variant_id=variant))
    assert plan["start_from"]["variant_id"] == variant
    assert plan["start_from"]["source_sha256"] == saved["artifact"]["sha256"]
    assert source.read_bytes() == before


@pytest.mark.asyncio
async def test_variants_cannot_bypass_source_or_sample_identity(setup):
    server, job, source = setup
    def call(name):
        return server._tool_manager.get_tool(name).fn
    saved = await call("create_motion_variant")(job, VariantEdit(start_frame=2))
    with pytest.raises(ValueError):
        call("inspect_motion")(job, "../motion_00.npz", variant_id=saved["variant_id"])
    with pytest.raises(ValueError):
        call("get_motion_variant")(job, saved["variant_id"], sample_index=1)
    source.write_bytes(source.read_bytes() + b"changed")
    with pytest.raises(ValueError):
        call("inspect_motion_rig")(job, variant_id=saved["variant_id"])


def test_every_motion_consumer_exposes_variant_selection(setup):
    server, *_ = setup
    for name in ("inspect_motion", "inspect_motion_frames", "inspect_motion_rig", "preview_motion",
                 "extract_pose_constraint", "extract_motion_constraints", "analyze_motion_quality",
                 "assess_motion_quality", "create_motion_review", "compare_motions", "export_motion",
                 "build_character_preview", "prepare_character_handoff", "prepare_blender_handoff",
                 "execute_blender_handoff", "get_handoff_report", "prepare_unreal_handoff",
                 "execute_unreal_handoff", "get_unreal_report"):
        schema = server._tool_manager.get_tool(name).parameters
        assert "variant_id" in schema["properties"], name
        assert "variant_id" not in schema.get("required", [])
    assert "other_variant_id" in server._tool_manager.get_tool("compare_motions").parameters["properties"]
