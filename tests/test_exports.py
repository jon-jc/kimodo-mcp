import hashlib
import json
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pytest
from filelock import FileLock

from kimodo_mcp import export, export_worker
from kimodo_mcp.config import Settings
from kimodo_mcp.jobs import atomic_json
from kimodo_mcp.pose import _soma_layout


def source(tmp_path, format="bvh", **changes):
    directory = tmp_path / ("a" * 32)
    directory.mkdir(exist_ok=True)
    path = directory / "motion_00.npz"
    joints, skeleton, model = {
        "bvh": (77, "somaskel77", "kimodo-soma-rp-v1.2"),
        "csv": (34, "g1skel34", "kimodo-g1-rp"),
        "amass": (22, "smplx22", "kimodo-smplx-rp"),
    }[format]
    names, parents = (
        _soma_layout(77) if joints == 77 else ([f"Bone{i}" for i in range(joints)], [-1] + [0] * (joints - 1))
    )
    values = dict(
        posed_joints=np.zeros((3, joints, 3)),
        local_rot_mats=np.broadcast_to(np.eye(3), (3, joints, 3, 3)),
        joint_names=np.array(names),
        joint_parents=np.array(parents),
        fps=np.array(30),
        skeleton=np.array(skeleton),
        model=np.array(model),
    )
    values.update(changes)
    np.savez(path, **{key: value for key, value in values.items() if value is not None})
    return path


def arguments(path, **changes):
    return (
        dict(
            source_path=path,
            source_sha256=export.digest(path),
            job_id=path.parent.name,
            format="bvh",
        )
        | changes
    )


def request(path, **changes):
    data = arguments(path, **changes)
    data["source_path"] = str(path)
    result = export.ExportRequest(output_directory="", **data)
    return result.model_copy(
        update={"output_directory": str(path.parent / "exports" / export.export_id(result))}
    )


def fake_artifact(path, format, frames=3, fps=30):
    if format == "bvh":
        path.write_text(
            "HIERARCHY\nROOT Root\n{\nOFFSET 0 0 0\nCHANNELS 6 Xposition Yposition Zposition Zrotation Yrotation Xrotation\n}\nMOTION\n"
            f"Frames: {frames}\nFrame Time: {1 / fps:.10f}\n" + "0 0 0 0 0 0\n" * frames
        )
    elif format == "csv":
        values = np.zeros((frames, 36))
        values[:, 3] = 1
        np.savetxt(path, values, delimiter=",")
    else:
        np.savez(
            path,
            trans=np.zeros((frames, 3)),
            root_orient=np.zeros((frames, 3)),
            pose_body=np.zeros((frames, 63)),
            mocap_frame_rate=np.array(fps),
        )


def fake_process(command, **kwargs):
    assert command[1:4] == ["-m", "kimodo_mcp.export_worker", "--request"]
    assert kwargs["timeout"] == 120 and kwargs["check"] is False
    assert kwargs["stdin"] == subprocess.DEVNULL
    assert kwargs["env"]["HF_HUB_OFFLINE"] == "1"
    assert kwargs["env"]["CUDA_VISIBLE_DEVICES"] == ""
    req = export.ExportRequest.model_validate_json(Path(command[4]).read_text())
    motion = export.load_source(req)
    directory = Path(req.output_directory)
    artifact = directory / req.basename
    fake_artifact(artifact, req.format)
    atomic_json(directory / "export-report.json", export.make_report(req, motion, artifact))
    return SimpleNamespace(returncode=0)


@pytest.mark.parametrize("format", ["bvh", "csv", "amass"])
def test_export_and_verified_reuse_listing_and_no_source_changes(tmp_path, monkeypatch, format):
    path = source(tmp_path, format)
    before = path.read_bytes()
    monkeypatch.setattr(export.subprocess, "run", fake_process)
    first = export.export_motion(Settings(tmp_path, "runtime-python"), **arguments(path, format=format))
    monkeypatch.setattr(export.subprocess, "run", lambda *a, **k: pytest.fail("Reuse spawned a worker"))
    second = export.export_motion(Settings(tmp_path, "runtime-python"), **arguments(path, format=format))
    assert first == second
    assert export.list_exports(path.parent) == [first]
    assert first["artifact"]["bytes"] > 0 and first["frames"] == 3
    assert first["source_sha256"] == hashlib.sha256(before).hexdigest()
    assert path.read_bytes() == before
    assert not (path.parent / "manifest.json").exists()


@pytest.mark.parametrize(
    "changes",
    [
        {"sample_index": True},
        {"sample_index": 4},
        {"format": "npz"},
        {"source_sha256": "invalid"},
        {"bvh_standard_tpose": 1},
        {"amass_z_up": False},
        {"mujoco_rest_zero": True},
    ],
)
def test_strict_request_and_option_validation(tmp_path, changes):
    with pytest.raises(ValueError):
        request(source(tmp_path), **changes)


@pytest.mark.parametrize(
    "changes",
    [
        {"fps": None},
        {"joint_names": None},
        {"joint_parents": None},
        {"local_rot_mats": None},
        {"model": np.array("unknown")},
        {"skeleton": np.array("g1skel34")},
        {"local_rot_mats": np.zeros((3, 77, 3, 3))},
        {"local_rot_mats": np.full((3, 77, 3, 3), np.nan)},
        {"posed_joints": np.zeros((2, 3, 77, 3)), "local_rot_mats": None},
    ],
)
def test_invalid_source_metadata_and_rotations_rejected(tmp_path, changes):
    with pytest.raises(ValueError):
        export.load_source(request(source(tmp_path, **changes)))


def test_digest_and_path_mismatch_rejected(tmp_path):
    path = source(tmp_path)
    req = request(path)
    with pytest.raises(ValueError, match="assigned"):
        export.load_source(req.model_copy(update={"output_directory": str(tmp_path / "outside")}))
    path.write_bytes(path.read_bytes() + b"changed")
    with pytest.raises(ValueError, match="digest"):
        export.load_source(req)


def test_different_options_have_distinct_ids_and_partial_attempt_refused(tmp_path, monkeypatch):
    path = source(tmp_path)
    first, second = request(path), request(path, bvh_standard_tpose=True)
    assert export.export_id(first) != export.export_id(second)
    Path(first.output_directory).mkdir(parents=True)
    monkeypatch.setattr(
        export.subprocess,
        "run",
        lambda *a, **k: pytest.fail("Retried incomplete attempt"),
    )
    with pytest.raises(ValueError, match="incomplete"):
        export.export_motion(Settings(tmp_path, "runtime"), **arguments(path))


def test_lock_refuses_concurrent_export(tmp_path):
    path = source(tmp_path)
    req = request(path)
    with FileLock(str(path.parent / f".export-{export.export_id(req)}.lock")):
        with pytest.raises(ValueError, match="already running"):
            export.export_motion(Settings(tmp_path, "runtime"), **arguments(path))


@pytest.mark.parametrize("timeout", [False, True])
def test_failed_worker_and_timeout_leave_inspectable_attempt_no_receipt(tmp_path, monkeypatch, timeout):
    path = source(tmp_path)

    def fail(*args, **kwargs):
        if timeout:
            raise subprocess.TimeoutExpired(args[0], 120)
        return SimpleNamespace(returncode=1)

    monkeypatch.setattr(export.subprocess, "run", fail)
    with pytest.raises((ValueError, subprocess.TimeoutExpired)):
        export.export_motion(Settings(tmp_path, "runtime"), **arguments(path))
    directory = Path(request(path).output_directory)
    assert (directory / "failure.json").is_file()
    assert (directory / "attempt.json").is_file()
    assert export.list_exports(path.parent) == []
    with pytest.raises(ValueError, match="incomplete"):
        export.export_motion(Settings(tmp_path, "runtime"), **arguments(path))


@pytest.mark.parametrize("target", ["artifact", "report", "source", "request"])
def test_tampered_export_not_listed_or_reused(tmp_path, monkeypatch, target):
    path = source(tmp_path)
    expected = arguments(path)
    monkeypatch.setattr(export.subprocess, "run", fake_process)
    report = export.export_motion(Settings(tmp_path, "runtime"), **arguments(path))
    directory = Path(report["output_directory"])
    if target == "artifact":
        artifact = Path(report["artifact"]["absolute_path"])
        artifact.write_text(artifact.read_text().replace("0 0 0 0 0 0", "1 0 0 0 0 0"))
    elif target == "source":
        path.write_bytes(path.read_bytes() + b"modified")
    else:
        item = directory / ("request.json" if target == "request" else "export-report.json")
        payload = json.loads(item.read_text())
        payload["source_sha256"] = "0" * 64
        atomic_json(item, payload)
    assert export.list_exports(path.parent) == []
    with pytest.raises(ValueError):
        export.verify_export(directory)
    with pytest.raises(ValueError):
        export.export_motion(Settings(tmp_path, "runtime"), **expected)


def test_source_changed_during_receipt_verification_refused(tmp_path, monkeypatch):
    path = source(tmp_path)
    monkeypatch.setattr(export.subprocess, "run", fake_process)
    report = export.export_motion(Settings(tmp_path, "runtime"), **arguments(path))
    original = export.validate_artifact

    def change(*args):
        original(*args)
        path.write_bytes(path.read_bytes() + b"changed while validating output")

    monkeypatch.setattr(export, "validate_artifact", change)
    with pytest.raises(ValueError, match="Source changed"):
        export.verify_export(Path(report["output_directory"]))


@pytest.mark.parametrize("format", ["bvh", "csv", "amass"])
def test_output_validation_rejects_wrong_timing_truncation_nonfinite(tmp_path, format):
    path = source(tmp_path, format)
    req = request(path, format=format)
    artifact = tmp_path / req.basename
    fake_artifact(artifact, format, frames=2)
    with pytest.raises(ValueError):
        export.validate_artifact(artifact, req, 3, 30)
    fake_artifact(artifact, format)
    export.validate_artifact(artifact, req, 3, 30)
    if format == "amass":
        np.savez(
            artifact,
            trans=np.full((3, 3), np.nan),
            root_orient=np.zeros((3, 3)),
            pose_body=np.zeros((3, 63)),
            mocap_frame_rate=np.array(30),
        )
    else:
        artifact.write_text(artifact.read_text().replace("0", "nan", 1))
    with pytest.raises(ValueError):
        export.validate_artifact(artifact, req, 3, 30)


def worker_dependencies(monkeypatch, names, parents, save):
    monkeypatch.setattr(export_worker.os, "environ", dict(export_worker.os.environ))
    skeleton = SimpleNamespace(
        name="somaskel77",
        bone_order_names=names,
        root_idx=0,
        joint_parents=SimpleNamespace(cpu=lambda: np.array(parents)),
    )
    monkeypatch.setitem(sys.modules, "torch", SimpleNamespace(tensor=np.array, float32=np.float32))
    monkeypatch.setitem(
        sys.modules, "kimodo.skeleton.registry", SimpleNamespace(build_skeleton=lambda _: skeleton)
    )
    monkeypatch.setitem(sys.modules, "kimodo.exports.bvh", SimpleNamespace(save_motion_bvh=save))


def test_worker_canonical_check_world_root_and_source_change_guard(tmp_path, monkeypatch):
    positions = np.zeros((3, 77, 3))
    positions[:, 0] = [4, 0.9, -3]
    path = source(tmp_path, posed_joints=positions)
    req = request(path, bvh_standard_tpose=True)
    directory = Path(req.output_directory)
    directory.mkdir(parents=True)
    with FileLock(str(directory / ".conversion.lock")), pytest.raises(ValueError, match="already running"):
        export_worker.convert(req)
    names, parents = _soma_layout(77)

    def save(target, local, root, **options):
        np.testing.assert_allclose(root, positions[:, 0], atol=1e-6)
        assert options["standard_tpose"] is True and options["fps"] == 30
        fake_artifact(Path(target), "bvh")

    worker_dependencies(monkeypatch, list(reversed(names)), parents, save)
    with pytest.raises(ValueError, match="canonical"):
        export_worker.convert(req)
    assert not (directory / req.basename).exists()
    worker_dependencies(monkeypatch, names, parents, save)
    export_worker.convert(req)
    assert (directory / "export-report.json").is_file()
    with pytest.raises(ValueError, match="partial output"):
        export_worker.convert(req)

    changed = request(path)
    changed_directory = Path(changed.output_directory)
    changed_directory.mkdir()

    def mutate(target, *args, **kwargs):
        fake_artifact(Path(target), "bvh")
        path.write_bytes(path.read_bytes() + b"changed during export")

    worker_dependencies(monkeypatch, names, parents, mutate)
    with pytest.raises(ValueError, match="Source changed"):
        export_worker.convert(changed)
    assert not (changed_directory / "export-report.json").exists()
