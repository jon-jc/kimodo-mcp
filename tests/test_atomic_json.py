import json
import sys
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from kimodo_mcp import jobs
from kimodo_mcp.config import Settings
from kimodo_mcp.worker_service import serve


def windows_error(code=5):
    error = PermissionError("Simulated Windows file reader collision")
    error.winerror = code
    return error


@pytest.mark.parametrize("code", [5, 32, 33])
def test_atomic_json_recovers_transient_windows_collision(tmp_path, monkeypatch, code):
    target = tmp_path / "heartbeat.json"
    target.write_text('{"old": true}')
    replace = Path.replace
    attempts, delays = [], []

    def transient(source, destination):
        attempts.append(source)
        if len(attempts) < 3:
            assert json.loads(target.read_text()) == {"old": True}
            assert json.loads(source.read_text()) == {"updated": 123}
            raise windows_error(code)
        return replace(source, destination)

    monkeypatch.setattr(Path, "replace", transient)
    monkeypatch.setattr(jobs.time, "sleep", delays.append)
    jobs.atomic_json(target, {"updated": 123})
    assert len(attempts) == 3 and delays == [0.01, 0.02]
    assert json.loads(target.read_text()) == {"updated": 123}
    assert list(tmp_path.glob("*.tmp")) == []


def test_persistent_windows_failure_is_bounded_and_preserves_previous_file(tmp_path, monkeypatch):
    target = tmp_path / "manifest.json"
    target.write_text('{"old": true}')
    attempts, delays = [], []

    def fail(source, destination):
        attempts.append(source)
        raise windows_error()

    monkeypatch.setattr(Path, "replace", fail)
    monkeypatch.setattr(jobs.time, "sleep", delays.append)
    with pytest.raises(PermissionError):
        jobs.atomic_json(target, {"new": True})
    assert len(attempts) == 6 and sum(delays) == pytest.approx(0.31)
    assert json.loads(target.read_text()) == {"old": True}
    assert list(tmp_path.glob("*.tmp")) == []


def test_non_windows_permissions_fail_immediately(tmp_path, monkeypatch):
    attempts = []

    def fail(source, destination):
        attempts.append(source)
        raise PermissionError("Persistent permissions")

    monkeypatch.setattr(Path, "replace", fail)
    monkeypatch.setattr(jobs.time, "sleep", lambda _: pytest.fail("Non-Windows errors must not retry"))
    with pytest.raises(PermissionError):
        jobs.atomic_json(tmp_path / "request.json", {"new": True})
    assert len(attempts) == 1 and list(tmp_path.iterdir()) == []


def test_invalid_json_never_changes_published_file(tmp_path):
    target = tmp_path / "state.json"
    target.write_text('{"old": true}')
    with pytest.raises(ValueError):
        jobs.atomic_json(target, {"invalid": float("nan")})
    assert json.loads(target.read_text()) == {"old": True}
    assert list(tmp_path.glob("*.tmp")) == []


def test_concurrent_writers_have_separate_complete_temporary_files(tmp_path, monkeypatch):
    target = tmp_path / "state.json"
    barrier = threading.Barrier(2)
    sources = []
    sources_lock = threading.Lock()
    replace = Path.replace

    def simultaneous(source, destination):
        # Synchronize the first publication attempts only. Windows can reject
        # a concurrent replacement transiently; its retry must not wait for a
        # writer that has already completed and left this barrier.
        with sources_lock:
            first_attempt = source not in sources
            if first_attempt:
                sources.append(source)
        if first_attempt:
            barrier.wait(timeout=3)
        return replace(source, destination)

    monkeypatch.setattr(Path, "replace", simultaneous)
    with ThreadPoolExecutor(max_workers=2) as executor:
        futures = [
            executor.submit(jobs.atomic_json, target, {"writer": i, "data": [i] * 100}) for i in range(2)
        ]
        for future in futures:
            future.result(timeout=4)
    assert len(set(sources)) == 2
    result = json.loads(target.read_text())
    assert result["data"] == [result["writer"]] * 100
    assert list(tmp_path.glob("*.tmp")) == []


def test_worker_keeps_publishing_after_transient_heartbeat_collision(tmp_path, monkeypatch):
    stop = threading.Event()
    replace = Path.replace
    attempts, published = [], []

    def collide(source, destination):
        if destination.name == "worker-service.json":
            attempts.append(source)
            if len(attempts) in (2, 3):
                raise windows_error()
            published.append(json.loads(source.read_text()))
            if len(published) == 2:
                stop.set()
        return replace(source, destination)

    monkeypatch.setattr(Path, "replace", collide)
    serve(Settings(tmp_path, sys.executable), stop)
    assert len(published) == 2 and len(attempts) == 4
    assert published[1]["updated"] >= published[0]["updated"]
    assert not (tmp_path / "worker-service.json").exists()
    assert list(tmp_path.glob("*.tmp")) == []
