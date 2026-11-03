import json
import math
from pathlib import Path

import numpy as np
import pytest
from filelock import FileLock

from kimodo_mcp.motion_variants import (
    JointAdjustment,
    VariantEdit,
    _digest,
    apply_edit,
    create_motion_variant,
    list_motion_variants,
    verify_motion_variant,
)
from kimodo_mcp.pose import _soma_layout


def fixture(joints=77, frames=12):
    names, parents = _soma_layout(joints)
    local = np.broadcast_to(np.eye(3), (frames, joints, 3, 3)).copy()
    global_ = local.copy()
    positions = np.zeros((frames, joints, 3))
    positions[:, 0] = np.array([4, 1, -3]) + np.arange(frames)[:, None] * [0.01, 0, 0.02]
    offsets = np.zeros((joints, 3))
    for joint, parent in enumerate(parents):
        if parent < 0:
            continue
        offsets[joint] = [0.04 + joint / 1000, 0.05, 0.01]
        if names[joint] == "LeftLeg":
            offsets[joint] = [0.1, -0.05, 0]
        if names[joint] == "RightLeg":
            offsets[joint] = [-0.1, -0.05, 0]
        positions[:, joint] = positions[:, parent] + offsets[joint]
    return {
        "posed_joints": positions,
        "local_rot_mats": local,
        "global_rot_mats": global_,
        "root_positions": positions[:, 0].copy(),
        "smooth_root_pos": positions[:, 0] * [1, 0, 1],
        "global_root_heading": np.tile([1.0, 0.0], (frames, 1)),
        "foot_contacts": np.ones((frames, 4 if joints == 30 else 6), dtype=bool),
        "joint_names": np.array(names),
        "joint_parents": np.array(parents),
        "fps": np.array(30.0),
        "model": np.array("kimodo-soma-rp-v1.2"),
        "skeleton": np.array(f"somaskel{joints}"),
    }


def adjustment(**changes):
    return JointAdjustment(
        joint_name="LeftArm",
        local_xyz_radians=[0.0, 0.0, 0.5],
        start_frame=2,
        end_frame_exclusive=11,
        **changes,
    )


@pytest.mark.parametrize("joints", [30, 77])
def test_identity_and_trim_preserve_every_channel_dtype_and_input(joints):
    original = fixture(joints)
    before = {key: value.copy() for key, value in original.items()}
    full, details = apply_edit(original, VariantEdit())
    trimmed, _ = apply_edit(original, VariantEdit(start_frame=3, end_frame_exclusive=10))
    for key, values in original.items():
        expected = values[3:10] if values.ndim and len(values) == 12 else values
        np.testing.assert_array_equal(trimmed[key], expected)
        np.testing.assert_array_equal(full[key], values)
        np.testing.assert_array_equal(before[key], values)
        assert full[key].dtype == values.dtype and not np.shares_memory(full[key], values)
    assert details["contact_status"] == "preserved_source_predictions"


@pytest.mark.parametrize("angle", [math.pi / 2, -math.pi / 2, math.pi])
def test_planar_world_transform_rotations_heading_lengths_and_height(angle):
    source = fixture()
    out, details = apply_edit(source, VariantEdit(yaw_radians=angle, translation_xz=[2.0, -1.0]))
    transform = np.asarray(details["root_transform_matrix"])
    expected = np.einsum("ab,tjb->tja", transform[:3, :3], source["posed_joints"]) + transform[:3, 3]
    np.testing.assert_allclose(out["posed_joints"], expected)
    np.testing.assert_array_equal(out["posed_joints"][..., 1], source["posed_joints"][..., 1])
    np.testing.assert_array_equal(out["local_rot_mats"][:, 1:], source["local_rot_mats"][:, 1:])
    np.testing.assert_allclose(
        out["global_root_heading"], np.tile([math.cos(angle), math.sin(angle)], (12, 1))
    )
    for joint, parent in enumerate(source["joint_parents"]):
        if parent >= 0:
            np.testing.assert_allclose(
                np.linalg.norm(out["posed_joints"][:, joint] - out["posed_joints"][:, parent], axis=1),
                np.linalg.norm(source["posed_joints"][:, joint] - source["posed_joints"][:, parent], axis=1),
            )
    np.testing.assert_array_equal(out["foot_contacts"], source["foot_contacts"])


def test_rebase_uses_retained_first_root_and_preserves_height():
    source = fixture()
    out, _ = apply_edit(
        source,
        VariantEdit(start_frame=3, root_origin="first_frame", yaw_radians=0.7, translation_xz=[5.0, 2.0]),
    )
    np.testing.assert_allclose(out["root_positions"][0], [5, 1, 2])
    np.testing.assert_allclose(out["smooth_root_pos"][0], [5, 0, 2])


def test_joint_correction_taper_untouched_frames_and_invalidated_contacts():
    source = fixture()
    out, details = apply_edit(source, VariantEdit(joint_adjustments=[adjustment()]))
    assert "foot_contacts" not in out
    assert details["contact_status"] == "invalidated_by_joint_adjustment"
    assert details["adjusted_frame_count"] == 7
    for key in ("posed_joints", "local_rot_mats", "global_rot_mats"):
        np.testing.assert_array_equal(out[key][[0, 1, 2, 10, 11]], source[key][[0, 1, 2, 10, 11]])
    for key in ("root_positions", "smooth_root_pos", "global_root_heading"):
        np.testing.assert_array_equal(out[key], source[key])
    joint = source["joint_names"].tolist().index("LeftArm")
    expected = np.array([[math.cos(0.5), -math.sin(0.5), 0], [math.sin(0.5), math.cos(0.5), 0], [0, 0, 1]])
    np.testing.assert_allclose(out["local_rot_mats"][5, joint], expected)
    assert not np.array_equal(out["posed_joints"][5], source["posed_joints"][5])


def test_local_axes_composition_noncommuting_and_hard_window():
    source = fixture()
    names = source["joint_names"].tolist()
    edit = JointAdjustment(
        joint_name="LeftArm",
        local_xyz_radians=[math.pi / 2, math.pi / 2, 0.0],
        start_frame=4,
        end_frame_exclusive=5,
        blend_frames=0,
    )
    out, _ = apply_edit(source, VariantEdit(joint_adjustments=[edit]))
    rx = np.array([[1, 0, 0], [0, 0, -1], [0, 1, 0]])
    ry = np.array([[0, 0, 1], [0, 1, 0], [-1, 0, 0]])
    np.testing.assert_allclose(out["local_rot_mats"][4, names.index("LeftArm")], rx @ ry, atol=1e-15)
    np.testing.assert_array_equal(out["posed_joints"][:4], source["posed_joints"][:4])
    np.testing.assert_array_equal(out["posed_joints"][5:], source["posed_joints"][5:])


def test_zero_adjustment_keeps_contacts_and_all_arrays():
    source = fixture()
    edit = JointAdjustment(
        joint_name="LeftArm", local_xyz_radians=[0.0, 0.0, 0.0], start_frame=0, end_frame_exclusive=12
    )
    out, details = apply_edit(source, VariantEdit(joint_adjustments=[edit]))
    assert details["adjusted_frame_count"] == 0
    assert "foot_contacts" in out
    for key in source:
        np.testing.assert_array_equal(source[key], out[key])


def test_parent_child_adjustments_order_independent_and_lengths_preserved():
    source = fixture()
    a = adjustment()
    b = a.model_copy(update={"joint_name": "LeftForeArm", "local_xyz_radians": [0.2, 0.3, 0.0]})
    left, _ = apply_edit(source, VariantEdit(joint_adjustments=[a, b]))
    right, _ = apply_edit(source, VariantEdit(joint_adjustments=[b, a]))
    for key in left:
        np.testing.assert_array_equal(left[key], right[key])
    for joint, parent in enumerate(source["joint_parents"]):
        if parent >= 0:
            np.testing.assert_allclose(
                np.linalg.norm(left["posed_joints"][:, joint] - left["posed_joints"][:, parent], axis=-1),
                np.linalg.norm(source["posed_joints"][:, joint] - source["posed_joints"][:, parent], axis=-1),
                atol=1e-12,
            )


@pytest.mark.parametrize(
    "values",
    [
        {"start_frame": True},
        {"end_frame_exclusive": 1},
        {"yaw_radians": float("nan")},
        {"yaw_radians": True},
        {"yaw_radians": 4.0},
        {"translation_xz": [1001.0, 0.0]},
        {"translation_xz": [0.0, float("inf")]},
        {
            "joint_adjustments": [
                {
                    "joint_name": "LeftArm",
                    "local_xyz_radians": [2.0, 0.0, 0.0],
                    "start_frame": 0,
                    "end_frame_exclusive": 10,
                }
            ]
        },
        {
            "joint_adjustments": [
                {
                    "joint_name": "LeftArm",
                    "local_xyz_radians": [0.0, 0.0, 0.2],
                    "start_frame": 0,
                    "end_frame_exclusive": 2,
                }
            ]
        },
    ],
)
def test_invalid_controls(values):
    with pytest.raises(ValueError):
        VariantEdit.model_validate(values)


@pytest.mark.parametrize("name", ["Hips", "Unknown"])
def test_root_or_unknown_joint_rejected(name):
    with pytest.raises(ValueError, match="nonroot"):
        apply_edit(
            fixture(), VariantEdit(joint_adjustments=[adjustment().model_copy(update={"joint_name": name})])
        )


def test_bad_window_duplicates_and_static_geometry_rejected():
    with pytest.raises(ValueError):
        VariantEdit(start_frame=4, joint_adjustments=[adjustment()])
    with pytest.raises(ValueError):
        VariantEdit(joint_adjustments=[adjustment(), adjustment()])
    source = fixture()
    source["posed_joints"][1, 20, 0] += 0.01
    with pytest.raises(ValueError, match="static"):
        apply_edit(source, VariantEdit(joint_adjustments=[adjustment()]))


@pytest.mark.parametrize("mutation", ["metadata", "rotation", "root", "heading", "channel", "contacts"])
def test_inconsistent_native_channels_rejected(mutation):
    source = fixture()
    if mutation == "metadata":
        source["joint_names"][0] = "Root"
    elif mutation == "rotation":
        source["global_rot_mats"][0, 2, 0, 0] = 2
    elif mutation == "root":
        source["root_positions"][0, 0] += 1
    elif mutation == "heading":
        source["global_root_heading"][0] = [0, 0]
    elif mutation == "channel":
        source["custom"] = np.zeros(1)
    else:
        del source["foot_contacts"]
    with pytest.raises(ValueError):
        apply_edit(source, VariantEdit())


def managed(tmp_path):
    job = tmp_path / ("a" * 32)
    job.mkdir()
    path = job / "motion_00.npz"
    np.savez(path, **fixture())
    return path


def test_receipt_reuse_metadata_source_immutability_and_no_chains(tmp_path):
    path = managed(tmp_path)
    before = path.read_bytes()
    kwargs = dict(
        source_sha256=_digest(path),
        job_id=path.parent.name,
        edit=VariantEdit(joint_adjustments=[adjustment()], root_origin="first_frame"),
    )
    report = create_motion_variant(path, **kwargs)
    assert report == create_motion_variant(path, **kwargs)
    assert list_motion_variants(path.parent) == [report]
    assert report["contact_status"] == "invalidated_by_joint_adjustment" and report["source_depth"] == 1
    assert path.read_bytes() == before
    with pytest.raises(ValueError, match="chains"):
        create_motion_variant(
            Path(report["artifact"]["absolute_path"]),
            source_sha256=report["artifact"]["sha256"],
            job_id=path.parent.name,
        )


def test_default_end_normalizes_same_id_and_changes_have_distinct_ids(tmp_path):
    path = managed(tmp_path)
    kwargs = dict(source_sha256=_digest(path), job_id=path.parent.name)
    first = create_motion_variant(path, **kwargs)
    explicit = create_motion_variant(path, **kwargs, edit=VariantEdit(end_frame_exclusive=12))
    moved = create_motion_variant(path, **kwargs, edit=VariantEdit(yaw_radians=0.1))
    assert first["variant_id"] == explicit["variant_id"] != moved["variant_id"]


@pytest.mark.parametrize("target", ["source", "output", "report", "request"])
def test_tampered_variants_never_listed_or_reused(tmp_path, target):
    path = managed(tmp_path)
    kwargs = dict(source_sha256=_digest(path), job_id=path.parent.name)
    report = create_motion_variant(path, **kwargs)
    directory = Path(report["output_directory"])
    if target == "source":
        path.write_bytes(path.read_bytes() + b"changed")
    elif target == "output":
        output = Path(report["artifact"]["absolute_path"])
        with np.load(output, allow_pickle=False) as archive:
            arrays = {key: archive[key] for key in archive.files}
        arrays["posed_joints"][0, 10, 0] += 0.01
        np.savez(output, **arrays)
    else:
        file = directory / ("variant-report.json" if target == "report" else "request.json")
        payload = json.loads(file.read_text())
        payload["source_sha256"] = "0" * 64
        file.write_text(json.dumps(payload))
    with pytest.raises(ValueError):
        verify_motion_variant(directory)
    with pytest.raises(ValueError):
        create_motion_variant(path, **kwargs)
    assert list_motion_variants(path.parent) == []


def test_locked_or_incomplete_variant_is_not_replaced(tmp_path):
    path = managed(tmp_path)
    kwargs = dict(source_sha256=_digest(path), job_id=path.parent.name)
    report = create_motion_variant(path, **kwargs)
    with FileLock(str(path.parent / f".variant-{report['variant_id']}.lock")):
        with pytest.raises(ValueError, match="already"):
            create_motion_variant(path, **kwargs)
    Path(report["report_path"]).unlink()
    with pytest.raises(ValueError, match="incomplete"):
        create_motion_variant(path, **kwargs)
