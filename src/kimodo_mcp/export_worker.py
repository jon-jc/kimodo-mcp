"""Fixed subprocess entrypoint for native exports; never loads inference weights."""

import argparse
import os
from pathlib import Path

from filelock import FileLock, Timeout

from .export import (
    ExportRequest,
    _paths,
    digest,
    load_source,
    make_report,
    validate_artifact,
)
from .jobs import atomic_json


def convert(request: ExportRequest) -> dict:
    _, directory = _paths(request)
    if not directory.is_dir():
        raise ValueError("The assigned export directory does not exist.")
    try:
        with FileLock(str(directory / ".conversion.lock"), timeout=0):
            return _convert(request)
    except Timeout as error:
        raise ValueError("This export worker is already running.") from error


def _convert(request: ExportRequest) -> dict:
    # These limits also apply when the worker is invoked independently.
    os.environ.update(
        HF_HUB_OFFLINE="1",
        TRANSFORMERS_OFFLINE="1",
        HF_HUB_DISABLE_TELEMETRY="1",
        KIMODO_ALLOW_DOWNLOADS="0",
        CUDA_VISIBLE_DEVICES="",
        TEXT_ENCODER_DEVICE="cpu",
    )
    motion = load_source(request)
    source, directory = _paths(request)
    if (
        not directory.is_dir()
        or (directory / request.basename).exists()
        or (directory / "export-report.json").exists()
    ):
        raise ValueError("Export directory must exist and contain no completed or partial output.")
    import torch
    from kimodo.skeleton.registry import build_skeleton

    skeleton = build_skeleton(motion["positions"].shape[2])
    if (
        skeleton.name != motion["skeleton"]
        or list(skeleton.bone_order_names) != motion["names"]
        or skeleton.joint_parents.cpu().tolist() != motion["parents"]
    ):
        raise ValueError(
            "Source skeleton names, order and hierarchy differ from the canonical runtime assets."
        )
    sample = {
        "local_rot_mats": torch.tensor(motion["rotations"]["local_rot_mats"][0], dtype=torch.float32),
        "root_positions": torch.tensor(
            motion["positions"][0, :, int(skeleton.root_idx)], dtype=torch.float32
        ),
    }
    path = directory / request.basename
    if request.format == "bvh":
        from kimodo.exports.bvh import save_motion_bvh

        save_motion_bvh(
            str(path),
            sample["local_rot_mats"],
            sample["root_positions"],
            skeleton=skeleton,
            fps=motion["fps"],
            standard_tpose=request.bvh_standard_tpose,
        )
    elif request.format == "amass":
        from kimodo.exports.smplx import AMASSConverter

        AMASSConverter(fps=motion["fps"], skeleton=skeleton).convert_save_npz(
            sample, str(path), z_up=request.amass_z_up
        )
    else:
        from kimodo.exports.mujoco import MujocoQposConverter

        converter = MujocoQposConverter(skeleton)
        values = converter.dict_to_qpos(
            sample,
            device="cpu",
            numpy=True,
            root_quat_w_first=True,
            mujoco_rest_zero=request.mujoco_rest_zero,
        )
        converter.save_csv(values, str(path))
    validate_artifact(path, request, motion["positions"].shape[1], motion["fps"])
    if digest(source) != request.source_sha256:
        raise ValueError("Source changed during export; no completed receipt will be published.")
    report = make_report(request, motion, path)
    atomic_json(directory / "export-report.json", report)
    return report


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--request", type=Path, required=True)
    args = parser.parse_args()
    if not args.request.is_file() or args.request.stat().st_size > 16384:
        raise ValueError("Export request is missing or oversized.")
    request = ExportRequest.model_validate_json(args.request.read_text(encoding="utf-8"))
    if args.request.resolve() != Path(request.output_directory).resolve() / "request.json":
        raise ValueError("Export worker requires the assigned request.json inside its output directory.")
    convert(request)


if __name__ == "__main__":
    main()
