import asyncio
import json
import os
import socket
import subprocess
import sys
from datetime import timedelta

import httpx
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from mcp.client.streamable_http import streamablehttp_client

EXPECTED_TOOLS = {
    "list_models", "health_check", "plan_animation", "generate_animation", "revise_animation",
    "extract_pose_constraint", "validate_motion", "generate_motion", "get_job", "list_jobs",
    "cancel_job", "get_job_log", "get_artifact", "inspect_motion", "preview_motion",
    "analyze_motion_quality", "create_motion_review", "compare_motions", "get_character_profile",
    "prepare_blender_handoff", "prepare_character_handoff", "execute_blender_handoff", "get_handoff_report",
    "prepare_unreal_handoff", "execute_unreal_handoff", "get_unreal_report", "get_capabilities",
    "get_workflow", "get_input_schema", "get_workspace_state", "get_unreal_target", "save_unreal_target",
    "build_character_preview", "list_job_outputs", "inspect_motion_frames", "extract_motion_constraints",
    "export_motion",
}


async def assert_agent_discovery(session):
    tools = (await session.list_tools()).tools
    assert {tool.name for tool in tools} == EXPECTED_TOOLS
    result = await session.call_tool("get_capabilities", {})
    assert not result.isError
    catalog = result.structuredContent
    assert {tool["name"] for tool in catalog["tools"]} == EXPECTED_TOOLS
    resources = (await session.list_resources()).resources
    assert {str(resource.uri) for resource in resources} == {
        "kimodo://models", "kimodo://request-schema", "kimodo://capabilities", "kimodo://workspace",
    }
    for resource in resources:
        assert resource.mimeType == "application/json" and resource.description
        content = await session.read_resource(str(resource.uri))
        assert content.contents[0].mimeType == "application/json"
        assert json.loads(content.contents[0].text) is not None
    templates = (await session.list_resource_templates()).resourceTemplates
    assert {template.uriTemplate for template in templates} == {
        "kimodo://jobs/{job_id}", "kimodo://schemas/{schema_name}",
        "kimodo://workflows/{workflow}", "kimodo://jobs/{job_id}/outputs",
    }
    assert all(template.mimeType == "application/json" and template.description for template in templates)
    for name in ("generate_request", "animation_brief", "animation_revision", "pose_reference",
                 "quality_thresholds", "unreal_target"):
        tool_schema = await session.call_tool("get_input_schema", {"schema_name": name})
        resource_schema = await session.read_resource(f"kimodo://schemas/{name}")
        assert not tool_schema.isError
        assert tool_schema.structuredContent == json.loads(resource_schema.contents[0].text)
        assert tool_schema.structuredContent["type"] == "object"
    for name in ("generate", "revise", "reuse-pose", "inspect", "compare", "export-motion", "export-skeleton",
                 "export-character", "import-unreal", "recover-job"):
        recipe = await session.call_tool("get_workflow", {"workflow": name})
        resource_recipe = await session.read_resource(f"kimodo://workflows/{name}")
        assert not recipe.isError
        assert recipe.structuredContent == json.loads(resource_recipe.contents[0].text)
        assert {step["tool"] for step in recipe.structuredContent["steps"]} <= EXPECTED_TOOLS
    bad = await session.call_tool("get_workflow", {"workflow": "invented-operation"})
    assert bad.isError
    prompt = await session.get_prompt("animation_workflow", {"description": "A person waves."})
    text = prompt.messages[0].content.text
    assert all(name in text for name in ("plan_animation", "build_character_preview", "get_unreal_target"))
    annotations = {tool.name: tool.annotations for tool in tools}
    assert annotations["get_capabilities"].readOnlyHint
    assert annotations["cancel_job"].destructiveHint
    assert annotations["save_unreal_target"].destructiveHint
    assert annotations["generate_animation"].idempotentHint is False
    assert annotations["build_character_preview"].readOnlyHint is False


async def test_real_stdio_handshake_and_validation(tmp_path):
    # Exercise revision discovery with an open MCP stdin pipe. Git's Windows
    # launcher must never inherit that pipe and wait for the client to disconnect.
    repo = tmp_path / "upstream"
    subprocess.run(["git", "init", str(repo)], check=True, capture_output=True)
    subprocess.run(["git", "-C", str(repo), "-c", "user.name=Protocol Fixture",
                    "-c", "user.email=fixture@example.test", "-c", "commit.gpgsign=false",
                    "commit", "--allow-empty", "-m", "Protocol fixture"], check=True, capture_output=True)
    revision = subprocess.run(["git", "-C", str(repo), "rev-parse", "HEAD"],
                              check=True, capture_output=True, text=True).stdout.strip()
    env = dict(os.environ, KIMODO_DATA_DIR=str(tmp_path), KIMODO_REPO=str(repo))
    params = StdioServerParameters(command=sys.executable, args=["-m", "kimodo_mcp"], env=env)
    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write, read_timeout_seconds=timedelta(seconds=15)) as session:
            initialized = await session.initialize()
            assert "get_capabilities" in initialized.instructions
            await assert_agent_discovery(session)
            workspace = await session.call_tool("get_workspace_state", {"recent_jobs": 0})
            assert workspace.structuredContent["health"]["installed_revision"] == revision
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
                initialized = await session.initialize()
                assert "get_workspace_state" in initialized.instructions
                await assert_agent_discovery(session)
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
