"""Synthetic publication hooks; no upstream runtime or worker is launched."""

import json
import queue
import sys
import threading
from types import ModuleType, SimpleNamespace

import pytest

from kimodo_mcp import freemocap_status_extension as extension


def binding(tmp_path):
    path = tmp_path / "abc123.json"
    value = {
        "pipeline_id": "abc123",
        "recording_path": str(tmp_path.resolve()),
        "phase": "queued",
        "instance_id": "e" * 32,
    }
    extension._write(path, value)
    return path


def test_observer_preserves_original_queue_and_mirrors_only_own_terminal(tmp_path, monkeypatch):
    path = binding(tmp_path)
    monkeypatch.setenv("KIMODO_FREEMOCAP_STATUS_SESSION", str(tmp_path))
    messages = [
        SimpleNamespace(pipeline_id="abc123:cam0", recording_path=str(tmp_path), phase="complete"),
        SimpleNamespace(pipeline_id="abc123", recording_path=str(tmp_path), phase="triangulating"),
        SimpleNamespace(pipeline_id="abc123", recording_path=str(tmp_path), phase="complete"),
    ]
    original_queue = queue.Queue()

    def original(**kwargs):
        assert kwargs["aggregator_progress_pub"] is original_queue
        for message in messages:
            kwargs["aggregator_progress_pub"].put(message)
            if message is not messages[-1]:
                assert extension._read(path)["phase"] == "queued"

    monkeypatch.setattr(extension, "_ORIGINAL_RUN", original)
    extension._run_observed(
        pipeline_id="abc123",
        recording_info=SimpleNamespace(full_recording_path=tmp_path),
        aggregator_progress_pub=original_queue,
    )
    assert [original_queue.get_nowait() for _ in messages] == messages
    record = extension._read(path)
    assert record["phase"] == "complete" and record["terminal_at"] > 0
    original_queue.put("still works")
    assert original_queue.get_nowait() == "still works"


def test_uncaught_worker_error_is_durable_failure_and_original_exception_survives(tmp_path, monkeypatch):
    path = binding(tmp_path)
    monkeypatch.setenv("KIMODO_FREEMOCAP_STATUS_SESSION", str(tmp_path))

    def original(**kwargs):
        raise ValueError("synthetic pre-initialization failure")

    monkeypatch.setattr(extension, "_ORIGINAL_RUN", original)
    with pytest.raises(ValueError, match="synthetic"):
        extension._run_observed(
            pipeline_id="abc123",
            recording_info=SimpleNamespace(full_recording_path=tmp_path),
            aggregator_progress_pub=queue.Queue(),
        )
    assert extension._read(path)["phase"] == "failed"
    extension._terminal(path, "complete")
    assert extension._read(path)["phase"] == "failed"


def test_wrong_recording_binding_never_runs_worker(tmp_path, monkeypatch):
    binding(tmp_path)
    monkeypatch.setenv("KIMODO_FREEMOCAP_STATUS_SESSION", str(tmp_path))
    monkeypatch.setattr(extension, "_ORIGINAL_RUN", lambda **k: pytest.fail("Wrong pipeline ran"))
    with pytest.raises(ValueError, match="binding"):
        extension._run_observed(
            pipeline_id="abc123",
            recording_info=SimpleNamespace(full_recording_path=tmp_path / "other"),
            aggregator_progress_pub=queue.Queue(),
        )


def test_installed_status_requires_terminal_event_and_dead_workers(tmp_path, monkeypatch):
    class App:
        routes = {}

        def add_middleware(self, cls, **options):
            assert cls is extension.LoopbackGuard

        def get(self, path):
            return lambda function: self.routes.setdefault(path, function)

    class Node:
        @classmethod
        def create(cls, **kwargs):
            return kwargs

        @staticmethod
        def _run(*, pipeline_id, recording_info, aggregator_progress_pub, ipc):
            pass

    class Life:
        def __init__(self, identifier):
            self.id, self._alive, self.exit_callback = identifier, True, None

        @property
        def alive(self):
            return self.exit_callback() if self.exit_callback else self._alive

        @alive.setter
        def alive(self, value):
            self._alive = value

    class Pipeline:
        @classmethod
        def create(cls, **kwargs):
            Node.create(**kwargs)
            return Life(kwargs["pipeline_id"])

    class HTTPException(Exception):
        def __init__(self, **kwargs):
            self.status_code = kwargs["status_code"]

    manager = SimpleNamespace(lock=threading.Lock(), pipelines={"abc123": SimpleNamespace(alive=True)})
    modules = {
        "fastapi": {"HTTPException": HTTPException},
        "freemocap.app.freemocap_application": {
            "get_freemocap_app": lambda: SimpleNamespace(posthoc_pipeline_manager=manager)
        },
        "freemocap.core.pipeline.posthoc.posthoc_aggregation_node": {"PosthocAggregationNode": Node},
        "freemocap.core.pipeline.posthoc.posthoc_pipeline": {"PosthocPipeline": Pipeline},
    }
    for name, members in modules.items():
        module = ModuleType(name)
        module.__dict__.update(members)
        monkeypatch.setitem(sys.modules, name, module)
    monkeypatch.setattr(extension, "verify_source", lambda _: None)
    monkeypatch.setattr(extension, "_SESSION_DIRECTORY", None)
    monkeypatch.setattr(extension, "_ORIGINAL_RUN", None)
    app = App()
    extension.install(app, repo=tmp_path, status_directory=tmp_path / "status")
    capabilities = app.routes[extension.PREFIX]()
    pipeline = Pipeline.create(
        pipeline_id="abc123",
        recording_info=SimpleNamespace(full_recording_path=tmp_path),
        pipeline_type="mocap",
    )
    status = app.routes[extension.PREFIX + "/pipelines/{pipeline_id}"]
    assert status("abc123")["terminal_verified"] is False
    path = tmp_path / "status" / capabilities["instance_id"] / "abc123.json"
    extension._terminal(path, "complete")
    assert status("abc123")["terminal_verified"] is False
    manager.pipelines.clear()
    assert status("abc123")["terminal_verified"] is False
    pipeline.alive = False
    assert status("abc123")["terminal_verified"] is True
    assert json.loads(path.read_text())["phase"] == "complete"
    late = Pipeline.create(pipeline_id="def456", recording_info=SimpleNamespace(full_recording_path=tmp_path), pipeline_type="mocap")
    late_path = path.with_name("def456.json")
    extension._terminal(late_path, "complete")
    late.exit_callback = lambda: extension._terminal(late_path, "failed", "Late failure before exit") or False
    assert status("def456")["phase"] == "failed"
    assert extension._read(late_path)["phase"] == "failed"
    with pytest.raises(ValueError, match="reused"):
        Node.create(
            pipeline_id="abc123",
            recording_info=SimpleNamespace(full_recording_path=tmp_path),
            pipeline_type="mocap",
        )
    with pytest.raises(HTTPException) as error:
        status("../escape")
    assert error.value.status_code == 404
    monkeypatch.delenv("KIMODO_FREEMOCAP_STATUS_SESSION", raising=False)


@pytest.mark.parametrize(
    ("kind", "client", "host", "origin", "allowed", "accepted"),
    [
        ("http", "127.0.0.1", "127.0.0.1:53117", None, [], True),
        ("http", "127.0.0.1", "localhost:53117", "http://localhost:53117", [], True),
        ("http", "127.0.0.1", "localhost:53117", "http://localhost:3000", ["http://localhost:3000"], True),
        ("http", "127.0.0.1", "evil.example:53117", None, [], False),
        ("http", "127.0.0.1", "127.0.0.1:53117", "https://evil.example", [], False),
        ("http", "127.0.0.1", "127.0.0.1:53117", "null", [], False),
        ("websocket", "127.0.0.1", "127.0.0.1:53117", "https://evil.example", [], False),
        ("http", "192.168.0.2", "127.0.0.1:53117", None, [], False),
        ("http", "127.0.0.1", "127.0.0.1:1", None, [], False),
    ],
)
async def test_outer_guard_blocks_foreign_origins_and_host_rebinding(
    kind, client, host, origin, allowed, accepted
):
    reached, messages = [], []

    async def app(scope, receive, send):
        reached.append(True)

    async def send(message):
        messages.append(message)

    headers = [(b"host", host.encode())] + ([(b"origin", origin.encode())] if origin is not None else [])
    guard = extension.LoopbackGuard(app, port=53117, allowed_origins=allowed)
    await guard({"type": kind, "client": (client, 1), "headers": headers}, None, send)
    assert bool(reached) is accepted
    if not accepted:
        assert messages[0].get("status", messages[0].get("code")) in {403, 1008}


async def test_explicit_loopback_origin_supports_browser_cross_host_classification():
    reached = []

    async def app(*args):
        reached.append(True)

    guard = extension.LoopbackGuard(app, port=53117, allowed_origins=["http://localhost:3000"])
    await guard({"type": "http", "client": ("127.0.0.1", 1), "headers": [
        (b"host", b"127.0.0.1:53117"), (b"origin", b"http://localhost:3000"), (b"sec-fetch-site", b"cross-site")
    ]}, None, None)
    assert reached == [True]
