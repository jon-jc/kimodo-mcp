import os
import sys

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client


async def test_real_stdio_handshake_and_validation(tmp_path):
    env = dict(os.environ, KIMODO_DATA_DIR=str(tmp_path))
    params = StdioServerParameters(command=sys.executable, args=["-m", "kimodo_mcp"], env=env)
    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            tools = await session.list_tools()
            names = {tool.name for tool in tools.tools}
            assert {"generate_motion", "get_job", "cancel_job", "validate_motion"} <= names
            result = await session.call_tool("validate_motion", {
                "request": {"segments": [{"text": "A person walks calmly.", "duration": 2}]},
            })
            assert not result.isError
            assert result.structuredContent["plan"]["total_frames"] == 60
            bad = await session.call_tool("validate_motion", {
                "request": {"segments": [{"text": "walk", "duration": -1}]},
            })
            assert bad.isError
            resources = await session.list_resources()
            assert any(str(r.uri) == "kimodo://models" for r in resources.resources)
            model_data = await session.read_resource("kimodo://models")
            assert "kimodo-soma-rp-v1.2" in model_data.contents[0].text
            prompts = await session.list_prompts()
            assert any(p.name == "animation_workflow" for p in prompts.prompts)
