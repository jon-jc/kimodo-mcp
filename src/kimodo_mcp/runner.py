"""Independent supervisor: MCP disconnections do not discard long generation jobs."""

from __future__ import annotations

import contextlib
import os
import subprocess
import sys
import time

from filelock import FileLock, Timeout

from .config import Settings
from .jobs import TERMINAL, JobStore, validate_manifest


def run(job_id: str, settings: Settings | None = None, command: list[str] | None = None) -> None:
    settings = settings or Settings.from_env()
    store = JobStore(settings)
    directory = store.directory(job_id)
    lock = FileLock(settings.data_dir / "generation.lock")
    started = time.monotonic()
    process = None
    acquired = False
    try:
        while not acquired:
            job = store.get(job_id)
            if job["status"] in {"completed", "failed", "cancelled", "interrupted"}:
                return
            if job["cancel_requested"]:
                store.update(job_id, "cancelled")
                return
            if time.monotonic() - started > settings.timeout:
                raise TimeoutError("Timed out waiting for the generation slot")
            store.update(job_id, runner_pid=os.getpid())
            try:
                lock.acquire(timeout=0.25)
                acquired = True
            except Timeout:
                continue
        current = store.get(job_id)
        if current["status"] in TERMINAL:
            return
        if current["cancel_requested"]:
            store.update(job_id, "cancelled")
            return
        store.update(job_id, "running", started=time.time())
        args = command or [
            settings.python, "-m", "kimodo_mcp.worker", "--request", str(directory / "request.json"),
            "--output", str(directory),
        ]
        with (directory / "worker.log").open("ab") as log:
            flags = {"creationflags": subprocess.CREATE_NO_WINDOW} if sys.platform == "win32" else {}
            process = subprocess.Popen(
                args, cwd=directory, env=settings.worker_env(), stdin=subprocess.DEVNULL,
                stdout=log, stderr=log, **flags,
            )
            generation_started = time.monotonic()
            while process.poll() is None:
                if store.get(job_id)["cancel_requested"]:
                    process.terminate()
                    try:
                        process.wait(timeout=10)
                    except subprocess.TimeoutExpired:
                        process.kill()
                        process.wait()
                    store.update(job_id, "cancelled")
                    return
                if time.monotonic() - generation_started > settings.timeout:
                    raise TimeoutError("Generation exceeded KIMODO_JOB_TIMEOUT")
                store.update(job_id)
                time.sleep(0.25)
        if store.get(job_id)["cancel_requested"]:
            store.update(job_id, "cancelled")
        elif process.returncode != 0:
            store.update(job_id, "failed", error=f"Generation worker exited with code {process.returncode}")
        elif not (directory / "manifest.json").is_file():
            store.update(job_id, "failed", error="Worker exited without an output manifest")
        else:
            validate_manifest(directory)
            store.update(job_id, "completed", finished=time.time())
    except Exception as exc:
        store.update(job_id, "failed", error=str(exc))
    finally:
        try:
            if process is not None and process.poll() is None:
                with contextlib.suppress(ProcessLookupError):
                    process.kill()
                process.wait(timeout=10)
        finally:
            if acquired:
                lock.release()


if __name__ == "__main__":
    run(sys.argv[1])
