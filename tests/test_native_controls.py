"""Native adapter contracts tested without models, downloads, or an editor."""

import copy
import hashlib
import json
import sys
from types import SimpleNamespace

import numpy as np
import pytest
from test_worker import FakeModel, fake_runtime, output_arrays

from kimodo_mcp.animation_plans import AnimationPlanStore
from kimodo_mcp.planning import AnimationRevision, plan_revision
from kimodo_mcp.schemas import GenerateRequest
from kimodo_mcp.worker import generate, project_g1_output


def request(**kwargs):
    return GenerateRequest(segments=[{"text": "Walk forward", "duration": 2.0}], **kwargs)


@pytest.mark.parametrize("mode,expected", [
    ("separated", [3.0, 4.0]), ("regular", 1.5), ("nocfg", 1.5),
])
def test_guidance_exact_native_argument_shape_and_preserved_weights(mode, expected, monkeypatch, tmp_path):
    req = request(cfg_type=mode, cfg_regular=1.5, cfg_text=3.0, cfg_constraint=4.0)
    model = FakeModel()
    fake_runtime(monkeypatch, model)
    manifest = generate(req, tmp_path)
    kwargs = model.calls[0][2]
    assert (kwargs["cfg_type"], kwargs["cfg_weight"]) == (mode, expected)
    assert req.validated_plan()["guidance"] == {"type": mode, "weights": None if mode == "nocfg" else expected}
    effective = manifest["effective_settings"]
    assert [effective[key] for key in ("cfg_regular", "cfg_text", "cfg_constraint")] == [1.5, 3.0, 4.0]


@pytest.mark.parametrize("changes", [
    {"cfg_type": "off"}, {"cfg_regular": True}, {"cfg_regular": "2"},
    {"cfg_regular": float("nan")}, {"cfg_regular": float("inf")},
    {"cfg_regular": -1}, {"cfg_regular": 21}, {"real_robot_rotations": "true"},
    {"real_robot_rotations": 1}, {"real_robot_rotations": True},
    {"model": "kimodo-smplx-rp", "real_robot_rotations": True},
])
def test_new_native_controls_reject_invalid_or_incompatible_values(changes):
    with pytest.raises(ValueError):
        request(**changes)


@pytest.mark.parametrize("model", ["kimodo-g1-rp", "kimodo-g1-seed"])
def test_projection_plan_explicit_caveat_and_revision_preservation(model):
    req = request(model=model, real_robot_rotations=True, cfg_type="regular", cfg_regular=1.25)
    original = req.model_dump(mode="json")
    revised = plan_revision(original, AnimationRevision(description="Walk and wave"))
    assert {key for key in revised["changes"]} == {"segments"}
    for key in ("model", "real_robot_rotations", "cfg_type", "cfg_regular", "cfg_text", "cfg_constraint"):
        assert revised["request"][key] == original[key]
    advice = next(item for item in revised["plan"]["advisories"] if item["code"] == "g1_kinematic_projection")
    assert "contact labels" in advice["message"] and "safe real-robot" in advice["action"]


def test_projection_updates_all_native_kinematic_arrays_without_mutating_input(monkeypatch):
    class Tensor:
        def __init__(self, value):
            self.value = value

        def to(self, device):
            assert device == "cpu"
            return self

        def detach(self):
            return self

        def cpu(self):
            return self

        def numpy(self):
            return self.value

    skeleton = object()
    source = output_arrays(joints=34)
    snapshot = copy.deepcopy(source)
    projected = {key: Tensor(source[key] + 0.25) for key in (
        "local_rot_mats", "global_rot_mats", "posed_joints", "root_positions")}

    class Converter:
        def __init__(self, received):
            assert received is skeleton

        def project_to_real_robot_rotations(self, local, root, *, clamp_to_limits):
            assert clamp_to_limits is True
            np.testing.assert_array_equal(local.value, snapshot["local_rot_mats"])
            np.testing.assert_array_equal(root.value, snapshot["root_positions"])
            return projected

    monkeypatch.setitem(sys.modules, "torch", SimpleNamespace(from_numpy=Tensor))
    monkeypatch.setitem(sys.modules, "kimodo.exports.mujoco", SimpleNamespace(MujocoQposConverter=Converter))
    result = project_g1_output(source, SimpleNamespace(device="cpu", output_skeleton=skeleton))
    for key, value in source.items():
        np.testing.assert_array_equal(value, snapshot[key])
        np.testing.assert_array_equal(result[key], projected[key].value if key in projected else value)


def test_worker_projects_before_publishing_npz_and_revalidates(monkeypatch, tmp_path):
    class G1Model(FakeModel):
        output_skeleton = SimpleNamespace(
            name="g1skel34", joint_parents=SimpleNamespace(
                detach=lambda: SimpleNamespace(cpu=lambda: SimpleNamespace(numpy=lambda: np.array([-1] + [0] * 33)))),
            bone_order_names=[f"joint_{index}" for index in range(34)],
        )

        def __call__(self, prompts, frames, **kwargs):
            return output_arrays(kwargs["num_samples"], sum(frames), 34)

    model = G1Model()
    fake_runtime(monkeypatch, model)
    calls = []

    def project(output, received_model):
        calls.append(received_model)
        return {**output, "posed_joints": output["posed_joints"] + 0.125}

    monkeypatch.setattr("kimodo_mcp.worker.project_g1_output", project)
    req = request(model="kimodo-g1-rp", real_robot_rotations=True)
    manifest = generate(req, tmp_path)
    assert calls == [model]
    with np.load(tmp_path / "motion_00.npz", allow_pickle=False) as result:
        np.testing.assert_array_equal(result["posed_joints"], np.full((60, 34, 3), 0.125))
    assert manifest["effective_settings"]["real_robot_rotations"] is True
    assert any("Contact labels remain" in warning for warning in manifest["warnings"])
    bad = tmp_path / "bad"
    bad.mkdir()
    monkeypatch.setattr("kimodo_mcp.worker.project_g1_output", lambda output, _: {**output, "posed_joints": np.zeros((1, 1, 1, 3))})
    with pytest.raises(ValueError, match="shape"):
        generate(req, bad)
    assert not (bad / "manifest.json").exists()


def test_old_saved_plan_keeps_its_original_identity_and_request_digest(tmp_path):
    def digest(value):
        return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"),
                                         ensure_ascii=True, allow_nan=False).encode()).hexdigest()

    store = AnimationPlanStore(tmp_path)
    original = request().model_dump(mode="json")
    for key in ("cfg_type", "cfg_regular", "real_robot_rotations"):
        original.pop(key)
    content = {"schema_version": 1, "title": "Pre-upgrade plan", "description": "",
               "request": original, "parent_plan_id": None}
    plan_id = digest(content)
    receipt = {**content, "plan_id": plan_id, "request_sha256": digest(original),
               "created_at": "2026-11-03T00:00:00+00:00"}
    receipt["receipt_sha256"] = digest(receipt)
    store.root.mkdir(parents=True)
    path = store.root / f"{plan_id}.json"
    path.write_text(json.dumps(receipt), encoding="utf-8")
    snapshot = path.read_bytes()
    result = store.get(plan_id)
    assert result["plan_id"] == plan_id and result["request_sha256"] == digest(original)
    assert result["request"] == original and path.read_bytes() == snapshot
    assert result["plan"]["guidance"] == {"type": "separated", "weights": [2.0, 2.0]}
    assert store.list()["invalid_count"] == 0
    revised = plan_revision(result["request"], AnimationRevision(seed=123))
    saved = store.save("Updated", GenerateRequest.model_validate(revised["request"]), parent_plan_id=plan_id)
    assert saved["parent_plan_id"] == plan_id and saved["request"]["cfg_type"] == "separated"
