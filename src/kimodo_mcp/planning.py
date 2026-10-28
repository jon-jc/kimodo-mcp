"""Turn explicit creative choices into reproducible requests without a hidden LLM.

The connected assistant interprets free-form intent. This module preserves its
description and timed beats, supplies transparent defaults, and validates the
executable result. It never pretends keyword matching understands choreography.
"""

from __future__ import annotations

from typing import Annotated, Literal

from pydantic import Field, StrictInt, field_validator, model_validator

from .catalog import ModelName, model_info
from .schemas import Finite, GenerateRequest, PromptSegment, StrictModel

Quality = Literal["draft", "balanced", "detailed"]
QUALITY_STEPS = {"draft": 20, "balanced": 50, "detailed": 80}
Duration = Annotated[Finite, Field(ge=0.2, le=80)]
Seed = Annotated[StrictInt, Field(ge=0, le=2**32 - 1)]
Candidates = Annotated[StrictInt, Field(ge=1, le=4)]
Beats = Annotated[list[PromptSegment], Field(min_length=1, max_length=8)]


def clean_description(value: str) -> str:
    return PromptSegment(text=value, duration=1.0).text


class AnimationBrief(StrictModel):
    description: Annotated[str, Field(min_length=1, max_length=2000, strict=True)]
    duration_seconds: Duration | None = None
    quality: Quality = "balanced"
    candidates: Candidates = 1
    seed: Seed = 0
    model: ModelName = "kimodo-soma-rp-v1.2"
    beats: Beats | None = None

    _description = field_validator("description")(clean_description)

    @model_validator(mode="after")
    def timing(self):
        if self.beats is None and self.duration_seconds is not None and self.duration_seconds > 10:
            raise ValueError("For more than ten seconds, provide explicit timed beats of at most ten seconds each.")
        if self.beats and self.duration_seconds is not None:
            if abs(sum(beat.duration for beat in self.beats) - self.duration_seconds) > 1e-6:
                raise ValueError("Timed beats must add up to duration_seconds.")
        return self


class AnimationRevision(StrictModel):
    description: Annotated[str, Field(min_length=1, max_length=2000, strict=True)] | None = None
    duration_seconds: Annotated[Finite, Field(ge=0.2, le=10)] | None = None
    quality: Quality | None = None
    candidates: Candidates | None = None
    seed: Seed | None = None
    beats: Beats | None = None
    clear_constraints: Annotated[bool, Field(strict=True)] = False

    @field_validator("description")
    @classmethod
    def description_text(cls, value):
        return clean_description(value) if value is not None else None

    @model_validator(mode="after")
    def distinct_edits(self):
        if self.beats is not None and (self.description is not None or self.duration_seconds is not None):
            raise ValueError("Use replacement beats or a single-action description/duration, not both.")
        if not any(getattr(self, key) is not None for key in
                   ("description", "duration_seconds", "quality", "candidates", "seed", "beats")) and not self.clear_constraints:
            raise ValueError("Choose at least one revision; use the saved request unchanged for an exact retry.")
        return self


def plan_animation(brief: AnimationBrief) -> dict:
    segments = brief.beats or [PromptSegment(text=brief.description, duration=brief.duration_seconds or 4.0)]
    info = model_info(brief.model)
    request = GenerateRequest(
        model=brief.model, segments=segments, seed=brief.seed, samples=brief.candidates,
        steps=QUALITY_STEPS[brief.quality], formats=info["formats"], bvh_standard_tpose=True,
        postprocess=info["postprocess_supported"],
    )
    notes = [
        "The description and supplied beats are used as written; the connected assistant interprets artistic intent.",
        "Quality presets change sampling effort, not a guaranteed realism level.",
        "Compare candidates and inspect the character before choosing an animation.",
    ]
    if brief.duration_seconds is None and not brief.beats:
        notes.append("Duration defaults to four seconds.")
    if brief.beats:
        notes.append("Each beat is a model prompt; the overall description is retained as context, not prepended.")
    notes.append("Timing is quantized to 30 FPS; plan.segment_frames records the exact generated samples.")
    return {"brief": brief.model_dump(mode="json"), "request": request.model_dump(mode="json"),
            "plan": request.validated_plan(), "notes": notes}


def plan_revision(original: dict, revision: AnimationRevision) -> dict:
    before = GenerateRequest.model_validate(original)
    values = before.model_dump(mode="json")
    timing_edit = revision.beats is not None or revision.duration_seconds is not None
    if timing_edit and before.constraints and not revision.clear_constraints:
        raise ValueError("Changing timing with existing constraints requires clear_constraints=true or a new explicit request.")
    if revision.beats is not None:
        values["segments"] = [beat.model_dump() for beat in revision.beats]
    elif revision.description is not None or revision.duration_seconds is not None:
        if len(before.segments) != 1:
            raise ValueError("Revise a multi-beat animation using explicit replacement beats.")
        values["segments"] = [{
            "text": revision.description if revision.description is not None else before.segments[0].text,
            "duration": revision.duration_seconds if revision.duration_seconds is not None else before.segments[0].duration,
        }]
    for key, value in {"seed": revision.seed, "samples": revision.candidates,
                       "steps": QUALITY_STEPS.get(revision.quality)}.items():
        if value is not None:
            values[key] = value
    if revision.clear_constraints:
        values["constraints"] = []
    request = GenerateRequest.model_validate(values)
    original_values = before.model_dump(mode="json")
    changes = {key: {"before": original_values[key], "after": value}
               for key, value in values.items() if original_values[key] != value}
    return {"request": request.model_dump(mode="json"), "plan": request.validated_plan(), "changes": changes,
            "notes": ["Unchanged model, seed, guidance, exports and constraints are preserved.",
                      "This generates a new performance; unmodified body parts are not guaranteed identical."]}
