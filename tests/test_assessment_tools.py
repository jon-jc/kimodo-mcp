"""Public assessments bind policies and results to verified managed sources."""

import hashlib
import json
import sys

import pytest
from test_quality_tools import completed_motion

from kimodo_mcp.assessment import CandidateReference, QualityPolicy
from kimodo_mcp.config import Settings
from kimodo_mcp.jobs import JobStore
from kimodo_mcp.server import create_server


@pytest.fixture
def managed(tmp_path):
    settings = Settings(tmp_path, sys.executable)
    store = JobStore(settings)
    first, source = completed_motion(store)
    second, _ = completed_motion(store, shift=2)
    server = create_server(settings)
    return server, first, second, source


@pytest.mark.asyncio
async def test_batch_policy_is_canonical_and_keeps_input_order(managed):
    server, first, second, source = managed
    policy = QualityPolicy(checks=[{"metric": "speed", "max_value": 1},
                                  {"metric": "bone_length_drift", "max_value": .001}])
    before = source.read_bytes()
    result = await server._tool_manager.get_tool("assess_motion_candidates").fn(
        [CandidateReference(job_id=second), CandidateReference(job_id=first)], policy,
    )
    assert [item["source"]["job_id"] for item in result["candidates"]] == [second, first]
    assert result["counts"] == {"pass": 2, "fail": 0, "unavailable": 0}
    assert result["automatic_selection"] is False
    canonical = json.dumps(result["policy"], sort_keys=True, separators=(",", ":"))
    assert result["policy_sha256"] == hashlib.sha256(canonical.encode()).hexdigest()
    assert all(item["policy"] == result["policy"] for item in result["candidates"])
    assert all(item["policy_sha256"] == result["policy_sha256"] for item in result["candidates"])
    assert result["candidates"][1]["source"]["sha256"] == hashlib.sha256(before).hexdigest()
    assert source.read_bytes() == before


@pytest.mark.asyncio
async def test_unavailable_is_not_a_pass(managed):
    server, first, _, _ = managed
    result = await server._tool_manager.get_tool("assess_motion_quality").fn(
        first, QualityPolicy(checks=[{"metric": "angular_speed", "max_value": 3}]),
    )
    assert result["status"] == "unavailable"
    assert result["human_review_required"] is True
    assert result["source"]["seed_scope"] == "generation_batch"


@pytest.mark.asyncio
async def test_duplicate_candidates_and_changed_sources_rejected(managed):
    server, first, _, source = managed
    call = server._tool_manager.get_tool("assess_motion_candidates").fn
    candidate = CandidateReference(job_id=first)
    policy = QualityPolicy(checks=[{"metric": "speed", "max_value": 1}])
    with pytest.raises(ValueError, match="distinct"):
        await call([candidate, candidate], policy)
    source.write_bytes(source.read_bytes() + b"changed")
    with pytest.raises(ValueError, match="changed|differs"):
        await call([candidate], policy)


def test_policy_required_and_no_arbitrary_paths(managed):
    server, *_ = managed
    for name in ("assess_motion_quality", "assess_motion_candidates"):
        schema = server._tool_manager.get_tool(name).parameters
        assert "policy" in schema["required"]
        assert not any("path" in name for name in schema["properties"])
