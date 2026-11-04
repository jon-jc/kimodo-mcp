import copy
import json
import sys
from concurrent.futures import ThreadPoolExecutor

import pytest

from kimodo_mcp.animation_plans import MAX_PLAN_BYTES, AnimationPlanStore
from kimodo_mcp.config import Settings
from kimodo_mcp.jobs import JobStore
from kimodo_mcp.planning import AnimationRevision
from kimodo_mcp.schemas import GenerateRequest
from kimodo_mcp.server import create_server


def request():
    return GenerateRequest(segments=[{"text": "Walk forward", "duration": 2},
                                     {"text": "Wave slowly", "duration": 2}],
                           seed=417, steps=37, cfg_text=3.0, cfg_constraint=4.0,
                           transition_frames=7, first_heading_angle=0.5,
                           constraints=[{"type": "root2d", "frame_indices": [0, 90],
                                         "smooth_root_2d": [[1, 2], [2, 3]]}])


def test_save_exact_request_deduplicates_and_reopens_without_jobs(tmp_path):
    store = AnimationPlanStore(tmp_path)
    original = request().model_dump(mode="json")
    first = store.save("Greeting", request(), "Two deliberate beats")
    second = AnimationPlanStore(tmp_path).save("Greeting", request(), "Two deliberate beats")
    assert first["reused"] is False and second["reused"] is True
    assert first["created_at"] == second["created_at"]
    assert first["plan_id"] == second["plan_id"]
    assert first["request"] == original
    assert not (tmp_path / "jobs").exists()
    first["request"]["constraints"].clear()
    assert store.get(first["plan_id"])["request"] == original


def test_parent_and_metadata_are_part_of_immutable_identity(tmp_path):
    store = AnimationPlanStore(tmp_path)
    first = store.save("First", request(), "Story")
    child = store.save("First", request(), "Story", first["plan_id"])
    changed = store.save("Other title", request(), "Story")
    assert len({first["plan_id"], child["plan_id"], changed["plan_id"]}) == 3
    assert child["parent_plan_id"] == first["plan_id"]
    with pytest.raises(ValueError, match="does not exist"):
        store.save("Missing parent", request(), parent_plan_id="0" * 64)


@pytest.mark.parametrize("field,value", [
    ("title", "Changed"), ("description", "Changed"), ("created_at", "2000-01-01T00:00:00+00:00"),
    ("request_sha256", "0" * 64), ("parent_plan_id", "0" * 64), ("unexpected", True),
])
def test_modified_receipts_are_rejected_and_counted(tmp_path, field, value):
    store = AnimationPlanStore(tmp_path)
    saved = store.save("Valid", request())
    path = store.root / f"{saved['plan_id']}.json"
    raw = json.loads(path.read_text())
    raw[field] = value
    path.write_text(json.dumps(raw))
    with pytest.raises(ValueError):
        store.get(saved["plan_id"])
    assert store.list()["invalid_count"] == 1
    assert store.list()["plans"] == []
    with pytest.raises(ValueError):
        store.save("Valid", request())


def test_request_corruption_and_oversized_reads_are_rejected(tmp_path):
    store = AnimationPlanStore(tmp_path)
    saved = store.save("Valid", request())
    path = store.root / f"{saved['plan_id']}.json"
    raw = json.loads(path.read_text())
    raw["request"]["seed"] += 1
    path.write_text(json.dumps(raw))
    with pytest.raises(ValueError, match="hash"):
        store.get(saved["plan_id"])
    path.write_bytes(b" " * (MAX_PLAN_BYTES + 1))
    with pytest.raises(ValueError, match="bounded"):
        store.get(saved["plan_id"])


def test_summary_paging_never_contains_heavy_requests(tmp_path):
    store = AnimationPlanStore(tmp_path)
    for index in range(4):
        store.save(f"Plan {index}", request())
    (store.root / "bad.json").write_text("not json")
    full = store.list()
    page = store.list(2, 1)
    assert (page["total"], page["invalid_count"]) == (4, 1)
    assert page["plans"] == full["plans"][1:3]
    assert all("request" not in item and item["segment_count"] == 2 for item in page["plans"])
    assert store.list(2, 10000)["plans"] == []


def test_deeply_malformed_json_does_not_break_listing(tmp_path):
    store = AnimationPlanStore(tmp_path)
    saved = store.save("Malformed", request())
    (store.root / f"{saved['plan_id']}.json").write_text("[" * 2000 + "0" + "]" * 2000)
    assert store.list()["invalid_count"] == 1


def test_resolved_receipt_must_stay_at_managed_location(tmp_path, monkeypatch):
    store = AnimationPlanStore(tmp_path)
    saved = store.save("Contained", request())
    target = store.root / f"{saved['plan_id']}.json"
    resolve = type(target).resolve

    def redirected(path, *args, **kwargs):
        return tmp_path / "outside.json" if path == target else resolve(path, *args, **kwargs)

    monkeypatch.setattr(type(target), "resolve", redirected)
    with pytest.raises(ValueError, match="redirect"):
        store.get(saved["plan_id"])


@pytest.mark.parametrize("identifier", ["../outside", "A" * 64, "a" * 63, 123, True])
def test_identifiers_cannot_select_arbitrary_paths(tmp_path, identifier):
    with pytest.raises(ValueError):
        AnimationPlanStore(tmp_path).get(identifier)


@pytest.mark.parametrize("kwargs", [{"limit": True}, {"offset": True}, {"limit": 101},
                                    {"offset": -1}, {"offset": 10001}])
def test_list_boundaries(tmp_path, kwargs):
    with pytest.raises(ValueError):
        AnimationPlanStore(tmp_path).list(**kwargs)


@pytest.mark.parametrize("title,description", [(" ", ""), ("x" * 161, ""),
                                               ("Title", "x" * 2001), ("bad\x00title", "")])
def test_metadata_boundaries(tmp_path, title, description):
    with pytest.raises(ValueError):
        AnimationPlanStore(tmp_path).save(title, request(), description)


def test_concurrent_identical_saves_publish_one_complete_receipt(tmp_path):
    def save(_):
        return AnimationPlanStore(tmp_path).save("Concurrent", request())
    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(save, range(8)))
    assert len({item["plan_id"] for item in results}) == 1
    assert sum(not item["reused"] for item in results) == 1
    assert AnimationPlanStore(tmp_path).list()["total"] == 1


def test_tools_preserve_advanced_request_revision_and_exact_submission(tmp_path, monkeypatch):
    settings = Settings(tmp_path, sys.executable)
    submit = JobStore.submit
    monkeypatch.setattr(JobStore, "submit", lambda self, value, *, context=None:
                        submit(self, value, launch=False, context=context))
    server = create_server(settings)
    def tool(name):
        return server._tool_manager.get_tool(name).fn
    original = tool("save_animation_plan")("Shared", request(), "Greeting scene")
    snapshot = copy.deepcopy(original["request"])
    revision = AnimationRevision(beats=[{"text": "Walk cautiously", "duration": 2.01},
                                        {"text": "Wave politely", "duration": 2}])
    preview = tool("revise_animation_plan")(original["plan_id"], revision)
    assert preview["request"]["constraints"] == snapshot["constraints"]
    assert tool("list_animation_plans")()["total"] == 1
    changed = tool("revise_animation_plan")(original["plan_id"], revision, save=True)
    assert changed["description"] == "Greeting scene"
    assert changed["parent_plan_id"] == original["plan_id"]
    assert JobStore(settings).list() == []
    job = tool("generate_animation_plan")(changed["plan_id"])
    assert job["request"] == changed["request"]
    assert job["context"] == {"saved_plan_id": changed["plan_id"], "request_sha256": changed["request_sha256"]}
    assert tool("get_animation_plan")(original["plan_id"])["request"] == snapshot


async def test_tool_boundaries_reject_coerced_flags_and_invalid_ids(tmp_path):
    server = create_server(Settings(tmp_path, sys.executable))
    _, saved = await server.call_tool("save_animation_plan", {"title": "Schema", "request": request().model_dump()})
    for name, arguments in [
        ("list_animation_plans", {"limit": True}),
        ("get_animation_plan", {"plan_id": "../outside"}),
        ("revise_animation_plan", {"plan_id": saved["plan_id"], "revision": {"seed": 2}, "save": "true"}),
    ]:
        with pytest.raises(Exception):
            await server.call_tool(name, arguments)
    assert JobStore(Settings(tmp_path, sys.executable)).list() == []
