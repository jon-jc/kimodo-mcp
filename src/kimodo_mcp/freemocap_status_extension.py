"""Optional, explicitly launched status observer for the pinned FreeMoCap runtime.

This module imports only the standard library until installation. It mirrors
aggregator terminal messages at publication; the existing UI queue is untouched.
No service is started merely by importing this module.
"""

from __future__ import annotations

import argparse
import inspect
import json
import multiprocessing
import os
import re
import subprocess
import time
import uuid
from pathlib import Path
from urllib.parse import urlsplit

REVISION = "700c8614d11d886dacfea8e52333d3c0b942ef52"
VERSION = "v2.0.0-alpha.25"
PREFIX = "/kimodo-status/v1"
_ORIGINAL_RUN = None
_SESSION_DIRECTORY = None


class LoopbackGuard:
    """Outer guard also covers upstream routes, preflights and WebSockets."""

    def __init__(self, app, *, port, allowed_origins=()):
        self.app, self.port = app, port
        self.allowed_origins = set()
        for origin in allowed_origins:
            parsed = urlsplit(origin)
            if (
                parsed.scheme != "http"
                or parsed.hostname not in {"127.0.0.1", "localhost", "::1"}
                or parsed.username
                or parsed.password
                or parsed.path
                or parsed.query
                or parsed.fragment
            ):
                raise ValueError("Additional UI origins must be explicit loopback HTTP origins")
            parsed.port  # Validate malformed ports before serving anything.
            self.allowed_origins.add(origin)

    async def __call__(self, scope, receive, send):
        if scope["type"] not in {"http", "websocket"}:
            return await self.app(scope, receive, send)
        headers = {key.lower(): value.decode("latin1") for key, value in scope.get("headers", [])}
        host, origin = headers.get(b"host", ""), headers.get(b"origin")
        try:
            parsed = urlsplit("http://" + host)
            valid = (
                parsed.hostname in {"127.0.0.1", "localhost", "::1"}
                and parsed.port == self.port
                and not parsed.username
                and not parsed.password
                and not parsed.path
                and not parsed.query
                and not parsed.fragment
                and (scope.get("client") or (None,))[0] in {"127.0.0.1", "::1"}
            )
            valid = valid and (origin is None or origin == "http://" + host or origin in self.allowed_origins)
            valid = valid and (headers.get(b"sec-fetch-site") != "cross-site" or origin in self.allowed_origins)
        except ValueError:
            valid = False
        if valid:
            return await self.app(scope, receive, send)
        if scope["type"] == "websocket":
            return await send({"type": "websocket.close", "code": 1008})
        await send(
            {
                "type": "http.response.start",
                "status": 403,
                "headers": [(b"content-type", b"text/plain"), (b"x-content-type-options", b"nosniff")],
            }
        )
        await send(
            {
                "type": "http.response.body",
                "body": b"Only explicitly allowed local clients can access this runtime.",
            }
        )


def _write(path, value):
    temporary = path.with_name(path.name + "." + uuid.uuid4().hex + ".tmp")
    try:
        temporary.write_text(json.dumps(value, allow_nan=False), encoding="utf-8")
        for attempt in range(10):
            try:
                temporary.replace(path)
                break
            except PermissionError:
                if attempt == 9:
                    raise
                time.sleep(0.02)
    finally:
        temporary.unlink(missing_ok=True)


def _read(path):
    if not path.is_file() or path.stat().st_size > 16_384:
        raise ValueError("Pipeline observation is missing or oversized")
    result = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(result, dict):
        raise ValueError("Invalid pipeline observation")
    return result


def _identifier(value):
    if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,79}", value):
        raise ValueError("Invalid pipeline identifier")
    return value


def _terminal(path, phase, detail=""):
    if phase not in {"complete", "failed"}:
        return
    record = _read(path)
    # Once failure is observed, a later accidental complete cannot erase it.
    if record.get("phase") == "failed":
        return
    record.update(phase=phase, terminal_at=time.time(), detail=str(detail)[:2000])
    _write(path, record)


def _run_observed(**kwargs):
    """Spawn-safe target: import the original class in the separate child."""
    global _ORIGINAL_RUN
    if _ORIGINAL_RUN is None:
        from freemocap.core.pipeline.posthoc.posthoc_aggregation_node import PosthocAggregationNode

        _ORIGINAL_RUN = PosthocAggregationNode._run
    directory = Path(os.environ["KIMODO_FREEMOCAP_STATUS_SESSION"])
    identifier = _identifier(kwargs["pipeline_id"])
    path = directory / (identifier + ".json")
    binding = _read(path)
    recording = str(Path(kwargs["recording_info"].full_recording_path).resolve())
    if binding["pipeline_id"] != identifier or binding["recording_path"] != recording:
        raise ValueError("Pipeline observation binding changed")
    queue = kwargs["aggregator_progress_pub"]
    original_put = queue.put

    def mirrored_put(message, *args, **options):
        # Preserve the actual Queue type for upstream runtime type checks.
        original_put(message, *args, **options)
        if (
            getattr(message, "pipeline_id", None) == identifier
            and str(Path(getattr(message, "recording_path", "")).resolve()) == recording
        ):
            _terminal(path, str(getattr(message, "phase", "")), getattr(message, "detail", ""))

    queue.put = mirrored_put
    try:
        _ORIGINAL_RUN(**kwargs)
    except BaseException as error:
        _terminal(path, "failed", type(error).__name__ + ": " + str(error))
        raise
    finally:
        queue.put = original_put


def verify_source(repo):
    repo = repo.resolve()
    kwargs = dict(
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        timeout=10,
        check=True,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    revision = subprocess.run(["git", "-C", str(repo), "rev-parse", "HEAD"], **kwargs).stdout.strip()
    dirty = subprocess.run(
        ["git", "-C", str(repo), "status", "--porcelain", "--untracked-files=no"], **kwargs
    ).stdout.strip()
    if revision != REVISION or dirty:
        raise ValueError("Status extension requires the exact clean reviewed FreeMoCap checkout")
    import freemocap

    if freemocap.__version__ != VERSION or Path(freemocap.__file__).resolve().parent.parent != repo:
        raise ValueError("Imported FreeMoCap does not match the configured source checkout")


def install(app, *, repo, status_directory, port=53117, allowed_origins=()):
    """Install before accepting requests; never consumes progress or log queues."""
    global _ORIGINAL_RUN, _SESSION_DIRECTORY
    if _SESSION_DIRECTORY is not None:
        raise RuntimeError("Status extension is already installed in this process")
    verify_source(repo)
    from fastapi import HTTPException
    from freemocap.core.pipeline.posthoc.posthoc_aggregation_node import PosthocAggregationNode
    from freemocap.core.pipeline.posthoc.posthoc_pipeline import PosthocPipeline

    original_create = PosthocAggregationNode.create.__func__
    original_pipeline_create = PosthocPipeline.create.__func__
    observed_pipelines = {}
    _ORIGINAL_RUN = PosthocAggregationNode._run
    required = {"pipeline_id", "recording_info", "aggregator_progress_pub", "ipc"}
    if not required <= inspect.signature(_ORIGINAL_RUN).parameters.keys():
        raise ValueError("Upstream aggregation hook signature changed")
    session_id = uuid.uuid4().hex
    directory = status_directory.resolve() / session_id
    directory.mkdir(parents=True, exist_ok=False)
    _SESSION_DIRECTORY = directory
    os.environ["KIMODO_FREEMOCAP_STATUS_SESSION"] = str(directory)

    def observed_create(cls, **kwargs):
        identifier = _identifier(kwargs["pipeline_id"])
        path = directory / (identifier + ".json")
        if path.exists():
            raise ValueError("Pipeline ID was reused in this service instance")
        _write(
            path,
            {
                "schema_version": 1,
                "instance_id": session_id,
                "pipeline_id": identifier,
                "recording_path": str(Path(kwargs["recording_info"].full_recording_path).resolve()),
                "pipeline_type": str(kwargs["pipeline_type"]),
                "phase": "queued",
                "created_at": time.time(),
                "terminal_at": None,
            },
        )
        return original_create(cls, **kwargs)

    PosthocAggregationNode.create = classmethod(observed_create)
    PosthocAggregationNode._run = staticmethod(_run_observed)

    def observed_pipeline_create(cls, **kwargs):
        pipeline = original_pipeline_create(cls, **kwargs)
        observed_pipelines[pipeline.id] = pipeline
        return pipeline

    PosthocPipeline.create = classmethod(observed_pipeline_create)
    # Last-added middleware wraps upstream permissive CORS and every native route.
    app.add_middleware(LoopbackGuard, port=port, allowed_origins=allowed_origins)

    @app.get(PREFIX)
    def status_capabilities():
        return {
            "schema_version": 1,
            "instance_id": session_id,
            "reviewed_revision": REVISION,
            "upstream_version": VERSION,
            "terminal_publication_observer": True,
        }

    @app.get(PREFIX + "/pipelines/{pipeline_id}")
    def pipeline_status(pipeline_id: str):
        try:
            value = _read(directory / (_identifier(pipeline_id) + ".json"))
        except (ValueError, OSError) as error:
            raise HTTPException(status_code=404, detail="Unknown observed pipeline") from error
        # The manager removes entries BEFORE shutdown; absence is not death.
        # Keep the actual pipeline reference from creation through observed exit.
        pipeline = observed_pipelines.get(pipeline_id)
        if value.get("exit_verified_at"):
            value["workers_alive"] = False
        elif pipeline is None:
            value["workers_alive"] = None
        else:
            try:
                value["workers_alive"] = bool(pipeline.alive)
            except (OSError, RuntimeError, ValueError):
                value["workers_alive"] = None
        if value["workers_alive"] is False:
            # A child can publish failure between the initial read and exit.
            # The post-exit snapshot is the only safe one to persist.
            value = {**_read(directory / (pipeline_id + ".json")), "workers_alive": False}
        value["terminal_verified"] = (
            value["phase"] in {"complete", "failed"} and value["workers_alive"] is False
        )
        if value["terminal_verified"] and not value.get("exit_verified_at"):
            value["exit_verified_at"] = time.time()
            _write(directory / (pipeline_id + ".json"), value)
            observed_pipelines.pop(pipeline_id, None)
        return value


def main():
    parser = argparse.ArgumentParser(
        description="Explicitly start pinned FreeMoCap with durable pipeline observation"
    )
    parser.add_argument("--repo", type=Path, required=True)
    parser.add_argument("--status-directory", type=Path, required=True)
    parser.add_argument("--port", type=int, default=53117)
    parser.add_argument("--allow-origin", action="append", default=[])
    args = parser.parse_args()
    if not 1024 <= args.port <= 65535:
        parser.error("Port must be 1024..65535")
    import sys

    sys.path.insert(0, str(args.repo.resolve()))
    verify_source(args.repo)
    import uvicorn
    from freemocap.app.app import create_fastapi_app
    from freemocap.system.logging.log_queue_consumer import start_log_queue_consumer, stop_log_queue_consumer
    from skellycam.core.ipc.process_management.managed_worker import WorkerMode
    from skellycam.core.ipc.process_management.worker_registry import WorkerRegistry

    start_log_queue_consumer()
    flag = multiprocessing.Value("b", False)
    registry = WorkerRegistry(global_kill_flag=flag, worker_mode=WorkerMode.PROCESS)
    registry.start_heartbeat()
    try:
        app = create_fastapi_app(global_kill_flag=flag, worker_registry=registry, port=args.port)
        install(
            app,
            repo=args.repo,
            status_directory=args.status_directory,
            port=args.port,
            allowed_origins=args.allow_origin,
        )
        # An occupied port fails normally. Never kill another local service.
        uvicorn.run(app, host="127.0.0.1", port=args.port, log_level="warning")
    finally:
        flag.value = True
        registry.shutdown_all()
        stop_log_queue_consumer()


if __name__ == "__main__":
    multiprocessing.freeze_support()
    main()
