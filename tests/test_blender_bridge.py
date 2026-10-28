"""Bounded editor transport, provenance, and ambiguous-retry regression checks."""

import hashlib
import json
import socket
import struct
from pathlib import Path
from types import SimpleNamespace

import pytest
from filelock import FileLock

from kimodo_mcp import blender_bridge as bridge


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def preview_bytes(bones, frames=3):
    # Metadata fixture for the receipt check, not a rendered glTF asset.
    document = {
        "nodes": [{"name": bone["name"]} for bone in bones],
        "skins": [{"joints": list(range(len(bones)))}], "meshes": [{}],
        "animations": [{"samplers": [{"input": 0}]}],
        "accessors": [{"type": "SCALAR", "componentType": 5126, "count": frames,
                       "min": [0], "max": [(frames - 1) / 30]}],
        "buffers": [{"byteLength": 0}], "images": [],
    }
    chunk = json.dumps(document).encode()
    chunk += b" " * (-len(chunk) % 4)
    return struct.pack("<4sIII4s", b"glTF", 2, len(chunk) + 20, len(chunk), b"JSON") + chunk


@pytest.fixture(params=["skeleton", "character"])
def handoff(tmp_path, monkeypatch, request):
    source_kind = request.param
    kind = "blender" if source_kind == "skeleton" else "character"
    directory = tmp_path / "handoff's clip"
    directory.mkdir()
    script = directory / f"{kind}_handoff.py"
    template = Path(bridge.__file__).with_name("handoff_scripts") / f"{kind}.py"
    script.write_text(template.read_text(encoding="utf-8"), encoding="utf-8", newline="\n")
    config = {"schema_version": 1, "kind": kind, "fps": 30.0, "frames": 3}
    report = {"schema_version": 1, "kind": "blender", "status": "completed", "fps": 30.0,
              "frames": 3, "frame_range": [1, 3]}
    if kind == "blender":
        source = tmp_path / "source.bvh"
        source.write_text("HIERARCHY\nROOT Root\n{\nJOINT Hips\n{\nEnd Site\n{\n}\n}\n}\n"
                          "MOTION\nFrames: 3\nFrame Time: 0.033333333\n")
        bones = [{"name": "Root", "parent": None}, {"name": "Hips", "parent": "Root"}]
        config.update(source_bvh=str(source), armature_name="KimodoRig", skeleton="somaskel77")
        report.update(source_bvh=str(source), fbx_bones=[{"name": "KimodoRig", "parent": None},
                      {"name": "Root", "parent": "KimodoRig"}, bones[1]])
    else:
        from kimodo_mcp import artifacts
        from kimodo_mcp.handoff_scripts import character

        source = tmp_path / "source.npz"
        source.write_bytes(b"motion fixture; archive validation is tested separately")
        target = tmp_path / "target.fbx"
        target.write_bytes(b"fitted character fixture")
        monkeypatch.setattr(character, "SOURCE_SHA256", sha(target))
        monkeypatch.setattr(artifacts, "inspect_motion", lambda _: {
            "fps": 30, "frames": 3, "samples": 1, "skeleton": "somaskel77", "joints": 77,
        })
        bones = [{"name": name, "parent": values[2]} for name, values in character.landmarks().items()]
        config.update(source_motion=str(source), character_fbx=str(target), character_sha256=sha(target),
                      sample_index=0)
        report.update(source_motion=str(source), character_sha256=sha(target), source_kind="character",
                      sample_index=0, root_motion=True, fbx_bones=bones, validation={
                          "frames": 3, "finite": True, "unweighted_vertices": 0,
                          "max_weight_sum_error": 1e-8, "max_bone_length_error_m": 1e-7,
                      })
    config["source_sha256"] = sha(source)
    report.update(source_sha256=sha(source), bones=bones, bone_count=len(bones))
    (directory / "handoff.json").write_text(json.dumps(config))
    output_specs = [("fbx", "animation.fbx"), ("blend", "character.blend" if kind == "character" else "animation.blend")]
    if kind == "character":
        output_specs += [("character_fbx", "character.fbx"), ("preview_glb", "character-preview.glb")]
    state = {"sent": [], "connections": 0, "closed": 0}

    def complete():
        for key, filename in output_specs:
            output = directory / filename
            output.write_bytes(preview_bytes(bones) if key == "preview_glb" else b"export fixture")
            report[key + "_path"] = str(output)
            report[key + "_sha256"] = sha(output)
        (directory / "blender-report.json").write_text(json.dumps(report))

    class Connection:
        def __init__(self):
            response = state.get("response", {"status": "ok", "result": {"handoff_completed": True}})
            raw = json.dumps(response).encode() + b"\0"
            self.chunks = [raw[:7], raw[7:19], raw[19:]]

        def __enter__(self):
            return self

        def __exit__(self, *args):
            state["closed"] += 1

        def settimeout(self, value):
            assert 0 < value <= bridge._RESPONSE_TIMEOUT

        def sendall(self, payload):
            state["sent"].append(payload)
            if not state.get("timeout") and not state.get("no_receipt"):
                complete()

        def recv(self, size):
            if state.get("timeout"):
                raise socket.timeout()
            return self.chunks.pop(0)

    def connect(address, timeout):
        assert address == ("127.0.0.1", 9876)
        assert timeout == 5.0
        state["connections"] += 1
        if state.get("connect_failure"):
            raise ConnectionRefusedError()
        return Connection()

    monkeypatch.setattr(bridge.socket, "create_connection", connect)
    monkeypatch.setattr(bridge.tempfile, "gettempdir", lambda: str(tmp_path))
    return SimpleNamespace(script=script, kind=source_kind, config=config, report=report,
                           source=source, state=state, complete=complete)


def execute(handoff):
    return bridge.execute_handoff(handoff.script, handoff.kind)


def test_fragmented_protocol_and_completed_replay(handoff):
    assert execute(handoff) == handoff.report
    assert handoff.state["closed"] == 1
    payload = handoff.state["sent"][0]
    assert payload.endswith(b"\0") and payload.count(b"\0") == 1
    command = json.loads(payload[:-1])
    assert command["type"] == "execute" and command["strict_json"] is True
    assert json.dumps(str(handoff.script)) in command["code"]
    assert "Handoff request changed before execution" in command["code"]
    assert execute(handoff) == handoff.report
    assert handoff.state["connections"] == 1


def test_altered_template_or_source_never_dispatched(handoff):
    handoff.script.write_text("print('different script')")
    with pytest.raises(ValueError, match="trusted"):
        execute(handoff)
    assert handoff.state["connections"] == 0


def test_changed_source_rejected_even_with_completed_receipt(handoff):
    handoff.complete()
    handoff.source.write_bytes(b"changed")
    with pytest.raises(ValueError, match="checksum"):
        execute(handoff)
    assert handoff.state["connections"] == 0


def test_timeout_blocks_resend_but_can_recover_completed_receipt(handoff):
    handoff.state["timeout"] = True
    with pytest.raises(RuntimeError, match="inspect the handoff report"):
        execute(handoff)
    assert handoff.state["closed"] == 1
    with pytest.raises(RuntimeError, match="already dispatched"):
        execute(handoff)
    assert handoff.state["connections"] == 1
    handoff.complete()
    assert execute(handoff) == handoff.report
    assert handoff.state["connections"] == 1


def test_connection_refused_is_safe_to_retry(handoff):
    handoff.state["connect_failure"] = True
    with pytest.raises(RuntimeError, match="connection failed"):
        execute(handoff)
    assert not handoff.script.with_name("bridge-dispatch.json").exists()
    handoff.state["connect_failure"] = False
    assert execute(handoff)["status"] == "completed"


def test_editor_failure_is_not_success_or_automatically_retried(handoff):
    handoff.state.update(response={"status": "error", "message": "Leave Edit/Pose mode"}, no_receipt=True)
    with pytest.raises(RuntimeError, match="Leave Edit/Pose mode"):
        execute(handoff)
    with pytest.raises(RuntimeError, match="already dispatched"):
        execute(handoff)
    assert handoff.state["connections"] == 1


@pytest.mark.parametrize("corruption", ["bone", "frame", "path", "file"])
def test_receipt_must_match_inputs_hierarchy_timing_and_output(handoff, corruption, tmp_path):
    handoff.complete()
    if corruption == "bone":
        handoff.report["bones"] = [{"name": "Other", "parent": None}]
    elif corruption == "frame":
        handoff.report["frames"] = 4
    elif corruption == "path":
        other = tmp_path / "other.fbx"
        other.write_bytes(b"export fixture")
        handoff.report["fbx_path"] = str(other)
    else:
        handoff.script.with_name("animation.fbx").write_bytes(b"changed")
    handoff.script.with_name("blender-report.json").write_text(json.dumps(handoff.report))
    with pytest.raises(RuntimeError):
        execute(handoff)
    assert handoff.state["connections"] == 0


def test_concurrent_owner_rejected_without_connection(handoff, tmp_path):
    with FileLock(str(tmp_path / "kimodo-blender-command-9876.lock")):
        with pytest.raises(RuntimeError, match="Another Blender"):
            execute(handoff)
    assert handoff.state["connections"] == 0


def test_crlf_template_checkout_uses_generated_lf_bytes(handoff, tmp_path, monkeypatch):
    package = tmp_path / "package"
    templates = package / "handoff_scripts"
    templates.mkdir(parents=True)
    filename = "blender.py" if handoff.kind == "skeleton" else "character.py"
    (templates / filename).write_bytes(handoff.script.read_bytes().replace(b"\n", b"\r\n"))
    monkeypatch.setattr(bridge, "__file__", str(package / "blender_bridge.py"))
    assert execute(handoff)["status"] == "completed"


@pytest.mark.parametrize("payload", [b"", b"{}\0extra", b"[]\0", b'{"value":NaN}\0'])
def test_malformed_response_rejected(payload):
    connection = SimpleNamespace(settimeout=lambda _: None, recv=lambda _: payload)
    with pytest.raises((RuntimeError, ValueError)):
        bridge._receive(connection)


def test_response_timeout_cap_and_deadline_survive_float_rounding(monkeypatch):
    # (76.1 + 180) - 76.1 rounds above 180 on a binary floating-point clock.
    clock = iter([76.1, 76.1, 166.1, 256.1])
    monkeypatch.setattr(bridge.time, "monotonic", lambda: next(clock))
    timeouts = []
    fragments = iter([b'{"status":', b'"ok"'])
    connection = SimpleNamespace(settimeout=timeouts.append, recv=lambda _: next(fragments))
    with pytest.raises(socket.timeout, match="deadline elapsed"):
        bridge._receive(connection)
    assert timeouts[0] == 180.0
    assert timeouts[1] == pytest.approx(90.0)
    assert all(0 < value <= 180.0 for value in timeouts)


def test_oversized_response_rejected(monkeypatch):
    monkeypatch.setattr(bridge, "_MAX_RESPONSE_BYTES", 16)
    connection = SimpleNamespace(settimeout=lambda _: None, recv=lambda _: b"X" * 17)
    with pytest.raises(RuntimeError, match="size limit"):
        bridge._receive(connection)


def test_nonfinite_bvh_interval_rejected(tmp_path):
    source = tmp_path / "source.bvh"
    source.write_text("HIERARCHY\nROOT Root\n{\n}\nMOTION\nFrames: 3\nFrame Time: 1e999\n")
    with pytest.raises(ValueError, match="interval"):
        bridge._bvh_bones(source, 3)


def test_sibling_order_can_change_but_parentage_and_uniqueness_cannot():
    bones = [{"name": "Root", "parent": None}, {"name": "Left", "parent": "Root"},
             {"name": "Right", "parent": "Root"}]
    assert bridge._same_hierarchy([bones[0], bones[2], bones[1]], bones)
    assert not bridge._same_hierarchy([bones[0], bones[1], bones[1]], bones)
    assert not bridge._same_hierarchy([bones[1], bones[0], bones[2]], bones)
    assert not bridge._same_hierarchy([bones[0], bones[1], {"name": "Right", "parent": "Left"}], bones)


def test_preview_requires_matching_skin_and_timeline(tmp_path):
    bones = [{"name": "Root", "parent": None}]
    preview = tmp_path / "preview.glb"
    preview.write_bytes(preview_bytes(bones))
    bridge._validate_glb(preview, {"frames": 3, "fps": 30}, bones)
    with pytest.raises(RuntimeError, match="rig and motion"):
        bridge._validate_glb(preview, {"frames": 4, "fps": 30}, bones)
    with pytest.raises(RuntimeError, match="rig and motion"):
        bridge._validate_glb(preview, {"frames": 3, "fps": 30}, [{"name": "Other", "parent": None}])
