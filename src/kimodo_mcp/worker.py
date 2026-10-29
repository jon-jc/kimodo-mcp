"""Isolated, one-job inference adapter around Kimodo's public Python API.

Only this process imports the optional inference dependencies. Terminating the
process cancels a generation and releases its model allocations.
"""

from __future__ import annotations

import argparse
import contextlib
import hashlib
import importlib
import importlib.metadata
import json
import os
import sys
import traceback
from pathlib import Path

from .catalog import UPSTREAM_REVISION, model_info
from .jobs import atomic_json
from .schemas import EndEffectorConstraint, GenerateRequest

MAX_REQUEST_BYTES = 8 * 1024 * 1024
MOTION_KEYS = (
    "local_rot_mats", "global_rot_mats", "posed_joints", "root_positions",
    "smooth_root_pos", "foot_contacts", "global_root_heading",
)


def write_progress(directory: Path, stage: str, **details) -> None:
    atomic_json(directory / "progress.json", {"stage": stage, **details})


def load_request(path: Path) -> GenerateRequest:
    if not path.is_file() or path.stat().st_size > MAX_REQUEST_BYTES:
        raise ValueError("Request file is missing or exceeds the 8 MiB limit.")
    return GenerateRequest.model_validate_json(path.read_bytes())


def constraints_to_upstream(request: GenerateRequest) -> list[dict]:
    """Use named limb classes so the native postprocessor receives limb masks."""
    result = []
    names = {"LeftHand": "left-hand", "RightHand": "right-hand", "LeftFoot": "left-foot", "RightFoot": "right-foot"}
    for constraint in request.constraints:
        payload = constraint.model_dump(mode="json", exclude_none=True)
        if isinstance(constraint, EndEffectorConstraint) and request.effective_postprocess:
            payload.pop("joint_names")
            for name in constraint.joint_names:
                result.append({**payload, "type": names[name]})
        else:
            result.append(payload)
    return result


def validate_output(output: dict, request: GenerateRequest, total_frames: int, joints: int) -> dict:
    """Reject malformed/nonfinite arrays before publishing an artifact manifest."""
    import numpy as np

    expected = {
        "local_rot_mats": (request.samples, total_frames, joints, 3, 3),
        "global_rot_mats": (request.samples, total_frames, joints, 3, 3),
        "posed_joints": (request.samples, total_frames, joints, 3),
        "root_positions": (request.samples, total_frames, 3),
        "smooth_root_pos": (request.samples, total_frames, 3),
        "global_root_heading": (request.samples, total_frames, 2),
    }
    clean = {}
    for key in MOTION_KEYS:
        if key not in output:
            raise ValueError(f"Kimodo output lacks {key}.")
        array = np.asarray(output[key])
        if array.dtype.kind not in "fbiu" or not np.isfinite(array).all():
            raise ValueError(f"Kimodo output {key} must contain finite numeric values.")
        if key == "foot_contacts":
            if array.shape not in [(request.samples, total_frames, 4), (request.samples, total_frames, 6)]:
                raise ValueError("Kimodo foot contact output has an unexpected shape.")
        elif array.shape != expected[key]:
            raise ValueError(f"Kimodo output {key} shape {array.shape} differs from {expected[key]}.")
        clean[key] = array
    return clean


def artifact_record(path: Path, format_name: str, sample: int) -> dict:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return {
        "path": path.name, "format": format_name, "sample_index": sample,
        "bytes": path.stat().st_size, "sha256": digest.hexdigest(),
    }


def export_samples(output: dict, request: GenerateRequest, model, directory: Path, fps: float) -> list[dict]:
    import numpy as np
    import torch

    skeleton = model.output_skeleton
    parents = skeleton.joint_parents.detach().cpu().numpy()
    joint_names = np.asarray(skeleton.bone_order_names, dtype=np.str_)
    if len(parents) != len(joint_names):
        raise ValueError("Output skeleton metadata is inconsistent.")
    artifacts = []
    for index in range(request.samples):
        sample = {key: value[index] for key, value in output.items()}
        stem = f"motion_{index:02d}"
        for format_name in request.formats:
            if format_name == "npz":
                path = directory / f"{stem}.npz"
                np.savez(
                    path, **sample, joint_parents=parents, joint_names=joint_names,
                    fps=np.asarray(fps), skeleton=np.asarray(skeleton.name), model=np.asarray(request.model),
                )
            elif format_name == "bvh":
                from kimodo.exports.bvh import save_motion_bvh

                path = directory / f"{stem}.bvh"
                save_motion_bvh(
                    path, torch.from_numpy(sample["local_rot_mats"]), torch.from_numpy(sample["root_positions"]),
                    skeleton=skeleton, fps=fps, standard_tpose=request.bvh_standard_tpose,
                )
            elif format_name == "amass":
                from kimodo.exports.smplx import AMASSConverter

                path = directory / f"{stem}_amass.npz"
                AMASSConverter(fps=fps, skeleton=skeleton).convert_save_npz(sample, str(path))
            else:
                from kimodo.exports.mujoco import MujocoQposConverter

                path = directory / f"{stem}.csv"
                converter = MujocoQposConverter(skeleton)
                qpos = converter.dict_to_qpos(sample, device="cpu", numpy=True)
                converter.save_csv(qpos, str(path))
            artifacts.append(artifact_record(path, format_name, index))
    return artifacts


def configure_runtime_environment() -> None:
    # Online downloads are an explicit operator choice, never a tool parameter.
    if os.environ.get("KIMODO_ALLOW_DOWNLOADS", "").lower() not in {"1", "true", "yes"}:
        os.environ["HF_HUB_OFFLINE"] = "1"
        os.environ["TRANSFORMERS_OFFLINE"] = "1"
    os.environ.setdefault("TEXT_ENCODER_MODE", "local")
    os.environ.setdefault("HF_HUB_DISABLE_TELEMETRY", "1")


def validate_constraint_overlap(model, constraints: list, total_frames: int) -> None:
    """Reject contradictory targets before upstream's unspecified deduplication."""
    import torch

    accumulated = None
    controlled = None
    for constraint in constraints:
        values, mask = model.motion_rep.create_conditions_from_constraints(
            [constraint], total_frames, to_normalize=False, device=model.device,
        )
        mask = mask.bool()
        if accumulated is None:
            accumulated, controlled = values.clone(), mask.clone()
            continue
        common = controlled & mask
        if common.any() and not torch.allclose(accumulated[common], values[common], atol=1e-5, rtol=1e-5):
            raise ValueError("Constraints provide conflicting targets for the same frame and motion channel.")
        accumulated[mask] = values[mask]
        controlled |= mask


def filter_empty_segment_constraints(model) -> None:
    """Skip empty temporal crops before upstream's reshape(0, -1) writers."""
    original = model.motion_rep.create_conditions_from_constraints_batched

    def filtered(constraints_lst, *args, **kwargs):
        if constraints_lst and isinstance(constraints_lst[0], list):
            cleaned = [[item for item in group if len(item.frame_indices)] for group in constraints_lst]
        else:
            cleaned = [item for item in constraints_lst if len(item.frame_indices)]
        return original(cleaned, *args, **kwargs)

    model.motion_rep.create_conditions_from_constraints_batched = filtered


def generate(request: GenerateRequest, directory: Path) -> dict:
    """Execute one validated request. All output filenames are server-assigned."""
    configure_runtime_environment()
    write_progress(directory, "loading_runtime")
    import torch
    from kimodo import load_model
    from kimodo.constraints import load_constraints_lst
    from kimodo.tools import seed_everything

    info = model_info(request.model)
    device = os.environ.get("KIMODO_DEVICE", "cuda:0" if torch.cuda.is_available() else "cpu")
    write_progress(directory, "loading_model", model=request.model, device=device)
    # The upstream URL/config/checkpoint knobs remain operator environment settings.
    model, resolved = load_model(
        request.model, device=device, default_family=None, return_resolved_name=True,
        text_encoder_fp32=os.environ.get("KIMODO_TEXT_ENCODER_FP32", "").lower() in {"1", "true", "yes"},
    )
    if resolved != request.model:
        raise ValueError("Loaded model does not match the requested exact model identifier.")
    fps = float(model.fps)
    plan = request.validated_plan(fps)
    if request.steps > int(model.diffusion.num_base_steps):
        raise ValueError("Requested denoising steps exceed the model's base diffusion schedule.")
    constraints = load_constraints_lst(constraints_to_upstream(request), model.skeleton)
    validate_constraint_overlap(model, constraints, plan["total_frames"])
    if constraints:
        filter_empty_segment_constraints(model)
    seed_everything(request.seed)
    write_progress(directory, "generating", **plan)
    with torch.inference_mode():
        result = model(
            [segment.text for segment in request.segments], plan["segment_frames"],
            num_denoising_steps=request.steps, num_samples=request.samples, multi_prompt=True,
            constraint_lst=constraints, cfg_type="separated",
            cfg_weight=[request.cfg_text, request.cfg_constraint],
            first_heading_angle=request.first_heading_angle,
            num_transition_frames=request.transition_frames,
            post_processing=plan["postprocess"], root_margin=request.root_margin,
            return_numpy=True, progress_bar=lambda iterator: iterator,
        )
    clean = validate_output(result, request, plan["total_frames"], info["output_joints"])
    write_progress(directory, "exporting", **plan)
    artifacts = export_samples(clean, request, model, directory, fps)
    warnings = []
    if request.postprocess and not plan["postprocess"]:
        warnings.append("Postprocessing is disabled for G1, following the upstream generation CLI.")
    if getattr(model.skeleton, "nbjoints", None) == 30:
        warnings.append("SOMA output expands the compact model skeleton with relaxed hand poses; detailed finger motion is not independently generated.")
    manifest = {
        "schema_version": 1, "model": request.model, "model_repo": info["repo_id"],
        "reviewed_upstream_revision": UPSTREAM_REVISION,
        "skeleton": model.output_skeleton.name, "fps": fps,
        "samples": request.samples, "frames": plan["total_frames"],
        "coordinate_system": {"up": "+Y", "forward": "+Z", "units": "meters"},
        "effective_settings": {
            **request.model_dump(mode="json", exclude={"constraints"}),
            "constraint_sets": len(request.constraints), "postprocess": plan["postprocess"], "device": device,
        },
        "artifacts": artifacts, "warnings": warnings,
    }
    atomic_json(directory / "manifest.json", manifest)
    write_progress(directory, "complete", **plan)
    return manifest


def probe() -> dict:
    """Check local runtime imports and device availability without fetching models."""
    os.environ["HF_HUB_OFFLINE"] = "1"
    os.environ["TRANSFORMERS_OFFLINE"] = "1"
    result = {"python": sys.executable, "python_version": sys.version.split()[0], "imports": {}, "cuda_available": False, "gpus": [], "cached_token_present": False}
    for name in ("torch", "kimodo", "motion_correction", "transformers", "bvhio"):
        try:
            module = importlib.import_module(name)
            entry = {"available": True}
            try:
                entry["version"] = importlib.metadata.version(name)
            except importlib.metadata.PackageNotFoundError:
                pass
            result["imports"][name] = entry
            if name == "torch":
                result["cuda_available"] = bool(module.cuda.is_available())
                result["torch_cuda_version"] = module.version.cuda
                if result["cuda_available"]:
                    for index in range(module.cuda.device_count()):
                        properties = module.cuda.get_device_properties(index)
                        result["gpus"].append({"index": index, "name": properties.name, "memory_bytes": properties.total_memory})
        except Exception as error:
            result["imports"][name] = {"available": False, "error": f"{type(error).__name__}: {error}"}
    try:
        from huggingface_hub import get_token

        result["cached_token_present"] = bool(get_token())
    except ImportError:
        pass
    result["generation_runtime_available"] = all(result["imports"][name]["available"] for name in ("torch", "kimodo"))
    result["weights_checked"] = False
    return result


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run one Kimodo inference job.")
    parser.add_argument("--request", type=Path)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--probe", action="store_true")
    args = parser.parse_args(argv)
    if args.probe:
        with contextlib.redirect_stdout(sys.stderr):
            result = probe()
        print(json.dumps(result, allow_nan=False))
        return 0
    if args.request is None or args.output is None:
        parser.error("--request and --output are required for generation")
    output_approved = False
    try:
        request = load_request(args.request)
        args.output.mkdir(parents=True, exist_ok=True)
        # The supervisor owns these files in its dedicated per-job directory.
        # Existing artifacts or a manifest mean this is not a fresh generation.
        supervisor_files = {"request.json", "runner.log", "worker.log", "progress.json", "progress.json.tmp"}
        if any(path.name not in supervisor_files or not path.is_file() or path.is_symlink() for path in args.output.iterdir()):
            raise ValueError("Worker output directory contains existing artifacts or unexpected files.")
        output_approved = True
        with contextlib.redirect_stdout(sys.stderr):
            from filelock import FileLock

            data_root = Path(os.environ.get("KIMODO_DATA_DIR", args.output.parent))
            data_root.mkdir(parents=True, exist_ok=True)
            # Survives supervisor failure: another worker cannot overlap an orphan.
            with FileLock(data_root / "generation-worker.lock", timeout=int(os.environ.get("KIMODO_JOB_TIMEOUT", "3600"))):
                generate(request, args.output)
        return 0
    except Exception as error:
        print(f"Generation failed: {type(error).__name__}: {error}", file=sys.stderr)
        traceback.print_exc(file=sys.stderr)
        if output_approved:
            with contextlib.suppress(Exception):
                write_progress(args.output, "failed", error=f"{type(error).__name__}: {error}")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
