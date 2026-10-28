"""Persistent local queue consumer, started independently of an MCP client's process tree."""

from __future__ import annotations

import os
import signal
import threading
import time

from filelock import FileLock

from .config import Settings
from .jobs import JobStore, atomic_json
from .runner import run


def serve(settings: Settings, stop: threading.Event, command: list[str] | None = None) -> None:
    store = JobStore(settings)
    heartbeat = settings.data_dir / "worker-service.json"
    active = None
    job_id = None
    with FileLock(settings.data_dir / "worker-service.lock", timeout=0):
        try:
            while not stop.is_set():
                atomic_json(heartbeat, {"pid": os.getpid(), "updated": time.time()})
                if active is None or not active.is_alive():
                    with store.connect() as conn:
                        row = conn.execute(
                            "SELECT id FROM jobs WHERE status IN ('queued','cancelling') ORDER BY created LIMIT 1"
                        ).fetchone()
                    if row:
                        job_id = row["id"]
                        active = threading.Thread(target=run, args=(job_id, settings, command), daemon=True)
                        active.start()
                # Queued jobs are owned by this live service even when one long job occupies the GPU.
                with store.connect() as conn:
                    conn.execute("UPDATE jobs SET updated=? WHERE status='queued'", (time.time(),))
                stop.wait(0.5)
        finally:
            if active is not None and active.is_alive():
                store.cancel(job_id)
                active.join(timeout=15)
            heartbeat.unlink(missing_ok=True)


def main() -> None:
    stop = threading.Event()
    signal.signal(signal.SIGINT, lambda *_: stop.set())
    signal.signal(signal.SIGTERM, lambda *_: stop.set())
    serve(Settings.from_env(), stop)


if __name__ == "__main__":
    main()
