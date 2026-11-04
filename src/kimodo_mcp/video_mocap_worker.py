"""Fixed EasyMocap stages, also executable by the separate legacy Python runtime.

Keep external-stage imports free of the MCP package and Python 3.11 dependencies.
The operator-installed body model is trusted local software, never uploaded data.
"""

from __future__ import annotations

import importlib.util
import json
import os
import runpy
import socket
import subprocess
import sys
import time
from pathlib import Path


class CaptureCancelled(Exception):
    pass


def _read(path, maximum=20_000_000):
    if path.stat().st_size > maximum:
        raise ValueError("Capture data exceeds its size limit")
    return json.loads(path.read_text(encoding="utf-8"))


def validate_detections(directory, frames):
    """Retain failed frames explicitly; never let InitSpin copy an earlier pose."""
    import numpy as np

    records, problems = [], []
    for frame in range(frames):
        path = directory / "dataset" / "annots" / "video" / ("%06d.json" % frame)
        record = {"frame": frame, "status": "missing", "person_id": None, "keypoints": None}
        if path.is_file():
            data = _read(path)
            people = data.get("annots", []) if isinstance(data, dict) else []
            if len(people) > 1:
                record["status"] = "ambiguous"
            elif len(people) == 1:
                person = people[0]
                points = np.asarray(person.get("keypoints", []), dtype=float)
                if points.shape == (25, 3) and np.isfinite(points).all() and ((points[:, 2] >= 0) & (points[:, 2] <= 1)).all():
                    record.update(person_id=person.get("personID"), keypoints=points.tolist(),
                                  usable_joints=int((points[:, 2] > 0.05).sum()))
                    if record["person_id"] == 0 and record["usable_joints"] >= 5:
                        extent = np.ptp(points[points[:, 2] > 0.05, :2], axis=0)
                        record["status"] = "detected" if (extent >= 5).all() else "degenerate"
                    else:
                        record["status"] = "insufficient"
                else:
                    record["status"] = "invalid"
        if record["status"] != "detected":
            problems.append({"frame": frame, "status": record["status"]})
        records.append(record)
    diagnostics = {"frames": frames, "detected_frames": frames - len(problems),
                   "problem_frame_count": len(problems), "problem_frames": problems,
                   "minimum_confidence": 0.05, "minimum_usable_joints": 5,
                   "single_detection_per_frame": not problems,
                   "single_person_verified": False,
                   "note": "MediaPipe pose is a single-person detector and cannot rule out an unseen second person."}
    return records, diagnostics


def _block_network():
    def unavailable(*args, **kwargs):
        raise RuntimeError("Network downloads are disabled for video capture; install required assets explicitly")

    socket.create_connection = unavailable
    socket.socket.connect = unavailable
    socket.socket.connect_ex = unavailable


def _external_stage(stage, directory):
    """Only named, reviewed entry points are executable; no user code/config input."""
    _block_network()
    request = _read(directory / "request.json")
    runtime = request["runtime"]
    repo = Path(runtime["repo_path"])
    os.chdir(str(repo))
    sys.path.insert(0, str(repo))
    dataset, output = directory / "dataset", directory / "fitted"
    frames = int(request["request"]["duration_seconds"] * request["request"]["fps"] + 1e-8)
    if stage == "detect":
        script = repo / "apps/preprocess/extract_keypoints.py"
        argv = [str(script), str(dataset), "--mode", request["request"]["detector"], "--subs", "video"]
    elif stage == "camera":
        import cv2

        first = cv2.imread(str(dataset / "images/video/000000.jpg"))
        if first is None:
            raise ValueError("The extracted first frame is unreadable")
        height, width = first.shape[:2]
        script = repo / "apps/calibration/create_blank_camera.py"
        argv = [str(script), str(dataset), "--shape", str(height), str(width)]
    elif stage == "fit":
        script = repo / "apps/fit/fit.py"
        argv = [str(script), "--cfg_model", "config/model/smpl.yml", "--cfg_data", "config/data/multivideo.yml",
                "--cfg_exp", "config/fit/1v1p.yml", "--opt_data", "args.path", str(dataset),
                "args.out", str(output), "args.camera", str(dataset), "args.subs", "['video']",
                "args.ranges", "[0,%d,1]" % frames, "args.writer.render.enable", "False",
                "--opt_exp", "args.stages.joints.loss.k2d.weight", "100.", "args.stages.joints.loss.k2d.args.norm", "gm",
                "--opt_model", "args.device", runtime["device"]]
    elif stage == "normalize":
        _normalize(directory, request)
        return
    else:
        raise ValueError("Unknown fixed video stage")
    sys.argv = argv
    runpy.run_path(str(script), run_name="__main__")


def _normalize(directory, request):
    import numpy as np
    from easymocap.bodymodel.smpl import SMPLModel

    spec = importlib.util.spec_from_file_location("capture_math", Path(__file__).with_name("video_mocap_math.py"))
    math_module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(math_module)
    controls = request["request"]
    frames = int(controls["duration_seconds"] * controls["fps"] + 1e-8)
    records, cameras = [], []
    for frame in range(frames):
        value = _read(directory / "fitted" / "smplfull" / "video" / ("%06d.json" % frame))
        people = value.get("annots", []) if isinstance(value, dict) else value
        if not isinstance(people, list) or len(people) != 1 or people[0].get("id") != 0:
            raise ValueError("Fitting must produce exactly one identified person in every selected frame")
        records.append(people[0])
        cameras.append({key: value[key] for key in ("K", "R", "T", "dist") if isinstance(value, dict) and key in value})
    params = {}
    for name, width in (("Rh", 3), ("Th", 3), ("poses", 72), ("shapes", 10)):
        values = [np.asarray(record[name], dtype=np.float64).reshape(-1) for record in records]
        if any(value.shape != (width,) for value in values):
            raise ValueError("Unexpected fitted parameter shape: " + name)
        params[name] = np.stack(values)
        if not np.isfinite(params[name]).all():
            raise ValueError("Fitted parameters contain invalid numbers")
    if any("scale" in record and not np.allclose(record["scale"], 1) for record in records):
        raise ValueError("Unexpected body scaling cannot be exported faithfully")
    repo = Path(request["runtime"]["repo_path"])
    model = SMPLModel(model_path=str(repo / request["assets"][0]["path"]),
                      regressor_path=str(repo / "data/smplx/J_regressor_body25.npy"),
                      device="cpu", NUM_SHAPES=10, use_pose_blending=True)
    if not np.array_equal(model.parents.detach().cpu().numpy(), math_module.SMPL_PARENTS):
        raise ValueError("The licensed model has an unexpected joint hierarchy")
    body_params = {name: value.astype(np.float32) for name, value in params.items()}
    body_params["poses"] = body_params["poses"][:, 3:]
    rest_params = {"poses": np.zeros((1, 69), dtype=np.float32), "Rh": np.zeros((1, 3), dtype=np.float32),
                   "Th": np.zeros((1, 3), dtype=np.float32), "shapes": body_params["shapes"][:1]}
    rest = model(return_verts=False, return_tensor=False, return_smpl_joints=True, **rest_params)[0]
    # Bound memory independently of selected clip length; validate every original frame.
    original = np.concatenate([model(return_verts=False, return_tensor=False, return_smpl_joints=True,
        **{name: value[start:start + 64] for name, value in body_params.items()}) for start in range(0, frames, 64)])
    motion = math_module.normalize_smpl(params, rest, original, fps=controls["fps"])
    detections = _read(directory / "detections.json")["frames"]
    motion["keypoints2d"] = np.asarray([record.get("fitted_keypoints", record["keypoints"]) for record in detections], dtype=np.float32)
    motion["estimated_frames"] = np.asarray([record["frame"] for record in detections if record.get("estimated")], dtype=np.int32)
    motion["source_time_seconds"] = controls["start_seconds"] + np.arange(frames, dtype=np.float64) / controls["fps"]
    motion["model"] = np.asarray("easymocap-smpl")
    np.savez_compressed(directory / "motion.npz", **motion)
    math_module.write_bvh(motion, directory / "motion.bvh")
    (directory / "smpl-parameters.json").write_text(json.dumps({"frames": records, "cameras": cameras,
        "convention": "EasyMocap: global Rh rotates the fitted body about the origin, then Th translates it",
        "source_sha256": request["source"]["sha256"]}, allow_nan=False), encoding="utf-8")
    diagnostics = _read(directory / "diagnostics.json")
    diagnostics.update(fk_max_error_m=float(motion["fk_max_error_m"]),
                       fitted_frames=frames, contacts="unavailable", scale="monocular estimate")
    (directory / "diagnostics.json").write_text(json.dumps(diagnostics, allow_nan=False), encoding="utf-8")


def _set_state(directory, **fields):
    from filelock import FileLock

    from kimodo_mcp.jobs import atomic_json
    from kimodo_mcp.video_mocap import _record

    with FileLock(str(directory / ".state.lock"), timeout=5):
        job = _record(directory)
        if job.get("status") == "cancelling" and fields.get("status") == "running":
            fields["status"] = "cancelling"
        if fields.get("status") in {"completed", "failed"} and (directory / "cancel.json").exists():
            fields.update(status="cancelled", output=None, error="Video capture was cancelled")
        job.update(fields, updated_at=time.time())
        atomic_json(directory / "job.json", job)


def _check_cancel(directory):
    if (directory / "cancel.json").exists():
        raise CaptureCancelled("Video capture was cancelled; no automatic retry was started")


def _run_stage(directory, stage, argv, *, cwd, env, deadline):
    from kimodo_mcp.jobs import atomic_json
    from kimodo_mcp.video_mocap import _process_identity

    _check_cancel(directory)
    _set_state(directory, status="running", stage=stage)
    print("Starting video stage: " + stage, flush=True)
    process = subprocess.Popen(argv, cwd=cwd, env=env, stdin=subprocess.DEVNULL,
                               creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    try:
        atomic_json(directory / "stage-process.json", {"pid": process.pid,
                    "identity": _process_identity(process.pid), "stage": stage})
        while process.poll() is None:
            _check_cancel(directory)
            if time.monotonic() >= deadline:
                raise TimeoutError("Video capture exceeded its configured execution time")
            time.sleep(0.2)
        if process.returncode:
            raise RuntimeError("The %s stage failed (exit %s); inspect the video job log" % (stage, process.returncode))
    except BaseException:
        if process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)
        raise


def run_job(directory):
    from filelock import FileLock, Timeout

    from kimodo_mcp.config import Settings
    from kimodo_mcp.jobs import atomic_json, redact
    from kimodo_mcp.video_mocap import (
        ARTIFACT_NAMES,
        EASYMOCAP_REVISION,
        VideoMocapRequest,
        _canonical,
        _directory,
        _hash,
        _record,
        _verified_manifest,
        plan_video_mocap,
    )

    directory = directory.resolve()
    settings = Settings(data_dir=directory.parents[2], python=sys.executable)
    _directory(settings, directory.name)
    if _record(directory).get("status") not in {"queued", "cancelling"}:
        raise ValueError("A video job can execute only once")
    try:
        with FileLock(str(directory.parents[1] / ".execution.lock"), timeout=0):
            request = _read(directory / "request.json")
            job = _record(directory)
            if job.get("status") not in {"queued", "cancelling"}:
                raise ValueError("A video job can execute only once")
            if _canonical(request) != job.get("request_sha256") or request.get("id") != directory.name:
                raise ValueError("Video request identity changed before execution")
            _check_cancel(directory)
            _set_state(directory, status="running", stage="preflight")
            controls = VideoMocapRequest.model_validate(request["request"])
            current = plan_video_mocap(settings, controls)
            if (not current["ready"] or current["source"] != request["source"]
                    or current["runtime"] != request["runtime"] or current["assets"] != request["assets"]
                    or current["runtime_assets"] != request["runtime_assets"]):
                raise ValueError("Video source, runtime, or model assets changed after dispatch; run readiness again")
            runtime = request["runtime"]
            repo, dataset = Path(runtime["repo_path"]), directory / "dataset"
            images = dataset / "images" / "video"
            images.mkdir(parents=True)
            deadline = time.monotonic() + request["timeout"]
            env = os.environ.copy()
            env.update(PYTHONPATH=str(repo), PYTHONUNBUFFERED="1", HF_HUB_OFFLINE="1", TRANSFORMERS_OFFLINE="1")
            if runtime["device"] == "cpu":
                env["CUDA_VISIBLE_DEVICES"] = ""

            def run(stage, argv):
                _run_stage(directory, stage, argv, cwd=repo, env=env, deadline=deadline)

            scale = "scale=w='min(1280,iw)':h='min(1280,ih)':force_original_aspect_ratio=decrease:force_divisible_by=2"
            run("extract", [runtime["ffmpeg"], "-nostdin", "-v", "warning", "-ss", str(controls.start_seconds),
                "-i", controls.video_path, "-t", str(controls.duration_seconds), "-an", "-vf", "fps=%s,%s" % (controls.fps, scale),
                "-frames:v", str(controls.frames), "-start_number", "0", "-q:v", "2", str(images / "%06d.jpg")])
            if len(list(images.glob("*.jpg"))) != controls.frames:
                raise ValueError("The video did not yield the requested number of sampled frames")
            run("reference", [runtime["ffmpeg"], "-nostdin", "-v", "warning", "-framerate", str(controls.fps),
                "-start_number", "0", "-i", str(images / "%06d.jpg"), "-frames:v", str(controls.frames),
                "-an", "-c:v", "libx264", "-pix_fmt", "yuv420p", "-movflags", "+faststart", str(directory / "reference.mp4")])
            script = str(Path(__file__).resolve())

            def external(stage):
                run(stage, [runtime["python"], script, "--external-step", stage, str(directory)])

            external("detect")
            _check_cancel(directory)
            _set_state(directory, stage="validate_detections")
            detections, diagnostics = validate_detections(directory, controls.frames)
            from kimodo_mcp.video_gap import bridge_detections

            detections, estimated = bridge_detections(detections, controls.max_gap_frames)
            remaining = [item for item in diagnostics["problem_frames"] if item["frame"] not in estimated]
            diagnostics.update(estimated_frames=estimated, estimated_frame_count=len(estimated),
                               unresolved_frame_count=len(remaining), unresolved_frames=remaining,
                               warnings=(["Short internal gaps use explicit low-confidence 2D interpolation; inspect every estimated frame."] if estimated else []))
            # Only managed fitting inputs change. detections.json preserves the
            # detector's original keypoints/status alongside the separate estimate.
            for frame in estimated:
                record = detections[frame]
                template = _read(directory / "dataset" / "annots" / "video" / ("%06d.json" % record["estimate"]["before_frame"]))
                points = record["fitted_keypoints"]
                visible = [point for point in points if point[2] > 0.05]
                x, y = [point[0] for point in visible], [point[1] for point in visible]
                width, height = max(x) - min(x), max(y) - min(y)
                template["filename"] = "images/video/%06d.jpg" % frame
                template["annots"] = [{"personID": 0, "keypoints": points,
                    "bbox": [min(x) - width * .05, min(y) - height * .05, max(x) + width * .05, max(y) + height * .05, .25]}]
                atomic_json(directory / "dataset" / "annots" / "video" / ("%06d.json" % frame), template)
            atomic_json(directory / "detections.json", {"frames": detections, "coordinates": "image pixels; BODY25 confidence in column 3"})
            atomic_json(directory / "diagnostics.json", diagnostics)
            _set_state(directory, diagnostics=diagnostics)
            if remaining:
                raise ValueError("Some sampled frames still have missing, ambiguous, or insufficient detections. Shorten the trim or choose clearer single-person footage; unresolved frames are not silently filled.")
            external("camera")
            external("fit")
            external("normalize")
            _check_cancel(directory)
            _set_state(directory, stage="verify")
            atomic_json(directory / "timing.json", {"fps": controls.fps, "frames": controls.frames,
                "start_seconds": controls.start_seconds, "duration_seconds": controls.frames / controls.fps,
                "source_time_seconds": [controls.start_seconds + i / controls.fps for i in range(controls.frames)],
                "timestamp_kind": "requested constant-rate sample times, not original camera exposure timestamps",
                "resampling": "FFmpeg fps filter may duplicate or drop source frames; reference.mp4 uses exactly the analyzed images",
                "source": request["source"]})
            if _hash(Path(controls.video_path)) != request["source"]["sha256"]:
                raise ValueError("The source video changed during processing")
            for asset in request["assets"] + request["runtime_assets"]:
                asset_path = Path(asset["path"])
                if not asset_path.is_absolute():
                    asset_path = repo / asset_path
                if asset_path.stat().st_size != asset["bytes"] or _hash(asset_path) != asset["sha256"]:
                    raise ValueError("A runtime model asset changed during processing")
            manifest = {"version": 1, "job_id": directory.name, "request_sha256": _canonical(request),
                "source_sha256": request["source"]["sha256"], "reviewed_revision": EASYMOCAP_REVISION,
                "frames": controls.frames, "fps": controls.fps, "skeleton": "smpl24", "model": "easymocap-smpl",
                "coordinates": {"up_axis": "Y", "units": "estimated meters", "space": "camera-relative",
                                "source_to_viewer": [[1, 0, 0], [0, -1, 0], [0, 0, -1]],
                                "bvh_units": "meters", "calibration": "assumed focal length 1.2 times minimum image dimension"},
                "artifacts": [{"name": name, "path": name, "bytes": (directory / name).stat().st_size,
                               "sha256": _hash(directory / name), "format": name.rsplit(".", 1)[-1]}
                              for name in sorted(ARTIFACT_NAMES)]}
            atomic_json(directory / "manifest.json", manifest)
            _verified_manifest(directory)
            _check_cancel(directory)
            _set_state(directory, status="completed", stage="completed", output=manifest,
                       diagnostics=_read(directory / "diagnostics.json"), error=None)
    except CaptureCancelled as error:
        _set_state(directory, status="cancelled", error=str(error))
    except Timeout:
        # A duplicate invocation must not overwrite the legitimate worker's state.
        print("Another video worker holds the execution lock; this invocation did not run", flush=True)
        return 1
    except Exception as error:
        _set_state(directory, status="failed", error=redact(str(error))[:2000])
        print("Video capture failed: " + redact(str(error)), flush=True)
        return 1
    return 0


def main():
    if len(sys.argv) == 4 and sys.argv[1] == "--external-step":
        _external_stage(sys.argv[2], Path(sys.argv[3]).resolve())
        return 0
    if len(sys.argv) != 2:
        raise ValueError("Expected one managed video job directory")
    return run_job(Path(sys.argv[1]))


if __name__ == "__main__":
    raise SystemExit(main())
