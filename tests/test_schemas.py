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
