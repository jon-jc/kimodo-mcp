"""Managed review tools preserve artifacts and reject stale or unbounded inputs."""

import hashlib
import json
import sys
from dataclasses import asdict
from pathlib import Path

import numpy as np
import pytest
from pydantic import ValidationError

from kimodo_mcp.config import Settings
from kimodo_mcp.jobs import JobStore
from kimodo_mcp.quality import ReviewThresholds
from kimodo_mcp.schemas import QualityThresholds
from kimodo_mcp.server import create_server


def completed_motion(store, shift=0):
    job = store.submit({}, launch=False)
    directory = store.directory(job["id"])
    source = directory / "motion_00.npz"
    positions = np.zeros((12, 2, 3))
    positions[:, 1, 1] = 1
    positions[:, :, 0] = shift
    np.savez(source, posed_joints=positions, fps=30, skeleton="fixture",
             joint_names=np.array(["Root", "Head"]), joint_parents=np.array([-1, 0]))
    (directory / "manifest.json").write_text(json.dumps({"samples": 1, "artifacts": [{
        "path": source.name, "format": "npz", "bytes": source.stat().st_size,
        "sha256": hashlib.sha256(source.read_bytes()).hexdigest(),
    }]}))
    store.update(job["id"], "completed")
    return job["id"], source


def test_managed_review_and_comparison_preserve_sources(tmp_path):
    settings = Settings(tmp_path, sys.executable)
    store = JobStore(settings)
    first, a = completed_motion(store)
    second, b = completed_motion(store, shift=2)
    before = [p.read_bytes() for p in (a, b)]
    server = create_server(settings)
    def call(name):
        return server._tool_manager.get_tool(name).fn
    analysis = call("analyze_motion_quality")(first)
    assert analysis["metrics"]["speed"]["max"] == 0
    review = call("create_motion_review")(first)
    assert Path(review["path"]).is_relative_to(a.parent)
    assert json.loads(Path(review["diagnostics_path"]).read_text())["frames"] == 12
    pair = call("compare_motions")(first, second)
    assert pair["comparison"]["position_rmse_m"] == 2
    assert pair["alignment"] == {"mode": "world", "offset_frames": 0}
    relative = call("compare_motions")(first, second, space="root_relative", offset_frames=2)
    assert relative["comparison"]["position_rmse_m"] == 0
    assert relative["comparison"]["frames"] == 10
    assert relative["path"] != pair["path"]
    assert [p.read_bytes() for p in (a, b)] == before


def test_changed_source_and_unlisted_paths_are_rejected(tmp_path):
    settings = Settings(tmp_path, sys.executable)
    store = JobStore(settings)
    job, source = completed_motion(store)
    call = create_server(settings)._tool_manager.get_tool("analyze_motion_quality").fn
    with pytest.raises(ValueError, match="not listed"):
        call(job, "../outside.npz")
    source.write_bytes(source.read_bytes() + b"changed")
    with pytest.raises(ValueError, match="changed|differs"):
        call(job)


def test_threshold_contract_is_finite_and_matches_diagnostics():
    assert QualityThresholds().model_dump() == asdict(ReviewThresholds())
    for values in ({"speed_m_s": float("nan")}, {"floor_y_m": 10001},
                   {"contact_probability": 1.1}, {"speed_m_s": True}, {"unknown": 1}):
        with pytest.raises(ValidationError):
            QualityThresholds(**values)


def test_tools_have_no_arbitrary_input_or_output_path(tmp_path):
    server = create_server(Settings(tmp_path, sys.executable))
    for name in ("analyze_motion_quality", "create_motion_review", "compare_motions"):
        properties = server._tool_manager.get_tool(name).parameters["properties"]
        assert not any("path" in name for name in properties)
