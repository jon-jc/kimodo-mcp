from kimodo_mcp.capabilities import WORKFLOW_IDS
from kimodo_mcp.native_coverage import get_native_control_coverage


def test_native_inventory_distinguishes_coverage_from_runtime_readiness():
    inventory = get_native_control_coverage()
    assert inventory["executes_actions"] is False
    assert inventory["full_native_parity"] is False
    assert {entry["id"] for entry in inventory["sources"]} == {
        "kimodo", "freemocap", "easymocap", "unirig", "animate", "editors",
    }
    for source in inventory["sources"]:
        assert source["source_url"].startswith("https://")
        assert source["available_controls"] and source["external_controls"]
        assert source["unavailable_controls"] and source["notes"]
        assert all(workflow in WORKFLOW_IDS for workflow in source["managed_workflows"])


def test_native_inventory_is_not_shared_mutable_state():
    first = get_native_control_coverage()
    first["sources"][0]["available_controls"].clear()
    assert get_native_control_coverage()["sources"][0]["available_controls"]
