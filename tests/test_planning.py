import copy

import pytest

from kimodo_mcp.catalog import list_models
from kimodo_mcp.planning import AnimationBrief, AnimationRevision, plan_animation, plan_revision
from kimodo_mcp.schemas import GenerateRequest


def original(**changes):
    values = dict(
        segments=[{"text": "Walk forward", "duration": 4.0}],
        seed=982,
        cfg_text=3.0,
        cfg_constraint=4.0,
        steps=37,
        formats=["npz", "bvh"],
        first_heading_angle=0.5,
        root_margin=0.02,
        bvh_standard_tpose=True,
        constraints=[
            {"type": "root2d", "frame_indices": [0, 60], "smooth_root_2d": [[1.0, 2.0], [1.0, 3.0]]}
        ],
    )
    values.update(changes)
    return GenerateRequest(**values).model_dump(mode="json")


def test_brief_defaults_explicit_and_no_inference():
    result = plan_animation(AnimationBrief(description="  Walk forward and wave.  "))
    request = result["request"]
    assert request["segments"] == [{"text": "Walk forward and wave.", "duration": 4.0}]
    assert (request["seed"], request["samples"], request["steps"]) == (0, 1, 50)
    assert request["formats"] == ["npz", "bvh"] and request["bvh_standard_tpose"]
    assert result["plan"]["total_frames"] == 120
    assert (
        result["request"] == plan_animation(AnimationBrief(description="Walk forward and wave."))["request"]
    )


@pytest.mark.parametrize("model", [m["id"] for m in list_models()])
def test_defaults_follow_allowlisted_model(model):
    request = plan_animation(AnimationBrief(description="Stand still", model=model))["request"]
    info = next(m for m in list_models() if m["id"] == model)
    assert request["formats"] == info["formats"]
    assert request["postprocess"] == info["postprocess_supported"]
    GenerateRequest.model_validate(request)


@pytest.mark.parametrize("quality,steps", [("draft", 20), ("balanced", 50), ("detailed", 80)])
def test_quality_only_sets_effort(quality, steps):
    result = plan_animation(AnimationBrief(description="Dance", quality=quality, candidates=4))
    assert result["request"]["steps"] == steps and result["request"]["samples"] == 4
    assert any("not a guaranteed" in note for note in result["notes"])


def test_explicit_beats_keep_text_timing_and_context_separate():
    beats = [{"text": "Walk", "duration": 8.0}, {"text": "Wave", "duration": 5.0}]
    result = plan_animation(AnimationBrief(description="Greeting scene", duration_seconds=13, beats=beats))
    assert result["request"]["segments"] == beats
    assert result["brief"]["description"] == "Greeting scene"
    assert result["plan"]["total_frames"] == 390


@pytest.mark.parametrize(
    "fields",
    [
        {"description": "!!!"},
        {"description": "\x00walk"},
        {"duration_seconds": 11},
        {"duration_seconds": float("nan")},
        {"candidates": True},
        {"seed": 1.5},
        {"model": "soma"},
        {"unexpected": "value"},
        {"duration_seconds": 4, "beats": [{"text": "Walk", "duration": 3}]},
    ],
)
def test_invalid_briefs_fail(fields):
    values = {"description": "Walk", **fields}
    with pytest.raises(ValueError):
        AnimationBrief(**values)


def test_revision_changes_only_requested_fields_and_does_not_mutate_source():
    source = original()
    snapshot = copy.deepcopy(source)
    result = plan_revision(source, AnimationRevision(description="Walk forward with a gentle wave"))
    expected = copy.deepcopy(snapshot)
    expected["segments"][0]["text"] = "Walk forward with a gentle wave"
    assert result["request"] == expected
    assert list(result["changes"]) == ["segments"]
    assert source == snapshot
    result["request"]["constraints"][0]["smooth_root_2d"][0][0] = 99
    assert source == snapshot


def test_seed_zero_and_candidate_changes_preserve_constraints_and_guidance():
    source = original()
    result = plan_revision(source, AnimationRevision(seed=0, candidates=2, quality="draft"))
    assert set(result["changes"]) == {"seed", "samples", "steps"}
    assert result["request"]["seed"] == 0
    for key in ("constraints", "cfg_text", "cfg_constraint", "model", "formats", "first_heading_angle"):
        assert result["request"][key] == source[key]


@pytest.mark.parametrize(
    "revision",
    [
        {"duration_seconds": 3},
        {"beats": [{"text": "Wave", "duration": 4}]},
    ],
)
def test_timing_edits_never_silently_retime_constraints(revision):
    with pytest.raises(ValueError, match="clear_constraints"):
        plan_revision(original(), AnimationRevision(**revision))
    result = plan_revision(original(), AnimationRevision(**revision, clear_constraints=True))
    assert result["request"]["constraints"] == []


def test_explicit_constraint_clear_is_a_valid_revision():
    result = plan_revision(original(), AnimationRevision(clear_constraints=True))
    assert set(result["changes"]) == {"constraints"}


def test_multibeat_revision_requires_beats_but_seed_only_is_safe():
    source = original(segments=[{"text": "Walk", "duration": 2}, {"text": "Wave", "duration": 2}])
    with pytest.raises(ValueError, match="multi-beat"):
        plan_revision(source, AnimationRevision(description="Walk slowly"))
    assert plan_revision(source, AnimationRevision(seed=3))["request"]["segments"] == source["segments"]


@pytest.mark.parametrize(
    "fields",
    [
        {},
        {"clear_constraints": "true"},
        {"quality": "best"},
        {"model": "kimodo-g1-rp"},
        {"description": "Walk", "beats": [{"text": "Wave", "duration": 2}]},
    ],
)
def test_empty_ambiguous_or_unsupported_revision_rejected(fields):
    with pytest.raises(ValueError):
        AnimationRevision(**fields)
