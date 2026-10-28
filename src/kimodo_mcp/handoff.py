"""Prepare bounded DCC handoffs without executing editor code in the MCP server."""

from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator


class UnrealTarget(BaseModel):
    """Explicit existing rig and new animation destination in an Unreal project."""

    model_config = ConfigDict(extra="forbid")
    project_file: str
    skeleton_path: str
    skeletal_mesh_path: str
    destination_path: str
    asset_name: Annotated[str, Field(pattern=r"^[A-Za-z_][A-Za-z0-9_]{0,63}$")]

    @field_validator("project_file")
    @classmethod
    def project_path(cls, value: str) -> str:
        path = Path(value)
        if not path.is_absolute() or path.suffix.lower() != ".uproject":
            raise ValueError("Use the absolute path of the intended .uproject file.")
        return str(path.resolve())

    @field_validator("skeleton_path", "skeletal_mesh_path", "destination_path")
    @classmethod
    def content_path(cls, value: str) -> str:
        if not re.fullmatch(r"/Game(?:/[A-Za-z_][A-Za-z0-9_]*)+(?:\.[A-Za-z_][A-Za-z0-9_]*)?", value):
            raise ValueError("Use an explicit /Game content path containing identifier segments.")
        return value

    @field_validator("destination_path")
    @classmethod
    def directory_only(cls, value: str) -> str:
        if "." in value:
            raise ValueError("The destination is a content directory, not an object path.")
        return value


class HandoffResult(BaseModel):
    kind: Literal["blender", "character", "unreal"]
    directory: str
    script_path: str
    request_path: str
    report_path: str
    execution: str


def _digest(path: Path) -> str:
    result = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            result.update(block)
    return result.hexdigest()


def _source(path: Path, suffix: str, expected_digest: str | None = None) -> Path:
    path = path.resolve(strict=True)
    if not path.is_file() or path.suffix.lower() != suffix:
        raise ValueError(f"Expected an existing {suffix} file.")
    if expected_digest is not None and _digest(path) != expected_digest:
        raise ValueError("Source checksum differs from its recorded artifact.")
    return path


def _prepare(kind: str, output_directory: Path, request: dict[str, Any]) -> HandoffResult:
    if output_directory.is_symlink():
        raise ValueError("Handoff directories cannot be symbolic links.")
    output = output_directory.resolve()
    template = (Path(__file__).with_name("handoff_scripts") / f"{kind}.py").read_text(encoding="utf-8")
    request_text = json.dumps(request, ensure_ascii=True, allow_nan=False, sort_keys=True, indent=2) + "\n"
    script = output / f"{kind}_handoff.py"
    config = output / "handoff.json"
    if output.exists():
        # Exact repeat calls return the same files; no existing file is replaced.
        if config.is_symlink() or script.is_symlink() or not config.is_file() or not script.is_file():
            raise FileExistsError("Handoff directory already exists without a complete matching preparation.")
        if config.read_text(encoding="utf-8") != request_text or script.read_text(encoding="utf-8") != template:
            raise FileExistsError("Handoff directory contains a different request or script version.")
    else:
        output.mkdir(parents=True, exist_ok=False)
        with config.open("x", encoding="utf-8", newline="\n") as stream:
            stream.write(request_text)
        with script.open("x", encoding="utf-8", newline="\n") as stream:
            stream.write(template)
    return HandoffResult(
        kind=kind, directory=str(output), script_path=str(script), request_path=str(config),
        report_path=str(output / ("unreal-report.json" if kind == "unreal" else "blender-report.json")),
        execution=("Run this exact script with Blender's Python or its existing MCP connection."
                   if kind != "unreal" else
                   "Run this exact script through Unreal Editor Python in the intended project."),
    )


def prepare_blender_handoff(
    bvh_path: Path, output_directory: Path, *, fps: float, frames: int,
    source_sha256: str, skeleton: str,
) -> HandoffResult:
    """Resolve a verified job BVH and prepare standalone Blender import/export files.

    The caller assigns output_directory beneath its managed handoff storage. It
    must obtain fps, frames, skeleton and checksum from the completed job manifest.
    No paths from prompt text should be substituted for the verified artifact.
    """
    if fps != 30 or not isinstance(frames, int) or not 2 <= frames <= 2400:
        raise ValueError("This handoff requires canonical 30 FPS and 2..2400 frames.")
    if skeleton not in {"somaskel30", "somaskel77"}:
        raise ValueError("The Blender BVH handoff currently supports the SOMA skeleton only.")
    source = _source(bvh_path, ".bvh", source_sha256)
    if source.stat().st_size > 128 * 1024 * 1024:
        raise ValueError("BVH exceeds the handoff size limit.")
    text = source.read_text(encoding="utf-8")
    frame_match = re.search(r"(?m)^Frames:\s*(\d+)\s*$", text)
    time_match = re.search(r"(?m)^Frame Time:\s*([0-9.eE+-]+)\s*$", text)
    if not frame_match or int(frame_match[1]) != frames or not time_match:
        raise ValueError("BVH header frame count differs from the job manifest.")
    if abs(float(time_match[1]) - 1 / fps) > 1e-6:
        raise ValueError("BVH frame interval differs from the job manifest.")
    request = {
        "schema_version": 1, "kind": "blender", "source_bvh": str(source),
        "source_sha256": source_sha256, "skeleton": skeleton, "fps": float(fps),
        "frames": frames, "armature_name": "KimodoRig", "source_units": "centimeters",
        "source_up": "Y", "source_forward": "Z",
    }
    return _prepare("blender", output_directory, request)


def prepare_character_handoff(
    character_fbx: Path, motion_npz: Path, output_directory: Path, *,
    source_sha256: str, fps: float, frames: int, sample_index: int = 0,
) -> HandoffResult:
    """Prepare the inspected character's fitted rig and managed SOMA animation.

    character_fbx comes only from operator configuration; validate_target binds
    this asset-specific rig to its reviewed source bytes. motion_npz and timing
    must be resolved from a completed job, not from arbitrary tool paths.
    """
    from .artifacts import inspect_motion
    from .handoff_scripts.character import SOURCE_SHA256, validate_target

    if fps != 30 or type(frames) is not int or not 2 <= frames <= 2400:
        raise ValueError("Character handoff requires canonical 30 FPS and 2..2400 frames.")
    if type(sample_index) is not int or not 0 <= sample_index <= 3:
        raise ValueError("Sample index must identify one of the job's four possible samples.")
    character = validate_target(character_fbx)
    motion = _source(motion_npz, ".npz", source_sha256)
    summary = inspect_motion(motion)
    if summary["fps"] != fps or summary["frames"] != frames:
        raise ValueError("Motion timing differs from the completed job manifest.")
    if summary["skeleton"] not in {"somaskel30", "somaskel77"} or summary["joints"] not in {30, 77}:
        raise ValueError("The fitted character requires SOMA motion.")
    if summary["samples"] != 1:
        raise ValueError("Resolve one per-sample job artifact before preparing a character handoff.")
    request = {
        "schema_version": 1, "kind": "character", "character_fbx": str(character),
        "character_sha256": SOURCE_SHA256, "source_motion": str(motion),
        "source_sha256": source_sha256, "fps": float(fps), "frames": frames,
        "sample_index": sample_index,
    }
    return _prepare("character", output_directory, request)


def prepare_unreal_handoff(
    blender_report_path: Path, output_directory: Path, *, target: UnrealTarget,
) -> HandoffResult:
    """Prepare animation-only import from a successful, checksummed Blender export.

    Caller must resolve blender_report_path from its own managed handoff records.
    The editor script checks the actual target rig before importing any asset.
    """
    report_path = _source(blender_report_path, ".json")
    if report_path.stat().st_size > 1024 * 1024:
        raise ValueError("Blender report exceeds the handoff size limit.")
    report = json.loads(report_path.read_text(encoding="utf-8"))
    if report.get("schema_version") != 1 or report.get("status") != "completed" or report.get("kind") != "blender":
        raise ValueError("A completed Blender handoff report is required.")
    if report.get("fps") != 30 or not isinstance(report.get("frames"), int) or not 2 <= report["frames"] <= 2400:
        raise ValueError("Blender report contains invalid timing.")
    fbx = _source(Path(report["fbx_path"]), ".fbx", report["fbx_sha256"])
    if fbx.parent != report_path.parent or fbx.name != "animation.fbx":
        raise ValueError("FBX must be the managed animation.fbx beside its report.")
    bones = report.get("fbx_bones")
    if not isinstance(bones, list) or not 2 <= len(bones) <= 256:
        raise ValueError("Blender report lacks a bounded FBX hierarchy.")
    names: set[str] = set()
    roots = 0
    for bone in bones:
        if not isinstance(bone, dict) or set(bone) != {"name", "parent"}:
            raise ValueError("Invalid FBX bone metadata.")
        name, parent = bone["name"], bone["parent"]
        if not isinstance(name, str) or not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]{0,127}", name):
            raise ValueError("Bone names must be simple identifiers.")
        if name in names or (parent is not None and parent not in names):
            raise ValueError("FBX hierarchy must be unique and ordered parent first.")
        roots += parent is None
        names.add(name)
    if roots != 1:
        raise ValueError("FBX hierarchy must have exactly one root.")
    request = {
        "schema_version": 1, "kind": "unreal", "source_fbx": str(fbx),
        "source_sha256": report["fbx_sha256"], "blender_report": str(report_path),
        "blender_report_sha256": _digest(report_path), "fps": float(report["fps"]),
        "frames": report["frames"], "expected_bones": bones,
        "target": target.model_dump(mode="json"),
    }
    return _prepare("unreal", output_directory, request)
