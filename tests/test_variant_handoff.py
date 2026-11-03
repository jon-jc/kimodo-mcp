import hashlib
import json
from pathlib import Path

import numpy as np
import pytest

from kimodo_mcp import export
from kimodo_mcp.handoff import (
    UnrealTarget,
    prepare_blender_handoff,
    prepare_character_handoff,
    prepare_unreal_handoff,
)
from kimodo_mcp.handoff_scripts import character
from kimodo_mcp.motion_variants import VariantEdit, create_motion_variant
from kimodo_mcp.pose import _soma_layout


@pytest.fixture
def variant(tmp_path, monkeypatch):
    job = tmp_path / ("a" * 32)
    job.mkdir()
    source = job / "motion_00.npz"
    names, parents = _soma_layout(30)
    np.savez(
        source,
        posed_joints=np.zeros((12, 30, 3)),
        local_rot_mats=np.broadcast_to(np.eye(3), (12, 30, 3, 3)),
        global_rot_mats=np.broadcast_to(np.eye(3), (12, 30, 3, 3)),
        root_positions=np.zeros((12, 3)),
        smooth_root_pos=np.zeros((12, 3)),
        global_root_heading=np.tile([1.0, 0.0], (12, 1)),
        foot_contacts=np.ones((12, 4), dtype=bool),
        joint_names=np.array(names),
        joint_parents=np.array(parents),
        fps=np.array(30.0),
        model=np.array("kimodo-soma-rp-v1.2"),
        skeleton=np.array("somaskel30"),
    )
    result = create_motion_variant(
        source,
        source_sha256=hashlib.sha256(source.read_bytes()).hexdigest(),
        job_id=job.name,
        edit=VariantEdit(start_frame=2, end_frame_exclusive=10),
    )
    asset = tmp_path / "operator-character.fbx"
    asset.write_bytes(b"approved fixture")
    monkeypatch.setattr(character, "validate_target", lambda path: asset)
    return job, result, asset


def directory(job, result, kind="character", attempt=""):
    return job / "handoffs" / f"{kind}-motion_00-variant{result['variant_id']}{attempt}"


def prepare_character(job, result, asset, **changes):
    args = dict(
        source_sha256=result["artifact"]["sha256"],
        fps=result["fps"],
        frames=result["frames"],
        variant_id=result["variant_id"],
    )
    args.update(changes)
    return prepare_character_handoff(
        asset, Path(result["artifact"]["absolute_path"]), directory(job, result), **args
    )


def fake_export(job, result, monkeypatch, **changes):
    export_id = "b" * 64
    folder = job / "exports" / export_id
    folder.mkdir(parents=True)
    source = folder / "motion_00.bvh"
    source.write_text("HIERARCHY\nROOT Root\n{\n}\nMOTION\nFrames: 8\nFrame Time: 0.033333333\n")
    checksum = hashlib.sha256(source.read_bytes()).hexdigest()
    receipt = dict(
        job_id=job.name,
        variant_id=result["variant_id"],
        sample_index=0,
        artifact=dict(absolute_path=str(source), sha256=checksum),
        options={"format": "bvh"},
        fps=30.0,
        frames=8,
        skeleton="somaskel30",
        source_sha256=result["artifact"]["sha256"],
    )
    receipt.update(changes)

    def verify(path):
        assert path == folder
        return receipt

    monkeypatch.setattr(export, "verify_export", verify)
    return source, checksum, export_id


def test_character_variant_uses_trimmed_timing_and_repeatable_receipt(variant):
    job, result, asset = variant
    prepared = prepare_character(job, result, asset)
    assert prepared == prepare_character(job, result, asset)
    request = json.loads(Path(prepared.request_path).read_text())
    assert request["frames"] == 8 and request["fps"] == 30
    assert request["variant_id"] == result["variant_id"]
    assert request["original_source_sha256"] == result["source_sha256"]
    assert request["source_sha256"] == result["artifact"]["sha256"]
    assert request["contact_status"] == "preserved_source_predictions"


@pytest.mark.parametrize(
    "changes",
    [
        {"variant_id": None},
        {"variant_id": "c" * 64},
        {"variant_id": "../bad"},
        {"frames": 12},
        {"sample_index": 1},
        {"source_sha256": "0" * 64},
    ],
)
def test_character_wrong_selector_digest_or_native_timing_rejected(variant, changes):
    job, result, asset = variant
    with pytest.raises(ValueError):
        prepare_character(job, result, asset, **changes)
    assert not directory(job, result).exists()


def test_variant_destination_prevents_cross_job_or_selector_mix(variant, tmp_path):
    job, result, asset = variant
    for wrong in (
        tmp_path / "outside",
        job / "handoffs" / "character-motion_00",
        directory(job, result, "skeleton"),
    ):
        with pytest.raises(ValueError, match="directory"):
            prepare_character_handoff(
                asset,
                Path(result["artifact"]["absolute_path"]),
                wrong,
                source_sha256=result["artifact"]["sha256"],
                fps=30,
                frames=8,
                variant_id=result["variant_id"],
            )


def test_derived_bvh_requires_matching_variant_export_receipt(variant, monkeypatch):
    job, result, _ = variant
    source, checksum, export_id = fake_export(job, result, monkeypatch)
    output = directory(job, result, "skeleton", "-attempt01")
    prepared = prepare_blender_handoff(
        source,
        output,
        fps=30,
        frames=8,
        source_sha256=checksum,
        skeleton="somaskel30",
        variant_id=result["variant_id"],
        export_id=export_id,
    )
    request = json.loads(Path(prepared.request_path).read_text())
    assert request["export_id"] == export_id and request["variant_id"] == result["variant_id"]
    assert request["variant_source_sha256"] == result["artifact"]["sha256"]
    with pytest.raises(ValueError, match="export_id"):
        prepare_blender_handoff(
            source,
            output,
            fps=30,
            frames=8,
            source_sha256=checksum,
            skeleton="somaskel30",
            variant_id=result["variant_id"],
        )


@pytest.mark.parametrize(
    "changes",
    [
        {"variant_id": "c" * 64},
        {"sample_index": 1},
        {"frames": 12},
        {"source_sha256": "0" * 64},
        {"job_id": "d" * 32},
        {"skeleton": "somaskel77"},
    ],
)
def test_export_from_another_source_cannot_be_handed_off(variant, monkeypatch, changes):
    job, result, _ = variant
    source, checksum, export_id = fake_export(job, result, monkeypatch, **changes)
    with pytest.raises(ValueError):
        prepare_blender_handoff(
            source,
            directory(job, result, "skeleton"),
            fps=30,
            frames=8,
            source_sha256=checksum,
            skeleton="somaskel30",
            variant_id=result["variant_id"],
            export_id=export_id,
        )


def test_character_to_unreal_variant_preserves_selected_provenance(variant):
    job, result, asset = variant
    prepared = prepare_character(job, result, asset)
    folder = Path(prepared.directory)
    fbx = folder / "animation.fbx"
    fbx.write_bytes(b"fixture animation")
    report = folder / "blender-report.json"
    report.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "kind": "blender",
                "status": "completed",
                "fps": 30,
                "frames": 8,
                "source_sha256": result["artifact"]["sha256"],
                "fbx_path": str(fbx),
                "fbx_sha256": hashlib.sha256(fbx.read_bytes()).hexdigest(),
                "fbx_bones": [{"name": "Root", "parent": None}, {"name": "Hips", "parent": "Root"}],
            }
        )
    )
    target = UnrealTarget(
        project_file=str(job / "Project.uproject"),
        skeleton_path="/Game/Rig/Skeleton",
        skeletal_mesh_path="/Game/Rig/Mesh",
        destination_path="/Game/Animations",
        asset_name="EditedWave",
    )
    output = prepare_unreal_handoff(
        report, folder / "unreal-fixture", target=target, variant_id=result["variant_id"]
    )
    request = json.loads(Path(output.request_path).read_text())
    assert request["variant_id"] == result["variant_id"] and request["frames"] == 8
    assert request["variant_source_sha256"] == result["artifact"]["sha256"]
    with pytest.raises(ValueError, match="different variant"):
        prepare_unreal_handoff(report, folder / "unreal-wrong", target=target, variant_id="c" * 64)
