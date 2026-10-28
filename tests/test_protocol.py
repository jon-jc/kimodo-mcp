import asyncio
import os
import socket
import subprocess
import sys

import httpx
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from mcp.client.streamable_http import streamablehttp_client


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


async def test_loopback_http_handshake_and_origin_guard(tmp_path):
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    env = dict(os.environ, KIMODO_DATA_DIR=str(tmp_path))
    process = subprocess.Popen(
        [sys.executable, "-m", "kimodo_mcp", "--transport", "streamable-http", "--port", str(port)],
        env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    url = f"http://127.0.0.1:{port}/mcp"
    try:
        async with httpx.AsyncClient() as client:
            for _ in range(100):
                try:
                    await client.get(url, timeout=1)
                    break
                except httpx.TransportError:
                    await asyncio.sleep(0.1)
            else:
                raise AssertionError("HTTP MCP server did not start")
            rejected = await client.post(url, headers={"Origin": "https://untrusted.example"}, json={})
            assert rejected.status_code == 403
        async with streamablehttp_client(url) as (read, write, _):
            async with ClientSession(read, write) as session:
                await session.initialize()
                tools = await session.list_tools()
                assert {"inspect_motion", "preview_motion"} <= {t.name for t in tools.tools}
                result = await session.call_tool("health_check", {})
                assert result.structuredContent["server"] == "ready"
    finally:
        process.terminate()
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait()
