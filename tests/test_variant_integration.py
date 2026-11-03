"""Managed variants remain separate from originals across transports and exports."""

import json
import struct
import sys
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pytest
from filelock import FileLock
from starlette.testclient import TestClient
from test_exports import fake_artifact

from kimodo_mcp import export
from kimodo_mcp.config import Settings
from kimodo_mcp.jobs import JobStore, atomic_json
from kimodo_mcp.motion_variants import VariantEdit, create_motion_variant
from kimodo_mcp.pose import _soma_layout
from kimodo_mcp.schemas import GenerateRequest
from kimodo_mcp.studio import create_app
from kimodo_mcp.workspace import Workspace, handoff_directory_name


def motion_arrays(frames=12):
    names, parents = _soma_layout(77)
    matrices = np.broadcast_to(np.eye(3), (frames, 77, 3, 3)).copy()
    positions = np.zeros((frames, 77, 3))
    positions[:, 0] = [0.0, 1.0, 0.0]
    for joint, parent in enumerate(parents):
        if parent >= 0:
            offset = [0.01 + joint / 1000, 0.04, 0.02]
            if names[joint] == "LeftLeg":
                offset = [0.1, -0.05, 0]
            if names[joint] == "RightLeg":
                offset = [-0.1, -0.05, 0]
            positions[:, joint] = positions[:, parent] + offset
    return {
        "posed_joints": positions, "local_rot_mats": matrices, "global_rot_mats": matrices.copy(),
        "root_positions": positions[:, 0].copy(), "smooth_root_pos": positions[:, 0] * [1, 0, 1],
        "global_root_heading": np.tile([1.0, 0.0], (frames, 1)), "foot_contacts": np.ones((frames, 6)),
        "joint_names": np.array(names), "joint_parents": np.array(parents), "fps": np.array(30.0),
        "model": np.array("kimodo-soma-rp-v1.2"), "skeleton": np.array("somaskel77"),
    }


def completed_motion(settings):
    store = JobStore(settings)
    job = store.submit(GenerateRequest(segments=[{"text": "Stand", "duration": 0.4}]).model_dump(), launch=False)
    directory = store.directory(job["id"])
    path = directory / "motion_00.npz"
    np.savez(path, **motion_arrays())
    atomic_json(directory / "manifest.json", {
        "samples": 1, "fps": 30, "frames": 12, "model": "kimodo-soma-rp-v1.2", "skeleton": "somaskel77",
        "artifacts": [{"path": path.name, "format": "npz", "sample_index": 0,
                       "bytes": path.stat().st_size, "sha256": export.digest(path)}],
    })
    store.update(job["id"], "completed")
    return Workspace(settings, store), job, path


@pytest.fixture
def managed(tmp_path):
    return completed_motion(Settings(tmp_path / "data", sys.executable))


def variant(workspace, job, path, **edit):
    return create_motion_variant(path, source_sha256=export.digest(path), job_id=job["id"],
                                 edit=VariantEdit(**edit))


def fake_process(command, **kwargs):
    request = export.ExportRequest.model_validate_json(Path(command[4]).read_text())
    motion = export.load_source(request)
    directory = Path(request.output_directory)
    artifact = directory / request.basename
    fake_artifact(artifact, request.format, motion["positions"].shape[1], motion["fps"])
    atomic_json(directory / "export-report.json", export.make_report(request, motion, artifact))
    return SimpleNamespace(returncode=0)


def export_selected(workspace, job, variant_id, monkeypatch):
    selected = workspace.resolve_motion(job["id"], variant_id=variant_id)
    monkeypatch.setattr(export.subprocess, "run", fake_process)
    return export.export_motion(workspace.settings, source_path=Path(selected["absolute_path"]),
                                source_sha256=selected["sha256"], job_id=job["id"], format="bvh",
                                variant_id=variant_id)


def test_resolver_preserves_original_and_binds_trimmed_range_and_digest(managed):
    workspace, job, path = managed
    before = path.read_bytes()
    report = variant(workspace, job, path, start_frame=2, end_frame_exclusive=10)
    native = workspace.resolve_motion(job["id"])
    edited = workspace.resolve_motion(job["id"], variant_id=report["variant_id"])
    assert native["variant_id"] is None and native["frames"] == 12
    assert edited["frames"] == 8 and edited["fps"] == native["fps"] == 30
    assert edited["sha256"] == edited["source_sha256"] == report["artifact"]["sha256"]
    assert edited["original_source_sha256"] == native["sha256"]
    assert edited["source_range"] == {"start_frame": 2, "end_frame_exclusive": 10}
    assert path.read_bytes() == before
    assert workspace.job_details(job["id"])["variants"][0]["edit"] == report["edit"]
    assert workspace.list_job_outputs(job["id"])["outputs"][1]["variant_id"] == report["variant_id"]


@pytest.mark.parametrize("change", ["original", "output", "report", "request", "manifest", "manifest_larger"])
def test_variant_reverification_invalidates_cached_receipts(managed, change):
    workspace, job, path = managed
    report = variant(workspace, job, path, start_frame=2, end_frame_exclusive=10)
    identifier = report["variant_id"]
    workspace.resolve_motion(job["id"], variant_id=identifier)
    paths = {"original": path, "output": Path(report["artifact"]["absolute_path"]),
             "report": Path(report["report_path"]), "request": Path(report["output_directory"]) / "request.json",
             "manifest": path.parent / "manifest.json", "manifest_larger": path.parent / "manifest.json"}
    target = paths[change]
    if change in {"original", "output"}:
        target.write_bytes(target.read_bytes() + b"tampered")
    else:
        data = json.loads(target.read_text())
        if change in {"manifest", "manifest_larger"}:
            data["frames"] = 7 if change == "manifest" else 24
        elif change == "request":
            data["edit"]["translation_xz"] = [1.0, 0.0]
        else:
            data["fps"] = 60
        atomic_json(target, data)
    with pytest.raises(ValueError):
        workspace.resolve_motion(job["id"], variant_id=identifier)
    assert workspace.list_motion_variants(job["id"]) == []


def test_variant_sample_and_job_identity_cannot_be_substituted(managed):
    workspace, job, path = managed
    report = variant(workspace, job, path)
    for index in (True, -1, 1, 4):
        with pytest.raises(ValueError):
            workspace.resolve_motion(job["id"], index, report["variant_id"])
    other_workspace, other_job, _ = completed_motion(workspace.settings)
    with pytest.raises(ValueError):
        other_workspace.resolve_motion(other_job["id"], variant_id=report["variant_id"])
    with pytest.raises(ValueError):
        workspace.resolve_motion(job["id"], variant_id="../outside")


def test_native_export_identity_and_receipt_remain_legacy_compatible(managed, monkeypatch):
    fixed = export.ExportRequest(job_id="a" * 32, source_path="unused", source_sha256="b" * 64,
                                 output_directory="unused", sample_index=2, format="bvh")
    assert export.export_id(fixed) == "827c75ff98b247033478e1d63c89bd58bb43c6e65993e07f5381e402a5d41d26"
    workspace, job, _ = managed
    report = export_selected(workspace, job, None, monkeypatch)
    assert "variant_id" not in report
    request = json.loads((Path(report["output_directory"]) / "request.json").read_text())
    assert "variant_id" not in request


def test_variant_export_stays_in_job_exports_and_uses_trimmed_timing(managed, monkeypatch):
    workspace, job, path = managed
    report = variant(workspace, job, path, start_frame=3, end_frame_exclusive=9)
    output = export_selected(workspace, job, report["variant_id"], monkeypatch)
    assert Path(output["output_directory"]).parent == path.parent / "exports"
    assert output["frames"] == 6 and output["variant_id"] == report["variant_id"]
    verified = workspace.verified_motion_export(job["id"], output["export_id"], sample_index=0)
    assert verified["variant_id"] == report["variant_id"] and verified["frames"] == 6
    with pytest.raises(ValueError, match="selected sample"):
        workspace.verified_motion_export(job["id"], output["export_id"], variant_id="f" * 64)
    assert export_selected(workspace, job, report["variant_id"], monkeypatch) == output
    path.write_bytes(path.read_bytes() + b"changed")
    with pytest.raises(ValueError):
        workspace.verified_motion_export(job["id"], output["export_id"])
    with pytest.raises(ValueError):
        export.verify_export(Path(output["output_directory"]))


def test_variant_source_cannot_be_exported_without_matching_selector(managed, monkeypatch):
    workspace, job, path = managed
    report = variant(workspace, job, path)
    selected = Path(report["artifact"]["absolute_path"])
    for identifier in (None, "f" * 64):
        with pytest.raises(ValueError):
            export.export_motion(workspace.settings, source_path=selected, source_sha256=export.digest(selected),
                                 job_id=job["id"], format="bvh", variant_id=identifier)


def skeleton_receipt(workspace, job, variant_id, monkeypatch):
    output = export_selected(workspace, job, variant_id, monkeypatch)
    directory = workspace.store.directory(job["id"]) / "handoffs" / handoff_directory_name(
        "skeleton", 0, variant_id=variant_id)
    directory.mkdir(parents=True)
    prepared = {"schema_version": 1, "kind": "blender", "source_bvh": output["artifact"]["absolute_path"],
                "source_sha256": output["artifact"]["sha256"], "fps": 30, "frames": output["frames"],
                "skeleton": "somaskel77", "armature_name": "KimodoRig", "variant_id": variant_id,
                "export_id": output["export_id"]}
    atomic_json(directory / "handoff.json", prepared)
    bones = [{"name": "Root", "parent": None}]
    report = {**prepared, "status": "completed", "frame_range": [1, output["frames"]],
              "bone_count": 1, "bones": bones,
              "fbx_bones": [{"name": "KimodoRig", "parent": None}, {"name": "Root", "parent": "KimodoRig"}]}
    for key in ("fbx", "blend"):
        path = directory / f"animation.{key}"
        path.write_bytes(b"skeleton fixture")
        report[key + "_path"], report[key + "_sha256"] = str(path), export.digest(path)
    atomic_json(directory / "blender-report.json", report)
    return directory, prepared


def test_skeleton_receipt_uses_variant_timing_and_matching_converted_source(managed, monkeypatch):
    workspace, job, path = managed
    report = variant(workspace, job, path, start_frame=2, end_frame_exclusive=9)
    directory, prepared = skeleton_receipt(workspace, job, report["variant_id"], monkeypatch)
    outputs = workspace.verified_skeleton_outputs(directory / "blender-report.json", path.parent,
                                                  workspace.store.get(job["id"]))
    assert len(outputs) == 2 and all(item["variant_id"] == report["variant_id"] for item in outputs)
    other = export_selected(workspace, job, None, monkeypatch)
    prepared["source_bvh"] = other["artifact"]["absolute_path"]
    prepared["source_sha256"] = other["artifact"]["sha256"]
    atomic_json(directory / "handoff.json", prepared)
    with pytest.raises(ValueError, match="verified converted BVH"):
        workspace.verified_skeleton_outputs(directory / "blender-report.json", path.parent,
                                             workspace.store.get(job["id"]))


def test_variant_export_id_is_explicit_even_when_selected_bytes_match_native(managed):
    _, job, _ = managed
    native = export.ExportRequest(job_id=job["id"], source_path="unused", source_sha256="b" * 64,
                                   output_directory="unused", format="bvh")
    changed = native.model_copy(update={"variant_id": "c" * 64})
    assert export.export_id(native) != export.export_id(changed)


def character_receipt(workspace, job, monkeypatch, variant_id=None, attempt=0):
    from kimodo_mcp.handoff_scripts import character

    selected = workspace.resolve_motion(job["id"], variant_id=variant_id)
    directory = workspace.store.directory(job["id"]) / "handoffs" / handoff_directory_name(
        "character", 0, attempt, variant_id)
    directory.mkdir(parents=True)
    target = workspace.settings.data_dir / "target.fbx"
    target.write_bytes(b"test fitted asset")
    checksum = export.digest(target)
    monkeypatch.setattr(character, "SOURCE_SHA256", checksum)
    frames = selected["frames"]
    prepared = {"schema_version": 1, "kind": "character", "sample_index": 0,
                "source_motion": selected["absolute_path"], "source_sha256": selected["sha256"],
                "character_fbx": str(target), "character_sha256": checksum, "fps": 30, "frames": frames}
    if variant_id is not None:
        prepared["variant_id"] = variant_id
    atomic_json(directory / "handoff.json", prepared)
    bones = [{"name": name, "parent": values[2]} for name, values in character.landmarks().items()]
    model = {"nodes": [{"name": bone["name"]} for bone in bones], "skins": [{"joints": list(range(24))}],
             "meshes": [{}], "animations": [{"samplers": [{"input": 0}]}],
             "accessors": [{"type": "SCALAR", "componentType": 5126, "count": frames,
                            "min": [0], "max": [(frames - 1) / 30]}], "buffers": [{"byteLength": 0}]}
    chunk = json.dumps(model).encode()
    chunk += b" " * (-len(chunk) % 4)
    glb = struct.pack("<4sIII4s", b"glTF", 2, len(chunk) + 20, len(chunk), b"JSON") + chunk
    report = {**prepared, "kind": "blender", "source_kind": "character", "status": "completed",
              "frame_range": [1, frames], "bone_count": 24, "bones": bones, "fbx_bones": bones,
              "root_motion": True, "validation": {"frames": frames, "finite": True, "unweighted_vertices": 0,
                "max_weight_sum_error": 0, "max_bone_length_error_m": 0}}
    for key, filename in (("preview_glb", "character-preview.glb"), ("character_fbx", "character.fbx"),
                          ("fbx", "animation.fbx"), ("blend", "character.blend")):
        output = directory / filename
        output.write_bytes(glb if key == "preview_glb" else b"test export")
        report[key + "_path"], report[key + "_sha256"] = str(output), export.digest(output)
    atomic_json(directory / "blender-report.json", report)
    return directory, report


def test_latest_previews_are_selected_independently_by_sample_and_variant(managed, monkeypatch):
    workspace, job, path = managed
    report = variant(workspace, job, path, start_frame=2, end_frame_exclusive=10)
    identifier = report["variant_id"]
    character_receipt(workspace, job, monkeypatch, attempt=9)
    character_receipt(workspace, job, monkeypatch, identifier, attempt=1)
    character_receipt(workspace, job, monkeypatch, identifier, attempt=3)
    details = workspace.job_details(job["id"])
    assert len(details["previews"]) == len(details["variant_previews"]) == 1
    assert details["previews"][0]["attempt"] == 9
    assert details["variant_previews"][0]["attempt"] == 3
    assert details["variant_previews"][0]["variant_id"] == identifier
    assert len(workspace.list_job_outputs(job["id"])["outputs"]) == 10
    for variant_id, expected in ((None, 9), (identifier, 3)):
        result = workspace.build_character_preview(job["id"], variant_id=variant_id,
                    prepare=lambda **_: pytest.fail("Unexpected editor dispatch"), execute=lambda **_: None)
        assert result["attempt"] == expected and result["reused"]


def test_preview_receipt_cannot_claim_variant_directory_with_native_motion(managed, monkeypatch):
    workspace, job, path = managed
    report = variant(workspace, job, path, start_frame=2, end_frame_exclusive=10)
    directory, _ = character_receipt(workspace, job, monkeypatch, report["variant_id"])
    prepared = json.loads((directory / "handoff.json").read_text())
    prepared["source_motion"] = str(path)
    prepared["source_sha256"] = export.digest(path)
    atomic_json(directory / "handoff.json", prepared)
    assert workspace.job_details(job["id"])["variant_previews"] == []


def test_variant_preview_attempt_lock_and_native_pending_are_isolated(managed, monkeypatch):
    workspace, job, path = managed
    report = variant(workspace, job, path)
    identifier = report["variant_id"]
    pending = path.parent / "handoffs" / "character-motion_00"
    pending.mkdir(parents=True)
    atomic_json(pending / "bridge-dispatch.json", {})
    calls = []

    def prepare(**arguments):
        calls.append(arguments)

    def execute(**arguments):
        character_receipt(workspace, job, monkeypatch, arguments["variant_id"], arguments["attempt"])

    with FileLock(str(path.parent / f".character-preview-00-variant{identifier}.lock")):
        with pytest.raises(RuntimeError, match="Another client"):
            workspace.build_character_preview(job["id"], variant_id=identifier, prepare=prepare, execute=execute)
    result = workspace.build_character_preview(job["id"], variant_id=identifier, prepare=prepare, execute=execute)
    assert result["attempt"] == 0 and not result["reused"]
    assert calls == [{"job_id": job["id"], "sample_index": 0, "attempt": 0, "variant_id": identifier}]


def test_source_rig_data_preserves_coordinates_and_reports_contact_invalidation(managed):
    workspace, job, path = managed
    native = workspace.motion_preview_data(job["id"])
    assert native["editable"]["supported"] and native["contact_status"] == "preserved_source_predictions"
    assert len(native["positions"]) == 12 and len(native["positions"][0]) == 77
    report = variant(workspace, job, path, start_frame=1, end_frame_exclusive=11, joint_adjustments=[{
        "joint_name": "LeftArm", "local_xyz_radians": [0.0, 0.0, 0.3],
        "start_frame": 2, "end_frame_exclusive": 10, "blend_frames": 3,
    }])
    edited = workspace.motion_preview_data(job["id"], variant_id=report["variant_id"])
    assert edited["frames"] == 10 and edited["source_range"] == {"start_frame": 1, "end_frame_exclusive": 11}
    assert edited["contact_status"] == "invalidated_by_joint_adjustment"
    assert edited["source_sha256"] != edited["original_source_sha256"] == native["source_sha256"]
    assert edited["edit"] == report["edit"]


def test_rig_data_can_render_but_cannot_edit_missing_native_auxiliary_channels(managed):
    workspace, job, path = managed
    arrays = motion_arrays()
    del arrays["smooth_root_pos"]
    np.savez(path, **arrays)
    manifest = json.loads((path.parent / "manifest.json").read_text())
    manifest["artifacts"][0].update(bytes=path.stat().st_size, sha256=export.digest(path))
    atomic_json(path.parent / "manifest.json", manifest)
    result = workspace.motion_preview_data(job["id"])
    assert result["editable"]["supported"] is False and result["editable"]["operations"] == []


def test_http_rig_data_and_only_verified_variant_downloads(managed, tmp_path):
    workspace, job, path = managed
    report = variant(workspace, job, path, start_frame=2, end_frame_exclusive=10)
    web = tmp_path / "web"
    web.mkdir()
    (web / "index.html").write_text("<html>Test studio</html>")
    with TestClient(create_app(workspace.settings, web), base_url="http://127.0.0.1:8768") as client:
        endpoint = f"/api/jobs/{job['id']}/motion-preview-data"
        result = client.get(endpoint, params={"variant_id": report["variant_id"]})
        assert result.status_code == 200 and result.json()["frames"] == 8
        assert result.headers["cache-control"] == "no-store"
        for query in ("sample_index=true", "sample_index=4", "sample_index=0&sample_index=1",
                      "source_path=outside.npz", "variant_id=bad"):
            assert client.get(endpoint + "?" + query).status_code == 400
        output = workspace.verified_motion_variant(job["id"], report["variant_id"])
        assert client.get(output["url"]).status_code == client.get(output["report_url"]).status_code == 200
        assert client.get(output["report_url"].replace("variant-report.json", "request.json")).status_code == 404
        Path(output["absolute_path"]).write_bytes(b"tampered")
        assert client.get(output["url"]).status_code == client.get(output["report_url"]).status_code == 404
