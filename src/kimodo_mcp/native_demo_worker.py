"""Fixed native-demo entrypoint; only an explicit start action launches this process."""

from __future__ import annotations

import argparse
import os
import shutil
import sys
import threading
import time
import traceback

from filelock import FileLock

from .config import Settings
from .jobs import atomic_json
from .native_demo import (
    LOG_LIMIT,
    NATIVE_PORT,
    RUN_ID,
    current_run,
    owned_path,
    port_open,
    read_json,
    validate_upstream,
    workspace_root,
)
from .worker import configure_runtime_environment


class BoundedLog:
    """Bound Python stdout/stderr on disk; native file descriptors go to DEVNULL."""

    encoding = "utf-8"

    def __init__(self, path):
        self.path = path
        self.lock = threading.Lock()

    def write(self, text):
        data = str(text).encode("utf-8", errors="replace")[-LOG_LIMIT:]
        with self.lock:
            old = b""
            if self.path.exists():
                with self.path.open("rb") as stream:
                    stream.seek(max(0, self.path.stat().st_size - max(0, LOG_LIMIT - len(data))))
                    old = stream.read(LOG_LIMIT - len(data))
            with self.path.open("wb") as stream:
                stream.write(old + data)
        return len(text)

    def flush(self):
        pass

    def isatty(self):
        return False


def seed_examples(settings: Settings):
    """Copy packaged examples once; never move or overwrite upstream or authored files."""
    import kimodo.assets as assets

    source = settings.repo / "kimodo" / "assets" / "demo" / "examples"
    destination = owned_path(workspace_root(settings), "examples")
    destination.mkdir(parents=True, exist_ok=True)
    for original in sorted(source.rglob("*")):
        if original.is_symlink() or original.resolve() != original:
            raise ValueError("Packaged example files cannot redirect outside the pinned checkout")
        target = owned_path(destination, *original.relative_to(source).parts)
        if original.is_dir():
            target.mkdir(parents=True, exist_ok=True)
        elif original.is_file() and not target.exists():
            # Exclusive creation protects user-authored native examples on reuse.
            with original.open("rb") as reader, target.open("xb") as writer:
                shutil.copyfileobj(reader, writer)
    assets.DEMO_EXAMPLES_ROOT = destination
    return destination


def serve(settings: Settings, run_id: str) -> None:
    if not RUN_ID.fullmatch(run_id):
        raise ValueError("Invalid native workspace run id")
    current = current_run(settings)
    if current is None or current[1]["run_id"] != run_id:
        raise ValueError("Native worker does not own the current request")
    directory, request = current
    root = workspace_root(settings)
    status_path = owned_path(directory, "status.json")
    stop_path = owned_path(directory, "stop.json")
    status_lock = threading.Lock()
    done = threading.Event()
    state = {"run_id": run_id, "status": "starting", "pid": os.getpid()}

    def publish(**changes):
        with status_lock:
            state.update(changes, updated_at=time.time())
            atomic_json(status_path, state)

    def watch():
        while not done.wait(0.5):
            try:
                if stop_path.exists() and read_json(stop_path).get("run_id") == run_id:
                    publish(status="stopped")
                    # This process exits itself. Never signal a PID supplied by a
                    # caller or stale receipt, including during blocking model load.
                    os._exit(0)
                publish()
            except Exception:
                # A transient file-reader collision must not silently orphan the
                # worker. Retry; readers expose a stale heartbeat as unresponsive.
                traceback.print_exc()

    previous = sys.stdout, sys.stderr
    logger = BoundedLog(owned_path(directory, "worker.log"))
    sys.stdout = sys.stderr = logger
    lease = FileLock(owned_path(root, "workspace.lock"), timeout=0)
    lease_owned = False
    try:
        # Serialize nonce adoption with start's receipt replacement. A delayed
        # old worker must never acquire ownership after another run was selected.
        with FileLock(owned_path(root, "control.lock"), timeout=5):
            lease.acquire()
            lease_owned = True
            latest = current_run(settings)
            if latest is None or latest[1]["run_id"] != run_id:
                raise ValueError("Native worker request was superseded before lease acquisition")
        with FileLock(
            owned_path(settings.data_dir.resolve(), "generation.lock"), timeout=0,
        ):
            publish()
            if stop_path.exists() and read_json(stop_path).get("run_id") == run_id:
                publish(status="stopped")
                return
            threading.Thread(target=watch, daemon=True).start()
            validate_upstream(settings)
            if port_open():
                raise ValueError("Port 8769 became occupied before the native server started")
            configure_runtime_environment()
            os.environ["SERVER_NAME"] = "127.0.0.1"
            os.environ["SERVER_PORT"] = str(NATIVE_PORT)
            # Upstream bool(os.environ.get(...)) treats the string 'false' as true.
            os.environ.pop("HF_MODE", None)
            examples = seed_examples(settings)
            os.chdir(root)
            # Import only after the examples path and loopback configuration are set.
            from kimodo.demo.app import Demo

            demo = Demo(default_model_name=request["model"])
            if demo.server.get_host() != "127.0.0.1" or demo.server.get_port() != NATIVE_PORT:
                demo.server.stop()
                raise ValueError("Native demo attempted to use a different host or port")
            if not port_open():
                raise ValueError("Native demo did not listen on its required loopback port")
            publish(status="ready", examples_directory=str(examples))
            demo.run()
            publish(status="stopped")
    except Exception as error:
        traceback.print_exc()
        # A duplicate worker must not overwrite the live owner's heartbeat.
        if lease_owned:
            publish(status="failed", error=str(error)[:2000])
    finally:
        done.set()
        if lease_owned:
            lease.release()
        sys.stdout, sys.stderr = previous


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument("--run-id", required=True)
    args = parser.parse_args(argv)
    serve(Settings.from_env(), args.run_id)


if __name__ == "__main__":
    main()
