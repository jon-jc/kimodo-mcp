import asyncio
import json
import os
import sys
import threading
import time

import pytest
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

from kimodo_mcp.config import Settings
from kimodo_mcp.jobs import JobStore
from kimodo_mcp.worker_service import serve


def test_generation_requires_live_service(tmp_path):
    store = JobStore(Settings(tmp_path, sys.executable))
    with pytest.raises(ValueError, match="worker is offline"):
        store.submit({})
    assert store.list() == []


async def test_job_completes_after_real_stdio_client_disconnect(tmp_path):
    settings = Settings(tmp_path, sys.executable)
    store = JobStore(settings)
    stop = threading.Event()
    code = (
        "import json,time,hashlib; from pathlib import Path; time.sleep(1); "
        "Path('motion_00.npz').write_bytes(b'fixture'); "
        "Path('manifest.json').write_text(json.dumps({'artifacts':[{'path':'motion_00.npz',"
        "'format':'npz','bytes':7,'sha256':hashlib.sha256(b'fixture').hexdigest()}]}))"
    )
    thread = threading.Thread(target=serve, args=(settings, stop, [sys.executable, "-c", code]))
    thread.start()
    try:
        for _ in range(100):
            if store.service_status()["available"]:
                break
            await asyncio.sleep(0.03)
        env = dict(os.environ, KIMODO_DATA_DIR=str(tmp_path), KIMODO_WORKER_MODE="service")
        params = StdioServerParameters(command=sys.executable, args=["-m", "kimodo_mcp"], env=env)
        async with stdio_client(params) as (read, write):
            async with ClientSession(read, write) as session:
                await session.initialize()
                result = await session.call_tool("generate_motion", {
                    "request": {"segments": [{"text": "Walk", "duration": 2}]},
                })
                assert not result.isError
                job_id = result.structuredContent["id"]
        # This is outside the closed client's lifecycle; SDK closes its entire job object on Windows.
        deadline = time.monotonic() + 10
        while store.get(job_id)["status"] in {"queued", "running"} and time.monotonic() < deadline:
            await asyncio.sleep(0.05)
        assert store.get(job_id)["status"] == "completed", json.dumps(store.get(job_id))
    finally:
        stop.set()
        thread.join(timeout=5)
        assert not thread.is_alive()
