"""The visual tool library must use the same complete contract as MCP clients."""

import sys

import pytest
from starlette.testclient import TestClient

from kimodo_mcp.config import Settings
from kimodo_mcp.server import create_server
from kimodo_mcp.studio import _ACTIONS, create_app


@pytest.mark.asyncio
async def test_every_registered_tool_has_matching_complete_controls_and_browser_route(tmp_path):
    server = create_server(Settings(tmp_path, sys.executable))
    registry = await server.list_tools()
    assert {tool.name for tool in registry} == _ACTIONS
    for tool in registry:
        result = await server.call_tool("get_tool_schema", {"tool_name": tool.name})
        value = result[1]
        assert value["name"] == tool.name
        assert value["input_schema"] == tool.inputSchema
        assert value["output_schema"] == tool.outputSchema
        assert value["annotations"] == tool.annotations.model_dump(exclude_none=True)
    with pytest.raises(Exception, match="Unknown tool"):
        await server.call_tool("get_tool_schema", {"tool_name": "execute_arbitrary_script"})
    assert not list(tmp_path.glob("jobs/*/job.json"))


def test_browser_reads_full_nested_controls_and_keeps_normal_mutation_validation(tmp_path):
    settings = Settings(tmp_path, sys.executable)
    with TestClient(create_app(settings), base_url="http://127.0.0.1") as client:
        token = client.get("/api/state").json()["csrf_token"]
        endpoint = "/api/actions/get_tool_schema"
        assert client.post(endpoint, json={"tool_name": "start_unirig"}).status_code == 403
        headers = {"X-Studio-Token": token}
        controls = client.post(endpoint, json={"tool_name": "start_unirig"}, headers=headers)
        assert controls.status_code == 200
        schema = controls.json()["result"]["input_schema"]
        fields = schema["$defs"]["UniRigRequest"]["properties"]
        assert fields["do_sample"]["default"] is True
        assert fields["max_new_tokens"]["maximum"] == 2048
        assert client.post("/api/actions/start_unirig", headers=headers, json={"request": {"assets": []}}).status_code == 400
        assert client.post("/api/actions/execute_arbitrary_script", headers=headers, json={}).status_code == 404
        catalog = client.post("/api/actions/get_capabilities", headers=headers, json={}).json()["result"]
        coverage = catalog["native_control_coverage"]
        assert coverage["full_native_parity"] is False
        assert coverage["executes_actions"] is False
        assert len(coverage["sources"]) == 6
