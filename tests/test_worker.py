import contextlib
import hashlib
import json
import sys
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pytest

from kimodo_mcp.schemas import GenerateRequest
from kimodo_mcp.worker import (
    artifact_record,
    configure_runtime_environment,
    constraints_to_upstream,
    filter_empty_segment_constraints,
    generate,
    load_request,
    main,
    validate_constraint_overlap,
    validate_output,
    write_progress,
)


def request(**overrides):
    return GenerateRequest.model_validate({"segments": [{"text": "A person walks.", "duration": 2.0}], **overrides})


def test_generation_progress_survives_a_transient_windows_reader_collision(tmp_path, monkeypatch):
    original = Path.replace
    attempts = []

    def replace(path, target):
        attempts.append(path)
        if len(attempts) == 1:
            error = PermissionError("Reader briefly holds the previous snapshot")
            error.winerror = 32
            raise error
        return original(path, target)

    monkeypatch.setattr(Path, "replace", replace)
    write_progress(tmp_path, "exporting", completed_samples=1)
    assert json.loads((tmp_path / "progress.json").read_text()) == {
        "stage": "exporting", "completed_samples": 1,
    }
    assert len(attempts) == 2
    assert list(tmp_path.iterdir()) == [tmp_path / "progress.json"]


def output_arrays(samples=1, frames=60, joints=77):
    return {
        "local_rot_mats": np.broadcast_to(np.eye(3), (samples, frames, joints, 3, 3)).copy(),
        "global_rot_mats": np.broadcast_to(np.eye(3), (samples, frames, joints, 3, 3)).copy(),
        "posed_joints": np.zeros((samples, frames, joints, 3)),
        "root_positions": np.zeros((samples, frames, 3)),
        "smooth_root_pos": np.zeros((samples, frames, 3)),
        "foot_contacts": np.ones((samples, frames, 6), dtype=bool),
        "global_root_heading": np.broadcast_to([1.0, 0.0], (samples, frames, 2)).copy(),
        "posed_joints_norootpos": np.zeros((samples, frames, 30, 3)),
    }


def test_output_filters_helper_fields_and_rejects_nonfinite_or_wrong_shapes():
    output = output_arrays()
    assert "posed_joints_norootpos" not in validate_output(output, request(), 60, 77)
    output["root_positions"][0, 0, 0] = np.nan
    with pytest.raises(ValueError, match="finite"):
        validate_output(output, request(), 60, 77)
    with pytest.raises(ValueError, match="shape"):
        validate_output(output_arrays(frames=59), request(), 60, 77)


def test_named_limb_expansion_for_native_masks():
    constraint = {
        "type": "end-effector", "frame_indices": [0],
        "local_joints_rot": [[[0.0, 0.0, 0.0] for _ in range(77)]],
        "root_positions": [[0.0, 1.0, 0.0]], "joint_names": ["LeftHand", "RightFoot"],
    }
    converted = constraints_to_upstream(request(constraints=[constraint]))
    assert [item["type"] for item in converted] == ["left-hand", "right-foot"]
    assert all("joint_names" not in item for item in converted)
    assert constraints_to_upstream(request(constraints=[constraint], postprocess=False))[0]["type"] == "end-effector"


class FakeParents:
    def detach(self):
        return self

    def cpu(self):
        return self

    def numpy(self):
        return np.asarray([-1] + [0] * 76)


class FakeModel:
    fps = 30
    diffusion = SimpleNamespace(num_base_steps=1000)
    device = "cpu"
    output_skeleton = SimpleNamespace(name="somaskel77", joint_parents=FakeParents(), bone_order_names=[f"joint_{index}" for index in range(77)])
    skeleton = object()

    def __init__(self):
        self.calls = []

    def __call__(self, prompts, frames, **kwargs):
        self.calls.append((prompts, frames, kwargs))
        return output_arrays(kwargs["num_samples"], sum(frames))


def fake_runtime(monkeypatch, model):
    fake_torch = SimpleNamespace(cuda=SimpleNamespace(is_available=lambda: False), inference_mode=contextlib.nullcontext)
    monkeypatch.setitem(sys.modules, "torch", fake_torch)
    monkeypatch.setitem(sys.modules, "kimodo", SimpleNamespace(load_model=lambda name, **kwargs: (model, name)))
    monkeypatch.setitem(sys.modules, "kimodo.constraints", SimpleNamespace(load_constraints_lst=lambda data, skeleton: []))
    monkeypatch.setitem(sys.modules, "kimodo.tools", SimpleNamespace(seed_everything=lambda seed: None))


def test_full_adapter_contract_with_stub_runtime(monkeypatch, tmp_path):
    """Exercises adapter wiring/serialization, without claiming model inference."""
    model = FakeModel()
    fake_runtime(monkeypatch, model)
    manifest = generate(request(samples=2), tmp_path)
    assert model.calls[0][0:2] == (["A person walks."], [60])
    assert model.calls[0][2]["multi_prompt"] is True
    assert model.calls[0][2]["num_samples"] == 2
    assert len(manifest["artifacts"]) == 2
    assert json.loads((tmp_path / "manifest.json").read_text())["frames"] == 60
    assert json.loads((tmp_path / "progress.json").read_text())["stage"] == "complete"
    for artifact in manifest["artifacts"]:
        file = tmp_path / artifact["path"]
        assert hashlib.sha256(file.read_bytes()).hexdigest() == artifact["sha256"]
        with np.load(file, allow_pickle=False) as data:
            assert data["posed_joints"].shape == (60, 77, 3)
            assert data["joint_names"].dtype.kind == "U"
            assert data["joint_parents"].shape == (77,)
            assert float(data["fps"]) == 30
            assert "posed_joints_norootpos" not in data


def test_failed_output_does_not_publish_manifest(monkeypatch, tmp_path):
    class BadModel(FakeModel):
        def __call__(self, *args, **kwargs):
            return output_arrays(frames=59)

    fake_runtime(monkeypatch, BadModel())
    with pytest.raises(ValueError, match="shape"):
        generate(request(), tmp_path)
    assert not (tmp_path / "manifest.json").exists()


def test_offline_default_and_operator_download_setting(monkeypatch):
    monkeypatch.delenv("KIMODO_ALLOW_DOWNLOADS", raising=False)
    monkeypatch.delenv("HF_HUB_OFFLINE", raising=False)
    monkeypatch.delenv("TRANSFORMERS_OFFLINE", raising=False)
    configure_runtime_environment()
    assert __import__("os").environ["HF_HUB_OFFLINE"] == "1"
    monkeypatch.setenv("KIMODO_ALLOW_DOWNLOADS", "1")
    monkeypatch.delenv("HF_HUB_OFFLINE")
    configure_runtime_environment()
    assert "HF_HUB_OFFLINE" not in __import__("os").environ


def test_request_revalidation_and_no_overwrite(tmp_path):
    req = tmp_path / "request.json"
    req.write_text(request().model_dump_json())
    assert load_request(req).frame_count == 60
    output = tmp_path / "output"
    output.mkdir()
    protected = output / "motion_00.npz"
    protected.write_bytes(b"existing")
    assert main(["--request", str(req), "--output", str(output)]) == 1
    assert protected.read_bytes() == b"existing"


def test_main_accepts_supervisor_files(monkeypatch, tmp_path):
    fake_runtime(monkeypatch, FakeModel())
    req = tmp_path / "request.json"
    req.write_text(request().model_dump_json())
    (tmp_path / "runner.log").write_text("supervisor")
    (tmp_path / "worker.log").write_text("")
    assert main(["--request", str(req), "--output", str(tmp_path)]) == 0
    assert (tmp_path / "motion_00.npz").is_file()


def test_actual_overlapping_channels_must_agree(monkeypatch):
    class Array(np.ndarray):
        def clone(self):
            return self.copy()

        def bool(self):
            return self.astype(bool)

    monkeypatch.setitem(sys.modules, "torch", SimpleNamespace(allclose=np.allclose))
    calls = []

    def build(constraints, frames, **kwargs):
        calls.append(kwargs)
        return np.asarray([[constraints[0], 0.0]]).view(Array), np.asarray([[True, False]]).view(Array)

    model = SimpleNamespace(device="cpu", motion_rep=SimpleNamespace(create_conditions_from_constraints=build))
    validate_constraint_overlap(model, [1.0, 1.0], 1)
    assert calls[0]["to_normalize"] is False
    with pytest.raises(ValueError, match="conflicting"):
        validate_constraint_overlap(model, [1.0, 2.0], 1)


def test_empty_segment_constraint_crops_are_filtered():
    rep = SimpleNamespace(create_conditions_from_constraints_batched=lambda constraints, **kwargs: constraints)
    model = SimpleNamespace(motion_rep=rep)
    empty = SimpleNamespace(frame_indices=[])
    present = SimpleNamespace(frame_indices=[0])
    filter_empty_segment_constraints(model)
    assert rep.create_conditions_from_constraints_batched([empty, present]) == [present]
    assert rep.create_conditions_from_constraints_batched([[empty], [present]]) == [[], [present]]


def test_artifact_metadata_hashes_file(tmp_path):
    artifact = tmp_path / "motion_00.csv"
    artifact.write_bytes(b"1,2,3\n")
    record = artifact_record(artifact, "csv", 0)
    assert record["bytes"] == 6
    assert record["path"] == "motion_00.csv"
