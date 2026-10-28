import json
import sqlite3
import sys
import threading
import time

import pytest

from kimodo_mcp.config import Settings
from kimodo_mcp.jobs import JobStore, contained_file, validate_manifest
from kimodo_mcp.runner import run


@pytest.fixture
def store(tmp_path):
    return JobStore(Settings(data_dir=tmp_path, python=sys.executable, timeout=10))


def test_shared_store_and_cancel_preserves_terminal_state(store):
    job = store.submit({"seed": 1}, launch=False)
    other = JobStore(store.settings)
    assert other.get(job["id"])["request"] == {"seed": 1}
    other.cancel(job["id"])
    store.update(job["id"], "running")
    assert store.get(job["id"])["status"] == "cancelling"
    run(job["id"], store.settings)
    assert store.get(job["id"])["status"] == "cancelled"
    other.cancel(job["id"])
    assert store.get(job["id"])["status"] == "cancelled"


def test_connection_commits_then_closes_and_releases_file_handle(store):
    with store.connect() as connection:
        connection.execute("CREATE TABLE close_probe (value INTEGER)")
        connection.execute("INSERT INTO close_probe VALUES (7)")
    with pytest.raises(sqlite3.ProgrammingError, match="closed"):
        connection.execute("SELECT 1")
    with store.connect() as check:
        assert check.execute("SELECT value FROM close_probe").fetchone()[0] == 7
    renamed = store.db.with_name("released.sqlite3")
    store.db.replace(renamed)
    renamed.unlink()
    assert not renamed.exists()


def test_failed_transaction_rolls_back_and_closes(store):
    job = store.submit({}, launch=False)
    with pytest.raises(RuntimeError, match="abort"):
        with store.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute("UPDATE jobs SET status='failed' WHERE id=?", (job["id"],))
            raise RuntimeError("abort")
    with pytest.raises(sqlite3.ProgrammingError, match="closed"):
        connection.execute("SELECT 1")
    assert store.get(job["id"])["status"] == "queued"


def test_runner_requires_manifest(store):
    job = store.submit({}, launch=False)
    run(job["id"], store.settings, [sys.executable, "-c", "pass"])
    assert store.get(job["id"])["status"] == "failed"


def test_runner_completes_and_artifacts_cannot_escape(store, tmp_path):
    job = store.submit({}, launch=False)
    code = (
        "from pathlib import Path; import json,hashlib; Path('motion_00.npz').write_bytes(b'data'); "
        "Path('manifest.json').write_text(json.dumps({'artifacts':[{'path':'motion_00.npz','bytes':4,'sha256':hashlib.sha256(b'data').hexdigest()}]}))"
    )
    run(job["id"], store.settings, [sys.executable, "-c", code])
    assert store.get(job["id"])["status"] == "completed"
    with pytest.raises(ValueError):
        store.artifact(job["id"], "../../jobs.sqlite3")
    outside = tmp_path / "private.txt"
    outside.write_text("private")
    with pytest.raises(ValueError):
        contained_file(store.directory(job["id"]), "../../private.txt")


def test_running_cancellation_stops_child_and_releases_slot(store):
    job = store.submit({}, launch=False)
    thread = threading.Thread(target=run, args=(
        job["id"], store.settings, [sys.executable, "-c", "import time; time.sleep(30)"],
    ))
    thread.start()
    deadline = time.monotonic() + 5
    while store.get(job["id"])["status"] != "running" and time.monotonic() < deadline:
        time.sleep(0.03)
    store.cancel(job["id"])
    thread.join(timeout=5)
    assert not thread.is_alive()
    assert store.get(job["id"])["status"] == "cancelled"
    next_job = store.submit({}, launch=False)
    run(next_job["id"], store.settings, [sys.executable, "-c", "raise SystemExit(3)"])
    assert store.get(next_job["id"])["status"] == "failed"


def test_queue_limit_is_transactional(tmp_path):
    store = JobStore(Settings(tmp_path, sys.executable, max_pending=1))
    store.submit({}, launch=False)
    with pytest.raises(ValueError, match="queue is full"):
        store.submit({}, launch=False)


def test_logs_bounded_and_token_redacted(store):
    job = store.submit({}, launch=False)
    (store.directory(job["id"]) / "worker.log").write_text("old\nhf_123secretABC\nlast")
    assert store.log(job["id"], 2) == "[redacted]\nlast"


def test_duplicate_client_cannot_override_cancel(store):
    job = store.submit({}, launch=False)
    store.cancel(job["id"])
    store.update(job["id"], "completed")
    assert store.get(job["id"])["status"] == "cancelled"


def test_invalid_ids_never_read_files(store):
    for value in ["../secret", "", "A" * 32]:
        with pytest.raises(ValueError):
            store.get(value)


def test_supervisor_serializes_workers(store):
    jobs = [store.submit({}, launch=False) for _ in range(2)]
    code = (
        "import time,json,hashlib; from pathlib import Path; "
        "start=time.time(); time.sleep(0.4); "
        "Path('motion_00.npz').write_bytes(b'data'); "
        "Path('manifest.json').write_text(json.dumps({'start':start,'end':time.time(),'artifacts':[{'path':'motion_00.npz','bytes':4,'sha256':hashlib.sha256(b'data').hexdigest()}]}))"
    )
    threads = [threading.Thread(target=run, args=(j["id"], store.settings, [sys.executable, "-c", code])) for j in jobs]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(6)
        assert not thread.is_alive()
    manifests = [json.loads((store.directory(j["id"]) / "manifest.json").read_text()) for j in jobs]
    manifests.sort(key=lambda m: m["start"])
    assert manifests[0]["end"] <= manifests[1]["start"]


def test_cancel_wins_failure_finalize_race(store):
    job = store.submit({}, launch=False)
    store.cancel(job["id"])
    store.update(job["id"], "failed", error="worker exited")
    assert store.get(job["id"])["status"] == "cancelled"


def test_manifest_rejects_missing_or_mismatched_files(store):
    job = store.submit({}, launch=False)
    directory = store.directory(job["id"])
    (directory / "manifest.json").write_text(json.dumps({"artifacts": [{"path": "motion_00.npz", "bytes": 4, "sha256": "0" * 64}]}))
    with pytest.raises(ValueError):
        validate_manifest(directory)
    (directory / "motion_00.npz").write_bytes(b"data")
    with pytest.raises(ValueError, match="checksum"):
        validate_manifest(directory)


def test_stale_heartbeat_recovery_preserves_recent_jobs(store):
    old = store.submit({}, launch=False)
    recent = store.submit({}, launch=False)
    with store.connect() as connection:
        connection.execute("UPDATE jobs SET updated=? WHERE id=?", (time.time() - 120, old["id"]))
    assert store.recover_stale() == 1
    assert store.get(old["id"])["status"] == "interrupted"
    assert store.get(recent["id"])["status"] == "queued"


def test_progress_tokens_redacted(store):
    job = store.submit({}, launch=False)
    (store.directory(job["id"]) / "progress.json").write_text(json.dumps({"error": "failed hf_123secretABC"}))
    assert store.get(job["id"])["progress"]["error"] == "failed [redacted]"
