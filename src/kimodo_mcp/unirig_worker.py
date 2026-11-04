"""Fixed UniRig stages plus a separate, cancellable supervisor.

The external-stage path imports only the separately installed runtime. It never
evaluates the upstream shell launchers or accepts an arbitrary script/config.
"""

from __future__ import annotations

import hashlib
import json
import os
import random
import runpy
import shutil
import socket
import subprocess
import sys
import threading
import time
from pathlib import Path


def _read(path):
    if path.stat().st_size > 4 * 1024**2:
        raise ValueError("UniRig request is too large")
    return json.loads(path.read_text(encoding="utf-8"))


def item_paths(request, index):
    item = request["assets"][index]
    mesh = f"inputs/item_{index:02d}/mesh.{item['mesh']['format']}"
    rig = f"inputs/item_{index:02d}/rig.{item['rig']['format']}" if item["rig"] else mesh
    output = f"outputs/item_{index:02d}"
    if request["request"]["operation"] == "pipeline":
        rig = output + "/skeleton.fbx"
    return {"mesh": mesh, "rig": rig, "output": output,
            "skeleton": output + "/skeleton.fbx", "skin": output + "/skin.fbx",
            "merged": output + "/rigged." + request["request"]["output_format"]}


def build_stage_argv(stage, request, index):
    """Relative fixed input names prevent upstream get_files() discarding npz_dir."""
    controls = request["request"]
    paths = item_paths(request, index)
    seed = (controls["seed"] + index) % 2**32
    if stage in {"extract_mesh", "extract_rig"}:
        source = paths["mesh" if stage == "extract_mesh" else "rig"]
        return ["-m", "src.data.extract", "--config=configs/data/quick_inference.yaml",
                "--require_suffix=obj,fbx,glb,vrm", "--num_runs=1", "--id=0", "--force_override=false",
                "--time=managed", f"--faces_target_count={controls['faces_target_count']}",
                f"--input={source}", "--output_dir=intermediate"]
    if stage in {"predict_skeleton", "predict_skin"}:
        kind = "skeleton" if stage == "predict_skeleton" else "skin"
        source = paths["mesh" if kind == "skeleton" else "rig"]
        return [str(Path(request["runtime"]["repo"]) / "run.py"),
                f"--task=configs/task/managed_{kind}.yaml", f"--seed={seed}",
                f"--input={source}", f"--output={paths[kind]}", "--npz_dir=intermediate",
                "--data_name=raw_data.npz"]
    if stage == "merge_rig":
        source = paths["skin"] if controls["operation"] == "pipeline" else paths["rig"]
        return ["-m", "src.inference.merge", "--require_suffix=obj,fbx,glb,vrm",
                "--num_runs=1", "--id=0", f"--source={source}", f"--target={paths['mesh']}",
                f"--output={paths['merged']}", f"--add_root={str(controls['add_root']).lower()}"]
    raise ValueError("Unknown fixed UniRig stage")


def _prepare_configs(directory, request):
    import yaml

    controls = request["request"]

    def load(relative):
        return yaml.safe_load((directory / relative).read_text(encoding="utf-8"))

    def write(relative, value):
        (directory / relative).write_text(json.dumps(value, indent=2, allow_nan=False), encoding="utf-8")

    data = load("configs/data/quick_inference.yaml")
    data["predict_dataset_config"].update(num_workers=0, batch_size=1, persistent_workers=False)
    write("configs/data/quick_inference.yaml", data)
    system = load("configs/system/ar_inference_articulationxl.yaml")
    system["generate_kwargs"].update({key: controls[key] for key in (
        "do_sample", "max_new_tokens", "num_beams", "top_k", "top_p", "temperature", "repetition_penalty")})
    # One skeleton per item; batch candidates are explicit separate input items.
    system["generate_kwargs"].update(num_return_sequences=1)
    write("configs/system/ar_inference_articulationxl.yaml", system)
    transform = load("configs/transform/inference_skin_transform.yaml")
    transform["predict_transform_config"]["vertex_group_config"]["kwargs"]["voxel_skin"]["backend"] = controls["skin_backend"]
    write("configs/transform/inference_skin_transform.yaml", transform)
    for kind, original in (
        ("skeleton", "quick_inference_skeleton_articulationxl_ar_256"),
        ("skin", "quick_inference_unirig_skin"),
    ):
        task = load(f"configs/task/{original}.yaml")
        task.pop("wandb", None)
        task.pop("checkpoint", None)
        task["trainer"].update(devices=1, num_nodes=1, strategy="auto", default_root_dir="trainer")
        task["writer"].update(repeat=1)
        write(f"configs/task/managed_{kind}.yaml", task)


def _external_stage(stage, directory, index, expected_request_sha256):
    request = _read(directory / "request.json")
    actual = hashlib.sha256(json.dumps(request, sort_keys=True, separators=(",", ":"),
                                      allow_nan=False).encode()).hexdigest()
    if actual != expected_request_sha256 or request.get("id") != directory.name:
        raise ValueError("UniRig request identity or checksum changed before the external stage")
    if type(index) is not int or not 0 <= index < len(request["assets"]):
        raise ValueError("Invalid UniRig batch item")
    os.chdir(directory)
    repo = Path(request["runtime"]["repo"])
    sys.path.insert(0, str(repo))
    if not request["runtime"]["downloads_allowed"]:
        os.environ["HF_HUB_OFFLINE"] = "1"
        os.environ["TRANSFORMERS_OFFLINE"] = "1"

        def offline(*args, **kwargs):
            raise RuntimeError("Network downloads are disabled for this UniRig job")

        socket.socket.connect = offline
        socket.socket.connect_ex = offline
    os.environ["WANDB_MODE"] = "offline"
    import numpy as np

    seed = (request["request"]["seed"] + index) % 2**32
    random.seed(seed)
    np.random.seed(seed)
    if stage == "configure":
        _prepare_configs(directory, request)
        return
    loaded_weights = []
    if stage in {"predict_skeleton", "predict_skin"}:
        from src.inference import download as checkpoint_module

        native_download = checkpoint_module.download

        def checkpoint_hash(path):
            value = hashlib.sha256()
            with path.open("rb") as stream:
                for block in iter(lambda: stream.read(1024 * 1024), b""):
                    value.update(block)
            return value.hexdigest()

        def observed_download(name):
            path = Path(native_download(name)).resolve()
            if not path.is_file():
                raise ValueError("The required UniRig checkpoint is unavailable locally")
            loaded_weights.append({"reference": name, "absolute_path": str(path),
                                   "bytes": path.stat().st_size, "sha256": checkpoint_hash(path)})
            return str(path)

        checkpoint_module.download = observed_download
    argv = build_stage_argv(stage, request, index)
    if argv[0] == "-m":
        sys.argv = [argv[1], *argv[2:]]
        runpy.run_module(argv[1], run_name="__main__")
    else:
        sys.argv = argv
        runpy.run_path(argv[0], run_name="__main__")
    if stage in {"predict_skeleton", "predict_skin"}:
        if not loaded_weights:
            raise ValueError("UniRig prediction did not report its checkpoint identity")
        for weight in loaded_weights:
            path = Path(weight["absolute_path"])
            if path.stat().st_size != weight["bytes"] or checkpoint_hash(path) != weight["sha256"]:
                raise ValueError("The UniRig checkpoint changed during prediction")
        report = directory / "weights" / f"item_{index:02d}-{stage}.json"
        report.parent.mkdir(exist_ok=True)
        report.write_text(json.dumps({"stage": stage, "item_index": index, "weights": loaded_weights},
                                     allow_nan=False), encoding="utf-8")


class UniRigCancelled(Exception):
    pass


def _set_state(directory, **changes):
    from filelock import FileLock

    from kimodo_mcp.jobs import atomic_json
    from kimodo_mcp.unirig import _record

    with FileLock(directory / ".state.lock", timeout=5):
        job = _record(directory)
        if (directory / "cancel.json").exists():
            if changes.get("status") in {"completed", "failed"}:
                changes.update(status="cancelled", output=None)
            elif changes.get("status") == "running":
                changes["status"] = "cancelling"
        job.update(changes, updated_at=time.time())
        atomic_json(directory / "job.json", job)


def _check_cancel(directory):
    if (directory / "cancel.json").exists():
        if _read(directory / "cancel.json").get("job_id") != directory.name:
            raise ValueError("UniRig cancellation identity does not match")
        raise UniRigCancelled("UniRig processing was cancelled; no automatic retry was started")


def _run_stage(directory, stage, index, request, *, deadline, logger):
    from kimodo_mcp.jobs import atomic_json
    from kimodo_mcp.unirig import _canonical
    from kimodo_mcp.video_mocap import _process_identity

    _check_cancel(directory)
    _set_state(directory, status="running", stage=stage, item_index=index)
    env = os.environ.copy()
    env["PYTHONPATH"] = request["runtime"]["repo"]
    env["PYTHONUNBUFFERED"] = "1"
    if request["runtime"]["downloads_allowed"]:
        env.pop("HF_HUB_OFFLINE", None)
        env.pop("TRANSFORMERS_OFFLINE", None)
    else:
        env.update(HF_HUB_OFFLINE="1", TRANSFORMERS_OFFLINE="1")
    env["WANDB_MODE"] = "offline"
    command = [request["runtime"]["python"], str(Path(__file__).resolve()),
               "--external-step", stage, str(index), str(directory), _canonical(request)]
    logger.write(f"\nStarting item {index}: {stage}\n")
    process = subprocess.Popen(command, cwd=directory, env=env, stdin=subprocess.DEVNULL,
                               stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                               creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))

    def drain():
        while data := process.stdout.read(4096):
            logger.write(data.decode("utf-8", errors="replace"))

    reader = threading.Thread(target=drain, daemon=True)
    reader.start()
    try:
        atomic_json(directory / "stage-process.json", {"pid": process.pid,
                    "identity": _process_identity(process.pid), "stage": stage, "item_index": index})
        while process.poll() is None:
            _check_cancel(directory)
            if time.monotonic() >= deadline:
                raise TimeoutError("UniRig job exceeded its configured execution time")
            time.sleep(0.2)
        if process.returncode != 0:
            raise RuntimeError(f"UniRig {stage} failed (exit {process.returncode}); inspect the job log")
    except BaseException:
        if process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)
        raise
    finally:
        reader.join(timeout=5)
        if process.stdout is not None:
            process.stdout.close()


def validate_artifact(path, *, require_skin=False):
    """Reject missing/truncated exports even when upstream catches its own error."""
    import struct
    import zipfile

    if not path.is_file() or not 0 < path.stat().st_size <= 512 * 1024**2:
        raise ValueError("UniRig did not produce a nonempty bounded artifact: " + path.name)
    with path.open("rb") as stream:
        head = stream.read(32)
        if path.suffix == ".fbx" and not head.startswith(b"Kaydara FBX Binary  \x00\x1a\x00"):
            raise ValueError("UniRig FBX export has an unexpected or truncated header")
        if path.suffix == ".glb":
            if len(head) < 20 or struct.unpack("<4sII", head[:12]) != (b"glTF", 2, path.stat().st_size):
                raise ValueError("UniRig GLB export has an invalid container header")
            length, kind = struct.unpack("<I4s", head[12:20])
            if kind != b"JSON" or not 0 < length <= 8 * 1024**2 or length > path.stat().st_size - 20:
                raise ValueError("UniRig GLB scene metadata is invalid")
            stream.seek(20)
            document = json.loads(stream.read(length))
            if not document.get("meshes") or (require_skin and not document.get("skins")):
                raise ValueError("UniRig merged GLB lacks required mesh or skin metadata")
    if path.suffix == ".npz":
        with zipfile.ZipFile(path) as archive:
            entries = archive.infolist()
            if len(entries) > 64 or sum(entry.file_size for entry in entries) > 1024**3:
                raise ValueError("UniRig preprocessed archive exceeds its bounded size")
            if not {"vertices.npy", "faces.npy"}.issubset(archive.namelist()):
                raise ValueError("UniRig preprocessing did not produce mesh data")


def run_job(directory):
    from filelock import FileLock, Timeout

    from kimodo_mcp.config import Settings
    from kimodo_mcp.jobs import atomic_json, redact
    from kimodo_mcp.native_demo_worker import BoundedLog
    from kimodo_mcp.unirig import (
        UNIRIG_REVISION,
        UniRigRequest,
        _canonical,
        _contained,
        _directory,
        _hash,
        _record,
        _runtime,
        _verified_manifest,
        validate_obj_source,
    )

    directory = directory.resolve()
    settings = Settings(data_dir=directory.parents[2], python=sys.executable)
    if _directory(settings, directory.name) != directory:
        raise ValueError("UniRig worker requires its assigned managed job directory")
    logger = BoundedLog(_contained(directory, "worker.log"))
    request = _read(directory / "request.json")
    deadline = time.monotonic() + request["timeout"]
    try:
        with FileLock(directory.parents[1] / ".execution.lock", timeout=0):
            job = _record(directory)
            if job.get("status") not in {"queued", "cancelling"}:
                logger.write("A UniRig job may execute only once; this invocation did not run.\n")
                return 1
            if request.get("id") != directory.name or _canonical(request) != job.get("request_sha256"):
                raise ValueError("UniRig request identity or checksum changed before execution")
            controls = UniRigRequest.model_validate(request["request"])
            _check_cancel(directory)
            # Read configuration from the operator environment again, never from
            # an arbitrary tool-supplied interpreter or executable path.
            current = _runtime(probe=True, operation=controls.operation)
            if not current["configuration_ready"] or any(current[key] != request["runtime"][key]
                    for key in ("repo", "python", "reviewed_revision", "downloads_allowed")):
                raise ValueError("UniRig runtime configuration changed or is no longer ready")

            def verify_execution_inputs():
                # A job can wait behind another model for a long time, and each
                # stage imports the live operator checkout in a fresh process.
                # Never publish the pinned revision after that checkout changed.
                saved = _read(_contained(directory, "request.json"))
                if saved.get("id") != directory.name or _canonical(saved) != job["request_sha256"]:
                    raise ValueError("UniRig request identity or checksum changed during execution")
                observed = _runtime(probe=False, operation=controls.operation)
                if not observed["configuration_ready"] or any(
                    observed[key] != request["runtime"][key]
                    for key in ("repo", "python", "reviewed_revision", "downloads_allowed")
                ):
                    raise ValueError("UniRig runtime configuration changed during execution")

            generation_lock = FileLock(settings.data_dir / "generation.lock")
            while True:
                _check_cancel(directory)
                if time.monotonic() >= deadline:
                    raise TimeoutError("UniRig timed out waiting for the shared generation slot")
                try:
                    generation_lock.acquire(timeout=0.2)
                    break
                except Timeout:
                    _set_state(directory, status="running", stage="waiting_for_runtime")
            try:
                verify_execution_inputs()
                sources = []
                for index, item in enumerate(request["assets"]):
                    paths = item_paths(request, index)
                    # Existing supplied rig sources use their copied input name,
                    # while a pipeline creates its rig inside outputs later.
                    for role in ("mesh", "rig"):
                        value = item[role]
                        if value is None:
                            continue
                        original = Path(value["absolute_path"])
                        if original.stat().st_size != value["bytes"] or _hash(original) != value["sha256"]:
                            raise ValueError("UniRig registered source changed before staging")
                        validate_obj_source(original)
                        relative = f"inputs/item_{index:02d}/{role}.{value['format']}"
                        target = _contained(directory, relative)
                        target.parent.mkdir(parents=True, exist_ok=True)
                        with original.open("rb") as reader, target.open("xb") as writer:
                            shutil.copyfileobj(reader, writer)
                        if _hash(target) != value["sha256"] or _hash(original) != value["sha256"]:
                            raise ValueError("UniRig input changed while it was being copied")
                        validate_obj_source(target)
                        sources.append({"path": relative, "sha256": value["sha256"], "bytes": value["bytes"],
                                        "item_index": index, "role": role, "asset_id": value.get("asset_id")})
                    _contained(directory, paths["output"]).mkdir(parents=True, exist_ok=False)
                config_root = Path(current["repo"]) / "configs"
                for original in config_root.rglob("*"):
                    if original.resolve() != original:
                        raise ValueError("UniRig configuration cannot redirect outside the reviewed checkout")
                shutil.copytree(config_root, _contained(directory, "configs"))

                configuration = []

                def verify_configuration():
                    for record in configuration:
                        path = _contained(directory, record["path"])
                        if path.stat().st_size != record["bytes"] or _hash(path) != record["sha256"]:
                            raise ValueError("Managed UniRig configuration changed during execution")

                def execute(stage, index):
                    verify_execution_inputs()
                    verify_configuration()
                    _run_stage(directory, stage, index, request, deadline=deadline, logger=logger)
                    verify_execution_inputs()
                    verify_configuration()

                execute("configure", 0)
                configuration = [{"path": path.relative_to(directory).as_posix(),
                                  "bytes": path.stat().st_size, "sha256": _hash(path)}
                                 for path in sorted((directory / "configs").rglob("*")) if path.is_file()]
                artifacts = []
                weights = []
                for index, item in enumerate(request["assets"]):
                    paths = item_paths(request, index)
                    operation = controls.operation
                    names = []
                    if operation in {"preprocess", "skeleton", "pipeline"}:
                        execute("extract_mesh", index)
                        raw = directory / "intermediate" / Path(paths["mesh"]).with_suffix("") / "raw_data.npz"
                        validate_artifact(raw)
                        if operation == "preprocess":
                            output = directory / paths["output"] / "raw_data.npz"
                            shutil.copyfile(raw, output)
                            names.append(output)
                    if operation in {"skeleton", "pipeline"}:
                        execute("predict_skeleton", index)
                        output = directory / paths["skeleton"]
                        validate_artifact(output)
                        weights.extend(_read(directory / "weights" / f"item_{index:02d}-predict_skeleton.json")["weights"])
                        names.append(output)
                    if operation in {"skin", "pipeline"}:
                        execute("extract_rig", index)
                        raw = directory / "intermediate" / Path(paths["rig"]).with_suffix("") / "raw_data.npz"
                        validate_artifact(raw)
                        execute("predict_skin", index)
                        output = directory / paths["skin"]
                        validate_artifact(output)
                        weights.extend(_read(directory / "weights" / f"item_{index:02d}-predict_skin.json")["weights"])
                        names.append(output)
                    if operation in {"merge", "pipeline"}:
                        execute("merge_rig", index)
                        output = directory / paths["merged"]
                        validate_artifact(output, require_skin=operation == "pipeline")
                        names.append(output)
                    for path in names:
                        artifacts.append({"path": path.relative_to(directory).as_posix(), "bytes": path.stat().st_size,
                                          "sha256": _hash(path), "format": path.suffix[1:], "item_index": index})
                _check_cancel(directory)
                verify_execution_inputs()
                manifest = {"schema_version": 1, "job_id": directory.name,
                    "request_sha256": _canonical(request), "reviewed_revision": UNIRIG_REVISION,
                    "operation": operation, "sources": sources, "artifacts": artifacts,
                    "configuration": configuration, "weights": weights,
                    "item_seeds": [(controls.seed + index) % 2**32 for index in range(len(request["assets"]))],
                    "validation": "Required outputs, format headers and content hashes verified; deformation and retarget quality are unverified.",
                    "warnings": ["Generated rigs are static and are not automatically mapped to Kimodo or an Unreal skeleton.",
                                 "Merge uses upstream geometric alignment and skin transfer; inspect scale, orientation, weights and material fidelity.",
                                 "Checkpoint content hashes are recorded; external weights are not copied into the job or bundled with the bridge."]}
                atomic_json(directory / "manifest.json", manifest)
                _set_state(directory, manifest_sha256=_canonical(manifest))
                _verified_manifest(directory)
                _check_cancel(directory)
                verify_execution_inputs()
                _set_state(directory, status="completed", stage="completed", output=manifest)
            finally:
                generation_lock.release()
    except UniRigCancelled as error:
        _set_state(directory, status="cancelled", error=str(error))
        return 0
    except Timeout:
        logger.write("A UniRig worker already owns the execution lease; duplicate invocation did not run.\n")
        return 1
    except Exception as error:
        detail = redact(str(error))[:2000]
        logger.write("UniRig processing failed: " + detail + "\n")
        _set_state(directory, status="failed", error=detail)
        return 1
    return 0


def main():
    if len(sys.argv) == 6 and sys.argv[1] == "--external-step":
        _external_stage(sys.argv[2], Path(sys.argv[4]).resolve(), int(sys.argv[3]), sys.argv[5])
        return 0
    if len(sys.argv) == 2:
        return run_job(Path(sys.argv[1]))
    raise ValueError("Expected one managed UniRig job directory or a fixed external stage")


if __name__ == "__main__":
    raise SystemExit(main())
