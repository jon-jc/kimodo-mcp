import sys

from kimodo_mcp.config import Settings
from kimodo_mcp.jobs import JobStore
from kimodo_mcp.planning import AnimationBrief, AnimationRevision
from kimodo_mcp.server import create_server


def test_generation_and_revision_keep_context_and_original(tmp_path, monkeypatch):
    settings = Settings(tmp_path, sys.executable)
    original_submit = JobStore.submit

    def submit(self, request, *, launch=True, context=None):
        return original_submit(self, request, launch=False, context=context)

    monkeypatch.setattr(JobStore, "submit", submit)
    server = create_server(settings)
    generate = server._tool_manager.get_tool("generate_animation").fn
    revise = server._tool_manager.get_tool("revise_animation").fn
    job = generate(AnimationBrief(description="A person waves with their right hand.", seed=4))
    assert job["context"]["brief"]["seed"] == 4
    assert job["request"]["segments"][0]["duration"] == 4
    plan = revise(job["id"], AnimationRevision(description="A person slowly waves with their right hand."))
    assert len(JobStore(settings).list()) == 1
    assert set(plan["changes"]) == {"segments"}
    changed = revise(job["id"], AnimationRevision(seed=0), submit=True)
    assert changed["context"]["parent_job_id"] == job["id"]
    assert changed["request"]["seed"] == 0
    assert JobStore(settings).get(job["id"])["request"]["seed"] == 4
    assert changed["id"] != job["id"]


async def test_planning_is_available_over_validated_tool_boundary(tmp_path):
    server = create_server(Settings(tmp_path, sys.executable))
    _, result = await server.call_tool("plan_animation", {"brief": {"description": "A person walks."}})
    assert result["plan"]["total_frames"] == 120
    assert result["request"]["formats"] == ["npz", "bvh"]
    assert JobStore(Settings(tmp_path, sys.executable)).list() == []
