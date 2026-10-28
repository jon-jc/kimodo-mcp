from types import SimpleNamespace

import pytest

from kimodo_mcp import capacity


def test_windows_memory_reads_correct_physical_and_commit_fields(monkeypatch):
    def report(pointer):
        status = pointer._obj
        assert status.length == 64
        status.available_physical = 12 * 1024**3
        status.available_commit = 3 * 1024**3
        return 1

    monkeypatch.setattr(
        capacity.ctypes, "WinDLL", lambda *a, **k: SimpleNamespace(GlobalMemoryStatusEx=report), raising=False
    )
    assert capacity._windows_memory() == (12 * 1024**3, 3 * 1024**3)


def test_windows_api_failure_is_not_zero_memory(monkeypatch):
    def failure(pointer):
        return 0

    monkeypatch.setattr(
        capacity.ctypes,
        "WinDLL",
        lambda *a, **k: SimpleNamespace(GlobalMemoryStatusEx=failure),
        raising=False,
    )
    monkeypatch.setattr(capacity.sys, "platform", "win32")
    assert capacity.capacity_snapshot() == {
        "available_memory_gib": None,
        "commit_headroom_gib": None,
        "cpu_text_encoder_warning": None,
    }


def test_linux_available_and_commit_are_distinct(monkeypatch):
    monkeypatch.setattr(capacity.sys, "platform", "linux")
    monkeypatch.setattr(
        capacity.Path,
        "read_text",
        lambda *a, **k: "MemAvailable: 25165824 kB\nCommitLimit: 33554432 kB\nCommitted_AS: 31457280 kB\n",
    )
    monkeypatch.setenv("TEXT_ENCODER_DEVICE", "cpu")
    result = capacity.capacity_snapshot()
    assert result["available_memory_gib"] == 24 and result["commit_headroom_gib"] == 2
    assert "20 GiB" in result["cpu_text_encoder_warning"]


def test_linux_missing_fields_and_overcommit(monkeypatch):
    monkeypatch.setattr(
        capacity.Path, "read_text", lambda *a, **k: "CommitLimit: 10 kB\nCommitted_AS: 20 kB\n"
    )
    assert capacity._linux_memory() == (None, 0)


@pytest.mark.parametrize("platform", ["linux", "darwin", "unsupported"])
def test_unavailable_memory_never_prevents_status(monkeypatch, platform):
    monkeypatch.setattr(capacity.sys, "platform", platform)
    monkeypatch.setattr(
        capacity.Path, "read_text", lambda *a, **k: (_ for _ in ()).throw(OSError("unavailable"))
    )
    result = capacity.capacity_snapshot()
    assert result["available_memory_gib"] is None and result["commit_headroom_gib"] is None


@pytest.mark.parametrize(
    "device,available,commit,warning",
    [
        ("cpu", 19, 30, True),
        ("cpu", 30, 19, True),
        ("cpu", 20, 20, False),
        ("cuda:0", 2, 2, False),
        ("cpu", None, 8, True),
    ],
)
def test_advisory_threshold_respects_encoder_configuration(monkeypatch, device, available, commit, warning):
    monkeypatch.setenv("TEXT_ENCODER_DEVICE", device)
    monkeypatch.setattr(capacity.sys, "platform", "win32")
    monkeypatch.setattr(
        capacity,
        "_windows_memory",
        lambda: tuple(None if v is None else v * 1024**3 for v in (available, commit)),
    )
    assert bool(capacity.capacity_snapshot()["cpu_text_encoder_warning"]) is warning


def test_editor_checks_fixed_loopback_ports_bounded_cached_and_copy_safe(monkeypatch):
    calls = []
    now = [10.0]

    class Connection:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

    def connect(address, timeout):
        calls.append((address, timeout))
        if address[1] == 8000:
            raise TimeoutError()
        return Connection()

    monkeypatch.setattr(capacity.socket, "create_connection", connect)
    monkeypatch.setattr(capacity.time, "monotonic", lambda: now[0])
    monkeypatch.setattr(capacity, "_editor_checked_at", float("-inf"))
    monkeypatch.setattr(capacity, "_editor_cache", {})
    result = capacity.editor_connections()
    assert result == {"blender": {"reachable": True}, "unreal": {"reachable": False}}
    assert calls == [(("127.0.0.1", 9876), 0.2), (("127.0.0.1", 8000), 0.2)]
    result["blender"]["reachable"] = False
    assert capacity.editor_connections()["blender"]["reachable"] is True
    assert len(calls) == 2
    now[0] = 15.0
    capacity.editor_connections()
    assert len(calls) == 4
