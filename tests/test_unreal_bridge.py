"""Transport selection and executable-content boundaries, without an editor."""

import json
import socket
from pathlib import Path
from types import SimpleNamespace

import pytest
from filelock import FileLock

from kimodo_mcp import unreal_bridge as bridge


@pytest.fixture
def handoff(tmp_path, monkeypatch):
    project = tmp_path / "Project" / "AnimationLab.uproject"
    project.parent.mkdir()
    project.write_text("{}")
    engine = tmp_path / "EngineInstall"
    (engine / "Engine").mkdir(parents=True)
    directory = tmp_path / "handoff's clip"
    directory.mkdir()
    script = directory / "unreal_handoff.py"
    template = Path(bridge.__file__).with_name("handoff_scripts") / "unreal.py"
    script.write_text(template.read_text(encoding="utf-8"), encoding="utf-8", newline="\n")
    request = {
        "kind": "unreal",
        "schema_version": 1,
        "fps": 30,
        "frames": 120,
        "source_sha256": "abc",
        "expected_bones": [{"name": "Root", "parent": None}],
        "target": {
            "project_file": str(project),
            "destination_path": "/Game/Motions",
            "asset_name": "Wave",
            "skeleton_path": "/Game/Character/Skeleton.Skeleton",
        },
    }
    (directory / "handoff.json").write_text(json.dumps(request))
    report = {
        "kind": "unreal",
        "status": "completed",
        "project_file": str(project),
        "source_sha256": "abc",
        "animation_path": "/Game/Motions/Wave.Wave",
        "fps": 30,
        "sampled_keys": 120,
        "skeleton_path": "/Game/Character/Skeleton.Skeleton",
        "frame_intervals": 119,
        "duration_seconds": 119 / 30,
        "bone_count": 1,
        "track_count": 1,
        "tracks": ["Root"],
    }
    node = {
        "project_root": str(project.parent),
        "project_name": project.stem,
        "engine_root": str(engine / "Engine"),
        "node_id": "exact",
    }
    state = {"stopped": False, "command": None, "node": None}

    class Remote:
        def __init__(self, config):
            assert config.multicast_bind_address == "127.0.0.1"
            assert config.multicast_ttl == 0
            assert config.command_endpoint == ("127.0.0.1", 6776)
            self.remote_nodes = [node]
            self._command_connection = SimpleNamespace(
                _command_channel_socket=SimpleNamespace(settimeout=lambda value: state.update(timeout=value))
            )

        def start(self):
            pass

        def stop(self):
            state["stopped"] = True

        def open_command_connection(self, identifier):
            state["node"] = identifier

        def run_command(self, command, **kwargs):
            state["command"] = command
            if state.get("timeout_error"):
                raise socket.timeout()
            if state.get("fail"):
                return {"success": False, "result": "Import rejected"}
            (directory / "unreal-report.json").write_text(json.dumps(report))
            return {"success": True}

    monkeypatch.setattr(bridge, "_DISCOVERY_SECONDS", 0)
    monkeypatch.setattr(bridge.tempfile, "gettempdir", lambda: str(tmp_path))
    monkeypatch.setattr(
        bridge,
        "_load_sdk",
        lambda _: SimpleNamespace(
            RemoteExecutionConfig=SimpleNamespace, RemoteExecution=Remote, MODE_EXEC_FILE="ExecuteFile"
        ),
    )
    return SimpleNamespace(
        script=script, project=project, engine=engine, report=report, node=node, state=state
    )


def test_exact_editor_receipt_and_quoted_script(handoff):
    result = bridge.execute_handoff(handoff.script, handoff.engine, handoff.project)
    assert result == handoff.report
    assert handoff.state["node"] == "exact"
    assert handoff.state["stopped"]
    assert handoff.state["timeout"] == 120
    assert json.dumps(str(handoff.script)) in handoff.state["command"]
    assert "_kh.sha256" in handoff.state["command"]


def test_completed_replay_does_not_reexecute(handoff):
    bridge.execute_handoff(handoff.script, handoff.engine, handoff.project)
    handoff.state["command"] = None
    assert bridge.execute_handoff(handoff.script, handoff.engine, handoff.project) == handoff.report
    assert handoff.state["command"] is None


def test_modified_script_never_reaches_editor(handoff):
    handoff.script.write_text("print('untrusted')")
    with pytest.raises(ValueError, match="trusted"):
        bridge.execute_handoff(handoff.script, handoff.engine, handoff.project)
    assert handoff.state["command"] is None


def test_project_mismatch_never_reaches_editor(handoff):
    other = handoff.project.with_name("Other.uproject")
    other.write_text("{}")
    with pytest.raises(ValueError, match="different project"):
        bridge.execute_handoff(handoff.script, handoff.engine, other)
    assert handoff.state["command"] is None


def test_wrong_engine_or_project_nodes_rejected(handoff):
    for key, value in [("project_root", "/elsewhere"), ("project_name", "Wrong"), ("engine_root", "/other")]:
        node = dict(handoff.node, **{key: value})
        with pytest.raises(RuntimeError, match="found 0"):
            bridge._select_node([node], handoff.project, handoff.engine)


def test_duplicate_matching_editors_rejected(handoff):
    with pytest.raises(RuntimeError, match="found 2"):
        bridge._select_node(
            [handoff.node, dict(handoff.node, node_id="other")], handoff.project, handoff.engine
        )


def test_remote_failure_closes_transport_and_is_not_success(handoff):
    handoff.state["fail"] = True
    with pytest.raises(RuntimeError, match="Import rejected"):
        bridge.execute_handoff(handoff.script, handoff.engine, handoff.project)
    assert handoff.state["stopped"]
    assert not handoff.script.with_name("unreal-report.json").exists()


def test_wrong_receipt_rejected(handoff):
    handoff.report["source_sha256"] = "wrong"
    with pytest.raises(RuntimeError, match="does not match"):
        bridge.execute_handoff(handoff.script, handoff.engine, handoff.project)
    assert handoff.state["stopped"]


def test_timeout_requires_receipt_inspection(handoff):
    handoff.state["timeout_error"] = True
    with pytest.raises(RuntimeError, match="inspect the handoff report"):
        bridge.execute_handoff(handoff.script, handoff.engine, handoff.project)
    assert handoff.state["stopped"]


def test_concurrent_connection_owner_rejected(handoff, tmp_path):
    with FileLock(str(tmp_path / "kimodo-unreal-command-6776.lock")):
        with pytest.raises(RuntimeError, match="Another Unreal handoff"):
            bridge.execute_handoff(handoff.script, handoff.engine, handoff.project)
    assert handoff.state["command"] is None


def test_wrong_skeleton_receipt_rejected(handoff):
    handoff.report["skeleton_path"] = "/Game/Other.Other"
    with pytest.raises(RuntimeError, match="does not match"):
        bridge.execute_handoff(handoff.script, handoff.engine, handoff.project)


def test_crlf_checkout_accepts_lf_prepared_script(handoff, tmp_path, monkeypatch):
    package = tmp_path / "crlf-package"
    templates = package / "handoff_scripts"
    templates.mkdir(parents=True)
    template = templates / "unreal.py"
    template.write_bytes(handoff.script.read_bytes().replace(b"\n", b"\r\n"))
    monkeypatch.setattr(bridge, "__file__", str(package / "unreal_bridge.py"))
    assert bridge.execute_handoff(handoff.script, handoff.engine, handoff.project)["status"] == "completed"
