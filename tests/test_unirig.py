"""Released-stage wiring/provenance tests with synthetic assets and fake runtimes."""

import copy
import json
import struct
import sys
import time
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pytest

from kimodo_mcp import unirig, unirig_worker
from kimodo_mcp.config import Settings
from kimodo_mcp.jobs import atomic_json


def glb_bytes(*, skin=True):
    value = {"asset": {"version": "2.0"}, "meshes": [{"primitives": []}]}
    if skin:
        value["skins"] = [{"joints": [0]}]
    text = json.dumps(value).encode()
    text += b" " * ((-len(text)) % 4)
    return struct.pack("<4sIII4s", b"glTF", 2, 20 + len(text), len(text), b"JSON") + text


@pytest.fixture
def environment(tmp_path, monkeypatch):
    settings = Settings(data_dir=tmp_path / "data", python=sys.executable)
    repo = tmp_path / "upstream"
    (repo / "configs").mkdir(parents=True)
    (repo / "configs/reviewed.yaml").write_text("test: true")
    asset = tmp_path / "a source with spaces.glb"
    asset.write_bytes(glb_bytes())
    digest = unirig._hash(asset)
    source = {"asset_id": digest, "absolute_path": str(asset), "sha256": digest,
              "bytes": asset.stat().st_size, "format": "glb"}
    monkeypatch.setattr(unirig, "resolve_registered_asset", lambda settings, identifier: copy.deepcopy(source))
    runtime = {"repo": str(repo), "python": sys.executable, "reviewed_revision": unirig.UNIRIG_REVISION,
               "checks": [{"name": "fixture", "ready": True, "detail": "Offline fixture"}],
               "configuration_ready": True, "downloads_allowed": False, "modules": {},
               "dependency_probe_performed": True, "gpu_verified": False, "weights_verified": False}
    monkeypatch.setattr(unirig, "_runtime", lambda **kwargs: copy.deepcopy(runtime))
    calls = []
    monkeypatch.setattr(unirig.subprocess, "Popen", lambda *args, **kwargs: (
        calls.append((args, {key: value for key, value in kwargs.items() if key != "env"}))
        or SimpleNamespace(pid=2147483646)))
    monkeypatch.setattr(unirig, "_process_identity", lambda pid: "fixture-start")
    return settings, source, runtime, calls


def request(source, **kwargs):
    return unirig.UniRigRequest(assets=[{"asset_id": source["asset_id"]}], **kwargs)


@pytest.mark.parametrize("changes", [
    {"assets": []}, {"assets": [{"asset_id": "../asset"}]},
    {"assets": [{"asset_id": "a" * 64}] * 5}, {"seed": True}, {"seed": -1},
    {"faces_target_count": 99}, {"faces_target_count": 100001}, {"num_beams": 31},
    {"top_k": 0}, {"top_p": 0}, {"top_p": 1.1}, {"temperature": float("nan")},
    {"temperature": True}, {"repetition_penalty": 11}, {"add_root": "false"},
    {"operation": "train"}, {"output_format": "blend"}, {"task": "other.yaml"},
    {"operation": "merge"}, {"operation": "preprocess", "temperature": 2.0},
    {"operation": "skeleton", "skin_backend": "open3d"},
    {"do_sample": "false"}, {"max_new_tokens": True}, {"max_new_tokens": 0},
    {"max_new_tokens": 2049}, {"operation": "skin", "do_sample": False},
    {"operation": "merge", "max_new_tokens": 1000},
    {"assets": [{"asset_id": "a" * 64, "rig_asset_id": "b" * 64, "source_job_id": "c" * 32}]},
    {"assets": [{"asset_id": "a" * 64, "source_item_index": 1}]},
])
def test_schema_rejects_unbounded_ambiguous_or_nonapplicable_inputs(changes):
    with pytest.raises(ValueError):
        unirig.UniRigRequest.model_validate({"assets": [{"asset_id": "a" * 64}], **changes})


def test_plan_does_not_launch_and_preserves_all_explicit_controls(environment):
    settings, source, runtime, calls = environment
    controls = request(source, num_beams=8, top_k=7, top_p=0.8, temperature=1.2,
                       repetition_penalty=2.1, seed=42, faces_target_count=12000, add_root=True)
    result = unirig.plan_unirig(settings, controls)
    assert result["ready"] and result["request"] == controls.model_dump(mode="json")
    assert result["assets"][0]["mesh"]["sha256"] == source["sha256"]
    assert result["stages"] == ["extract_mesh", "predict_skeleton", "extract_rig", "predict_skin", "merge_rig"]
    assert not settings.data_dir.exists() and calls == []
    assert not result["runtime"]["gpu_verified"] and not result["runtime"]["weights_verified"]


def test_fixed_supervisor_command_and_explicit_cancellation(environment):
    settings, source, _, calls = environment
    job = unirig.start_unirig(settings, request(source))
    directory = unirig._directory(settings, job["id"])
    args, kwargs = calls[0]
    assert args[0] == [sys.executable, "-m", "kimodo_mcp.unirig_worker", str(directory)]
    assert "shell" not in kwargs and job["status"] == "queued"
    internal = unirig._json(directory / "request.json")
    assert unirig._canonical(internal) == job["request_sha256"]
    with pytest.raises(ValueError, match="already active"):
        unirig.start_unirig(settings, request(source))
    cancelled = unirig.cancel_unirig_job(settings, job["id"])
    assert cancelled["status"] == "cancelling"
    assert unirig._json(directory / "cancel.json")["job_id"] == job["id"]
    assert unirig_worker.run_job(directory) == 0
    assert unirig.get_unirig_job(settings, job["id"])["status"] == "cancelled"
    assert unirig.list_unirig_jobs(settings)["total"] == 1


def test_relative_stage_argv_and_native_skin_reextract_contract(environment):
    settings, source, _, _ = environment
    plan = unirig.plan_unirig(settings, request(source, seed=20))
    for stage in plan["stages"]:
        command = unirig_worker.build_stage_argv(stage, plan, 0)
        assert not any(value in {"bash", "eval", "sh", "pip"} for value in command)
        for value in command:
            if value.startswith(("--input=", "--source=", "--target=", "--output=", "--npz_dir=")):
                assert not Path(value.split("=", 1)[1]).is_absolute()
                assert ".." not in Path(value.split("=", 1)[1]).parts
    extract = unirig_worker.build_stage_argv("extract_rig", plan, 0)
    assert "--input=outputs/item_00/skeleton.fbx" in extract
    skin = unirig_worker.build_stage_argv("predict_skin", plan, 0)
    assert "--data_name=raw_data.npz" in skin and "--input=outputs/item_00/skeleton.fbx" in skin
    merge = unirig_worker.build_stage_argv("merge_rig", plan, 0)
    assert "--source=outputs/item_00/skin.fbx" in merge
    assert "--target=inputs/item_00/mesh.glb" in merge
    with pytest.raises(ValueError, match="Unknown fixed"):
        unirig_worker.build_stage_argv("arbitrary_script", plan, 0)


def fake_stages(monkeypatch, *, omit=None):
    stages = []

    def run(directory, stage, index, request, **kwargs):
        stages.append(stage)
        if stage == omit:
            return  # Simulates upstream swallowing an export exception and exiting zero.
        paths = unirig_worker.item_paths(request, index)
        if stage.startswith("extract_"):
            src = paths["mesh" if stage == "extract_mesh" else "rig"]
            output = directory / "intermediate" / Path(src).with_suffix("") / "raw_data.npz"
            output.parent.mkdir(parents=True, exist_ok=True)
            np.savez(output, vertices=np.zeros((3, 3)), faces=np.array([[0, 1, 2]]))
        elif stage.startswith("predict_"):
            output = directory / paths["skeleton" if stage == "predict_skeleton" else "skin"]
            output.write_bytes(b"Kaydara FBX Binary  \x00\x1a\x00" + b"synthetic-test-export")
            weights = directory / "weights"
            weights.mkdir(exist_ok=True)
            atomic_json(weights / f"item_{index:02d}-{stage}.json", {"weights": [{
                "reference": "synthetic-fixture-checkpoint", "sha256": "a" * 64, "bytes": 1,
            }]})
        elif stage == "merge_rig":
            output = directory / paths["merged"]
            output.write_bytes(glb_bytes())

    monkeypatch.setattr(unirig_worker, "_run_stage", run)
    return stages


@pytest.mark.parametrize("operation,outputs", [("preprocess", 1), ("skeleton", 1), ("skin", 1), ("pipeline", 3)])
def test_full_fake_stage_pipeline_and_verified_immutable_outputs(environment, monkeypatch, operation, outputs):
    settings, source, _, _ = environment
    stages = fake_stages(monkeypatch)
    job = unirig.start_unirig(settings, request(source, operation=operation))
    directory = unirig._directory(settings, job["id"])
    assert unirig_worker.run_job(directory) == 0
    result = unirig.get_unirig_job(settings, job["id"])
    assert result["status"] == "completed" and len(result["output"]["artifacts"]) == outputs
    manifest = result["output"]
    assert manifest["sources"][0]["sha256"] == source["sha256"]
    assert "unverified" in manifest["validation"]
    for artifact in manifest["artifacts"]:
        path = unirig.get_unirig_artifact(settings, job["id"], artifact["path"])
        assert unirig._hash(path) == artifact["sha256"]
    snapshot = (directory / "job.json").read_bytes()
    assert unirig_worker.run_job(directory) == 1
    assert (directory / "job.json").read_bytes() == snapshot
    assert stages[0] == "configure"
    with pytest.raises(ValueError, match="not listed"):
        unirig.get_unirig_artifact(settings, job["id"], "request.json")


def test_zero_exit_without_export_fails_instead_of_claiming_completion(environment, monkeypatch):
    settings, source, _, _ = environment
    fake_stages(monkeypatch, omit="predict_skeleton")
    job = unirig.start_unirig(settings, request(source, operation="skeleton"))
    directory = unirig._directory(settings, job["id"])
    assert unirig_worker.run_job(directory) == 1
    result = unirig.get_unirig_job(settings, job["id"])
    assert result["status"] == "failed" and "nonempty bounded artifact" in result["error"]
    assert not (directory / "manifest.json").exists()


@pytest.mark.parametrize("change", ["source", "artifact", "manifest", "request"])
def test_verified_outputs_reject_changed_source_receipt_or_artifact(environment, monkeypatch, change):
    settings, source, _, _ = environment
    fake_stages(monkeypatch)
    job = unirig.start_unirig(settings, request(source, operation="skeleton"))
    directory = unirig._directory(settings, job["id"])
    assert unirig_worker.run_job(directory) == 0
    manifest = unirig._json(directory / "manifest.json")
    if change == "source":
        (directory / manifest["sources"][0]["path"]).write_bytes(b"changed")
    elif change == "artifact":
        (directory / manifest["artifacts"][0]["path"]).write_bytes(b"changed")
    else:
        path = directory / (change + ".json")
        payload = unirig._json(path)
        payload["unexpected"] = "changed"
        atomic_json(path, payload)
    result = unirig.get_unirig_job(settings, job["id"])
    assert result["output"] is None and result["verification_error"]
    with pytest.raises(ValueError):
        unirig.get_unirig_artifact(settings, job["id"], manifest["artifacts"][0]["path"])


def test_registered_asset_changed_before_stage_is_rejected(environment, monkeypatch):
    settings, source, _, _ = environment
    stages = fake_stages(monkeypatch)
    job = unirig.start_unirig(settings, request(source))
    Path(source["absolute_path"]).write_bytes(b"changed source")
    assert unirig_worker.run_job(unirig._directory(settings, job["id"])) == 1
    assert "source changed" in unirig.get_unirig_job(settings, job["id"])["error"]
    assert stages == []


def test_prior_completed_rig_source_is_verified_and_available_for_merge(environment, monkeypatch):
    settings, source, _, _ = environment
    fake_stages(monkeypatch)
    previous = unirig.start_unirig(settings, request(source, operation="pipeline"))
    assert unirig_worker.run_job(unirig._directory(settings, previous["id"])) == 0
    controls = unirig.UniRigRequest(operation="merge", assets=[{
        "asset_id": source["asset_id"], "source_job_id": previous["id"], "rig_output": "skin",
    }])
    plan = unirig.plan_unirig(settings, controls)
    assert plan["assets"][0]["rig"]["source_artifact"] == "outputs/item_00/skin.fbx"
    merged = unirig.start_unirig(settings, controls)
    assert unirig_worker.run_job(unirig._directory(settings, merged["id"])) == 0
    result = unirig.get_unirig_job(settings, merged["id"])
    assert result["status"] == "completed" and len(result["output"]["sources"]) == 2


def test_artifact_container_validation_and_skeleton_only_merge(tmp_path):
    path = tmp_path / "result.glb"
    path.write_bytes(glb_bytes(skin=False))
    unirig_worker.validate_artifact(path)
    with pytest.raises(ValueError, match="skin metadata"):
        unirig_worker.validate_artifact(path, require_skin=True)
    path.write_bytes(b"glTF" + b"truncated")
    with pytest.raises(ValueError, match="header"):
        unirig_worker.validate_artifact(path)
    fbx = tmp_path / "result.fbx"
    fbx.write_bytes(b"not an FBX")
    with pytest.raises(ValueError, match="header"):
        unirig_worker.validate_artifact(fbx)


def test_interrupted_supervisor_and_surviving_child_prevent_duplicate_start(environment, monkeypatch):
    settings, source, _, _ = environment
    job = unirig.start_unirig(settings, request(source))
    directory = unirig._directory(settings, job["id"])
    record = unirig._record(directory)
    record["created_at"] = time.time() - 60
    atomic_json(directory / "job.json", record)
    monkeypatch.setattr(unirig, "_alive", lambda *args: False)
    assert unirig.get_unirig_job(settings, job["id"])["status"] == "interrupted"
    monkeypatch.setattr(unirig, "_live_child", lambda *args: True)
    with pytest.raises(ValueError, match="already active"):
        unirig.start_unirig(settings, request(source))


def test_logs_are_bounded_and_paths_and_limits_are_not_arbitrary(environment):
    settings, source, _, _ = environment
    job = unirig.start_unirig(settings, request(source))
    directory = unirig._directory(settings, job["id"])
    (directory / "worker.log").write_text("line\n" * 20000)
    assert len(unirig.get_unirig_log(settings, job["id"], 3)["text"].splitlines()) == 3
    for value in (0, True, 101):
        with pytest.raises(ValueError):
            unirig.list_unirig_jobs(settings, value)
    with pytest.raises(ValueError):
        unirig.get_unirig_job(settings, "../escape")
    with pytest.raises(ValueError):
        unirig._contained(directory, "../../escape")


def test_external_entrypoint_uses_exact_argument_count(monkeypatch, tmp_path):
    calls = []
    monkeypatch.setattr(unirig_worker, "_external_stage", lambda *args: calls.append(args))
    monkeypatch.setattr(sys, "argv", ["worker.py", "--external-step", "configure", "0", str(tmp_path), "a" * 64])
    assert unirig_worker.main() == 0
    assert calls == [("configure", tmp_path.resolve(), 0, "a" * 64)]


def test_runtime_discovery_is_stage_specific_and_does_not_import_models(tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "run.py").write_text("reviewed placeholder")
    monkeypatch.setenv("KIMODO_UNIRIG_REPO", str(repo))
    monkeypatch.setenv("KIMODO_UNIRIG_PYTHON", sys.executable)
    calls = []

    def command(argv, **kwargs):
        import ast
        calls.append(argv)
        if "rev-parse" in argv:
            return unirig.UNIRIG_REVISION
        if "status" in argv:
            return ""
        assert "find_spec" in argv[-1] and "import torch" not in argv[-1]
        names = ast.literal_eval(argv[-1].split("names=", 1)[1].split("; print", 1)[0])
        return json.dumps({"python_version": [3, 11], "modules": {name: True for name in names}})

    monkeypatch.setattr(unirig, "_read_command", command)
    result = unirig._runtime(probe=True, operation="merge")
    assert result["configuration_ready"] and "torch" not in result["required_modules"]
    assert "weights_verified" in result and not result["weights_verified"]
    assert f"safe.directory={repo}" in calls[0]
    calls.clear()
    result = unirig._runtime(probe=True, operation="pipeline")
    assert "torch" in result["required_modules"] and "flash_attn" in result["required_modules"]


def test_per_job_configuration_edits_only_reviewed_parameters(tmp_path, monkeypatch):
    monkeypatch.setitem(sys.modules, "yaml", SimpleNamespace(safe_load=json.loads))
    fixtures = {
        "configs/data/quick_inference.yaml": {"predict_dataset_config": {"num_workers": 1, "batch_size": 1}},
        "configs/system/ar_inference_articulationxl.yaml": {"generate_kwargs": {"num_beams": 15, "max_new_tokens": 2048}},
        "configs/transform/inference_skin_transform.yaml": {
            "predict_transform_config": {"vertex_group_config": {"kwargs": {"voxel_skin": {"backend": "pyrender", "grid": 196}}}}},
    }
    for name in ("quick_inference_skeleton_articulationxl_ar_256", "quick_inference_unirig_skin"):
        fixtures[f"configs/task/{name}.yaml"] = {
            "mode": "predict", "trainer": {"precision": "bf16-mixed", "devices": 1},
            "writer": {"repeat": 1}, "components": {"model": "reviewed-model"},
        }
    for relative, value in fixtures.items():
        path = tmp_path / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(value))
    controls = unirig.UniRigRequest(assets=[{"asset_id": "a" * 64}], num_beams=6,
                                  do_sample=False, max_new_tokens=1024, skin_backend="open3d")
    unirig_worker._prepare_configs(tmp_path, {"request": controls.model_dump(mode="json")})
    system = json.loads((tmp_path / "configs/system/ar_inference_articulationxl.yaml").read_text())
    assert system["generate_kwargs"]["num_beams"] == 6
    assert system["generate_kwargs"]["max_new_tokens"] == 1024
    assert system["generate_kwargs"]["do_sample"] is False
    data = json.loads((tmp_path / "configs/data/quick_inference.yaml").read_text())
    assert data["predict_dataset_config"]["num_workers"] == 0
    task = json.loads((tmp_path / "configs/task/managed_skin.yaml").read_text())
    assert task["components"]["model"] == "reviewed-model"
    assert task["trainer"]["devices"] == 1 and task["trainer"]["strategy"] == "auto"


@pytest.mark.parametrize("change", ["runtime", "request"])
def test_changes_between_stages_fail_before_more_work_or_publication(environment, monkeypatch, change):
    settings, source, _, _ = environment
    stages = fake_stages(monkeypatch)
    original_stage = unirig_worker._run_stage
    original_runtime = unirig._runtime
    changed = False

    def runtime(**kwargs):
        result = original_runtime(**kwargs)
        if changed and change == "runtime":
            result["configuration_ready"] = False
        return result

    def stage(directory, name, index, request, **kwargs):
        nonlocal changed
        original_stage(directory, name, index, request, **kwargs)
        if name == "extract_mesh":
            changed = True
            if change == "request":
                value = unirig._json(directory / "request.json")
                value["request"]["seed"] += 1
                atomic_json(directory / "request.json", value)

    monkeypatch.setattr(unirig, "_runtime", runtime)
    monkeypatch.setattr(unirig_worker, "_run_stage", stage)
    job = unirig.start_unirig(settings, request(source))
    directory = unirig._directory(settings, job["id"])
    assert unirig_worker.run_job(directory) == 1
    failed = unirig.get_unirig_job(settings, job["id"])
    assert failed["status"] == "failed" and "changed during execution" in failed["error"]
    assert stages == ["configure", "extract_mesh"]
    assert not (directory / "manifest.json").exists()


def test_runtime_is_rechecked_after_generation_lease_wait(environment, monkeypatch):
    from filelock import FileLock

    settings, source, _, _ = environment
    stages = fake_stages(monkeypatch)
    job = unirig.start_unirig(settings, request(source))
    original_runtime = unirig._runtime
    acquired = False

    class Lease:
        def __init__(self, path, *args, **kwargs):
            self.path = Path(path)
            self.actual = FileLock(path, *args, **kwargs)

        def __enter__(self):
            return self.actual.__enter__()

        def __exit__(self, *args):
            return self.actual.__exit__(*args)

        def acquire(self, *args, **kwargs):
            nonlocal acquired
            acquired = True
            return self.actual.acquire(*args, **kwargs)

        def release(self):
            return self.actual.release()

    def runtime(**kwargs):
        result = original_runtime(**kwargs)
        if acquired:
            result["configuration_ready"] = False
        return result

    monkeypatch.setattr(unirig, "_runtime", runtime)
    monkeypatch.setattr("filelock.FileLock", Lease)
    assert unirig_worker.run_job(unirig._directory(settings, job["id"])) == 1
    assert unirig.get_unirig_job(settings, job["id"])["status"] == "failed"
    assert stages == []


def test_external_stage_refuses_modified_request_before_loading_runtime(tmp_path):
    directory = tmp_path / ("b" * 32)
    directory.mkdir()
    original = {"id": directory.name, "runtime": {"repo": "must-not-be-imported"}}
    atomic_json(directory / "request.json", original | {"changed": True})
    with pytest.raises(ValueError, match="checksum changed before the external stage"):
        unirig_worker._external_stage("configure", directory, 0, unirig._canonical(original))


@pytest.mark.parametrize("directive", [
    b"mtllib materials.mtl", b" \tMTLLIB\tC:/private/material.mtl", b"usemtl material",
    b"\tUseMtl\tmaterial", b"\xef\xbb\xbfmtllib material.mtl",
])
def test_obj_material_references_rejected_for_mesh_and_explicit_rig(tmp_path, monkeypatch, directive):
    from kimodo_mcp.studio_assets import import_studio_asset

    settings = Settings(data_dir=tmp_path / "data", python=sys.executable)
    geometry = tmp_path / "geometry.obj"
    geometry.write_bytes(b"v 0 0 0\nv 1 0 0\nv 0 1 0\nf 1 2 3\n")
    material = tmp_path / "materials.obj"
    material.write_bytes(directive + b"\n" + geometry.read_bytes())
    clean = import_studio_asset(settings, str(geometry), "Geometry")
    other = import_studio_asset(settings, str(material), "Material references")
    monkeypatch.setattr(unirig, "_runtime", lambda **kwargs: pytest.fail("Plan must reject before runtime probe"))
    for controls in (
        {"operation": "pipeline", "assets": [{"asset_id": other["asset_id"]}]},
        {"operation": "skin", "assets": [{"asset_id": clean["asset_id"], "rig_asset_id": other["asset_id"]}]},
    ):
        with pytest.raises(ValueError, match="geometry-only OBJ"):
            unirig.plan_unirig(settings, controls)


def test_obj_material_references_rechecked_before_staging(environment, monkeypatch):
    settings, source, _, _ = environment
    bad = Path(source["absolute_path"]).with_suffix(".obj")
    bad.write_bytes(b"mtllib C:/unrecorded/material.mtl\nv 0 0 0\n")
    source.update(absolute_path=str(bad), format="obj", sha256=unirig._hash(bad), bytes=bad.stat().st_size)
    # The fake planner models an older already-prepared request. The worker
    # still refuses its material references before starting any native process.
    stages = fake_stages(monkeypatch)
    job = unirig.start_unirig(settings, request(source))
    assert unirig_worker.run_job(unirig._directory(settings, job["id"])) == 1
    assert "geometry-only OBJ" in unirig.get_unirig_job(settings, job["id"])["error"]
    assert stages == []


def test_obj_validation_accepts_geometry_and_bounds_line_size(tmp_path):
    path = tmp_path / "mesh.obj"
    path.write_bytes(b"# mtllib in a comment is not a directive\nv 0 0 0\nv 1 0 0\nv 0 1 0\nf 1 2 3\n")
    unirig.validate_obj_source(path)
    path.write_bytes(b"v " + b"0 " * 40000)
    with pytest.raises(ValueError, match="64 KiB"):
        unirig.validate_obj_source(path)
