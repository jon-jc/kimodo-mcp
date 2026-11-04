"""Immutable rigid-prop attachment recipes and isolated animated scene exports."""

from __future__ import annotations

import itertools
import re
from pathlib import Path
from typing import Annotated, Literal

from filelock import FileLock
from pydantic import Field, StrictInt, field_validator, model_validator

from .handoff_scripts.character import landmarks
from .jobs import atomic_json
from .schemas import StrictModel
from .studio_assets import (
    _file,
    canonical,
    digest,
    inspect_glb,
    managed_directory,
    read_json,
    resolve_asset,
    run_blender,
)

SHA = Annotated[str, Field(strict=True, pattern=r"^[0-9a-f]{64}$")]
Number = Annotated[float, Field(strict=True, allow_inf_nan=False, ge=-10000, le=10000)]
Triple = Annotated[list[Number], Field(min_length=3, max_length=3)]


class PropAttachment(StrictModel):
    asset_id: SHA
    bone: Annotated[str, Field(strict=True, max_length=64)] | None = None
    translation_m: Triple = [0.0, 0.0, 0.0]
    rotation_degrees: Triple = [0.0, 0.0, 0.0]
    scale: Triple = [1.0, 1.0, 1.0]

    @field_validator("bone")
    @classmethod
    def known_bone(cls, value):
        if value is not None and value not in landmarks():
            raise ValueError("Attachment bone must belong to the registered 24-bone target")
        return value

    @field_validator("scale")
    @classmethod
    def positive_scale(cls, value):
        if any(not 0.001 <= x <= 100 for x in value):
            raise ValueError("Each scale component must be 0.001 to 100")
        return value

    @field_validator("rotation_degrees")
    @classmethod
    def bounded_angles(cls, value):
        if any(abs(x) > 360 for x in value):
            raise ValueError("Each rotation must be within -360 to 360 degrees")
        return value


class PropSceneRequest(StrictModel):
    title: Annotated[str, Field(strict=True, min_length=1, max_length=160)]
    attachments: Annotated[list[PropAttachment], Field(min_length=1, max_length=16)]

    @field_validator("title")
    @classmethod
    def title_not_blank(cls, value):
        if not value.strip():
            raise ValueError("Scene title cannot be blank")
        return value


class PropMotionSource(StrictModel):
    kind: Literal["kimodo", "capture_character"]
    job_id: Annotated[str, Field(strict=True, pattern=r"^[0-9a-f]{32}$")] | None = None
    sample_index: Annotated[StrictInt, Field(ge=0, le=3)] = 0
    variant_id: SHA | None = None
    attempt: Annotated[StrictInt, Field(ge=0, le=99)] = 0
    capture_handoff_id: SHA | None = None

    @model_validator(mode="after")
    def matching_source(self):
        if self.kind == "kimodo":
            if self.job_id is None or self.capture_handoff_id is not None:
                raise ValueError("Kimodo source requires job_id and no capture_handoff_id")
        elif (
            self.capture_handoff_id is None
            or self.job_id is not None
            or self.variant_id is not None
            or self.sample_index != 0
            or self.attempt != 0
        ):
            raise ValueError("Capture source requires only capture_handoff_id")
        return self


def save_prop_scene(settings, request):
    request = PropSceneRequest.model_validate(request)
    value = request.model_dump(mode="json")
    for attachment in request.attachments:
        if resolve_asset(settings, attachment.asset_id)["kind"] != "prop":
            raise ValueError("Only assets registered as props can be attached")
    identity = {"schema_version": 1, "request": value}
    scene_id = canonical(identity)
    directory = managed_directory(settings, "prop-scenes", scene_id)
    directory.parent.mkdir(parents=True, exist_ok=True)
    with FileLock(str(directory.parent / (scene_id + ".lock")), timeout=10):
        if not directory.exists():
            directory.mkdir()
            atomic_json(directory / "scene.json", identity | {"scene_id": scene_id})
    return get_prop_scene(settings, scene_id)


def get_prop_scene(settings, scene_id):
    directory = managed_directory(settings, "prop-scenes", scene_id)
    value = read_json(directory / "scene.json")
    request = PropSceneRequest.model_validate(value.get("request"))
    if (
        value.get("scene_id") != scene_id
        or value.get("schema_version") != 1
        or canonical({"schema_version": 1, "request": request.model_dump(mode="json")}) != scene_id
    ):
        raise ValueError("Prop scene receipt changed")
    assets = [resolve_asset(settings, item.asset_id) for item in request.attachments]
    if any(asset["kind"] != "prop" for asset in assets):
        raise ValueError("Prop scene references a non-prop asset")
    return value | {
        "assets": assets,
        "coordinates": {
            "units": "meters",
            "world_up_axis": "Z",
            "rotation": "intrinsic local XYZ degrees",
            "attachment_origin": "bone head and posed bone axes; null bone uses world origin",
        },
    }


def _list_receipts(settings, category, filename, key, limit, offset):
    if type(limit) is not int or not 1 <= limit <= 100 or type(offset) is not int or not 0 <= offset <= 10000:
        raise ValueError("Invalid prop pagination")
    root = settings.data_dir.resolve() / category
    if root.resolve() != root:
        raise ValueError("Managed prop path escapes its storage")
    entries = list(itertools.islice(root.iterdir(), 10001)) if root.exists() else []
    records, invalid = [], 0
    for directory in sorted(entries[:10000]):
        if not re.fullmatch(r"[0-9a-f]{64}", directory.name):
            continue
        try:
            if directory.resolve() != directory:
                raise ValueError("Managed prop path escapes its storage")
            value = read_json(directory / filename)
            if directory.is_symlink() or value.get(key) != directory.name:
                raise ValueError("Invalid prop receipt")
            summary = {key: directory.name, "verification": "metadata_only"}
            if category == "prop-scenes":
                summary.update(
                    title=value["request"]["title"], attachments=len(value["request"]["attachments"])
                )
            else:
                summary.update(
                    scene_id=value["scene_id"],
                    source=value["motion"]["source"],
                    status="report_available"
                    if (directory / "report.json").exists()
                    else "outcome_unknown"
                    if (directory / "dispatch.json").exists()
                    else "prepared",
                )
            records.append(summary)
        except (OSError, ValueError, KeyError, TypeError):
            invalid += 1
    return {
        "scenes" if category == "prop-scenes" else "handoffs": records[offset : offset + limit],
        "total": len(records),
        "invalid_count": invalid,
        "limit": limit,
        "offset": offset,
        "scan_truncated": len(entries) > 10000,
    }


def list_prop_scenes(settings, limit=20, offset=0):
    return _list_receipts(settings, "prop-scenes", "scene.json", "scene_id", limit, offset)


def list_prop_scene_handoffs(settings, limit=20, offset=0):
    return _list_receipts(settings, "prop-handoffs", "handoff.json", "handoff_id", limit, offset)


def _motion(settings, source):
    source = PropMotionSource.model_validate(source)
    if source.kind == "capture_character":
        from .capture_retarget import get_capture_character_report

        report = get_capture_character_report(settings, source.capture_handoff_id)
        if report["status"] != "completed":
            raise ValueError("Capture character handoff must be completed")
        report_path = Path(report["report_path"])
    else:
        from .workspace import Workspace, handoff_directory_name

        workspace = Workspace(settings)
        job = workspace.store.get(source.job_id)
        directory = workspace.store.directory(source.job_id)
        report_path = (
            directory
            / "handoffs"
            / handoff_directory_name("character", source.sample_index, source.attempt, source.variant_id)
            / "blender-report.json"
        )
        workspace._verified_character(report_path, directory, job)
        report = read_json(report_path)
    blend = report_path.parent / "character.blend"
    _file(blend, report["blend_sha256"])
    return {
        "source": source.model_dump(mode="json"),
        "report_path": str(report_path),
        "report_sha256": digest(report_path),
        "blend_path": str(blend),
        "blend_sha256": report["blend_sha256"],
        "fps": report["fps"],
        "frames": report["frames"],
    }


def _scripts():
    root = Path(__file__).parent / "handoff_scripts"
    return {name: digest(root / name) for name in ("prop_scene.py", "asset_preview.py")}


def prepare_prop_scene_handoff(settings, scene_id, source, attempt=0):
    if type(attempt) is not int or not 0 <= attempt <= 99:
        raise ValueError("Attempt must be 0 to 99")
    scene = get_prop_scene(settings, scene_id)
    identity = {
        "schema_version": 1,
        "scene_id": scene_id,
        "motion": _motion(settings, source),
        "scene": scene["request"],
        "assets": scene["assets"],
        "scripts": _scripts(),
        "attempt": attempt,
    }
    handoff_id = canonical(identity)
    directory = managed_directory(settings, "prop-handoffs", handoff_id)
    directory.parent.mkdir(parents=True, exist_ok=True)
    with FileLock(str(directory.parent / (handoff_id + ".lock")), timeout=10):
        if not directory.exists():
            directory.mkdir()
            atomic_json(directory / "handoff.json", identity | {"handoff_id": handoff_id})
    return get_prop_scene_handoff(settings, handoff_id)


def _verify(settings, handoff_id):
    directory = managed_directory(settings, "prop-handoffs", handoff_id)
    request = read_json(directory / "handoff.json")
    identity = {key: value for key, value in request.items() if key != "handoff_id"}
    if request.get("handoff_id") != handoff_id or canonical(identity) != handoff_id:
        raise ValueError("Prop handoff identity changed")
    scene = get_prop_scene(settings, request["scene_id"])
    if (
        request["scene"] != scene["request"]
        or request["assets"] != scene["assets"]
        or request["scripts"] != _scripts()
        or request["motion"] != _motion(settings, request["motion"]["source"])
    ):
        raise ValueError("Prop handoff source or converter changed")
    return directory, request


def get_prop_scene_handoff(settings, handoff_id):
    directory, request = _verify(settings, handoff_id)
    result = {"handoff_id": handoff_id, "status": "prepared", "request": request, "outputs": []}
    if not (directory / "report.json").exists():
        if (directory / "dispatch.json").exists():
            result["status"] = "outcome_unknown"
        return result
    report = read_json(directory / "report.json")
    if (
        report.get("handoff_id") != handoff_id
        or report.get("status") != "completed"
        or report.get("animation_preserved") is not True
        or sorted(report.get("character_bones", [])) != sorted(landmarks())
        or report.get("fps") != request["motion"]["fps"]
        or report.get("frames") != request["motion"]["frames"]
    ):
        raise ValueError("Prop export report differs from its prepared source")
    names = ["scene.blend", "scene-preview.glb", "attachment-recipe.json"] + [
        f"prop_{index:02d}.fbx" for index in range(len(request["assets"]))
    ]
    outputs = []
    for name in names:
        path = directory / name
        expected = report.get("outputs", {}).get(name)
        if not isinstance(expected, str):
            raise ValueError("Missing prop output hash")
        _file(path, expected)
        outputs.append(
            {"path": name, "absolute_path": str(path), "sha256": expected, "bytes": path.stat().st_size}
        )
    inspect_glb(directory / "scene-preview.glb")
    _verify(settings, handoff_id)
    return result | report | {"outputs": outputs}


def execute_prop_scene_handoff(settings, handoff_id):
    directory, _ = _verify(settings, handoff_id)
    with FileLock(str(directory / "execution.lock"), timeout=1):
        existing = get_prop_scene_handoff(settings, handoff_id)
        if existing["status"] == "completed":
            return existing
        if (directory / "dispatch.json").exists():
            raise ValueError("This attempt was dispatched; inspect it or prepare a new attempt")
        atomic_json(directory / "dispatch.json", {"handoff_id": handoff_id})
        run_blender(
            Path(__file__).parent / "handoff_scripts" / "prop_scene.py", directory / "handoff.json", directory
        )
        return get_prop_scene_handoff(settings, handoff_id)


def get_prop_scene_artifact(settings, handoff_id, path):
    report = get_prop_scene_handoff(settings, handoff_id)
    matches = [entry for entry in report["outputs"] if entry["path"] == path]
    if len(matches) != 1:
        raise ValueError("Select an exact verified prop output path")
    return matches[0]
