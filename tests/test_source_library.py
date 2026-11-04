import json
import sqlite3
import sys

import pytest
from starlette.testclient import TestClient

from kimodo_mcp.config import Settings
from kimodo_mcp.jobs import JobStore, atomic_json
from kimodo_mcp.server import create_server
from kimodo_mcp.source_library import list_motion_sources
from kimodo_mcp.studio import create_app


def records(tmp_path):
    settings = Settings(tmp_path / "data", sys.executable)
    store = JobStore(settings)
    job = store.submit({"segments": [{"text": "Walk and turn.", "duration": 2}], "num_samples": 2}, launch=False)
    with store.connect() as conn:
        conn.execute("UPDATE jobs SET created=1,status='completed' WHERE id=?", (job["id"],))
    video = settings.data_dir / "video-mocap/jobs" / ("a" * 32)
    video.mkdir(parents=True)
    atomic_json(video / "job.json", {"id": video.name, "status": "completed", "created_at": 2,
                                    "request": {"video_path": "D:/clips/walk.mp4"}})
    capture = settings.data_dir / "freemocap/jobs"
    capture.mkdir(parents=True)
    atomic_json(capture / ("a" * 32 + ".json"), {"job_id": "a" * 32, "status": "completed", "created_at": 3,
                                              "operation": "process", "recording_id": "take-one", "request": {}})
    atomic_json(capture / ("b" * 32 + ".json"), {"job_id": "b" * 32, "status": "submission_unknown", "created_at": 4,
                                              "operation": "import", "recording_id": "take-two", "request": {}})
    return settings, job


def test_combined_history_recovers_agent_receipts_without_runtime_or_file_verification(tmp_path, monkeypatch):
    import httpx

    settings, job = records(tmp_path)
    monkeypatch.setattr(httpx.Client, "request", lambda *args, **kw: pytest.fail("History must stay offline"))
    result = list_motion_sources(settings, limit=2)
    assert result["total"] == 4 and result["next_offset"] == 2 and not result["partial"]
    assert result["sources"][0]["recorded_status"] == "submission_unknown"
    assert result["sources"][0]["review"] is None
    assert result["sources"][1]["review"] == {"tool": "inspect_freemocap_frames", "arguments": {"job_id": "a" * 32, "frame_indices": [0]}}
    rest = list_motion_sources(settings, offset=2)["sources"]
    assert [item["source_kind"] for item in rest] == ["easymocap", "kimodo"]
    assert rest[-1]["source_id"] == job["id"] and rest[-1]["title"] == "Walk and turn."
    assert all(item["verification"] == "not_checked" for item in result["sources"] + rest)
    assert list_motion_sources(settings, "freemocap")["total"] == 2
    # No manifests/outputs/runtime exist: a recorded completion is not re-labelled as verified.
    assert not (settings.data_dir / "freemocap/runtime.json").exists()
    json.dumps(result, allow_nan=False)


def test_history_skips_corrupt_identity_and_oversized_records_without_hiding_other_sources(tmp_path):
    settings, _ = records(tmp_path)
    root = settings.data_dir / "freemocap/jobs"
    (root / ("c" * 32 + ".json")).write_text("{", encoding="utf-8")
    atomic_json(root / ("d" * 32 + ".json"), {"job_id": "e" * 32, "status": "completed", "operation": "process"})
    (root / ("f" * 32 + ".json")).write_bytes(b" " * 2_000_001)
    result = list_motion_sources(settings)
    assert result["total"] == 4
    assert "Skipped 3" in result["warnings"][0]


def test_empty_library_does_not_create_a_database_or_reference_folders(tmp_path):
    settings = Settings(tmp_path / "not-created", sys.executable)
    assert list_motion_sources(settings)["sources"] == []
    assert not settings.data_dir.exists()


def test_readonly_database_handle_closes_without_waiting_for_garbage_collection(tmp_path, monkeypatch):
    settings, _ = records(tmp_path)
    closed = []
    connect = sqlite3.connect

    class CheckedConnection(sqlite3.Connection):
        def close(self):
            closed.append(True)
            super().close()

    monkeypatch.setattr(sqlite3, "connect", lambda *a, **kw: connect(*a, **kw, factory=CheckedConnection))
    assert list_motion_sources(settings)["total"] == 4
    assert closed == [True]


def test_combined_scan_reports_partial_when_pagination_budget_is_reached(tmp_path, monkeypatch):
    import kimodo_mcp.source_library as library

    settings, _ = records(tmp_path)
    monkeypatch.setattr(library, "MAX_RECORDS", 3)
    result = library.list_motion_sources(settings, limit=2)
    assert result["total"] == 3 and result["partial"] is True
    assert library.list_motion_sources(settings, limit=2, offset=2)["next_offset"] is None


def test_history_filters_and_pagination_are_strict_and_stable(tmp_path):
    settings, _ = records(tmp_path)
    for arguments in ({"limit": True}, {"limit": 101}, {"offset": -1}, {"offset": 10001}, {"source_kind": "../data"}):
        with pytest.raises(ValueError):
            list_motion_sources(settings, **arguments)
    assert list_motion_sources(settings, offset=100)["sources"] == []
    assert list_motion_sources(settings, "easymocap")["sources"][0]["title"] == "walk.mp4"


def test_large_timestamp_cannot_hide_history_and_exports_remain_reviewable(tmp_path):
    settings, _ = records(tmp_path)
    atomic_json(settings.data_dir / "freemocap/jobs" / ("c" * 32 + ".json"), {
        "job_id": "c" * 32, "status": "completed", "operation": "export", "created_at": 10**400,
    })
    result = list_motion_sources(settings)
    assert result["total"] == 5
    assert result["sources"][-1]["created_at"] == 0
    assert result["sources"][-1]["review"]["tool"] == "inspect_freemocap_frames"


def test_redirected_capture_record_is_never_read(tmp_path):
    settings, _ = records(tmp_path)
    outside = tmp_path / "outside.json"
    outside.write_text('{}')
    link = settings.data_dir / "freemocap/jobs" / ("c" * 32 + ".json")
    try:
        link.symlink_to(outside)
    except OSError:
        pytest.skip("Symlink creation unavailable")
    result = list_motion_sources(settings)
    assert result["total"] == 4 and "Skipped 1" in result["warnings"][0]


async def test_history_is_available_through_mcp_and_studio(tmp_path):
    settings, _ = records(tmp_path)
    server = create_server(settings)
    value = await server.call_tool("list_motion_sources", {"source_kind": "freemocap", "limit": 1})
    assert value[1]["sources"][0]["source_id"] == "b" * 32
    with TestClient(create_app(settings), base_url="http://127.0.0.1") as client:
        token = client.get("/api/state").json()["csrf_token"]
        response = client.post("/api/actions/list_motion_sources", json={"source_kind": "all"}, headers={"x-studio-token": token})
        assert response.status_code == 200
        assert response.json()["result"]["total"] == 4
