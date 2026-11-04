import json
import struct
import subprocess
from pathlib import Path

import pytest

from kimodo_mcp import studio_assets as assets
from kimodo_mcp.config import Settings


@pytest.fixture
def settings(tmp_path):
    return Settings(data_dir=tmp_path / "workspace", python="python")


def glb(path, extra=None):
    data = json.dumps({"asset": {"version": "2.0"}, **(extra or {})}).encode()
    data += b" " * (-len(data) % 4)
    path.write_bytes(struct.pack("<4sIIII", b"glTF", 2, 20 + len(data), len(data), 0x4E4F534A) + data)
    return path


def test_content_address_copy_dedup_and_integrity(settings, tmp_path):
    source = tmp_path / "weapon.obj"
    source.write_text("v 0 0 0\nv 1 0 0\nv 0 1 0\nf 1 2 3\n")
    original = source.read_bytes()
    first = assets.import_studio_asset(settings, str(source), "Blade", "prop")
    assert first["asset_id"] == assets.digest(source)
    assert Path(first["absolute_path"]).read_bytes() == original
    second = assets.import_studio_asset(settings, str(source), "New label", "character")
    assert second["reused"] and second["label"] == "Blade" and second["kind"] == "prop"
    source.write_text("changed original")
    assert assets.resolve_asset(settings, first["asset_id"])["bytes"] == len(original)
    Path(first["absolute_path"]).write_text("corrupt copy")
    with pytest.raises(ValueError, match="changed"):
        assets.resolve_asset(settings, first["asset_id"])
    assert assets.list_studio_assets(settings)["invalid_count"] == 1


@pytest.mark.parametrize("identifier", ["../x", "A" * 64, "a" * 63, None])
def test_bad_identifier(settings, identifier):
    with pytest.raises(ValueError):
        assets.resolve_asset(settings, identifier)


@pytest.mark.parametrize("uri", ["../secret.png", "file:///C:/private.png", "https://example.com/a"])
def test_glb_external_references_rejected(settings, tmp_path, uri):
    source = glb(tmp_path / "external.glb", {"images": [{"uri": uri}]})
    with pytest.raises(ValueError, match="external"):
        assets.import_studio_asset(settings, str(source), "External")


def test_format_and_pagination(settings, tmp_path):
    source = tmp_path / "unsafe.blend"
    source.write_bytes(b"not accepted")
    with pytest.raises(ValueError, match="formats"):
        assets.import_studio_asset(settings, str(source), "Unsafe")
    for index in range(3):
        assets.import_studio_asset(
            settings, str(glb(tmp_path / f"{index}.glb", {"extras": index})), str(index)
        )
    page = assets.list_studio_assets(settings, limit=1, offset=1)
    assert page["total"] == 3 and len(page["assets"]) == 1
    with pytest.raises(ValueError):
        assets.list_studio_assets(settings, limit=True)


def test_preview_fixed_process_and_reuse(settings, tmp_path, monkeypatch):
    asset = assets.import_studio_asset(settings, str(glb(tmp_path / "a.glb")), "Asset")
    calls = []

    def convert(script, request_path, directory):
        request = assets.read_json(request_path)
        output = glb(directory / "preview.glb")
        assets.atomic_json(
            directory / "report.json",
            {
                "schema_version": 1,
                "status": "completed",
                "asset_id": asset["asset_id"],
                "script_sha256": request["script_sha256"],
                "sha256": assets.digest(output),
            },
        )
        calls.append(request)

    monkeypatch.setattr(assets, "run_blender", convert)
    first = assets.build_studio_asset_preview(settings, asset["asset_id"])
    assert assets.build_studio_asset_preview(settings, asset["asset_id"]) == first
    assert len(calls) == 1
    Path(first["preview_path"]).write_bytes(b"corrupt")
    with pytest.raises(ValueError):
        assets.build_studio_asset_preview(settings, asset["asset_id"])


def test_preview_get_is_readonly_and_request_is_bound(settings, tmp_path, monkeypatch):
    asset = assets.import_studio_asset(settings, str(glb(tmp_path / "a.glb")), "Asset")
    monkeypatch.setattr(assets, "run_blender", lambda *_: pytest.fail("Getter launched Blender"))
    result = assets.get_studio_asset_preview(settings, asset["asset_id"])
    assert result["status"] == "not_prepared"
    directory, request, _ = assets._preview_request(settings, asset["asset_id"], 0)
    assert not directory.exists()
    directory.mkdir(parents=True)
    assets.atomic_json(directory / "request.json", request)
    assert assets.get_studio_asset_preview(settings, asset["asset_id"])["status"] == "outcome_unknown"
    assets.atomic_json(directory / "request.json", request | {"output": str(tmp_path / "escape.glb")})
    with pytest.raises(ValueError, match="request"):
        assets.get_studio_asset_preview(settings, asset["asset_id"])


def test_listing_hashes_only_selected_page(settings, tmp_path, monkeypatch):
    for index in range(4):
        assets.import_studio_asset(
            settings, str(glb(tmp_path / f"{index}.glb", {"extras": index})), str(index)
        )
    original = assets.digest
    calls = []
    monkeypatch.setattr(assets, "digest", lambda path: calls.append(path) or original(path))
    result = assets.list_studio_assets(settings, limit=1, offset=2)
    assert len(calls) == 1 and result["total"] == 4


def test_failed_preview_attempt_never_auto_reexecutes(settings, tmp_path, monkeypatch):
    asset = assets.import_studio_asset(settings, str(glb(tmp_path / "a.glb")), "Asset")
    calls = []

    def fail(*args):
        calls.append(True)
        raise subprocess.TimeoutExpired("blender", 180)

    monkeypatch.setattr(assets, "run_blender", fail)
    with pytest.raises(subprocess.TimeoutExpired):
        assets.build_studio_asset_preview(settings, asset["asset_id"])
    with pytest.raises(ValueError):
        assets.build_studio_asset_preview(settings, asset["asset_id"])
    assert len(calls) == 1


def test_fixed_subprocess_disables_scripts_and_stdin(tmp_path, monkeypatch):
    monkeypatch.setattr(assets, "blender_executable", lambda: "blender")
    calls = []
    monkeypatch.setattr(
        assets.subprocess,
        "run",
        lambda command, **kwargs: calls.append((command, kwargs)) or subprocess.CompletedProcess(command, 0),
    )
    assets.run_blender(Path("fixed.py"), Path("request.json"), tmp_path)
    command, kwargs = calls[0]
    assert "--factory-startup" in command and "--disable-autoexec" in command
    assert kwargs["stdin"] == subprocess.DEVNULL and kwargs["timeout"] == 180
    assert "shell" not in kwargs
