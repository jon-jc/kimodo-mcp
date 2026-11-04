"""Immutable shared authoring plans, independent of generation jobs and editors."""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Annotated, Literal

from filelock import FileLock
from pydantic import Field, TypeAdapter, field_validator

from .jobs import atomic_json
from .schemas import GenerateRequest, StrictModel

PlanId = Annotated[str, Field(pattern=r"^[0-9a-f]{64}$", min_length=64, max_length=64, strict=True)]
PlanTitle = Annotated[str, Field(min_length=1, max_length=160, strict=True)]
PlanDescription = Annotated[str, Field(max_length=2000, strict=True)]
MAX_PLAN_BYTES = 8_000_000


def _hash(value: dict) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"),
                                     ensure_ascii=True, allow_nan=False).encode()).hexdigest()


class _Content(StrictModel):
    schema_version: Literal[1] = 1
    title: PlanTitle
    description: PlanDescription = ""
    request: GenerateRequest
    parent_plan_id: PlanId | None = None

    @field_validator("title", "description")
    @classmethod
    def readable_text(cls, value: str, info):
        if any(ord(char) < 32 and char not in "\n\r\t" for char in value):
            raise ValueError("Plan text cannot contain control characters")
        if info.field_name == "title" and not value.strip():
            raise ValueError("Plan title cannot be blank")
        return value


class _Receipt(_Content):
    plan_id: PlanId
    request_sha256: PlanId
    created_at: Annotated[str, Field(max_length=40, strict=True)]
    receipt_sha256: PlanId


class AnimationPlanStore:
    def __init__(self, data_dir: Path):
        self.data_dir = Path(data_dir).resolve()
        self.root = self.data_dir / "animation-plans"

    def _root(self) -> Path:
        if self.root.resolve() != self.root:
            raise ValueError("Saved-plan directory must remain inside the configured data directory")
        return self.root

    def _path(self, plan_id: str) -> Path:
        TypeAdapter(PlanId).validate_python(plan_id)
        path = self._root() / f"{plan_id}.json"
        if path.resolve() != path:
            raise ValueError("Saved-plan receipt cannot redirect outside its managed path")
        return path

    def get(self, plan_id: str) -> dict:
        path = self._path(plan_id)
        try:
            with path.open("rb") as stream:
                payload = stream.read(MAX_PLAN_BYTES + 1)
        except FileNotFoundError as error:
            raise ValueError("Saved animation plan does not exist") from error
        if len(payload) > MAX_PLAN_BYTES:
            raise ValueError("Saved animation plan exceeds the bounded receipt size")
        try:
            raw = json.loads(payload)
            receipt = _Receipt.model_validate(raw)
            # New request controls may acquire defaults after a plan is saved.
            # Validate them, but hash exactly the fields originally recorded so
            # immutable plan identities do not change across additive releases.
            values = receipt.model_dump(mode="json", exclude_unset=True)
            if values != raw:
                raise ValueError("Saved animation plan is not canonical")
            content = _Content.model_validate({key: values[key] for key in _Content.model_fields})
            if _hash(content.model_dump(mode="json", exclude_unset=True)) != plan_id or receipt.plan_id != plan_id:
                raise ValueError("Saved animation plan content hash does not match its identifier")
            if _hash(values["request"]) != receipt.request_sha256:
                raise ValueError("Saved animation plan request checksum does not match")
            checksum = values.pop("receipt_sha256")
            if _hash(values) != checksum:
                raise ValueError("Saved animation plan receipt checksum does not match")
            timestamp = datetime.fromisoformat(receipt.created_at)
            if timestamp.tzinfo is None:
                raise ValueError("Saved animation plan timestamp must have a timezone")
        except (UnicodeError, TypeError, KeyError, RecursionError) as error:
            raise ValueError("Invalid saved animation plan receipt") from error
        values.pop("schema_version")
        return {**values, "plan": receipt.request.validated_plan()}

    def save(self, title: str, request: GenerateRequest, description: str = "",
             parent_plan_id: str | None = None) -> dict:
        content = _Content(title=title, description=description, request=request,
                           parent_plan_id=parent_plan_id)
        values = content.model_dump(mode="json")
        plan_id = _hash(values)
        root = self._root()
        root.mkdir(parents=True, exist_ok=True)
        lock = root / ".write.lock"
        if lock.resolve() != lock:
            raise ValueError("Saved-plan lock cannot redirect outside its managed path")
        with FileLock(str(lock), timeout=10):
            if parent_plan_id is not None:
                self.get(parent_plan_id)
            path = self._path(plan_id)
            if path.exists():
                return {**self.get(plan_id), "reused": True}
            receipt = {**values, "plan_id": plan_id, "request_sha256": _hash(values["request"]),
                       "created_at": datetime.now(timezone.utc).isoformat()}
            receipt["receipt_sha256"] = _hash(receipt)
            if len(json.dumps(receipt, indent=2).encode("utf-8")) > MAX_PLAN_BYTES:
                raise ValueError("Saved animation plan exceeds the bounded receipt size")
            atomic_json(path, receipt)
            return {**self.get(plan_id), "reused": False}

    def list(self, limit: int = 20, offset: int = 0) -> dict:
        if type(limit) is not int or not 1 <= limit <= 100:
            raise ValueError("Plan list limit must be an integer from 1 to 100")
        if type(offset) is not int or not 0 <= offset <= 10000:
            raise ValueError("Plan list offset must be an integer from 0 to 10000")
        summaries, invalid_count = [], 0
        for path in self._root().glob("*.json"):
            try:
                saved = self.get(path.stem)
                plan = saved["plan"]
                summaries.append({**{key: saved[key] for key in (
                    "plan_id", "title", "description", "created_at", "parent_plan_id", "request_sha256")},
                    "model": plan["model"], "frames": plan["total_frames"],
                    "duration_seconds": plan["duration_seconds"], "segment_count": len(plan["segment_frames"]),
                    "samples": plan["samples"]})
            except (ValueError, OSError):
                invalid_count += 1
        summaries.sort(key=lambda item: (item["created_at"], item["plan_id"]), reverse=True)
        return {"plans": summaries[offset:offset + limit], "total": len(summaries),
                "invalid_count": invalid_count, "limit": limit, "offset": offset}
