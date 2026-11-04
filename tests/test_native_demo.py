"""Synthetic native workspace lifecycle tests: never start an inference runtime."""

import json
import sys
import time
from pathlib import Path
from types import ModuleType, SimpleNamespace

import pytest
from filelock import FileLock

from kimodo_mcp.config import Settings
from kimodo_mcp.jobs import atomic_json
from kimodo_mcp.native_demo import (
    LOG_LIMIT,
    LOG_TAIL,
    NATIVE_URL,
    current_run,
    get_native_workspace,
    owned_path,
    start_native_workspace,
    stop_native_workspace,
    validate_upstream,
    workspace_root,
)
from kimodo_mcp.native_demo_worker import BoundedLog, seed_examples, serve


@pytest.fixture
def settings(tmp_path):
    repo = tmp_path / "upstream"
    repo.mkdir()
    return Settings(data_dir=tmp_path / "data", python=sys.executable, repo=repo)


def launched(monkeypatch, settings):
    calls = []
    monkeypatch.setattr("kimodo_mcp.native_demo.validate_upstream", lambda _: None)
    monkeypatch.setattr("kimodo_mcp.native_demo.port_open", lambda: False)
    monkeypatch.setattr("kimodo_mcp.native_demo.subprocess.Popen", lambda *a, **kw: calls.append((a, kw)))
    result = start_native_workspace(settings)
    return result, calls


def status(settings, state, **extra):
    directory, request = current_run(settings)
    value = {"run_id": request["run_id"], "status": state, "updated_at": time.time(), **extra}
    atomic_json(directory / "status.json", value)
    return directory


def test_status_without_setup_does_not_create_files_or_import_runtime(settings):
    before = set(sys.modules)
    result = get_native_workspace(settings)
    assert result["status"] == "stopped" and result["url"] == NATIVE_URL
    assert not result["owned"] and not settings.data_dir.exists()
    assert not any(name.startswith(("kimodo.", "torch")) for name in set(sys.modules) - before)
    assert stop_native_workspace(settings) == result


def test_fixed_command_owned_reuse_and_nonce_stop(monkeypatch, settings):
    result, calls = launched(monkeypatch, settings)
    assert result["status"] == "starting" and result["owned"] and not result["reused"]
    args, kwargs = calls[0]
    assert args[0] == [sys.executable, "-m", "kimodo_mcp.native_demo_worker", "--run-id", result["owned_run_id"]]
    assert kwargs["cwd"] == workspace_root(settings)
    assert kwargs["env"]["KIMODO_REPO"] == str(settings.repo)
    assert kwargs["env"]["KIMODO_DATA_DIR"] == str(settings.data_dir)
    assert "shell" not in kwargs
    assert start_native_workspace(settings)["reused"] is True and len(calls) == 1
    with pytest.raises(ValueError, match="Stop the existing"):
        start_native_workspace(settings, "kimodo-g1-rp")
    stopped = stop_native_workspace(settings)
    assert stopped["status"] == "stopping"
    directory, _ = current_run(settings)
    assert json.loads((directory / "stop.json").read_text()) == {"run_id": result["owned_run_id"]}


def test_external_port_or_generation_lock_is_not_adopted(monkeypatch, settings):
    monkeypatch.setattr("kimodo_mcp.native_demo.port_open", lambda: True)
    with pytest.raises(ValueError, match="already occupied"):
        start_native_workspace(settings)
    monkeypatch.setattr("kimodo_mcp.native_demo.port_open", lambda: False)
    with FileLock(settings.data_dir / "generation.lock"):
        with pytest.raises(ValueError, match="generation is busy"):
            start_native_workspace(settings)
    assert get_native_workspace(settings)["status"] == "stopped"


def test_readiness_requires_matching_fresh_owned_heartbeat_and_port(monkeypatch, settings):
    launched(monkeypatch, settings)
    directory = status(settings, "ready")
    monkeypatch.setattr("kimodo_mcp.native_demo.port_open", lambda: True)
    assert get_native_workspace(settings)["status"] == "failed"
    with FileLock(workspace_root(settings) / "workspace.lock"):
        assert get_native_workspace(settings)["status"] == "ready"
        status(settings, "ready", updated_at=time.time() - 30)
        assert get_native_workspace(settings)["status"] == "unresponsive"
        status(settings, "ready", run_id="0" * 32)
        with pytest.raises(ValueError, match="identity"):
            get_native_workspace(settings)
        value = json.loads((directory / "status.json").read_text())
        value.update(run_id=current_run(settings)[1]["run_id"], updated_at=float("nan"))
        (directory / "status.json").write_text(json.dumps(value))
        with pytest.raises(ValueError, match="heartbeat"):
            get_native_workspace(settings)
    assert directory.exists()


def test_log_tail_bounded_and_expired_start_is_not_owned(monkeypatch, settings):
    launched(monkeypatch, settings)
    directory, request = current_run(settings)
    request["created_at"] = time.time() - 120
    atomic_json(directory / "request.json", request)
    (directory / "worker.log").write_bytes(b"a" * (LOG_TAIL * 2) + b"end")
    result = get_native_workspace(settings)
    assert result["status"] == "failed" and not result["owned"]
    assert len(result["log_tail"].encode()) == LOG_TAIL and result["log_tail"].endswith("end")
    assert stop_native_workspace(settings)["status"] == "failed"
    assert not (directory / "stop.json").exists()


def test_start_failure_is_recorded_without_adopting_a_pid(monkeypatch, settings):
    monkeypatch.setattr("kimodo_mcp.native_demo.validate_upstream", lambda _: None)
    monkeypatch.setattr("kimodo_mcp.native_demo.port_open", lambda: False)

    def fail(*args, **kwargs):
        raise OSError("test launch failure")

    monkeypatch.setattr("kimodo_mcp.native_demo.subprocess.Popen", fail)
    with pytest.raises(ValueError, match="could not start"):
        start_native_workspace(settings)
    result = get_native_workspace(settings)
    assert result["status"] == "failed" and not result["owned"]
    assert "test launch failure" in result["error"]


@pytest.mark.parametrize("revision,dirty,error", [
    ("wrong", "", "reviewed upstream"),
    ("e1c219c99b4b94ba48be1fb6a1cbc64ecee4c954", " M kimodo/demo/app.py", "clean upstream"),
])
def test_pinned_clean_checkout_required(monkeypatch, settings, revision, dirty, error):
    calls = []

    def run(args, **kwargs):
        import subprocess
        assert kwargs["stdin"] == subprocess.DEVNULL
        if sys.platform == "win32":
            assert kwargs["creationflags"] == subprocess.CREATE_NO_WINDOW
        calls.append(args)
        return SimpleNamespace(stdout=revision if "rev-parse" in args else dirty)

    monkeypatch.setattr("kimodo_mcp.native_demo.subprocess.run", run)
    with pytest.raises(ValueError, match=error):
        validate_upstream(settings)
    assert len(calls) == 2 and all(call[:3] == ["git", "-C", str(settings.repo)] for call in calls)


def test_owned_paths_and_run_ids_are_not_caller_file_paths(settings):
    root = workspace_root(settings)
    with pytest.raises(ValueError, match="paths cannot"):
        owned_path(root, "..", "elsewhere")
    root.mkdir(parents=True)
    atomic_json(root / "current.json", {"run_id": "../../elsewhere"})
    with pytest.raises(ValueError, match="identity"):
        get_native_workspace(settings)
    with pytest.raises(ValueError, match="run id"):
        serve(settings, "../../elsewhere")
    with pytest.raises(ValueError, match="exact identifier"):
        start_native_workspace(settings, "arbitrary/model")


def test_examples_are_copied_without_overwriting_upstream_or_user_data(monkeypatch, settings):
    package = ModuleType("kimodo")
    assets = ModuleType("kimodo.assets")
    package.assets = assets
    monkeypatch.setitem(sys.modules, "kimodo", package)
    monkeypatch.setitem(sys.modules, "kimodo.assets", assets)
    source = settings.repo / "kimodo/assets/demo/examples/model/example"
    source.mkdir(parents=True)
    original = source / "meta.json"
    original.write_text('{"prompt":"original"}')
    destination = seed_examples(settings)
    copied = destination / "model/example/meta.json"
    assert copied.read_bytes() == original.read_bytes()
    copied.write_text("user-authored")
    seed_examples(settings)
    assert copied.read_text() == "user-authored" and "original" in original.read_text()
    assert assets.DEMO_EXAMPLES_ROOT == destination


def test_bounded_worker_log_never_exceeds_cap(tmp_path):
    path = tmp_path / "worker.log"
    log = BoundedLog(path)
    log.write("a" * (LOG_LIMIT + 5))
    log.write("end")
    assert path.stat().st_size == LOG_LIMIT and path.read_bytes().endswith(b"end")
    assert not log.isatty() and log.encoding == "utf-8"


def test_worker_honors_stop_before_runtime_import(monkeypatch, settings):
    launched(monkeypatch, settings)
    stop_native_workspace(settings)
    directory, request = current_run(settings)
    before = set(sys.modules)
    serve(settings, request["run_id"])
    assert json.loads((directory / "status.json").read_text())["status"] == "stopped"
    assert not any(name.startswith(("kimodo.", "torch")) for name in set(sys.modules) - before)


def test_delayed_worker_cannot_adopt_a_superseded_nonce(monkeypatch, settings):
    launched(monkeypatch, settings)
    directory, request = current_run(settings)
    original = __import__("kimodo_mcp.native_demo_worker", fromlist=["current_run"]).current_run
    calls = []

    def replaced(config):
        calls.append(1)
        if len(calls) == 1:
            return original(config)
        return directory, {**request, "run_id": "1" * 32}

    monkeypatch.setattr("kimodo_mcp.native_demo_worker.current_run", replaced)
    monkeypatch.setattr("kimodo_mcp.native_demo_worker.validate_upstream", lambda _: pytest.fail("must not load runtime"))
    serve(settings, request["run_id"])
    value = json.loads((directory / "status.json").read_text())
    assert value["status"] == "failed" and "superseded" in value["error"]
    assert len(calls) == 2


def test_worker_lifecycle_with_fake_native_demo_sets_loopback_before_import(monkeypatch, settings):
    launched(monkeypatch, settings)
    directory, request = current_run(settings)
    monkeypatch.setattr("kimodo_mcp.native_demo_worker.validate_upstream", lambda _: None)
    monkeypatch.setattr("kimodo_mcp.native_demo_worker.seed_examples", lambda _: workspace_root(settings) / "examples")
    ports = iter([False, True])
    monkeypatch.setattr("kimodo_mcp.native_demo_worker.port_open", lambda: next(ports))
    monkeypatch.delenv("KIMODO_ALLOW_DOWNLOADS", raising=False)
    monkeypatch.setenv("HF_MODE", "false")
    for key in ("SERVER_NAME", "SERVER_PORT", "HF_HUB_OFFLINE", "TRANSFORMERS_OFFLINE"):
        monkeypatch.setenv(key, "previous")
    model_calls = []

    class Demo:
        def __init__(self, *, default_model_name):
            import os
            assert os.environ["SERVER_NAME"] == "127.0.0.1"
            assert os.environ["SERVER_PORT"] == "8769" and "HF_MODE" not in os.environ
            assert os.environ["HF_HUB_OFFLINE"] == "1"
            model_calls.append(default_model_name)
            self.server = SimpleNamespace(get_host=lambda: "127.0.0.1", get_port=lambda: 8769)

        def run(self):
            assert json.loads((directory / "status.json").read_text())["status"] == "ready"

    monkeypatch.setitem(sys.modules, "kimodo.demo.app", SimpleNamespace(Demo=Demo))
    # The production worker owns its process cwd; restore it in this in-process test.
    monkeypatch.chdir(Path.cwd())
    serve(settings, request["run_id"])
    assert model_calls == ["kimodo-soma-rp-v1.2"]
    assert json.loads((directory / "status.json").read_text())["status"] == "stopped"
