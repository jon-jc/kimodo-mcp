"""Execute only verified handoff scripts through Unreal's installed Python SDK.

This companion transport fills the native MCP server's animation-only import
gap. It never accepts arbitrary Python, installs engine plugins, or selects an
editor merely because it is the first discovered instance.
"""

from __future__ import annotations

import hashlib
import importlib.util
import json
import math
import socket
import tempfile
import time
from pathlib import Path
from typing import Any

from filelock import FileLock, Timeout

_DISCOVERY_SECONDS = 3.0
_COMMAND_TIMEOUT = 120.0
_SDK_RELATIVE = "Engine/Plugins/Experimental/PythonScriptPlugin/Content/Python/remote_execution.py"


def _read_json(path: Path) -> dict[str, Any]:
    if not path.is_file() or path.stat().st_size > 1024 * 1024:
        raise ValueError("Expected a bounded handoff JSON file.")
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError("Handoff JSON must be an object.")
    return value


def _load_sdk(engine_directory: Path):
    sdk_path = engine_directory / _SDK_RELATIVE
    if not sdk_path.is_file():
        raise FileNotFoundError("The configured engine lacks PythonScriptPlugin's remote_execution.py.")
    spec = importlib.util.spec_from_file_location("kimodo_installed_unreal_remote_execution", sdk_path)
    if spec is None or spec.loader is None:
        raise RuntimeError("Cannot load the installed Unreal Python transport.")
    sdk = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(sdk)
    return sdk


def _select_node(nodes: list[dict[str, Any]], project_file: Path, engine_directory: Path) -> dict[str, Any]:
    matches = []
    for node in nodes:
        try:
            if (
                Path(node["project_root"]).resolve() == project_file.parent
                and node["project_name"] == project_file.stem
                and Path(node["engine_root"]).resolve() == engine_directory / "Engine"
                and isinstance(node["node_id"], str)
                and node["node_id"]
            ):
                matches.append(node)
        except (KeyError, TypeError, OSError, ValueError):
            continue
    if len(matches) != 1:
        raise RuntimeError(
            f"Expected exactly one matching Unreal editor; found {len(matches)}. "
            "Open the intended project with Python remote execution enabled on loopback."
        )
    return matches[0]


def _validate_report(report_path: Path, request: dict[str, Any], project_file: Path) -> dict[str, Any]:
    report = _read_json(report_path)
    target = request["target"]
    expected_asset = target["destination_path"] + "/" + target["asset_name"] + "." + target["asset_name"]
    expected_skeleton = target["skeleton_path"]
    if "." not in expected_skeleton.rsplit("/", 1)[-1]:
        expected_skeleton += "." + expected_skeleton.rsplit("/", 1)[-1]
    expected_tracks = {bone["name"].casefold() for bone in request["expected_bones"]}
    tracks = report.get("tracks", [])
    duration = report.get("duration_seconds")
    if (
        report.get("kind") != "unreal"
        or report.get("status") != "completed"
        or Path(report.get("project_file", "")).resolve() != project_file
        or report.get("source_sha256") != request["source_sha256"]
        or report.get("animation_path") != expected_asset
        or report.get("skeleton_path") != expected_skeleton
        or report.get("fps") != request["fps"]
        or report.get("sampled_keys") != request["frames"]
        or report.get("frame_intervals") != request["frames"] - 1
        or report.get("bone_count") != len(expected_tracks)
        or report.get("track_count") != len(expected_tracks)
        or not isinstance(tracks, list)
        or not all(isinstance(name, str) for name in tracks)
        or len(tracks) != len(expected_tracks)
        or {name.casefold() for name in tracks} != expected_tracks
        or not isinstance(duration, (float, int))
        or not math.isfinite(duration)
        or abs(duration - (request["frames"] - 1) / request["fps"]) > 1e-5
    ):
        raise RuntimeError("Unreal's completion report does not match the prepared handoff.")
    return report


def execute_handoff(script_path: Path, engine_directory: Path, project_file: Path) -> dict[str, Any]:
    """Run a managed handoff on exactly its intended local editor and read its receipt.

    Callers must resolve script_path within their managed job storage and obtain
    engine_directory from operator configuration, never from generated code.
    The report is the success criterion. A timeout must be inspected before retry.
    """
    script_path = Path(script_path)
    if script_path.is_symlink() or script_path.name != "unreal_handoff.py":
        raise ValueError("Expected the managed unreal_handoff.py file.")
    script_path = script_path.resolve(strict=True)
    project_file = Path(project_file).resolve(strict=True)
    engine_directory = Path(engine_directory).resolve(strict=True)
    if project_file.suffix.lower() != ".uproject" or not project_file.is_file():
        raise ValueError("Expected an existing .uproject file.")
    trusted = Path(__file__).with_name("handoff_scripts") / "unreal.py"
    # Preparation reads text and writes LF UTF-8, even when a Windows checkout
    # stores the bundled template with CRLF. Hash those exact generated bytes.
    trusted_hash = hashlib.sha256(trusted.read_text(encoding="utf-8").encode("utf-8")).hexdigest()
    if (
        script_path.stat().st_size > 256 * 1024
        or hashlib.sha256(script_path.read_bytes()).hexdigest() != trusted_hash
    ):
        raise ValueError("Prepared script differs from the bundled trusted Unreal handoff; prepare it again.")
    request = _read_json(script_path.with_name("handoff.json"))
    if (
        request.get("kind") != "unreal"
        or request.get("schema_version") != 1
        or not isinstance(request.get("target"), dict)
        or Path(request["target"].get("project_file", "")).resolve() != project_file
    ):
        raise ValueError("Prepared handoff targets a different project.")
    report_path = script_path.with_name("unreal-report.json")
    lock = FileLock(str(Path(tempfile.gettempdir()) / "kimodo-unreal-command-6776.lock"), timeout=0)
    try:
        with lock:
            if report_path.exists():
                return _validate_report(report_path, request, project_file)
            sdk = _load_sdk(engine_directory)
            config = sdk.RemoteExecutionConfig()
            config.multicast_bind_address = "127.0.0.1"
            config.multicast_ttl = 0
            config.command_endpoint = ("127.0.0.1", 6776)
            remote = sdk.RemoteExecution(config)
            try:
                remote.start()
                deadline = time.monotonic() + _DISCOVERY_SECONDS
                while time.monotonic() < deadline:
                    time.sleep(0.1)
                node = _select_node(remote.remote_nodes, project_file, engine_directory)
                remote.open_command_connection(node["node_id"])
                # The bundled SDK has no public read-timeout option. Bound the
                # accepted loopback command socket instead of waiting forever.
                channel = remote._command_connection._command_channel_socket
                channel.settimeout(_COMMAND_TIMEOUT)
                command = (
                    "import hashlib as _kh, pathlib as _kp, runpy as _kr\n"
                    f"_ks = _kp.Path({json.dumps(str(script_path))})\n"
                    f"if _kh.sha256(_ks.read_bytes()).hexdigest() != {json.dumps(trusted_hash)}:\n"
                    "    raise RuntimeError('Handoff script changed before execution')\n"
                    "_kr.run_path(str(_ks), run_name='__main__')\n"
                )
                response = remote.run_command(command, unattended=True, exec_mode=sdk.MODE_EXEC_FILE)
                if not response.get("success"):
                    raise RuntimeError("Unreal handoff failed: " + str(response.get("result", ""))[:4000])
                return _validate_report(report_path, request, project_file)
            except socket.timeout as exc:
                raise RuntimeError(
                    "Unreal response timed out; inspect the handoff report before retrying."
                ) from exc
            finally:
                remote.stop()
    except Timeout as exc:
        raise RuntimeError("Another Unreal handoff owns the loopback command connection.") from exc
