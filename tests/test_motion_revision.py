import copy
import json
import sys
from pathlib import Path

import numpy as np
import pytest
from starlette.testclient import TestClient
from test_motion_variants import adjustment, fixture
from test_variant_tools import setup as shared_setup

from kimodo_mcp import motion_revision
from kimodo_mcp.config import Settings
from kimodo_mcp.jobs import JobStore
from kimodo_mcp.motion_revision import VariantRevision, preview_edit, revise_edit
from kimodo_mcp.motion_variants import VariantEdit, _digest, create_motion_variant
from kimodo_mcp.studio import create_app


@pytest.fixture
def setup(tmp_path):
    return shared_setup.__wrapped__(tmp_path)


def original_edit():
    return VariantEdit(start_frame=1, end_frame_exclusive=12, yaw_radians=0.4,
                       translation_xz=[1.0, -2.0], root_origin="first_frame",
                       joint_adjustments=[adjustment(), adjustment().model_copy(update={"joint_name": "RightArm"})])


def test_partial_joint_patch_preserves_every_other_setting_and_source():
    original = original_edit()
    before = original.model_dump(mode="json")
    result = revise_edit(original, VariantRevision(joint_updates=[
        {"joint_name": "LeftArm", "local_xyz_radians": [0.1, 0.0, 0.0]}]))
    expected = copy.deepcopy(before)
    expected["joint_adjustments"][0]["local_xyz_radians"] = [0.1, 0.0, 0.0]
    assert result["edit"] == expected and original.model_dump(mode="json") == before
    assert result["joint_changes"] == {"added": [], "removed": [], "updated": ["LeftArm"]}
    assert list(result["changes"]) == ["joint_adjustments"]


def test_joint_add_remove_and_trim_changes_are_explicit_and_complete():
    result = revise_edit(original_edit(), VariantRevision(remove_joints=["RightArm"], joint_updates=[
        {"joint_name": "LeftHand", "local_xyz_radians": [0.0, 0.1, 0.0],
         "start_frame": 2, "end_frame_exclusive": 11}]))
    assert [item["joint_name"] for item in result["edit"]["joint_adjustments"]] == ["LeftArm", "LeftHand"]
    assert result["edit"]["joint_adjustments"][1]["blend_frames"] == 3
    assert result["joint_changes"] == {"added": ["LeftHand"], "removed": ["RightArm"], "updated": []}
    with pytest.raises(ValueError):
        revise_edit(original_edit(), VariantRevision(joint_updates=[{"joint_name": "LeftHand", "blend_frames": 2}]))
    with pytest.raises(ValueError, match="not present"):
        revise_edit(original_edit(), VariantRevision(remove_joints=["LeftHand"]))
    with pytest.raises(ValueError, match="inside"):
        revise_edit(original_edit(), VariantRevision(start_frame=4))
    with pytest.raises(ValueError, match=r"2\*blend"):
        revise_edit(original_edit(), VariantRevision(joint_updates=[{"joint_name": "LeftArm", "blend_frames": 9}]))


@pytest.mark.parametrize("values", [
    {}, {"yaw_radians": None}, {"yaw_radians": True}, {"yaw_radians": float("nan")},
    {"start_frame": True}, {"root_origin": "unknown"}, {"remove_joints": ["A", "A"]},
    {"joint_updates": [{"joint_name": "LeftArm"}]},
    {"joint_updates": [{"joint_name": "LeftArm", "blend_frames": None}]},
    {"joint_updates": [{"joint_name": "LeftArm", "blend_frames": 1}] * 2},
    {"joint_updates": [{"joint_name": "LeftArm", "blend_frames": 1}], "remove_joints": ["LeftArm"]},
    {"joint_updates": [{"joint_name": "LeftArm", "local_xyz_radians": [2.0, 0.0, 0.0]}]},
    {"remove_joints": [f"Joint{i}" for i in range(17)]},
])
def test_revision_rejects_ambiguous_nonfinite_or_unbounded_patches(values):
    with pytest.raises(ValueError):
        VariantRevision.model_validate(values)


@pytest.mark.parametrize("joints", [30, 77])
def test_preview_is_exact_saved_geometry_and_original_data_without_writes(tmp_path, joints):
    directory = tmp_path / ("a" * 32)
    directory.mkdir()
    source = directory / "motion_00.npz"
    arrays = fixture(joints)
    np.savez(source, **arrays)
    before = source.read_bytes()
    edit = original_edit()
    result = preview_edit(source, source_sha256=_digest(source), job_id=directory.name, sample_index=0,
                          edit=edit, frame_indices=[5, 2, 10])
    assert list(directory.iterdir()) == [source]
    assert source.read_bytes() == before
    assert result["frames"] == 11 and result["source_range"] == {"start_frame": 1, "end_frame_exclusive": 12}
    assert result["joint_editing"]["supported"]
    np.testing.assert_array_equal(result["first_retained_root_position"], arrays["root_positions"][1])
    assert [item["original_frame"] for item in result["frames_preview"]] == [5, 2, 10]
    assert [item["joint_weights"]["LeftArm"] for item in result["frames_preview"]] == [1.0, 0.0, 0.0]
    saved = create_motion_variant(source, source_sha256=_digest(source), job_id=directory.name, edit=edit)
    assert saved["variant_id"] == result["predicted_variant_id"]
    with np.load(Path(saved["output_directory"]) / "motion_00.npz", allow_pickle=False) as output:
        for item in result["frames_preview"]:
            index, original = item["frame_index"], item["original_frame"]
            np.testing.assert_array_equal(item["positions_m"], output["posed_joints"][index])
            np.testing.assert_array_equal(item["global_rot_mats"], output["global_rot_mats"][index])
            np.testing.assert_array_equal(item["original_positions_m"], arrays["posed_joints"][original])
            np.testing.assert_array_equal(item["original_global_rot_mats"], arrays["global_rot_mats"][original])
    json.dumps(result, allow_nan=False)


def test_preview_checks_integrity_after_math_and_bounds_before_publishing(tmp_path, monkeypatch):
    directory = tmp_path / ("a" * 32)
    directory.mkdir()
    source = directory / "motion_00.npz"
    np.savez(source, **fixture())
    digest = _digest(source)
    for frames in [[0], [12], [2, 2], [True], list(range(17))]:
        with pytest.raises(ValueError):
            preview_edit(source, source_sha256=digest, job_id=directory.name, sample_index=0,
                         edit=VariantEdit(start_frame=2), frame_indices=frames)
    calls = iter([digest, "0" * 64])
    monkeypatch.setattr(motion_revision, "_digest", lambda _: next(calls))
    with pytest.raises(ValueError, match="changed while"):
        preview_edit(source, source_sha256=digest, job_id=directory.name, sample_index=0, edit=VariantEdit())
    assert list(directory.iterdir()) == [source]


async def test_preview_and_partial_revision_tools_do_not_write_until_explicit_save(setup):
    server, job, source = setup

    async def call(name, arguments):
        _, result = await server.call_tool(name, arguments)
        return result

    before = source.read_bytes()
    preview = await call("preview_motion_variant", {"job_id": job, "edit": original_edit().model_dump(mode="json")})
    assert preview["frames_preview"][0]["original_frame"] == 1
    assert not (source.parent / "variants").exists()
    saved = await call("create_motion_variant", {"job_id": job, "edit": preview["edit"]})
    old_files = {str(path): path.read_bytes() for path in source.parent.rglob("*") if path.is_file()}
    revision = {"remove_joints": ["LeftArm", "RightArm"]}
    arguments = {"job_id": job, "variant_id": saved["variant_id"], "revision": revision, "frame_indices": [3]}
    planned = await call("revise_motion_variant", arguments)
    assert planned["contact_status"] == "preserved_source_predictions"
    assert planned["source_variant_id"] == saved["variant_id"] and planned["saved"] is False
    assert old_files == {str(path): path.read_bytes() for path in source.parent.rglob("*") if path.is_file()}
    revised = await call("revise_motion_variant", {**arguments, "save": True})
    assert revised["variant_id"] == planned["predicted_variant_id"]
    assert revised["saved"] and revised["edit"]["translation_xz"] == [1.0, -2.0]
    repeat = await call("revise_motion_variant", {**arguments, "save": True})
    assert repeat["variant_id"] == revised["variant_id"]
    assert source.read_bytes() == before
    for path, contents in old_files.items():
        assert Path(path).read_bytes() == contents
    assert len(JobStore(Settings(source.parents[2], sys.executable)).list()) == 1
    with pytest.raises(Exception):
        await call("revise_motion_variant", {**arguments, "sample_index": 1})
    with pytest.raises(Exception):
        await call("revise_motion_variant", {**arguments, "save": "true"})


def test_studio_preview_shares_validated_tool_and_does_not_create_artifacts(setup, tmp_path):
    _, job, source = setup
    web = tmp_path / "web"
    web.mkdir()
    (web / "index.html").write_text("Studio")
    with TestClient(create_app(Settings(source.parents[2], sys.executable), web),
                    base_url="http://127.0.0.1:8768") as client:
        token = client.get("/api/state").json()["csrf_token"]
        response = client.post("/api/actions/preview_motion_variant", headers={"x-studio-token": token},
                               json={"job_id": job, "edit": {"start_frame": 3}, "frame_indices": [3]})
        assert response.status_code == 200, response.text
        assert response.json()["result"]["frames_preview"][0]["frame_index"] == 0
        invalid = client.post("/api/actions/preview_motion_variant", headers={"x-studio-token": token},
                              json={"job_id": job, "edit": {}, "frame_indices": [True]})
        assert invalid.status_code == 400
    assert not (source.parent / "variants").exists()
