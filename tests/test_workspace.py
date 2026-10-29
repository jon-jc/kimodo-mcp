"""Transport parity, verified export discovery, and cross-client workflow locks."""

import copy
import hashlib
import json
import shutil
import sys
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest
from filelock import FileLock
from test_studio import completed, completed_preview

from kimodo_mcp.config import Settings
from kimodo_mcp.handoff import UnrealTarget
from kimodo_mcp.jobs import atomic_json
from kimodo_mcp.workspace import Workspace


@pytest.fixture
def workspace(tmp_path):
    return Workspace(Settings(data_dir=tmp_path / "data", python=sys.executable))


def target(workspace, name="Wave"):
    return UnrealTarget(
        project_file=str(workspace.settings.data_dir / "AnimationLab.uproject"),
        skeleton_path="/Game/Character/Skeleton", skeletal_mesh_path="/Game/Character/Mesh",
        destination_path="/Game/Animations", asset_name=name,
    )


def unexpected(**kwargs):
    pytest.fail("A completed or blocked workflow must not dispatch an editor callback")


def copy_handoff(source, destination, report):
    shutil.copytree(source, destination)
    updated = copy.deepcopy(report)
    for key in ("preview_glb", "character_fbx", "fbx", "blend"):
        if key + "_path" in report:
            updated[key + "_path"] = str(destination / Path(report[key + "_path"]).name)
    atomic_json(destination / "blender-report.json", updated)


def test_target_save_preserves_unrelated_fields_and_roundtrips(workspace):
    path = workspace.settings.data_dir / "studio.json"
    atomic_json(path, {"layout": {"theme": "dark"}, "last_project": "kept"})
    result = workspace.save_unreal_target(target(workspace))
    assert workspace.project_target() == result["target"]
    data = json.loads(path.read_text(encoding="utf-8"))
    assert data["layout"] == {"theme": "dark"} and data["last_project"] == "kept"
    assert Workspace(workspace.settings).project_target() == result["target"]


@pytest.mark.parametrize("content", ["[]", "bad json", " " * 16385])
def test_bad_configuration_is_not_overwritten(workspace, content):
    path = workspace.settings.data_dir / "studio.json"
    path.write_text(content)
    assert workspace.project_target() is None
    with pytest.raises(ValueError):
        workspace.save_unreal_target(target(workspace))
    assert path.read_text() == content


def test_target_lock_blocks_concurrent_writers(workspace):
    with FileLock(str(workspace.settings.data_dir / "studio-config.lock")):
        with pytest.raises(RuntimeError, match="Another client"):
            workspace.save_unreal_target(target(workspace))
    assert workspace.project_target() is None


def test_output_list_contains_only_verified_latest_character_exports(workspace, monkeypatch):
    _, job, directory, handoff, _, report = completed_preview(workspace.settings, monkeypatch, attempt=2)
    latest = directory / "handoffs" / "character-motion_00-attempt10"
    copy_handoff(handoff, latest, report)
    result = workspace.list_job_outputs(job["id"])
    assert result["job_id"] == job["id"]
    assert len(result["outputs"]) == 5
    native, *exports = result["outputs"]
    assert native["path"] == "motion_00.npz" and native["attempt"] is None
    assert {item["role"] for item in exports} == {"character_preview", "character_mesh", "animation", "blender_project"}
    assert all(item["attempt"] == 10 and "attempt10" in item["path"] for item in exports)
    for item in result["outputs"]:
        path = Path(item["absolute_path"])
        assert path.as_uri() == item["uri"]
        assert path.relative_to(directory).as_posix() == item["path"]
        assert item["sha256"] == hashlib.sha256(path.read_bytes()).hexdigest()
        assert item["bytes"] == path.stat().st_size
        assert item["verified"] is True and item["sample_index"] == 0


def test_old_unbound_reports_are_not_advertised(workspace):
    _, job, directory = completed(workspace.settings)
    report = directory / "handoffs" / "character-motion_00" / "blender-report.json"
    report.parent.mkdir(parents=True)
    atomic_json(report, {"status": "completed", "preview_glb_path": "untrusted.glb"})
    result = workspace.list_job_outputs(job["id"])
    assert len(result["outputs"]) == 1 and result["outputs"][0]["role"] == "motion"
    assert workspace.job_details(job["id"])["previews"] == []


@pytest.mark.parametrize("change", ["bytes", "same_length", "checksum", "missing", "traversal"])
def test_native_output_discovery_rejects_changed_or_unmanaged_files(workspace, change):
    _, job, directory = completed(workspace.settings)
    path = directory / "motion_00.npz"
    manifest = json.loads((directory / "manifest.json").read_text())
    if change == "bytes":
        path.write_bytes(b"changed")
    elif change == "same_length":
        path.write_bytes(b"X" * path.stat().st_size)
    elif change == "checksum":
        manifest["artifacts"][0]["sha256"] = "0" * 64
    elif change == "missing":
        path.unlink()
    else:
        manifest["artifacts"][0]["path"] = "../other.npz"
    atomic_json(directory / "manifest.json", manifest)
    with pytest.raises((ValueError, OSError)):
        workspace.list_job_outputs(job["id"])


def test_cached_preview_cannot_be_changed_by_a_caller(workspace, monkeypatch):
    _, job, _, _, _, _ = completed_preview(workspace.settings, monkeypatch)
    first = workspace.job_details(job["id"])["previews"][0]
    original_url = first["exports"][0]["url"]
    first["exports"][0]["url"] = "unexpected"
    outputs = workspace.list_job_outputs(job["id"])["outputs"]
    outputs[-1]["sha256"] = "unexpected"
    assert workspace.job_details(job["id"])["previews"][0]["exports"][0]["url"] == original_url
    assert workspace.list_job_outputs(job["id"])["outputs"][-1]["sha256"] != "unexpected"


@pytest.mark.parametrize("attempt", [None, 2])
def test_completed_preview_is_reused_without_current_template_or_editor(workspace, monkeypatch, attempt):
    _, job, _, _, _, _ = completed_preview(workspace.settings, monkeypatch, attempt=2)
    result = workspace.build_character_preview(job["id"], attempt=attempt, prepare=unexpected, execute=unexpected)
    assert result["reused"] is True and result["attempt"] == 2 and result["sample_index"] == 0
    assert len(result["exports"]) == 4 and result["url"].endswith("/character-preview.glb")


@pytest.mark.parametrize("marker", ["bridge-dispatch.json", "execution.lock"])
def test_uncertain_dispatch_cannot_allocate_a_fresh_attempt(workspace, marker):
    _, job, directory = completed(workspace.settings)
    pending = directory / "handoffs" / "character-motion_00"
    pending.mkdir(parents=True)
    atomic_json(pending / marker, {})
    with pytest.raises(ValueError, match="may still be running"):
        workspace.build_character_preview(job["id"], prepare=unexpected, execute=unexpected)
    assert list(pending.parent.iterdir()) == [pending]


@pytest.mark.parametrize("arguments", [{"sample_index": True}, {"sample_index": -1}, {"sample_index": 4},
                                      {"attempt": True}, {"attempt": -1}, {"attempt": 100}])
def test_preview_indices_are_strict_and_bounded(workspace, arguments):
    _, job, _ = completed(workspace.settings)
    with pytest.raises(ValueError):
        workspace.build_character_preview(job["id"], **arguments, prepare=unexpected, execute=unexpected)


def test_preview_requires_completed_job_and_existing_sample(workspace):
    store, job, _ = completed(workspace.settings)
    with pytest.raises(ValueError, match="Choose a sample"):
        workspace.build_character_preview(job["id"], sample_index=1, prepare=unexpected, execute=unexpected)
    pending = store.submit(store.get(job["id"])["request"], launch=False)
    assert workspace.list_job_outputs(pending["id"])["outputs"] == []
    with pytest.raises(ValueError, match="Choose a sample"):
        workspace.build_character_preview(pending["id"], prepare=unexpected, execute=unexpected)


def test_two_clients_lock_attempt_selection_until_verified_completion(workspace, monkeypatch):
    _, job, directory, initial, _, report = completed_preview(workspace.settings, monkeypatch, attempt=0)
    initial.joinpath("blender-report.json").unlink()
    entered, release = threading.Event(), threading.Event()
    calls = []
    output = directory / "handoffs" / "character-motion_00-attempt01"

    def prepare(**arguments):
        calls.append(("prepare", arguments))
        entered.set()
        assert release.wait(5)

    def execute(**arguments):
        calls.append(("execute", arguments))
        copy_handoff(initial, output, report)
        return report

    other = Workspace(workspace.settings)
    with ThreadPoolExecutor(max_workers=1) as executor:
        future = executor.submit(workspace.build_character_preview, job["id"], prepare=prepare, execute=execute)
        assert entered.wait(5)
        try:
            with pytest.raises(RuntimeError, match="Another client is building"):
                other.build_character_preview(job["id"], prepare=unexpected, execute=unexpected)
        finally:
            release.set()
        result = future.result(timeout=5)
    assert result["attempt"] == 1 and result["reused"] is False
    assert calls == [("prepare", {"job_id": job["id"], "sample_index": 0, "attempt": 1}),
                     ("execute", {"job_id": job["id"], "sample_index": 0, "attempt": 1, "source_kind": "character"})]
    reused = other.build_character_preview(job["id"], prepare=unexpected, execute=unexpected)
    assert reused["attempt"] == 1 and reused["reused"] is True


def test_editor_return_value_alone_does_not_establish_completion(workspace):
    _, job, _ = completed(workspace.settings)
    with pytest.raises(ValueError, match="existing file"):
        workspace.build_character_preview(job["id"], prepare=lambda **_: None,
                                          execute=lambda **_: {"status": "completed"})


def completed_skeleton(settings, *, attempt=1):
    store, job, directory = completed(settings)
    bvh = directory / "motion_00.bvh"
    # Minimal hierarchy fixture; receipt tests do not claim generated motion.
    bvh.write_text("HIERARCHY\nROOT Root\n{\nJOINT Hips\n{\nEnd Site\n{\n}\n}\n}\n"
                   "MOTION\nFrames: 30\nFrame Time: 0.033333333\n")
    checksum = hashlib.sha256(bvh.read_bytes()).hexdigest()
    manifest = json.loads((directory / "manifest.json").read_text())
    manifest["skeleton"] = "somaskel77"
    manifest["artifacts"].append({"path": bvh.name, "format": "bvh", "sample_index": 0,
                                  "bytes": bvh.stat().st_size, "sha256": checksum})
    atomic_json(directory / "manifest.json", manifest)
    suffix = f"-attempt{attempt:02d}" if attempt else ""
    handoff = directory / "handoffs" / ("skeleton-motion_00" + suffix)
    handoff.mkdir(parents=True)
    prepared = {"schema_version": 1, "kind": "blender", "source_bvh": str(bvh), "source_sha256": checksum,
                "fps": 30, "frames": 30, "skeleton": "somaskel77", "armature_name": "KimodoRig"}
    atomic_json(handoff / "handoff.json", prepared)
    bones = [{"name": "Root", "parent": None}, {"name": "Hips", "parent": "Root"}]
    report = {**prepared, "status": "completed", "frame_range": [1, 30], "bone_count": 2, "bones": bones,
              "fbx_bones": [{"name": "KimodoRig", "parent": None},
                            {"name": "Root", "parent": "KimodoRig"}, bones[1]]}
    for key in ("fbx", "blend"):
        output = handoff / f"animation.{key}"
        output.write_bytes(b"exported skeleton fixture")
        report[key + "_path"] = str(output)
        report[key + "_sha256"] = hashlib.sha256(output.read_bytes()).hexdigest()
    atomic_json(handoff / "blender-report.json", report)
    return store, job, directory, handoff, prepared, report


def test_latest_skeleton_exports_are_listed_without_character_preview(workspace):
    _, job, directory, handoff, _, report = completed_skeleton(workspace.settings)
    latest = directory / "handoffs" / "skeleton-motion_00-attempt10"
    copy_handoff(handoff, latest, report)
    outputs = workspace.list_job_outputs(job["id"])["outputs"]
    assert len(outputs) == 4
    skeleton = [item for item in outputs if item.get("source_kind") == "skeleton"]
    assert {item["format"] for item in skeleton} == {"fbx", "blend"}
    assert all(item["attempt"] == 10 and item["verified"] is True for item in skeleton)
    assert workspace.job_details(job["id"])["previews"] == []


@pytest.mark.parametrize("corruption", ["foreign_job", "stale_output", "timing", "hierarchy", "legacy"])
def test_invalid_skeleton_receipts_are_excluded(workspace, corruption):
    _, job, directory, handoff, prepared, report = completed_skeleton(workspace.settings)
    assert len(workspace.list_job_outputs(job["id"])["outputs"]) == 4
    if corruption == "foreign_job":
        _, _, other = completed(workspace.settings)
        foreign = other / "motion_00.bvh"
        foreign.write_bytes((directory / "motion_00.bvh").read_bytes())
        prepared["source_bvh"] = report["source_bvh"] = str(foreign)
    elif corruption == "stale_output":
        (handoff / "animation.fbx").write_bytes(b"changed export")
    elif corruption == "timing":
        prepared["frames"] = report["frames"] = 31
    elif corruption == "hierarchy":
        report["fbx_bones"][0]["name"] = "OtherWrapper"
    else:
        report = {"status": "completed", "fbx_path": report["fbx_path"]}
    atomic_json(handoff / "handoff.json", prepared)
    atomic_json(handoff / "blender-report.json", report)
    assert len(workspace.list_job_outputs(job["id"])["outputs"]) == 2


def test_native_and_skeleton_downloads_share_workspace_verification(tmp_path):
    from starlette.testclient import TestClient

    from kimodo_mcp.studio import create_app

    settings = Settings(data_dir=tmp_path / "data", python=sys.executable)
    _, job, _, handoff, _, _ = completed_skeleton(settings)
    with TestClient(create_app(settings), base_url="http://127.0.0.1:8768") as client:
        url = f"/api/files/{job['id']}/handoffs/{handoff.name}/animation.fbx"
        assert client.get(url).status_code == 200
        (handoff / "animation.fbx").write_bytes(b"changed export")
        assert client.get(url).status_code == 404
