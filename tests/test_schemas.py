import math

import pytest
from pydantic import ValidationError

from kimodo_mcp.catalog import list_models
from kimodo_mcp.schemas import GenerateRequest


def request_data(**overrides):
    return {"segments": [{"text": "A person walks.", "duration": 2.0}], **overrides}


def pose_constraint(kind="left-hand", *, frame=0, joints=77):
    return {
        "type": kind, "frame_indices": [frame],
        "local_joints_rot": [[[0.0, 0.0, 0.0] for _ in range(joints)]],
        "root_positions": [[0.0, 1.0, 0.0]],
    }


def test_catalog_uses_fixed_model_identifiers():
    models = list_models()
    assert len(models) == len({model["id"] for model in models}) == 9
    assert "kimodo-soma-rp-ardyrep" in {model["id"] for model in models}
    for model in models:
        GenerateRequest.model_validate(request_data(model=model["id"], formats=model["formats"]))


def test_defaults_roundtrip_without_inference_imports():
    request = GenerateRequest.model_validate(request_data())
    assert request.frame_count == 60
    assert request.validated_plan()["segment_frames"] == [60]
    assert GenerateRequest.model_validate(request.model_dump(mode="json")) == request


@pytest.mark.parametrize("field,value", [
    ("model", "nvidia/arbitrary"), ("model", "soma"), ("samples", True),
    ("samples", 0), ("samples", 5), ("seed", -1), ("seed", 2**32),
    ("steps", 101), ("cfg_text", math.nan), ("cfg_constraint", math.inf),
    ("transition_frames", 0), ("postprocess", "false"), ("output", "../escape"),
    ("checkpoint", "malicious.yaml"), ("formats", ["npz", "npz"]),
    ("formats", ["csv"]),
])
def test_invalid_requests_rejected(field, value):
    with pytest.raises(ValidationError):
        GenerateRequest.model_validate(request_data(**{field: value}))


@pytest.mark.parametrize("segment", [
    {"text": " ", "duration": 2.0}, {"text": "###", "duration": 2.0},
    {"text": "walk\x00", "duration": 2.0}, {"text": "walk", "duration": math.nan},
    {"text": "walk", "duration": 0.1}, {"text": "walk", "duration": 11},
])
def test_segment_limits(segment):
    with pytest.raises(ValidationError):
        GenerateRequest.model_validate(request_data(segments=[segment]))


def test_runtime_fps_revalidation_and_transitions():
    request = GenerateRequest.model_validate(request_data())
    with pytest.raises(ValueError, match="six frames"):
        request.validated_plan(1)
    with pytest.raises(ValueError, match="2400"):
        GenerateRequest.model_validate(request_data(segments=[{"text": "walk", "duration": 10.0}] * 8)).validated_plan(60)
    with pytest.raises(ValidationError, match="Transition"):
        GenerateRequest.model_validate(request_data(segments=[{"text": "walk", "duration": 0.2}] * 2, transition_frames=6))


def test_g1_postprocess_disabled_explicitly_in_plan():
    request = GenerateRequest.model_validate(request_data(model="kimodo-g1-seed", formats=["csv"]))
    assert request.postprocess is True
    assert request.validated_plan()["postprocess"] is False


def test_matching_root_and_limb_overlap_allowed_conflict_rejected():
    root = {"type": "root2d", "frame_indices": [0], "smooth_root_2d": [[0.0, 0.0]]}
    GenerateRequest.model_validate(request_data(constraints=[root, pose_constraint()]))
    root["smooth_root_2d"] = [[1.0, 0.0]]
    with pytest.raises(ValidationError, match="Conflicting"):
        GenerateRequest.model_validate(request_data(constraints=[root, pose_constraint()]))


@pytest.mark.parametrize("constraint", [
    {"type": "root2d", "frame_indices": [-1], "smooth_root_2d": [[0.0, 0.0]]},
    {"type": "root2d", "frame_indices": [True], "smooth_root_2d": [[0.0, 0.0]]},
    {"type": "root2d", "frame_indices": [0], "smooth_root_2d": [[0.0, 1.0, 0.0]]},
    {"type": "root2d", "frame_indices": [0], "smooth_root_2d": [[math.inf, 0.0]]},
    {"type": "root2d", "frame_indices": [0], "smooth_root_2d": [[0.0, 0.0]], "global_root_heading": [[0.0, 0.0]]},
    {"type": "root2d", "frame_indices": [0, 0], "smooth_root_2d": [[0.0, 0.0], [0.0, 0.0]]},
    {"type": "root2d", "frame_indices": [60], "smooth_root_2d": [[0.0, 0.0]]},
    pose_constraint(joints=22),
])
def test_malformed_constraints_rejected(constraint):
    with pytest.raises(ValidationError):
        GenerateRequest.model_validate(request_data(constraints=[constraint]))


def test_generic_hips_limit_and_pose_shape():
    constraint = {**pose_constraint(), "type": "end-effector", "joint_names": ["Hips"]}
    with pytest.raises(ValidationError, match="Hips"):
        GenerateRequest.model_validate(request_data(constraints=[constraint]))
    GenerateRequest.model_validate(request_data(constraints=[constraint], postprocess=False))
    constraint["local_joints_rot"][0][0] = [math.nan, 0.0, 0.0]
    with pytest.raises(ValidationError):
        GenerateRequest.model_validate(request_data(constraints=[constraint], postprocess=False))


def test_full_ten_second_root_path():
    path = {"type": "root2d", "frame_indices": list(range(300)), "smooth_root_2d": [[float(index) / 300, 0.0] for index in range(300)]}
    result = GenerateRequest.model_validate(request_data(segments=[{"text": "walk", "duration": 10.0}], constraints=[path]))
    assert len(result.constraints[0].frame_indices) == 300


def repeated_pose_constraint(kind, frames, *, joints=77, **extra):
    return {
        "type": kind, "frame_indices": frames,
        "local_joints_rot": [[[0.0, 0.0, 0.0] for _ in range(joints)] for _ in frames],
        "root_positions": [[0.0, 1.0, 0.0] for _ in frames],
        **extra,
    }


def test_single_segment_plan_preserves_legacy_fields_and_has_no_transition():
    plan = GenerateRequest.model_validate(request_data()).validated_plan()
    legacy = {key: plan[key] for key in (
        "fps", "segment_frames", "total_frames", "duration_seconds", "samples", "postprocess", "formats", "model"
    )}
    assert legacy == {
        "fps": 30.0, "segment_frames": [60], "total_frames": 60, "duration_seconds": 2.0,
        "samples": 1, "postprocess": True, "formats": ["npz"], "model": "kimodo-soma-rp-v1.2",
    }
    assert plan["frame_range_convention"] == "zero_based_half_open"
    segment = plan["segment_timeline"][0]
    assert segment["incoming_transition_start_frame"] is None
    assert segment["incoming_transition_end_frame_exclusive"] is None
    assert segment["transition_method"] is None
    assert segment["transition_frames"] == segment["tail_reprocessed_frames"] == 0
    assert segment["action_frames"] == segment["generation_frames"] == segment["untouched_action_frames"] == 60
    assert plan["constraint_density"] == plan["advisories"] == []


@pytest.mark.parametrize("postprocess,method", [
    (True, "postprocess_history_and_action"), (False, "blend_history_features"),
])
def test_multi_prompt_history_windows_preserve_every_requested_action_frame(postprocess, method):
    request = GenerateRequest.model_validate(request_data(
        segments=[{"text": "Walk", "duration": 1.0}, {"text": "Stop", "duration": 0.5},
                  {"text": "Wave", "duration": 2.0}],
        transition_frames=5, postprocess=postprocess,
    ))
    plan = request.validated_plan()
    first, second, third = plan["segment_timeline"]
    assert [(s["action_start_frame"], s["action_end_frame_exclusive"]) for s in (first, second, third)] == [
        (0, 30), (30, 45), (45, 105),
    ]
    assert [(s["incoming_transition_start_frame"], s["incoming_transition_end_frame_exclusive"])
            for s in (second, third)] == [(25, 30), (40, 45)]
    assert [s["generation_frames"] for s in (first, second, third)] == [30, 20, 65]
    assert [s["untouched_action_frames"] for s in (first, second, third)] == [25, 10, 60]
    assert [s["transition_method"] for s in (second, third)] == [method, method]
    assert sum(s["action_frames"] for s in plan["segment_timeline"]) == plan["total_frames"] == 105
    assert [item["code"] for item in plan["advisories"]] == ["transition_context"]


def test_timeline_uses_runtime_fps_and_truncation_instead_of_requested_seconds():
    request = GenerateRequest.model_validate(request_data(
        segments=[{"text": "Walk", "duration": 0.27}, {"text": "Stop", "duration": 0.39}],
        transition_frames=5,
    ))
    plan = request.validated_plan(24.0)
    assert plan["segment_frames"] == [6, 9]
    first, second = plan["segment_timeline"]
    assert first["untouched_action_frames"] == 1
    assert (second["start_frame"], second["end_frame_exclusive"]) == (6, 15)
    assert (second["incoming_transition_start_frame"], second["incoming_transition_end_frame_exclusive"]) == (1, 6)
    assert plan["duration_seconds"] == 15 / 24


@pytest.mark.parametrize("count,warns", [(19, False), (20, True), (21, True)])
def test_sparse_constraint_recommendation_warns_at_twenty_without_rejecting(count, warns):
    constraint = repeated_pose_constraint("fullbody", list(range(count)))
    request = GenerateRequest.model_validate(request_data(constraints=[constraint]))
    snapshot = request.model_dump(mode="json")
    plan = request.validated_plan()
    assert request.model_dump(mode="json") == snapshot
    assert plan["constraint_density"] == [{
        "constraint_type": "fullbody", "unique_frames": count,
        "segment_frame_counts": [count], "sparse_recommendation_exempt": False,
    }]
    warnings = [item for item in plan["advisories"] if item["code"] == "sparse_constraint_density"]
    assert bool(warnings) is warns
    if warns:
        assert warnings[0]["severity"] == "warning"
        assert warnings[0]["recommended_max_unique_frames"] == 19


def test_density_unions_duplicate_sets_and_reports_per_segment_counts():
    constraints = [
        repeated_pose_constraint("left-hand", list(range(15))),
        repeated_pose_constraint("left-hand", list(range(10, 20))),
        repeated_pose_constraint("right-hand", list(range(10))),
    ]
    plan = GenerateRequest.model_validate(request_data(
        segments=[{"text": "Reach", "duration": 0.5}, {"text": "Hold", "duration": 0.5}],
        constraints=constraints,
    )).validated_plan()
    assert plan["constraint_density"] == [
        {"constraint_type": "left-hand", "unique_frames": 20, "segment_frame_counts": [15, 5],
         "sparse_recommendation_exempt": False},
        {"constraint_type": "right-hand", "unique_frames": 10, "segment_frame_counts": [10, 0],
         "sparse_recommendation_exempt": False},
    ]
    assert [a["constraint_type"] for a in plan["advisories"] if a["code"] == "sparse_constraint_density"] == ["left-hand"]


def test_dense_root_path_is_exempt_even_with_heading():
    root = {
        "type": "root2d", "frame_indices": list(range(60)),
        "smooth_root_2d": [[0.0, 0.0] for _ in range(60)],
        "global_root_heading": [[1.0, 0.0] for _ in range(60)],
    }
    plan = GenerateRequest.model_validate(request_data(constraints=[root])).validated_plan()
    assert plan["constraint_density"][0]["sparse_recommendation_exempt"] is True
    assert plan["advisories"] == []


@pytest.mark.parametrize("postprocess,expected", [
    (True, [("left-hand", 20), ("right-hand", 10)]),
    (False, [("end-effector", 10), ("left-hand", 10)]),
])
def test_density_follows_workers_generic_end_effector_expansion(postprocess, expected):
    constraints = [
        repeated_pose_constraint("end-effector", list(range(10)), joint_names=["LeftHand", "RightHand"]),
        repeated_pose_constraint("left-hand", list(range(10, 20))),
    ]
    plan = GenerateRequest.model_validate(request_data(constraints=constraints, postprocess=postprocess)).validated_plan()
    assert [(entry["constraint_type"], entry["unique_frames"]) for entry in plan["constraint_density"]] == expected


@pytest.mark.parametrize("model,joints,supported", [
    ("kimodo-soma-rp-v1.2", 77, True), ("kimodo-g1-rp", 34, False),
])
def test_postprocess_advice_respects_models_without_suggesting_unsupported_setting(model, joints, supported):
    plan = GenerateRequest.model_validate(request_data(
        model=model, postprocess=False,
        constraints=[pose_constraint(kind="fullbody", joints=joints)],
    )).validated_plan()
    advice = next(item for item in plan["advisories"] if item["code"] == "constraint_postprocess_disabled")
    assert advice["postprocess_supported"] is supported
    assert ("postprocess=true" in advice["action"]) is supported
