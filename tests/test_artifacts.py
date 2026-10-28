"""Numerical and adversarial archive tests; fixtures are synthetic, not generated motion."""

import io
import json
import re
import zipfile

import numpy as np
import pytest

from kimodo_mcp.artifacts import create_preview, inspect_motion


def save_motion(path, *, batched=False, **overrides):
    positions = np.array(
        [
            [[0, 1, 0], [0, 2, 0], [1, 1, 0]],
            [[0, 1, 1], [0, 2, 1], [1, 1, 1]],
            [[0, 1, 2], [0, 2, 2], [1, 1, 2]],
        ],
        dtype=np.float32,
    )
    rotations = np.broadcast_to(np.eye(3), (3, 3, 3, 3)).copy()
    if batched:
        positions = np.stack([positions, positions + np.array([2, 0, 0])])
        rotations = np.stack([rotations, rotations])
    data = {
        "posed_joints": positions,
        "global_rot_mats": rotations,
        "local_rot_mats": rotations.copy(),
        "joint_parents": np.array([-1, 0, 0]),
        "joint_names": np.array(["Hips", "Head", "LeftHand"]),
        "fps": np.array(30),
        "skeleton": np.array("test-skeleton"),
        "model": np.array("test-model"),
    }
    data.update(overrides)
    data = {key: value for key, value in data.items() if value is not None}
    np.savez_compressed(path, **data)
    return data


def test_inspection_measures_motion_and_rotation_validity(tmp_path):
    path = tmp_path / "motion.npz"
    save_motion(path)
    result = inspect_motion(path)
    assert result["frames"] == 3
    assert result["joints"] == 3
    assert result["samples"] == 1
    assert result["duration_seconds"] == pytest.approx(0.1)
    assert result["sample_statistics"][0]["root_travel_m"] == pytest.approx(2)
    assert result["sample_statistics"][0]["mean_root_speed_m_s"] == pytest.approx(30)
    assert result["max_bone_length_variation_m"] == pytest.approx(0)
    assert result["rotation_checks"]["global_rot_mats"]["determinant_min"] == pytest.approx(1)
    assert result["warnings"] == []


def test_batches_have_independent_bounds(tmp_path):
    path = tmp_path / "batch.npz"
    save_motion(path, batched=True)
    result = inspect_motion(path)
    assert result["samples"] == 2
    assert result["sample_statistics"][1]["bounds_min_m"] == [2, 1, 0]


def test_boolean_foot_contacts_from_upstream_examples_are_supported(tmp_path):
    path = tmp_path / "contacts.npz"
    save_motion(path, foot_contacts=np.array([[True, False]] * 3))
    result = inspect_motion(path)
    assert result["foot_contact_range"] == [0.0, 1.0]


@pytest.mark.parametrize("metadata", [{"fps": None}, {"joint_parents": None}])
def test_missing_metadata_is_reported_without_invented_timing_or_bones(tmp_path, metadata):
    path = tmp_path / "plain.npz"
    save_motion(path, **metadata)
    result = inspect_motion(path)
    assert result["warnings"]
    if "fps" in metadata:
        assert result["duration_seconds"] is None
    with pytest.raises(ValueError, match="metadata"):
        create_preview(path, tmp_path / "preview.html")


@pytest.mark.parametrize(
    "overrides,match",
    [
        ({"joint_parents": np.array([-1, 2, 1])}, "cycle"),
        ({"joint_parents": np.array([-1, -1, 0])}, "one root"),
        ({"joint_parents": np.array([-1, 0, 3])}, "out-of-range"),
        ({"joint_parents": np.array([-1.0, 0.0, 0.0])}, "integer"),
        ({"posed_joints": np.ones((0, 3, 3))}, "empty"),
        ({"posed_joints": np.ones((3, 2))}, "shape"),
        ({"posed_joints": np.full((3, 3, 3), np.nan)}, "finite"),
        ({"posed_joints": np.full((3, 3, 3), np.inf)}, "finite"),
        ({"fps": np.array(0)}, "between"),
        ({"fps": np.array([30, 60])}, "scalar"),
        ({"global_rot_mats": np.ones((2, 3, 3, 3))}, "shape"),
        ({"foot_contacts": np.ones((2, 4))}, "shape"),
        ({"joint_names": np.array(["only one"])}, "joint_names"),
    ],
)
def test_invalid_motion_is_rejected(tmp_path, overrides, match):
    path = tmp_path / "invalid.npz"
    save_motion(path, **overrides)
    with pytest.raises(ValueError, match=match):
        inspect_motion(path)


def test_object_array_is_rejected_even_in_unused_field(tmp_path):
    path = tmp_path / "object.npz"
    save_motion(path, unused=np.array([{"payload": "must not deserialize"}], dtype=object))
    with pytest.raises(ValueError, match="only numeric"):
        inspect_motion(path)


def test_forged_huge_array_header_rejected_before_allocation(tmp_path):
    path = tmp_path / "forged.npz"
    stream = io.BytesIO()
    np.lib.format.write_array_header_1_0(
        stream, {"descr": "<f8", "fortran_order": False, "shape": (10**12, 3)}
    )
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("posed_joints.npy", stream.getvalue())
    with pytest.raises(ValueError, match="header"):
        inspect_motion(path)


def test_duplicate_and_path_members_are_rejected(tmp_path):
    path = tmp_path / "traversal.npz"
    stream = io.BytesIO()
    np.save(stream, np.ones((3, 3, 3)))
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("../posed_joints.npy", stream.getvalue())
    with pytest.raises(ValueError, match="invalid array names"):
        inspect_motion(path)


def test_truncated_npz_is_rejected(tmp_path):
    path = tmp_path / "incomplete.npz"
    path.write_bytes(b"PK\x03\x04partial")
    with pytest.raises(ValueError, match="valid NPZ"):
        inspect_motion(path)


def test_numeric_warnings_do_not_claim_quality(tmp_path):
    path = tmp_path / "distorted.npz"
    data = save_motion(path)
    data["posed_joints"][1, 1, 1] += 1
    data["global_rot_mats"][0, 0] *= 2
    np.savez_compressed(path, **data)
    result = inspect_motion(path)
    assert result["max_bone_length_variation_m"] == pytest.approx(1)
    assert len(result["warnings"]) == 2
    assert "do not establish motion quality" in result["assessment_scope"]


def test_preview_is_self_contained_and_escapes_untrusted_strings(tmp_path):
    path = tmp_path / "motion.npz"
    payload = '</script><img src="https://example.test" onerror="alert(1)">'
    save_motion(path, skeleton=np.array(payload), joint_names=np.array([payload, "head", "hand"]))
    output = tmp_path / "preview.html"
    result = create_preview(path, output)
    source = output.read_text(encoding="utf-8")
    assert payload not in source
    assert "<script src=" not in source
    assert "<link " not in source
    assert "connect-src 'none'" in source
    embedded = re.search(r'<script id="motion-data" type="application/json">(.*?)</script>', source, re.S)
    assert embedded
    motion = json.loads(embedded.group(1))
    assert motion["skeleton"] == payload
    assert motion["positions"][0][2][0] == [0, 1, 2]
    assert motion["frameIndices"] == [0, 1, 2]
    assert result["offline"] is True
    assert result["downsampled"] is False
    assert len(result["source_sha256"]) == 64


def test_preview_downsampling_preserves_source_endpoints_and_timing(tmp_path):
    path = tmp_path / "long.npz"
    positions = np.zeros((1601, 3, 3))
    positions[:, :, 0] = np.arange(1601)[:, None] * 0.01
    save_motion(path, posed_joints=positions, global_rot_mats=None, local_rot_mats=None)
    result = create_preview(path, tmp_path / "long.html")
    assert result["preview_frames"] == 1500
    assert result["frames"] == 1601
    assert result["duration_seconds"] == pytest.approx(1601 / 30, abs=1e-6)
    assert result["downsampled"] is True


def test_preview_requires_html_extension(tmp_path):
    path = tmp_path / "motion.npz"
    save_motion(path)
    with pytest.raises(ValueError, match=".html"):
        create_preview(path, tmp_path / "wrong.py")
