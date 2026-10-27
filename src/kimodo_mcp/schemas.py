"""Bounded JSON contracts; validation never imports the inference runtime."""

from __future__ import annotations

import math
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, StrictInt, field_validator, model_validator

from .catalog import ModelName, OutputFormat, model_info

Finite = Annotated[float, Field(allow_inf_nan=False, strict=True)]
Vec2 = Annotated[list[Finite], Field(min_length=2, max_length=2)]
Vec3 = Annotated[list[Finite], Field(min_length=3, max_length=3)]
Frames = Annotated[list[Annotated[StrictInt, Field(ge=0, lt=2400)]], Field(min_length=1, max_length=512)]
Positions2 = Annotated[list[Vec2], Field(min_length=1, max_length=512)]
Positions3 = Annotated[list[Vec3], Field(min_length=1, max_length=512)]
Pose = Annotated[list[Vec3], Field(min_length=22, max_length=77)]
Poses = Annotated[list[Pose], Field(min_length=1, max_length=512)]


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", validate_default=True)


class PromptSegment(StrictModel):
    text: Annotated[str, Field(min_length=1, max_length=2000, strict=True)]
    duration: Annotated[Finite, Field(ge=0.2, le=10.0)]

    @field_validator("text")
    @classmethod
    def meaningful_text(cls, value: str) -> str:
        if not any(character.isalnum() for character in value):
            raise ValueError("Prompt must contain at least one letter or number.")
        if any(ord(character) < 32 and character not in "\n\t\r" for character in value):
            raise ValueError("Prompt contains unsupported control characters.")
        return value.strip()


class ConstraintBase(StrictModel):
    frame_indices: Frames

    @field_validator("frame_indices")
    @classmethod
    def unique_ordered_frames(cls, values: list[int]) -> list[int]:
        if values != sorted(set(values)):
            raise ValueError("Constraint frame indices must be unique and increasing.")
        return values


class Root2DConstraint(ConstraintBase):
    type: Literal["root2d"]
    smooth_root_2d: Positions2
    global_root_heading: Positions2 | None = None

    @model_validator(mode="after")
    def aligned_arrays(self):
        if len(self.smooth_root_2d) != len(self.frame_indices):
            raise ValueError("Root positions must have one row per constrained frame.")
        if self.global_root_heading is not None:
            if len(self.global_root_heading) != len(self.frame_indices):
                raise ValueError("Heading must have one row per constrained frame.")
            if any(abs(math.hypot(*row) - 1.0) > 0.01 for row in self.global_root_heading):
                raise ValueError("Heading rows must be unit [cos(angle), sin(angle)] vectors.")
        if any(abs(value) > 1000 for row in self.smooth_root_2d for value in row):
            raise ValueError("Constraint positions must stay within 1000 meters of the origin.")
        return self


class PoseConstraint(ConstraintBase):
    type: Literal["fullbody", "left-hand", "right-hand", "left-foot", "right-foot"]
    local_joints_rot: Poses
    root_positions: Positions3
    smooth_root_2d: Positions2 | None = None

    @model_validator(mode="after")
    def aligned_arrays(self):
        count = len(self.frame_indices)
        if len(self.local_joints_rot) != count or len(self.root_positions) != count:
            raise ValueError("Pose rotations and root positions must have one row per frame.")
        joints = len(self.local_joints_rot[0])
        if any(len(pose) != joints for pose in self.local_joints_rot):
            raise ValueError("Every pose must have the same number of joints.")
        if self.smooth_root_2d is not None and len(self.smooth_root_2d) != count:
            raise ValueError("Smooth root positions must have one row per frame.")
        positions = self.root_positions + (self.smooth_root_2d or [])
        if any(abs(value) > 1000 for row in positions for value in row):
            raise ValueError("Constraint positions must stay within 1000 meters of the origin.")
        if any(abs(value) > 100 for pose in self.local_joints_rot for row in pose for value in row):
            raise ValueError("Axis-angle components must be within 100 radians.")
        return self


class EndEffectorConstraint(PoseConstraint):
    type: Literal["end-effector"]
    joint_names: Annotated[
        list[Literal["LeftHand", "RightHand", "LeftFoot", "RightFoot", "Hips"]],
        Field(min_length=1, max_length=5),
    ]

    @field_validator("joint_names")
    @classmethod
    def unique_names(cls, values):
        if len(values) != len(set(values)):
            raise ValueError("End-effector joint names must be unique.")
        return values


Constraint = Annotated[Root2DConstraint | PoseConstraint | EndEffectorConstraint, Field(discriminator="type")]


class GenerateRequest(StrictModel):
    model: ModelName = "kimodo-soma-rp-v1.2"
    segments: Annotated[list[PromptSegment], Field(min_length=1, max_length=8)]
    seed: Annotated[StrictInt, Field(ge=0, le=2**32 - 1)] = 0
    samples: Annotated[StrictInt, Field(ge=1, le=4)] = 1
    steps: Annotated[StrictInt, Field(ge=1, le=100)] = 50
    cfg_text: Annotated[Finite, Field(ge=0, le=20)] = 2.0
    cfg_constraint: Annotated[Finite, Field(ge=0, le=20)] = 2.0
    transition_frames: Annotated[StrictInt, Field(ge=1, le=30)] = 5
    first_heading_angle: Annotated[Finite, Field(ge=-math.tau, le=math.tau)] = 0.0
    root_margin: Annotated[Finite, Field(ge=0, le=1)] = 0.04
    postprocess: Annotated[bool, Field(strict=True)] = True
    bvh_standard_tpose: Annotated[bool, Field(strict=True)] = False
    formats: Annotated[list[OutputFormat], Field(min_length=1, max_length=4)] = Field(default_factory=lambda: ["npz"])
    constraints: Annotated[list[Constraint], Field(max_length=32)] = Field(default_factory=list)

    @property
    def frame_count(self) -> int:
        """Estimated total at 30 Hz; worker revalidates using the model's actual fps."""
        return sum(int(segment.duration * 30) for segment in self.segments)

    @property
    def effective_postprocess(self) -> bool:
        return self.postprocess and model_info(self.model)["postprocess_supported"]

    @model_validator(mode="after")
    def compatible_settings(self):
        info = model_info(self.model)
        if len(self.formats) != len(set(self.formats)):
            raise ValueError("Output formats must be unique.")
        if not set(self.formats).issubset(info["formats"]):
            raise ValueError(f"This model supports only {', '.join(info['formats'])}.")
        if sum(len(constraint.frame_indices) for constraint in self.constraints) > 512:
            raise ValueError("At most 512 constrained keyframes are allowed per request.")
        root_values: dict[tuple[int, str], float] = {}
        for constraint in self.constraints:
            # All pose constraints also control the planar root. Detect conflicts
            # expressible directly in JSON now; the worker compares actual feature
            # masks/values after FK to catch conflicting joints and headings.
            for row, frame in enumerate(constraint.frame_indices):
                planar = constraint.smooth_root_2d
                if planar is not None:
                    x, z = planar[row]
                else:
                    x, _, z = constraint.root_positions[row]
                channels = {"x": x, "z": z}
                if isinstance(constraint, PoseConstraint):
                    channels["y"] = constraint.root_positions[row][1]
                elif constraint.global_root_heading is not None:
                    channels.update(zip(("heading_cos", "heading_sin"), constraint.global_root_heading[row]))
                for name, value in channels.items():
                    key = (frame, name)
                    if key in root_values and not math.isclose(root_values[key], value, rel_tol=1e-5, abs_tol=1e-5):
                        raise ValueError(f"Conflicting {name} constraints at frame {frame}.")
                    root_values[key] = value
            if isinstance(constraint, PoseConstraint):
                if len(constraint.local_joints_rot[0]) not in info["constraint_joints"]:
                    raise ValueError(f"Model requires pose joint counts {info['constraint_joints']}.")
            if isinstance(constraint, EndEffectorConstraint) and "Hips" in constraint.joint_names and self.effective_postprocess:
                raise ValueError("Hips end-effector constraints require postprocess=false; use fullbody for corrected keyframes.")
        self.validated_plan(30.0)
        return self

    def validated_plan(self, fps: float = 30.0) -> dict:
        if not math.isfinite(fps) or fps <= 0 or fps > 240:
            raise ValueError("Model frame rate is invalid.")
        frames = [int(segment.duration * fps) for segment in self.segments]
        if any(count < 6 for count in frames):
            raise ValueError("Every segment must contain at least six frames.")
        total = sum(frames)
        if total > 2400:
            raise ValueError("A request may contain at most 2400 frames.")
        if len(frames) > 1 and self.transition_frames >= min(frames):
            raise ValueError("Transition frames must be less than the shortest segment.")
        if any(index >= total for constraint in self.constraints for index in constraint.frame_indices):
            raise ValueError("Constraint frame index is outside the requested motion.")
        return {
            "fps": fps, "segment_frames": frames, "total_frames": total,
            "duration_seconds": total / fps, "samples": self.samples,
            "postprocess": self.effective_postprocess, "formats": list(self.formats),
            "model": self.model,
        }


def validated_plan(request: GenerateRequest, fps: float = 30.0) -> dict:
    return request.validated_plan(fps)
