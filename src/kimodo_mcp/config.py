"""Operator settings, deliberately separate from tool input."""

from __future__ import annotations

import os
import sys
from dataclasses import dataclass
from pathlib import Path

UPSTREAM_REVISION = "e1c219c99b4b94ba48be1fb6a1cbc64ecee4c954"


@dataclass(frozen=True)
class Settings:
    data_dir: Path
    python: str
    repo: Path | None = None
    timeout: int = 3600
    max_pending: int = 16

    @classmethod
    def from_env(cls) -> Settings:
        base = Path(os.environ.get("LOCALAPPDATA", Path.home() / ".local" / "share"))
        repo = os.environ.get("KIMODO_REPO")
        return cls(
            data_dir=Path(os.environ.get("KIMODO_DATA_DIR", base / "kimodo-mcp")).resolve(),
            python=os.environ.get("KIMODO_PYTHON", sys.executable),
            repo=Path(repo).resolve() if repo else None,
            timeout=max(10, min(86400, int(os.environ.get("KIMODO_JOB_TIMEOUT", "3600")))),
        )

    def worker_env(self) -> dict[str, str]:
        env = os.environ.copy()
        source = str(Path(__file__).resolve().parent.parent)
        paths = [source] + ([str(self.repo)] if self.repo else [])
        env["PYTHONPATH"] = os.pathsep.join(paths)
        env["PYTHONUNBUFFERED"] = "1"
        env.setdefault("TEXT_ENCODER_DEVICE", "cpu")
        env["KIMODO_DATA_DIR"] = str(self.data_dir)
        env["KIMODO_PYTHON"] = self.python
        env["KIMODO_JOB_TIMEOUT"] = str(self.timeout)
        return env
