"""Explicit, owned access to the separately installed original Kimodo demo.

No runtime imports or model loading occur during status reads. Stop requests are
consumed by the worker that owns the nonce, never translated into arbitrary PID
termination. Native files and sessions remain separate from managed motion jobs.
"""

from __future__ import annotations

import json
import math
import os
import re
import socket
import subprocess
import time
import uuid
from pathlib import Path

from filelock import FileLock, Timeout

from .catalog import UPSTREAM_REVISION, model_info
from .config import Settings
from .jobs import atomic_json

NATIVE_PORT = 8769
NATIVE_URL = f"http://127.0.0.1:{NATIVE_PORT}"
RUN_ID = re.compile(r"^[0-9a-f]{32}$")
LOG_LIMIT = 1024 * 1024
LOG_TAIL = 32768
HEARTBEAT_TIMEOUT = 10
START_TIMEOUT = 60
STATES = {"starting", "ready", "stopping", "stopped", "failed"}
NOTES = [
    "This is the original Kimodo UI in a separate local session, not managed job history.",
    "Starting immediately loads the selected model and may require substantial RAM/GPU memory.",
    "The native workspace holds the generation lock until stopped; managed inference waits.",
    "Downloads are disabled unless the operator sets KIMODO_ALLOW_DOWNLOADS=1.",
    "Native local-file load/save controls retain upstream behavior; use trusted local inputs.",
    "The optional upstream demo dependencies, licensed body assets and model access must already be installed.",
]


def workspace_root(settings: Settings) -> Path:
    root = settings.data_dir.resolve() / "native-workspace"
    if root.resolve() != root:
        raise ValueError("Native workspace must remain inside the configured data directory")
    return root


def owned_path(root: Path, *parts: str) -> Path:
    path = root.joinpath(*parts)
    if path.resolve() != path or not path.is_relative_to(root):
        raise ValueError("Native workspace paths cannot redirect outside their managed location")
    return path


def read_json(path: Path) -> dict:
    with path.open("rb") as stream:
        payload = stream.read(16385)
    if len(payload) > 16384:
        raise ValueError("Native workspace receipt exceeds its size limit")
    value = json.loads(payload)
    if not isinstance(value, dict):
        raise ValueError("Native workspace receipt must be an object")
    return value


def port_open() -> bool:
    try:
        with socket.create_connection(("127.0.0.1", NATIVE_PORT), timeout=0.2):
            return True
    except OSError:
        return False


def lock_held(path: Path) -> bool:
    if not path.exists():
        return False
    try:
        with FileLock(path, timeout=0):
            return False
    except Timeout:
        return True


def validate_upstream(settings: Settings) -> None:
    if settings.repo is None or not settings.repo.is_dir():
        raise ValueError("KIMODO_REPO must identify the installed pinned upstream checkout")
    try:
        flags = {"creationflags": subprocess.CREATE_NO_WINDOW} if os.name == "nt" else {}
        revision = subprocess.run(
            ["git", "-C", str(settings.repo), "rev-parse", "HEAD"], check=True,
            capture_output=True, text=True, timeout=10, stdin=subprocess.DEVNULL, **flags,
        ).stdout.strip()
        dirty = subprocess.run(
            ["git", "-C", str(settings.repo), "status", "--porcelain", "--untracked-files=all"],
            check=True, capture_output=True, text=True, timeout=10, stdin=subprocess.DEVNULL, **flags,
        ).stdout.strip()
    except (OSError, subprocess.SubprocessError) as error:
        raise ValueError("Cannot verify the installed upstream checkout") from error
    if revision != UPSTREAM_REVISION:
        raise ValueError("Native workspace requires the reviewed upstream revision")
    if dirty:
        raise ValueError("Native workspace requires a clean upstream checkout; keep authored files in the data directory")


def current_run(settings: Settings) -> tuple[Path, dict] | None:
    root = workspace_root(settings)
    current = owned_path(root, "current.json")
    if not current.exists():
        return None
    value = read_json(current)
    token = value.get("run_id")
    if not isinstance(token, str) or not RUN_ID.fullmatch(token):
        raise ValueError("Invalid native workspace run identity")
    directory = owned_path(root, "runs", token)
    request = read_json(owned_path(directory, "request.json"))
    if request.get("run_id") != token or request.get("schema_version") != 1:
        raise ValueError("Native workspace request identity does not match")
    model_info(request.get("model"))
    created = request.get("created_at")
    if isinstance(created, bool) or not isinstance(created, (int, float)) or not math.isfinite(created):
        raise ValueError("Invalid native workspace creation time")
    return directory, request


def get_native_workspace(settings: Settings) -> dict:
    result = {"status": "stopped", "url": NATIVE_URL, "model": None,
              "owned_run_id": None, "owned": False, "updated_at": None,
              "log_tail": "", "notes": list(NOTES), "reviewed_revision": UPSTREAM_REVISION}
    current = current_run(settings)
    if current is None:
        return result
    directory, request = current
    token = request["run_id"]
    status_path = owned_path(directory, "status.json")
    status = read_json(status_path) if status_path.exists() else {
        "run_id": token, "status": "starting", "updated_at": request["created_at"],
    }
    if status.get("run_id") != token or status.get("status") not in STATES:
        raise ValueError("Native workspace status identity does not match")
    updated = status.get("updated_at")
    if isinstance(updated, bool) or not isinstance(updated, (int, float)) or not math.isfinite(updated):
        raise ValueError("Invalid native workspace heartbeat")
    held = lock_held(owned_path(workspace_root(settings), "workspace.lock"))
    state = status["status"]
    pending = state == "starting" and time.time() - request["created_at"] <= START_TIMEOUT
    fresh = 0 <= time.time() - updated <= HEARTBEAT_TIMEOUT
    if state in {"starting", "ready", "stopping"}:
        if not held and not pending:
            state = "failed"
            result["error"] = "The owned native worker is no longer running; inspect its log before restarting."
        elif not fresh and held:
            state = "unresponsive"
        elif state == "ready" and (not held or not port_open()):
            state = "unresponsive"
    if status.get("error"):
        result["error"] = str(status["error"])[:2000]
    stop = owned_path(directory, "stop.json")
    if stop.exists() and (held or pending):
        if read_json(stop).get("run_id") != token:
            raise ValueError("Native workspace stop identity does not match")
        state = "stopping"
    log = owned_path(directory, "worker.log")
    if log.exists():
        with log.open("rb") as stream:
            stream.seek(max(0, log.stat().st_size - LOG_TAIL))
            result["log_tail"] = stream.read(LOG_TAIL).decode("utf-8", errors="replace")
    result.update(status=state, model=request["model"], owned_run_id=token,
                  owned=held or pending, updated_at=updated)
    return result


def start_native_workspace(settings: Settings, model: str = "kimodo-soma-rp-v1.2") -> dict:
    model_info(model)
    root = workspace_root(settings)
    root.mkdir(parents=True, exist_ok=True)
    with FileLock(owned_path(root, "control.lock"), timeout=5):
        current = get_native_workspace(settings)
        if current["owned"]:
            if current["model"] == model and current["status"] in {"starting", "ready"}:
                return {**current, "reused": True}
            raise ValueError("Stop the existing owned native workspace before starting another model")
        if lock_held(owned_path(root, "workspace.lock")):
            raise ValueError("A native worker owns the workspace lease")
        if port_open():
            raise ValueError("Port 8769 is already occupied; the bridge will not adopt or stop that process")
        if lock_held(owned_path(settings.data_dir.resolve(), "generation.lock")):
            raise ValueError("Managed generation is busy; finish or cancel it before opening the native workspace")
        validate_upstream(settings)
        if not Path(settings.python).is_file():
            raise ValueError("KIMODO_PYTHON must point to the installed runtime Python executable")
        token = uuid.uuid4().hex
        directory = owned_path(root, "runs", token)
        directory.mkdir(parents=True, exist_ok=False)
        request = {"schema_version": 1, "run_id": token, "model": model, "created_at": time.time()}
        atomic_json(owned_path(directory, "request.json"), request)
        atomic_json(owned_path(root, "current.json"), {"run_id": token})
        env = settings.worker_env()
        env["KIMODO_REPO"] = str(settings.repo.resolve())
        flags = ({"creationflags": subprocess.CREATE_NO_WINDOW | subprocess.CREATE_NEW_PROCESS_GROUP}
                 if os.name == "nt" else {"start_new_session": True})
        try:
            subprocess.Popen(
                [settings.python, "-m", "kimodo_mcp.native_demo_worker", "--run-id", token],
                cwd=root, env=env, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL, **flags,
            )
        except OSError as error:
            atomic_json(owned_path(directory, "status.json"), {
                "run_id": token, "status": "failed", "updated_at": time.time(), "error": str(error)[:2000],
            })
            raise ValueError("Native workspace worker could not start") from error
        return {**get_native_workspace(settings), "reused": False}


def stop_native_workspace(settings: Settings) -> dict:
    root = workspace_root(settings)
    if not root.exists():
        return get_native_workspace(settings)
    with FileLock(owned_path(root, "control.lock"), timeout=5):
        result = get_native_workspace(settings)
        if not result["owned"]:
            return result
        directory, request = current_run(settings)
        atomic_json(owned_path(directory, "stop.json"), {"run_id": request["run_id"]})
        return get_native_workspace(settings)
