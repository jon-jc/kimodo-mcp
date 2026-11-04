"""No downloaded models, footage processing, or GPU use in adapter tests."""

import os
import subprocess
import time
from types import SimpleNamespace

import numpy as np
import pytest
from filelock import FileLock
from pydantic import ValidationError

from kimodo_mcp import video_mocap as capture
from kimodo_mcp import video_mocap_worker as worker
from kimodo_mcp.config import Settings
from kimodo_mcp.jobs import atomic_json


@pytest.fixture
def settings(tmp_path):
    return Settings(data_dir=tmp_path / "data", python="unused-runtime", timeout=30)


@pytest.fixture
def source(tmp_path):
    path = tmp_path / "my footage ; $(do not execute).mp4"
    path.write_bytes(b"test-only input, not real video")
    return path


def ready_plan(settings, source):
    request = capture.VideoMocapRequest(video_path=str(source), duration_seconds=0.6, fps=10)
    return {"request": request.model_dump(), "frames": 6, "ready": True,
            "preflight": {"ready": True, "checks": []},
            "source": {"path": str(source), "bytes": source.stat().st_size, "sha256": capture._hash(source),
                       "width": 320, "height": 240, "duration_seconds": 1},
            "runtime": {"python": "external-python", "repo_path": str(source.parent),
                        "ffmpeg": "ffmpeg-test", "ffprobe": "ffprobe-test", "device": "cpu"},
            "assets": [], "runtime_assets": []}


def create_job(settings, source, monkeypatch):
    plan = ready_plan(settings, source)
    monkeypatch.setattr(capture, "plan_video_mocap", lambda *args: plan)
    launches = []

    def launch(argv, **kwargs):
        launches.append((argv, kwargs))
        return SimpleNamespace(pid=os.getpid())

    monkeypatch.setattr(capture.subprocess, "Popen", launch)
    job = capture.start_video_mocap(settings, plan["request"])
    return capture._directory(settings, job["id"]), plan, launches


def detector_file(directory, frame, *, points=25, confidence=0.9, people=1, person_id=0):
    path = directory / "dataset" / "annots" / "video" / f"{frame:06d}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    value = {"annots": [{"personID": person_id, "keypoints": [[10 + i, 20 + i, confidence] for i in range(points)]} for _ in range(people)]}
    atomic_json(path, value)


def synthetic_stages(directory, stage, argv, **kwargs):
    if stage == "extract":
        for i in range(6):
            (directory / "dataset" / "images" / "video" / f"{i:06d}.jpg").write_bytes(b"synthetic image")
    elif stage == "reference":
        (directory / "reference.mp4").write_bytes(b"synthetic reference")
    elif stage == "detect":
        for i in range(6):
            detector_file(directory, i)
    elif stage == "normalize":
        np.savez_compressed(directory / "motion.npz", posed_joints=np.zeros((6, 24, 3)),
                            joint_names=np.array([f"Joint{i}" for i in range(24)]),
                            joint_parents=np.array([-1] + [0] * 23),
                            source_time_seconds=np.arange(6) / 10, keypoints2d=np.ones((6, 25, 3)))
        (directory / "motion.bvh").write_text("synthetic BVH", encoding="utf-8")
        atomic_json(directory / "smpl-parameters.json", {"frames": [{}] * 6})


@pytest.mark.parametrize("changes", [
    {"duration_seconds": 81}, {"fps": 61}, {"duration_seconds": 80, "fps": 60},
    {"duration_seconds": 0.01}, {"start_seconds": float("nan")}, {"max_frames": True},
    {"detector": "shell-command"}, {"video_path": "https://example.com/video.mp4"}, {"unknown": 4},
])
def test_request_rejects_unbounded_or_remote_inputs(source, changes):
    with pytest.raises(ValidationError):
        capture.VideoMocapRequest.model_validate({"video_path": str(source), **changes})


def test_request_alias_and_exact_frame_budget(source):
    request = capture.VideoMocapRequest(local_video_path=str(source), duration_seconds=80, fps=30)
    assert request.frames == 2400
    assert request.model_dump()["video_path"] == str(source)


def test_five_frames_rejected_due_to_upstream_temporal_smoother(source):
    with pytest.raises(ValidationError, match="6 to max_frames"):
        capture.VideoMocapRequest(video_path=str(source), duration_seconds=0.5, fps=10)


def test_missing_runtime_preflight_never_launches(settings, source, monkeypatch):
    monkeypatch.setattr(subprocess, "Popen", lambda *a, **k: pytest.fail("readiness launched a worker"))
    result = capture.plan_video_mocap(settings, {"video_path": str(source)})
    assert not result["ready"]
    assert result["source"]["sha256"] == capture._hash(source)
    assert result["preflight"]["checks"][-1]["name"] == "runtime_configuration"


def test_preflight_requires_pin_assets_dependency_and_duration(settings, source, monkeypatch):
    repo = source.parent / "runtime repo"
    for name in capture.PIPELINE_FILES + capture.MODEL_FILES:
        file = repo / name
        file.parent.mkdir(parents=True, exist_ok=True)
        file.write_bytes(b"preflight fixture only")
    capture._base(settings).mkdir(parents=True)
    atomic_json(capture._base(settings) / "runtime.json", {
        "python": str(repo / "python.exe"), "repo_path": str(repo),
        "ffmpeg": str(repo / "ffmpeg.exe"), "ffprobe": str(repo / "ffprobe.exe"), "device": "cpu"})

    def read(argv, **kwargs):
        if "rev-parse" in argv:
            return capture.EASYMOCAP_REVISION
        if "-encoders" in argv:
            return "libx264"
        return ""

    monkeypatch.setattr(capture, "_run_read", read)
    monkeypatch.setattr(capture, "_runtime_probe", lambda *a: {"missing": [], "assets": [], "cuda": False})
    monkeypatch.setattr(capture, "_probe_video", lambda *a: {"duration_seconds": 2, "width": 320, "height": 240})
    request = {"video_path": str(source), "duration_seconds": 1}
    plan = capture.plan_video_mocap(settings, request)
    assert plan["ready"] and len(plan["assets"]) == 4
    missing = repo / capture.MODEL_FILES[-1]
    missing.unlink()
    result = capture.plan_video_mocap(settings, request)
    assert not result["ready"]
    assert any(c["name"] == "model_assets" and not c["ready"] for c in result["preflight"]["checks"])
    missing.write_bytes(b"fixture")
    result = capture.plan_video_mocap(settings, {**request, "duration_seconds": 3})
    assert any(c["name"] == "selected_time_range" and not c["ready"] for c in result["preflight"]["checks"])


def test_dispatch_fixed_argv_isolated_env_and_no_source_shell(settings, source, monkeypatch):
    directory, plan, launches = create_job(settings, source, monkeypatch)
    assert len(launches) == 1
    argv, kwargs = launches[0]
    assert argv[-2:] == ["kimodo_mcp.video_mocap_worker", str(directory)]
    assert str(source) not in argv
    assert kwargs["stdin"] == subprocess.DEVNULL
    assert "shell" not in kwargs
    assert kwargs["env"]["PYTHONPATH"].endswith("src")
    assert capture.get_video_mocap_job(settings, directory.name)["status"] == "queued"
    with pytest.raises(ValueError, match="already active"):
        capture.start_video_mocap(settings, plan["request"])
    assert len(launches) == 1


def test_dispatch_busy_lock_does_not_start(settings, source, monkeypatch):
    capture._base(settings).mkdir(parents=True)
    with FileLock(str(capture._base(settings) / ".dispatch.lock")):
        with pytest.raises(ValueError, match="Another client"):
            capture.start_video_mocap(settings, {"video_path": str(source)})


def test_not_ready_cannot_start(settings, source, monkeypatch):
    monkeypatch.setattr(subprocess, "Popen", lambda *a, **k: pytest.fail("launched when not ready"))
    with pytest.raises(ValueError, match="not ready"):
        capture.start_video_mocap(settings, {"video_path": str(source)})


def test_checked_source_digest_is_bound_before_job_creation(settings, source, monkeypatch):
    plan = ready_plan(settings, source)
    monkeypatch.setattr(capture, "plan_video_mocap", lambda *a: plan)
    monkeypatch.setattr(subprocess, "Popen", lambda *a, **k: pytest.fail("changed source launched"))
    with pytest.raises(ValueError, match="changed since"):
        capture.start_video_mocap(settings, plan["request"], expected_source_sha256="0" * 64)
    assert not list((capture._base(settings) / "jobs").glob("*"))


def test_detections_missing_ambiguous_and_insufficient_are_explicit(tmp_path):
    detector_file(tmp_path, 0)
    detector_file(tmp_path, 2, people=2)
    detector_file(tmp_path, 3, confidence=0)
    detector_file(tmp_path, 4, points=24)
    records, diagnostics = worker.validate_detections(tmp_path, 5)
    assert [r["status"] for r in records] == ["detected", "missing", "ambiguous", "insufficient", "invalid"]
    assert records[1]["keypoints"] is None
    assert diagnostics["detected_frames"] == 1
    assert diagnostics["problem_frame_count"] == 4


def test_all_detector_confidence_values_remain_available(tmp_path):
    detector_file(tmp_path, 0, confidence=0.75)
    values, diagnostics = worker.validate_detections(tmp_path, 1)
    assert values[0]["keypoints"][24][2] == 0.75
    assert diagnostics["problem_frames"] == []


def test_degenerate_person_box_is_not_a_usable_detection(tmp_path):
    detector_file(tmp_path, 0)
    path = tmp_path / "dataset/annots/video/000000.json"
    value = capture._json(path)
    value["annots"][0]["keypoints"] = [[10, 10, 0.9]] * 25
    atomic_json(path, value)
    _, diagnostics = worker.validate_detections(tmp_path, 1)
    assert diagnostics["problem_frames"] == [{"frame": 0, "status": "degenerate"}]


def test_complete_mocked_pipeline_and_verified_preview(settings, source, monkeypatch):
    directory, _, _ = create_job(settings, source, monkeypatch)
    stages = []

    def run(*args, **kwargs):
        stages.append(args[1])
        synthetic_stages(*args, **kwargs)

    monkeypatch.setattr(worker, "_run_stage", run)
    assert worker.run_job(directory) == 0
    assert stages == ["extract", "reference", "detect", "camera", "fit", "normalize"]
    result = capture.get_video_mocap_job(settings, directory.name)
    assert result["status"] == "completed"
    assert result["output"]["source_sha256"] == capture._hash(source)
    assert {a["name"] for a in result["output"]["artifacts"]} == capture.ARTIFACT_NAMES
    preview = capture.get_video_mocap_preview_data(settings, directory.name)
    assert preview["frames"] == 6 and preview["skeleton"] == "smpl24"
    assert not preview["editable"]["supported"]
    assert preview["source_time_seconds"] == [0, 0.1, 0.2, 0.3, 0.4, 0.5]
    with pytest.raises(ValueError, match="only once"):
        worker.run_job(directory)
    assert capture.get_video_mocap_job(settings, directory.name)["status"] == "completed"


def test_missing_frame_prevents_fitting_and_preserves_diagnostic(settings, source, monkeypatch):
    directory, _, _ = create_job(settings, source, monkeypatch)
    stages = []

    def run(*args, **kwargs):
        stages.append(args[1])
        synthetic_stages(*args, **kwargs)
        if args[1] == "detect":
            (directory / "dataset/annots/video/000001.json").unlink()

    monkeypatch.setattr(worker, "_run_stage", run)
    assert worker.run_job(directory) == 1
    result = capture.get_video_mocap_job(settings, directory.name)
    assert result["status"] == "failed" and "fit" not in stages
    assert result["diagnostics"]["problem_frames"] == [{"frame": 1, "status": "missing"}]
    assert not (directory / "manifest.json").exists()


def test_cancel_before_work_does_not_launch_any_stage(settings, source, monkeypatch):
    directory, _, _ = create_job(settings, source, monkeypatch)
    capture.cancel_video_mocap_job(settings, directory.name)
    monkeypatch.setattr(worker, "_run_stage", lambda *a, **k: pytest.fail("cancelled worker ran stage"))
    assert worker.run_job(directory) == 0
    assert capture.get_video_mocap_job(settings, directory.name)["status"] == "cancelled"


def test_duplicate_worker_refuses_global_execution_lock_without_rewriting_job(settings, source, monkeypatch):
    directory, _, _ = create_job(settings, source, monkeypatch)
    before = capture._record(directory)
    monkeypatch.setattr(worker, "_run_stage", lambda *a, **k: pytest.fail("concurrent stage launched"))
    with FileLock(str(directory.parents[1] / ".execution.lock")):
        assert worker.run_job(directory) == 1
    assert capture._record(directory) == before


def test_cancel_running_stage_terminates_only_owned_process(settings, source, monkeypatch):
    directory, _, _ = create_job(settings, source, monkeypatch)
    events = []

    class Process:
        returncode = None
        pid = os.getpid()

        def poll(self):
            return self.returncode

        def terminate(self):
            events.append("terminate")
            self.returncode = -1

        def wait(self, timeout):
            events.append("wait")
            return self.returncode

    monkeypatch.setattr(subprocess, "Popen", lambda *a, **k: Process())
    monkeypatch.setattr(worker.time, "sleep", lambda _: atomic_json(directory / "cancel.json", {}))
    with pytest.raises(worker.CaptureCancelled):
        worker._run_stage(directory, "test", ["test"], cwd=directory, env={}, deadline=time.monotonic() + 30)
    assert events == ["terminate", "wait"]


def test_timeout_terminates_owned_stage_and_does_not_retry(settings, source, monkeypatch):
    directory, _, _ = create_job(settings, source, monkeypatch)
    events = []

    class Process:
        pid = os.getpid()
        returncode = None

        def poll(self):
            return self.returncode

        def terminate(self):
            events.append("terminate")
            self.returncode = -1

        def wait(self, timeout):
            return self.returncode

    monkeypatch.setattr(subprocess, "Popen", lambda *a, **k: Process())
    with pytest.raises(TimeoutError):
        worker._run_stage(directory, "test", ["test"], cwd=directory, env={}, deadline=0)
    assert events == ["terminate"]


def test_cancel_wins_over_late_failed_stage(settings, source, monkeypatch):
    directory, _, _ = create_job(settings, source, monkeypatch)
    capture.cancel_video_mocap_job(settings, directory.name)
    worker._set_state(directory, status="failed", error="stage exited")
    assert capture.get_video_mocap_job(settings, directory.name)["status"] == "cancelled"


def test_source_change_during_work_rejects_result(settings, source, monkeypatch):
    directory, _, _ = create_job(settings, source, monkeypatch)

    def run(*args, **kwargs):
        synthetic_stages(*args, **kwargs)
        if args[1] == "normalize":
            source.write_bytes(b"changed source")

    monkeypatch.setattr(worker, "_run_stage", run)
    assert worker.run_job(directory) == 1
    assert "source video changed" in capture.get_video_mocap_job(settings, directory.name)["error"]


@pytest.mark.parametrize("tamper", ["artifact", "request", "frame_count", "artifact_path", "source"])
def test_completed_provenance_tampering_is_not_advertised(settings, source, monkeypatch, tamper):
    directory, _, _ = create_job(settings, source, monkeypatch)
    monkeypatch.setattr(worker, "_run_stage", synthetic_stages)
    assert worker.run_job(directory) == 0
    if tamper == "artifact":
        (directory / "reference.mp4").write_bytes(b"different video")
    elif tamper == "request":
        record = capture._json(directory / "request.json")
        record["request"]["fps"] = 20
        atomic_json(directory / "request.json", record)
    else:
        record = capture._json(directory / "manifest.json")
        if tamper == "frame_count":
            record["frames"] = 300
        elif tamper == "artifact_path":
            record["artifacts"][0]["path"] = "../foreign.mp4"
        else:
            record["source_sha256"] = "0" * 64
        atomic_json(directory / "manifest.json", record)
    job = capture.get_video_mocap_job(settings, directory.name)
    assert job["output"] is None and job["verification_error"]
    with pytest.raises(ValueError):
        capture.get_video_mocap_artifact(settings, directory.name, "reference.mp4")


def test_unknown_artifact_and_directory_traversal(settings):
    with pytest.raises(ValueError, match="Unknown"):
        capture.get_video_mocap_artifact(settings, "a" * 32, "../runtime.json")
    with pytest.raises(ValueError, match="ID"):
        capture.get_video_mocap_job(settings, "../foreign")


def test_stale_process_marks_interrupted_not_automatic_retry(settings, source, monkeypatch):
    directory, _, launches = create_job(settings, source, monkeypatch)
    record = capture._record(directory)
    record["created_at"] -= 100
    atomic_json(directory / "job.json", record)
    monkeypatch.setattr(capture, "_alive", lambda *args: False)
    assert capture.get_video_mocap_job(settings, directory.name)["status"] == "interrupted"
    assert len(launches) == 1


def test_readonly_process_identity_never_uses_kill(monkeypatch):
    monkeypatch.setattr(os, "kill", lambda *a: pytest.fail("liveness must not signal a process"))
    identity = capture._process_identity(os.getpid())
    assert identity and capture._alive(os.getpid(), identity)
    assert not capture._alive(os.getpid(), identity + "wrong")


def test_orphan_stage_blocks_new_job_even_when_supervisor_interrupted(settings, source, monkeypatch):
    directory, plan, launches = create_job(settings, source, monkeypatch)
    record = capture._record(directory)
    record["status"] = "interrupted"
    atomic_json(directory / "job.json", record)
    atomic_json(directory / "stage-process.json", {"pid": os.getpid(), "identity": capture._process_identity(os.getpid())})
    with pytest.raises(ValueError, match="already active"):
        capture.start_video_mocap(settings, plan["request"])
    assert len(launches) == 1


def test_pid_reuse_does_not_block_completed_stage(settings, source, monkeypatch):
    directory, _, _ = create_job(settings, source, monkeypatch)
    atomic_json(directory / "stage-process.json", {"pid": os.getpid(), "identity": "not-current-process-start"})
    assert not capture._live_child(directory)


def test_log_bounded_and_redacted(settings, source, monkeypatch):
    directory, _, _ = create_job(settings, source, monkeypatch)
    (directory / "worker.log").write_text("old\nsecret hf_fakeexampletoken\nlast", encoding="utf-8")
    result = capture.get_video_mocap_log(settings, directory.name, lines=2)
    assert result["text"] == "secret [redacted]\nlast"


def test_external_stage_rejects_arbitrary_entrypoint(tmp_path, monkeypatch):
    atomic_json(tmp_path / "request.json", {"runtime": {"repo_path": str(tmp_path)},
                "request": {"duration_seconds": 1, "fps": 30}})
    monkeypatch.setattr(worker, "_block_network", lambda: None)
    monkeypatch.setattr(worker.os, "chdir", lambda _: None)
    with pytest.raises(ValueError, match="Unknown fixed"):
        worker._external_stage("user-python", tmp_path)


def test_fixed_fitting_stage_preserves_paths_as_single_arguments(tmp_path, monkeypatch):
    runtime = {"repo_path": str(tmp_path), "device": "cpu"}
    atomic_json(tmp_path / "request.json", {"runtime": runtime,
                "request": {"duration_seconds": 1, "fps": 30}})
    monkeypatch.setattr(worker, "_block_network", lambda: None)
    monkeypatch.setattr(worker.os, "chdir", lambda _: None)
    argv = []
    monkeypatch.setattr(worker.runpy, "run_path", lambda *a, **k: argv.extend(worker.sys.argv))
    monkeypatch.setattr(worker.sys, "argv", [])
    worker._external_stage("fit", tmp_path)
    assert "config/fit/1v1p.yml" in argv
    assert str(tmp_path / "dataset") in argv
    assert argv[argv.index("args.writer.render.enable") + 1] == "False"
    assert argv[argv.index("args.stages.joints.loss.k2d.args.norm") + 1] == "gm"
