"""Best-effort, read-only capacity and local editor connection snapshots.

No model imports, process launch, editor commands, or network destinations from
user input. Capacity is an estimate, never an admission decision for a job.
"""

from __future__ import annotations

import ctypes
import os
import socket
import sys
import threading
import time
from pathlib import Path

_GIB = 1024**3
_EDITOR_PORTS = {"blender": 9876, "unreal": 8000}
_editor_lock = threading.Lock()
_editor_cache: dict[str, dict[str, bool]] = {}
_editor_checked_at = float("-inf")


class _MemoryStatus(ctypes.Structure):
    _fields_ = [
        ("length", ctypes.c_uint32),
        ("load", ctypes.c_uint32),
        ("total_physical", ctypes.c_uint64),
        ("available_physical", ctypes.c_uint64),
        ("total_commit", ctypes.c_uint64),
        ("available_commit", ctypes.c_uint64),
        ("total_virtual", ctypes.c_uint64),
        ("available_virtual", ctypes.c_uint64),
        ("available_extended_virtual", ctypes.c_uint64),
    ]


def _windows_memory() -> tuple[int, int]:
    status = _MemoryStatus()
    status.length = ctypes.sizeof(status)
    function = ctypes.WinDLL("kernel32", use_last_error=True).GlobalMemoryStatusEx
    function.argtypes = [ctypes.POINTER(_MemoryStatus)]
    function.restype = ctypes.c_int
    if not function(ctypes.byref(status)):
        raise OSError("Windows memory snapshot unavailable")
    return status.available_physical, status.available_commit


def _linux_memory() -> tuple[int | None, int | None]:
    values = {}
    for line in Path("/proc/meminfo").read_text(encoding="ascii").splitlines():
        name, separator, value = line.partition(":")
        if separator and name in {"MemAvailable", "CommitLimit", "Committed_AS"}:
            parts = value.split()
            if len(parts) == 2 and parts[1] == "kB":
                values[name] = max(0, int(parts[0])) * 1024
    commit = None
    if "CommitLimit" in values and "Committed_AS" in values:
        commit = max(0, values["CommitLimit"] - values["Committed_AS"])
    return values.get("MemAvailable"), commit


def capacity_snapshot() -> dict:
    """Return GiB estimates or null when the platform cannot report them."""
    available, commit = None, None
    try:
        if sys.platform == "win32":
            available, commit = _windows_memory()
        elif sys.platform.startswith("linux"):
            available, commit = _linux_memory()
    except (OSError, ValueError, AttributeError, OverflowError):
        pass
    warning = None
    cpu_encoder = os.environ.get("TEXT_ENCODER_DEVICE", "cpu").lower().startswith("cpu")
    if cpu_encoder and any(value is not None and value < 20 * _GIB for value in (available, commit)):
        warning = (
            "Available memory or commit headroom is below the estimated 20 GiB budget for the default "
            "CPU text encoder (about 16 GiB of weights plus overhead). Generation may fail or page heavily. "
            "This is an advisory estimate; custom runtime configurations may need different resources."
        )
    return {
        "available_memory_gib": round(available / _GIB, 2) if available is not None else None,
        "commit_headroom_gib": round(commit / _GIB, 2) if commit is not None else None,
        "cpu_text_encoder_warning": warning,
    }


def editor_connections() -> dict[str, dict[str, bool]]:
    """Check fixed loopback TCP listeners at most once per five seconds.

    A listener is not proof of application identity, the selected project,
    remote Python readiness, or export permissions. No protocol commands run.
    """
    global _editor_cache, _editor_checked_at
    with _editor_lock:
        if time.monotonic() - _editor_checked_at >= 5:
            result = {}
            for name, port in _EDITOR_PORTS.items():
                try:
                    with socket.create_connection(("127.0.0.1", port), timeout=0.2):
                        reachable = True
                except OSError:
                    reachable = False
                result[name] = {"reachable": reachable}
            _editor_cache = result
            _editor_checked_at = time.monotonic()
        return {name: dict(value) for name, value in _editor_cache.items()}
