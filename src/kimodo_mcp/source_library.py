"""Bounded, offline history across generation and both capture workflows.

This index reads recorded metadata only. It neither promotes pending work to
completed nor treats a historical status as current artifact verification.
"""

from __future__ import annotations

import json
import math
import re
import sqlite3
from contextlib import closing
from typing import Literal

from .config import Settings
from .jobs import redact

SourceKind = Literal["all", "kimodo", "easymocap", "freemocap"]
KINDS = ("kimodo", "easymocap", "freemocap")
IDENTIFIER = re.compile(r"^[0-9a-f]{32}$")
MAX_RECORDS = 10_000
MAX_BYTES = 64 * 1024 * 1024


def _timestamp(value: object) -> float:
    try:
        return float(value) if type(value) in {int, float} and math.isfinite(value) and value >= 0 else 0.0
    except OverflowError:
        return 0.0


def _summary(kind: str, record: dict) -> dict:
    identifier = record.get("job_id") if kind == "freemocap" else record.get("id")
    if not isinstance(identifier, str) or not IDENTIFIER.fullmatch(identifier):
        raise ValueError("Invalid stored source identity")
    status = record.get("status")
    if not isinstance(status, str) or len(status) > 40:
        raise ValueError("Invalid stored status")
    request = record.get("request", {})
    if not isinstance(request, dict):
        raise ValueError("Invalid stored request")
    operation = record.get("operation") if kind == "freemocap" else "generate" if kind == "kimodo" else "capture"
    if kind == "kimodo":
        segments = request.get("segments", [])
        text = segments[0].get("text", "Generated motion") if segments and isinstance(segments[0], dict) else "Generated motion"
        title = str(text)
        status_tool = "get_job"
        review_tool = "inspect_motion_frames"
        review_args = {"job_id": identifier, "sample_index": 0, "frame_indices": [0]}
    elif kind == "easymocap":
        title = str(request.get("video_path", "Video capture")).replace("\\", "/").rsplit("/", 1)[-1]
        status_tool = "get_video_mocap_job"
        review_tool = "inspect_video_mocap_frames"
        review_args = {"job_id": identifier, "frame_indices": [0]}
    else:
        if operation not in {"check_sync", "synchronize", "import", "calibrate", "process", "export"}:
            raise ValueError("Invalid stored capture operation")
        title = f"{operation.replace('_', ' ')} · {record.get('recording_id') or identifier[:8]}"
        status_tool = "get_freemocap_job"
        review_tool = "inspect_freemocap_frames" if operation in {"process", "export"} else None
        review_args = {"job_id": identifier, "frame_indices": [0]}
    result = {
        "source_kind": kind, "source_id": identifier, "title": redact(title)[:180],
        "operation": operation, "recorded_status": status,
        "created_at": _timestamp(record.get("created_at", record.get("created"))),
        "updated_at": _timestamp(record.get("updated_at", record.get("updated", record.get("created_at")))),
        "verification": "not_checked", "status_action": {"tool": status_tool, "arguments": {"job_id": identifier}},
        "review": {"tool": review_tool, "arguments": review_args} if status == "completed" and review_tool else None,
    }
    if kind == "freemocap":
        recording = record.get("recording_id")
        result["recording_id"] = recording if isinstance(recording, str) and re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,79}", recording) else None
        runtime = record.get("runtime_key")
        result["runtime_key"] = runtime if isinstance(runtime, str) and re.fullmatch(r"[0-9a-f]{64}", runtime) else None
    return result


def list_motion_sources(settings: Settings, source_kind: SourceKind = "all", limit: int = 30, offset: int = 0) -> dict:
    """Read bounded local metadata without service calls, state repair or file hashing."""
    if source_kind not in ("all", *KINDS) or type(limit) is not int or not 1 <= limit <= 100:
        raise ValueError("Choose a supported source kind and a limit from 1 to 100")
    if type(offset) is not int or not 0 <= offset <= MAX_RECORDS:
        raise ValueError("Offset must be from 0 to 10000")
    root = settings.data_dir.resolve()
    sources, warnings = [], []
    consumed = 0
    skipped = 0
    truncated = False

    def add(kind: str, payload: str, expected_id: str | None = None):
        nonlocal consumed, skipped, truncated
        consumed += len(payload.encode("utf-8"))
        if consumed > MAX_BYTES:
            truncated = True
            return
        try:
            record = json.loads(payload)
            if not isinstance(record, dict):
                raise ValueError("Expected record object")
            summary = _summary(kind, record)
            if expected_id is not None and summary["source_id"] != expected_id:
                raise ValueError("Record identity disagrees with storage")
            sources.append(summary)
        except (ValueError, TypeError, KeyError, IndexError, AttributeError):
            skipped += 1

    if source_kind in {"all", "kimodo"}:
        db = root / "jobs.sqlite3"
        if db.is_file() and db.resolve() == db:
            try:
                with closing(sqlite3.connect(db.as_uri() + "?mode=ro", uri=True, timeout=5)) as conn:
                    rows = conn.execute("SELECT id,status,created,updated,record FROM jobs ORDER BY created DESC,id DESC LIMIT ?", (MAX_RECORDS + 1,))
                    for index, (identifier, status, created, updated, payload) in enumerate(rows):
                        if index >= MAX_RECORDS or consumed > MAX_BYTES:
                            truncated = True
                            break
                        if not isinstance(payload, str) or len(payload) > 2_000_000:
                            skipped += 1
                            continue
                        try:
                            record = json.loads(payload)
                            record.update(id=identifier, status=status, created=created, updated=updated)
                            add("kimodo", json.dumps(record), identifier)
                        except (ValueError, AttributeError, TypeError):
                            skipped += 1
            except sqlite3.Error:
                warnings.append("Generation history could not be read; other source histories remain available.")
    for kind, relative in (("easymocap", "video-mocap/jobs"), ("freemocap", "freemocap/jobs")):
        if source_kind not in {"all", kind}:
            continue
        base = root / relative
        if not base.is_dir():
            continue
        if base.resolve() != base:
            warnings.append(f"{kind} history redirects outside its managed location and was skipped.")
            continue
        count = 0
        for entry in base.iterdir():
            count += 1
            if count > MAX_RECORDS or consumed > MAX_BYTES:
                truncated = True
                break
            identifier = entry.stem if kind == "freemocap" else entry.name
            if not IDENTIFIER.fullmatch(identifier) or (kind == "freemocap" and entry.suffix != ".json"):
                continue
            path = entry if kind == "freemocap" else entry / "job.json"
            try:
                if path.resolve() != path or not path.is_file() or path.stat().st_size > 2_000_000:
                    skipped += 1
                    continue
                add(kind, path.read_text(encoding="utf-8"), identifier)
            except (OSError, UnicodeError):
                skipped += 1
    sources.sort(key=lambda item: (item["created_at"], item["source_kind"], item["source_id"]), reverse=True)
    if len(sources) > MAX_RECORDS:
        sources = sources[:MAX_RECORDS]
        truncated = True
    if skipped:
        warnings.append(f"Skipped {skipped} malformed, oversized or redirected record(s).")
    if truncated:
        warnings.append("History exceeds the bounded scan budget; narrow the source filter. This is a partial index.")
    end = offset + limit
    return {"sources": sources[offset:end], "source_kind": source_kind, "total": len(sources),
            "limit": limit, "offset": offset, "next_offset": end if end < len(sources) else None,
            "partial": truncated, "warnings": warnings, "metadata_only": True,
            "note": "Statuses are recorded history, not live completion or artifact verification. Use status_action and the source-specific inspection tools before continuing."}
