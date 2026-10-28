"""Run only prepared, verified handoffs in the existing loopback Blender session.

The official Blender MCP add-on uses null-delimited JSON over TCP. This client
implements that small protocol without copying add-on code or exposing a code
execution tool. A durable dispatch marker prevents ambiguous retries.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
import socket
import struct
import tempfile
import time
from pathlib import Path
from typing import Any, Literal

from filelock import FileLock, Timeout

_ADDRESS = ("127.0.0.1", 9876)
_RESPONSE_TIMEOUT = 180.0
_MAX_RESPONSE_BYTES = 1024 * 1024
_MAX_ARTIFACT_BYTES = 1024 * 1024 * 1024


def _digest(path: Path) -> str:
    result = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            result.update(chunk)
    return result.hexdigest()


def _reject_constant(value: str):
    raise ValueError(f"Nonfinite JSON value: {value}")


def _read_json(path: Path) -> dict[str, Any]:
    if path.is_symlink() or not path.is_file() or path.stat().st_size > 1024 * 1024:
        raise ValueError("Expected a bounded, regular handoff JSON file.")
    result = json.loads(path.read_text(encoding="utf-8"), parse_constant=_reject_constant)
    if not isinstance(result, dict):
        raise ValueError("Handoff JSON must be an object.")
    return result


def _verified_source(value: str, checksum: str, suffix: str, limit: int) -> Path:
    if not isinstance(value, str) or not isinstance(checksum, str) or not re.fullmatch(r"[0-9a-f]{64}", checksum):
        raise ValueError("Source path and SHA-256 are required.")
    path = Path(value)
    if not path.is_absolute() or path.is_symlink():
        raise ValueError("Source must be an absolute, regular file.")
    path = path.resolve(strict=True)
    if not path.is_file() or path.suffix.lower() != suffix or not 0 < path.stat().st_size <= limit:
        raise ValueError("Source has an unexpected type or size.")
    if _digest(path) != checksum:
        raise ValueError("Source checksum differs from the prepared handoff.")
    return path


def _bvh_bones(path: Path, frames: int) -> list[dict[str, Any]]:
    text = path.read_text(encoding="utf-8")
    hierarchy, separator, motion = text.partition("\nMOTION")
    if not separator or not re.search(rf"(?m)^Frames:\s*{frames}\s*$", motion):
        raise ValueError("BVH frame count differs from the prepared handoff.")
    interval = re.search(r"(?m)^Frame Time:\s*([0-9.eE+-]+)\s*$", motion)
    if not interval or abs(float(interval[1]) - 1 / 30) > 1e-6:
        raise ValueError("BVH frame interval differs from the prepared handoff.")
    bones, stack, names = [], [], set()
    pending: str | None = None
    awaiting_brace = False
    for raw in hierarchy.splitlines():
        line = raw.strip()
        match = re.fullmatch(r"(ROOT|JOINT)\s+([A-Za-z_][A-Za-z0-9_]{0,127})", line)
        if match:
            name = match[2]
            if awaiting_brace or name in names or len(bones) >= 256:
                raise ValueError("BVH hierarchy is not unique and bounded.")
            if (match[1] == "ROOT") != (not stack and not bones):
                raise ValueError("BVH hierarchy must have one root.")
            if stack and stack[-1] is None:
                raise ValueError("BVH joints cannot be children of an end site.")
            bones.append({"name": name, "parent": stack[-1] if stack else None})
            names.add(name)
            pending, awaiting_brace = name, True
        elif line == "End Site":
            if not stack or awaiting_brace:
                raise ValueError("Invalid BVH end site.")
            pending, awaiting_brace = None, True
        elif line == "{":
            if not awaiting_brace:
                raise ValueError("Invalid BVH opening brace.")
            stack.append(pending)
            awaiting_brace = False
        elif line == "}":
            if not stack or awaiting_brace:
                raise ValueError("Invalid BVH closing brace.")
            stack.pop()
    if stack or awaiting_brace or not bones:
        raise ValueError("BVH hierarchy is incomplete.")
    return bones


def _validate_request(request: dict[str, Any], kind: str) -> list[dict[str, Any]]:
    if (
        request.get("schema_version") != 1 or request.get("kind") != kind
        or request.get("fps") != 30 or type(request.get("frames")) is not int
        or not 2 <= request["frames"] <= 2400
    ):
        raise ValueError("Invalid prepared Blender handoff type or timing.")
    if kind == "blender":
        if request.get("armature_name") != "KimodoRig" or request.get("skeleton") not in {"somaskel30", "somaskel77"}:
            raise ValueError("Expected the canonical SOMA Blender handoff.")
        source = _verified_source(request["source_bvh"], request["source_sha256"], ".bvh", 128 * 1024 * 1024)
        return _bvh_bones(source, request["frames"])
    from .artifacts import inspect_motion
    from .handoff_scripts.character import SOURCE_SHA256, landmarks

    if request.get("character_sha256") != SOURCE_SHA256:
        raise ValueError("Character checksum differs from the fitted rig profile.")
    _verified_source(request["character_fbx"], SOURCE_SHA256, ".fbx", 16 * 1024 * 1024)
    motion = _verified_source(request["source_motion"], request["source_sha256"], ".npz", 128 * 1024 * 1024)
    summary = inspect_motion(motion)
    if (
        summary["fps"] != 30 or summary["frames"] != request["frames"] or summary["samples"] != 1
        or summary["skeleton"] not in {"somaskel30", "somaskel77"} or summary["joints"] not in {30, 77}
        or type(request.get("sample_index")) is not int or not 0 <= request["sample_index"] <= 3
        or type(request.get("root_motion", True)) is not bool
    ):
        raise ValueError("Character motion metadata differs from the prepared handoff.")
    return [{"name": name, "parent": values[2]} for name, values in landmarks().items()]


def _output(report: dict[str, Any], directory: Path, key: str, filename: str) -> Path:
    value, checksum = report.get(key + "_path"), report.get(key + "_sha256")
    if not isinstance(value, str) or Path(value) != directory / filename:
        raise RuntimeError("Blender report contains an unexpected output path.")
    try:
        return _verified_source(value, checksum, Path(filename).suffix, _MAX_ARTIFACT_BYTES)
    except (ValueError, OSError) as exc:
        raise RuntimeError("Blender output checksum or file validation failed.") from exc


def _same_hierarchy(actual: Any, expected: list[dict[str, Any]]) -> bool:
    # Blender may reorder siblings when edit bones become object-mode bones.
    # Preserve exact names/parents and parent-before-child order, not incidental
    # sibling insertion order from the fitting profile.
    if not isinstance(actual, list) or len(actual) != len(expected):
        return False
    parents: dict[str, str | None] = {}
    for bone in actual:
        if not isinstance(bone, dict) or set(bone) != {"name", "parent"}:
            return False
        name, parent = bone["name"], bone["parent"]
        if (not isinstance(name, str) or name in parents
                or (parent is not None and (not isinstance(parent, str) or parent not in parents))):
            return False
        parents[name] = parent
    return parents == {bone["name"]: bone["parent"] for bone in expected}


def _validate_glb(path: Path, request: dict[str, Any], bones: list[dict[str, Any]]) -> None:
    """Check the embedded preview rig and timing without loading external files."""
    try:
        with path.open("rb") as stream:
            header = stream.read(12)
            if len(header) != 12 or struct.unpack("<4sII", header) != (b"glTF", 2, path.stat().st_size):
                raise ValueError("Invalid binary glTF header")
            length, kind = struct.unpack("<I4s", stream.read(8))
            if kind != b"JSON" or not 0 < length <= 16 * 1024 * 1024 or length > path.stat().st_size - 20:
                raise ValueError("Invalid bounded glTF JSON chunk")
            model = json.loads(stream.read(length), parse_constant=_reject_constant)
        if len(model["skins"]) != 1 or len(model["animations"]) != 1 or not model["meshes"]:
            raise ValueError("Expected one animated character skin")
        if any("uri" in entry for entry in model.get("buffers", []) + model.get("images", [])):
            raise ValueError("Character preview must embed all buffers and images")
        joints = model["skins"][0]["joints"]
        if not all(type(index) is int and 0 <= index < len(model["nodes"]) for index in joints):
            raise ValueError("Invalid character skin joint indices")
        names = [model["nodes"][index]["name"] for index in joints]
        if len(names) != len(bones) or set(names) != {bone["name"] for bone in bones}:
            raise ValueError("Preview skin differs from the fitted rig")
        samplers = model["animations"][0]["samplers"]
        if not samplers:
            raise ValueError("Preview animation has no timeline")
        for sampler in samplers:
            index = sampler["input"]
            if type(index) is not int or not 0 <= index < len(model["accessors"]):
                raise ValueError("Invalid preview animation accessor")
            timeline = model["accessors"][index]
            if (timeline["type"] != "SCALAR" or timeline["componentType"] != 5126
                    or timeline["count"] != request["frames"] or timeline["min"] != [0]
                    or len(timeline["max"]) != 1
                    or abs(timeline["max"][0] - (request["frames"] - 1) / request["fps"]) > 1e-5):
                raise ValueError("Preview animation timing differs from the source motion")
    except (ValueError, TypeError, KeyError, IndexError, struct.error) as exc:
        raise RuntimeError("Character GLB preview does not match the prepared rig and motion.") from exc


def _validate_report(
    directory: Path, request: dict[str, Any], expected_bones: list[dict[str, Any]],
) -> dict[str, Any]:
    report = _read_json(directory / "blender-report.json")
    character = request["kind"] == "character"
    frame_range = report.get("frame_range")
    if (
        report.get("schema_version") != 1 or report.get("kind") != "blender"
        or report.get("status") != "completed" or report.get("source_sha256") != request["source_sha256"]
        or report.get("fps") != request["fps"] or report.get("frames") != request["frames"]
        or frame_range != [1, request["frames"]] or report.get("bone_count") != len(expected_bones)
        or not _same_hierarchy(report.get("bones"), expected_bones)
    ):
        raise RuntimeError("Blender completion report does not match the prepared handoff.")
    source_key = "source_motion" if character else "source_bvh"
    if report.get(source_key) != request[source_key]:
        raise RuntimeError("Blender completion report names a different input.")
    if character:
        validation = report.get("validation", {})
        if (
            report.get("source_kind") != "character"
            or report.get("character_sha256") != request["character_sha256"]
            or report.get("sample_index") != request["sample_index"]
            or report.get("root_motion") != request.get("root_motion", True)
            or not _same_hierarchy(report.get("fbx_bones"), expected_bones)
            or not isinstance(validation, dict) or validation.get("finite") is not True
            or validation.get("frames") != request["frames"] or validation.get("unweighted_vertices") != 0
        ):
            raise RuntimeError("Character completion report does not match the prepared rig.")
        for key, limit in (("max_weight_sum_error", 1e-5), ("max_bone_length_error_m", 1e-4)):
            value = validation.get(key)
            if type(value) not in (int, float) or not math.isfinite(value) or not 0 <= value <= limit:
                raise RuntimeError("Character deformation validation failed.")
        _output(report, directory, "character_fbx", "character.fbx")
        preview = _output(report, directory, "preview_glb", "character-preview.glb")
        _validate_glb(preview, request, expected_bones)
    else:
        exported = report.get("fbx_bones", [])
        if not isinstance(exported, list) or not exported or not isinstance(exported[0], dict):
            raise RuntimeError("Blender report lacks the FBX armature wrapper.")
        wrapper = exported[0].get("name")
        if wrapper != "KimodoRig":
            raise RuntimeError("Blender report has an unexpected FBX armature wrapper.")
        expected_export = [{"name": wrapper, "parent": None}] + [
            {"name": bone["name"], "parent": bone["parent"] or wrapper} for bone in expected_bones
        ]
        if not _same_hierarchy(exported, expected_export):
            raise RuntimeError("Blender FBX hierarchy differs from its source.")
    _output(report, directory, "fbx", "animation.fbx")
    _output(report, directory, "blend", "character.blend" if character else "animation.blend")
    return report


def _receive(connection: socket.socket) -> dict[str, Any]:
    deadline = time.monotonic() + _RESPONSE_TIMEOUT
    buffer = bytearray()
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise socket.timeout("Blender response deadline elapsed")
        connection.settimeout(remaining)
        chunk = connection.recv(min(65536, _MAX_RESPONSE_BYTES + 1 - len(buffer)))
        if not chunk:
            raise RuntimeError("Blender closed the connection before a complete response.")
        buffer.extend(chunk)
        if len(buffer) > _MAX_RESPONSE_BYTES:
            raise RuntimeError("Blender response exceeds the size limit.")
        if b"\0" in buffer:
            message, trailing = bytes(buffer).split(b"\0", 1)
            if trailing:
                raise RuntimeError("Unexpected data after Blender's response delimiter.")
            result = json.loads(message.decode("utf-8"), parse_constant=_reject_constant)
            if not isinstance(result, dict):
                raise RuntimeError("Blender returned an invalid response.")
            return result


def execute_handoff(
    script_path: Path, source_kind: Literal["skeleton", "character"] = "skeleton",
) -> dict[str, Any]:
    """Run a fixed managed script once, or validate its completed receipt.

    The caller resolves this path under its job storage. No host, port, code,
    or template can be supplied by prompt text. After an uncertain dispatch,
    inspect the existing receipt; this function never resends that operation.
    """
    if source_kind not in {"skeleton", "character"}:
        raise ValueError("Source kind must be skeleton or character.")
    kind = "blender" if source_kind == "skeleton" else "character"
    script = Path(script_path)
    if script.is_symlink() or script.name != kind + "_handoff.py":
        raise ValueError("Expected the managed Blender handoff script.")
    script = script.resolve(strict=True)
    directory = script.parent
    template = Path(__file__).with_name("handoff_scripts") / (kind + ".py")
    trusted_hash = hashlib.sha256(template.read_text(encoding="utf-8").encode("utf-8")).hexdigest()
    if not script.is_file() or script.stat().st_size > 256 * 1024 or _digest(script) != trusted_hash:
        raise ValueError("Prepared script differs from the bundled trusted Blender handoff; prepare it again.")
    request_path = directory / "handoff.json"
    request = _read_json(request_path)
    expected_bones = _validate_request(request, kind)
    request_hash = _digest(request_path)
    lock = FileLock(str(Path(tempfile.gettempdir()) / "kimodo-blender-command-9876.lock"), timeout=0)
    try:
        with lock:
            if (directory / "blender-report.json").exists():
                return _validate_report(directory, request, expected_bones)
            marker = directory / "bridge-dispatch.json"
            if marker.exists() or (directory / "execution.lock").exists():
                raise RuntimeError("Blender handoff was already dispatched; inspect its report before a new attempt.")
            command = (
                "import hashlib as _kh, pathlib as _kp, runpy as _kr\n"
                f"_ks = _kp.Path({json.dumps(str(script))})\n"
                f"if _kh.sha256(_ks.read_bytes()).hexdigest() != {json.dumps(trusted_hash)}:\n"
                "    raise RuntimeError('Handoff script changed before execution')\n"
                f"if _kh.sha256(_ks.with_name('handoff.json').read_bytes()).hexdigest() != {json.dumps(request_hash)}:\n"
                "    raise RuntimeError('Handoff request changed before execution')\n"
                "_kr.run_path(str(_ks), run_name='__main__')\n"
                "result = {'handoff_completed': True}\n"
            )
            payload = json.dumps({"type": "execute", "code": command, "strict_json": True}).encode("utf-8") + b"\0"
            try:
                with socket.create_connection(_ADDRESS, timeout=5.0) as connection:
                    # Connect failure is safe to retry. Once dispatch can begin,
                    # keep the marker even if send/receive or Blender fails.
                    with marker.open("x", encoding="utf-8") as stream:
                        json.dump({"schema_version": 1, "script_sha256": trusted_hash,
                                   "request_sha256": request_hash}, stream)
                    connection.settimeout(5.0)
                    connection.sendall(payload)
                    response = _receive(connection)
            except (OSError, ValueError) as exc:
                raise RuntimeError("Blender connection failed or timed out; inspect the handoff report before retrying.") from exc
            if response.get("status") != "ok" or response.get("result") != {"handoff_completed": True}:
                raise RuntimeError("Blender handoff failed: " + str(response.get("message", "Invalid response"))[:4000])
            return _validate_report(directory, request, expected_bones)
    except Timeout as exc:
        raise RuntimeError("Another Blender handoff owns the loopback connection.") from exc
