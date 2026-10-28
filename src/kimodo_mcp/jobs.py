"""Durable job records shared by independent MCP client processes."""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
import subprocess
import sys
import time
import uuid
from pathlib import Path
from typing import Any

from .config import Settings

TERMINAL = {"completed", "failed", "cancelled", "interrupted"}
ACTIVE = {"queued", "running", "cancelling"}
JOB_ID = re.compile(r"^[0-9a-f]{32}$")


def atomic_json(path: Path, value: Any) -> None:
    temp = path.with_suffix(path.suffix + ".tmp")
    temp.write_text(json.dumps(value, indent=2, allow_nan=False), encoding="utf-8")
    temp.replace(path)


def contained_file(directory: Path, name: str) -> Path:
    candidate = (directory / name).resolve()
    if not candidate.is_relative_to(directory.resolve()) or not candidate.is_file():
        raise ValueError("Artifact must be an existing file inside its job directory")
    return candidate


def redact(text: str) -> str:
    return re.sub(r"\bhf_[A-Za-z0-9]+\b", "[redacted]", text)


def validate_manifest(directory: Path) -> dict:
    """Verify every exported artifact before declaring the job complete."""
    path = contained_file(directory, "manifest.json")
    if path.stat().st_size > 2_000_000:
        raise ValueError("Output manifest is too large")
    manifest = json.loads(path.read_text(encoding="utf-8"))
    entries = manifest.get("artifacts") if isinstance(manifest, dict) else None
    if not isinstance(entries, list) or not 1 <= len(entries) <= 32:
        raise ValueError("Output manifest must list exported artifacts")
    seen = set()
    for entry in entries:
        if not isinstance(entry, dict):
            raise ValueError("Invalid artifact record")
        name = entry.get("path")
        if not isinstance(name, str) or not re.fullmatch(r"motion_\d{2}(?:_amass)?\.(?:npz|csv|bvh|html)", name) or name in seen:
            raise ValueError("Artifact path is invalid or duplicated")
        seen.add(name)
        artifact = contained_file(directory, name)
        if type(entry.get("bytes")) is not int or entry["bytes"] <= 0 or artifact.stat().st_size != entry["bytes"]:
            raise ValueError("Artifact size does not match its manifest")
        digest = hashlib.sha256()
        with artifact.open("rb") as handle:
            for block in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(block)
        if digest.hexdigest() != entry.get("sha256"):
            raise ValueError("Artifact checksum does not match its manifest")
    return manifest


class JobStore:
    def __init__(self, settings: Settings):
        self.settings = settings
        self.root = settings.data_dir
        self.root.mkdir(parents=True, exist_ok=True)
        (self.root / "jobs").mkdir(exist_ok=True)
        self.db = self.root / "jobs.sqlite3"
        with self.connect() as conn:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute(
                "CREATE TABLE IF NOT EXISTS jobs (id TEXT PRIMARY KEY, status TEXT NOT NULL, "
                "created REAL NOT NULL, updated REAL NOT NULL, record TEXT NOT NULL)"
            )

    def connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db, timeout=30)
        conn.row_factory = sqlite3.Row
        return conn

    def directory(self, job_id: str) -> Path:
        if not JOB_ID.fullmatch(job_id):
            raise ValueError("Invalid job ID")
        path = self.root / "jobs" / job_id
        if not path.resolve().is_relative_to((self.root / "jobs").resolve()):
            raise ValueError("Job directory escapes configured storage")
        return path

    def get(self, job_id: str) -> dict:
        directory = self.directory(job_id)
        with self.connect() as conn:
            row = conn.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
        if row is None:
            raise ValueError("Unknown job ID")
        result = json.loads(row["record"])
        result.update(id=job_id, status=row["status"], created=row["created"], updated=row["updated"])
        for name in ("progress", "manifest"):
            path = directory / (name + ".json")
            if path.is_file() and path.stat().st_size < 2_000_000:
                try:
                    result[name] = json.loads(path.read_text(encoding="utf-8"))
                except (OSError, ValueError):
                    pass
        if "error" in result:
            result["error"] = redact(str(result["error"]))
        if isinstance(result.get("progress"), dict) and "error" in result["progress"]:
            result["progress"]["error"] = redact(str(result["progress"]["error"]))
        return result

    def update(self, job_id: str, status: str | None = None, **values: Any) -> None:
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute("SELECT status,record FROM jobs WHERE id=?", (job_id,)).fetchone()
            if row is None:
                raise ValueError("Unknown job ID")
            # A concurrent cancellation must never be overwritten by a running/completed update.
            current = row["status"]
            if current in TERMINAL:
                return
            if current == "cancelling":
                if status in TERMINAL:
                    status = "cancelled"
                elif status is not None:
                    status = "cancelling"
            record = json.loads(row["record"])
            record.update(values)
            conn.execute(
                "UPDATE jobs SET status=?, updated=?, record=? WHERE id=?",
                (status or current, time.time(), json.dumps(record), job_id),
            )

    def submit(self, request: dict, *, launch: bool = True, context: dict | None = None) -> dict:
        if launch and self.settings.worker_mode == "service" and not self.service_status()["available"]:
            raise ValueError("Generation worker is offline. Start kimodo-mcp-worker outside the MCP client first.")
        job_id = uuid.uuid4().hex
        now = time.time()
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            pending = conn.execute(
                "SELECT COUNT(*) FROM jobs WHERE status IN ('queued','running','cancelling')"
            ).fetchone()[0]
            if pending >= self.settings.max_pending:
                raise ValueError("Job queue is full; wait for a job to finish or cancel one")
            directory = self.directory(job_id)
            directory.mkdir()
            atomic_json(directory / "request.json", request)
            record = {"request": request, "cancel_requested": False, "context": context or {}}
            conn.execute("INSERT INTO jobs VALUES (?,?,?,?,?)", (job_id, "queued", now, now, json.dumps(record)))
        if launch and self.settings.worker_mode == "child":
            try:
                with (directory / "runner.log").open("ab") as log:
                    options: dict[str, Any] = {"start_new_session": True}
                    if sys.platform == "win32":
                        options = {"creationflags": subprocess.CREATE_NO_WINDOW}
                    subprocess.Popen(
                        [sys.executable, "-m", "kimodo_mcp.runner", job_id],
                        env=self.settings.worker_env(), cwd=self.root,
                        stdin=subprocess.DEVNULL, stdout=log, stderr=log, **options,
                    )
            except Exception as exc:
                self.update(job_id, "failed", error=f"Cannot start supervisor: {exc}")
        return self.get(job_id)

    def service_status(self) -> dict:
        try:
            data = json.loads((self.root / "worker-service.json").read_text(encoding="utf-8"))
            return {**data, "available": 0 <= time.time() - data["updated"] < 15}
        except (OSError, ValueError, KeyError, TypeError):
            return {"available": False}

    def cancel(self, job_id: str) -> dict:
        self.update(job_id, "cancelling", cancel_requested=True)
        return self.get(job_id)

    def list(self, limit: int = 20) -> list[dict]:
        with self.connect() as conn:
            rows = conn.execute(
                "SELECT id,status,created,updated FROM jobs ORDER BY created DESC LIMIT ?", (limit,)
            ).fetchall()
        return [dict(row) for row in rows]

    def recover_stale(self, age_seconds: float = 60) -> int:
        """Mark abandoned supervisors terminal; workers retain their own GPU lock."""
        count = 0
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            rows = conn.execute(
                "SELECT id,status,record FROM jobs WHERE status IN ('queued','running','cancelling') AND updated < ?",
                (time.time() - age_seconds,),
            ).fetchall()
            for row in rows:
                record = json.loads(row["record"])
                record["error"] = "Supervisor heartbeat expired. An orphan worker may finish before the next job acquires the worker lock."
                status = "cancelled" if record.get("cancel_requested") else "interrupted"
                conn.execute("UPDATE jobs SET status=?,updated=?,record=? WHERE id=?", (status, time.time(), json.dumps(record), row["id"]))
                count += 1
        return count

    def log(self, job_id: str, lines: int = 40) -> str:
        self.get(job_id)
        path = self.directory(job_id) / "worker.log"
        if not path.is_file():
            return ""
        with path.open("rb") as stream:
            stream.seek(max(0, path.stat().st_size - 32_768))
            return redact("\n".join(stream.read().decode("utf-8", errors="replace").splitlines()[-lines:]))

    def artifact(self, job_id: str, name: str) -> dict:
        job = self.get(job_id)
        if job["status"] != "completed":
            raise ValueError("Artifacts are available after successful completion")
        entries = job.get("manifest", {}).get("artifacts", [])
        item = next((entry for entry in entries if entry["path"] == name), None)
        if item is None:
            raise ValueError("Artifact is not listed in this job's manifest")
        path = contained_file(self.directory(job_id), name)
        return {**item, "absolute_path": str(path), "uri": path.as_uri()}
