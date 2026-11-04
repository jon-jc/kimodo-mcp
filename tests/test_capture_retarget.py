"""Capture retarget preparation/cleanup tests; synthetic motion, no editor or runtime."""

import json
from pathlib import Path

import numpy as np
import pytest

from kimodo_mcp import capture_retarget as retarget
from kimodo_mcp import freemocap as capture
from kimodo_mcp.config import Settings
from kimodo_mcp.handoff_scripts.capture_character import cleanup_motion


def synthetic_bvh(frames=6, fps=30, rest_pose="tpose"):
    bones = {
        "pelvis": (None, (0, 0, 1)),
        "spine": ("pelvis", (0, 0, 0.1)),
        "spine.001": ("spine", (0, 0, 0.15)),
        "neck": ("spine.001", (0, 0, 0.2)),
        "face": ("neck", (0, 0, 0.1)),
    }
    for side, sign in (("L", 1), ("R", -1)):
        downward = -0.16 if rest_pose == "apose" else 0
        bones.update(
            {
                "shoulder." + side: ("spine.001", (0.05 * sign, 0, 0.15)),
                "upper_arm." + side: ("shoulder." + side, (0.1 * sign, 0, 0)),
                "forearm." + side: ("upper_arm." + side, (0.25 * sign, 0, downward)),
                "hand." + side: ("forearm." + side, (0.23 * sign, 0, downward)),
                "pelvis." + side: ("pelvis", (0.1 * sign, 0, 0)),
                "thigh." + side: ("pelvis." + side, (0, 0, -0.02)),
                "shin." + side: ("thigh." + side, (0, 0, -0.4)),
                "foot." + side: ("shin." + side, (0, 0, -0.4)),
            }
        )
    lines, order = ["HIERARCHY"], []

    def add(name, depth=0):
        parent, offset = bones[name]
        order.append(name)
        lines.extend(
            [
                " " * depth + ("ROOT " if parent is None else "JOINT ") + name,
                " " * depth + "{",
                " " * (depth + 1) + "OFFSET " + " ".join(map(str, offset)),
                " " * (depth + 1)
                + (
                    "CHANNELS 6 Xposition Yposition Zposition Zrotation Xrotation Yrotation"
                    if parent is None
                    else "CHANNELS 3 Zrotation Xrotation Yrotation"
                ),
            ]
        )
        children = [n for n, (p, _) in bones.items() if p == name]
        for child in children:
            add(child, depth + 1)
        if not children:
            end = (
                (0.1 * (1 if name.endswith(".L") else -1), 0, 0)
                if "hand" in name
                else (0, -0.1, 0)
                if "foot" in name
                else (0, 0, 0.1)
            )
            lines.extend(["End Site", "{", "OFFSET " + " ".join(map(str, end)), "}"])
        lines.append(" " * depth + "}")

    add("pelvis")
    lines.extend(["MOTION", f"Frames: {frames}", f"Frame Time: {1 / fps:.12g}"])
    for f in range(frames):
        values = [f * 0.01, 0, 0, 0, 0, 0] + [0.0] * (3 * (len(order) - 1))
        # A bounded visible right arm wave, with no rest/hierarchy changes.
        index = 6 + (order.index("upper_arm.R") - 1) * 3
        values[index + 1] = 15 * np.sin(f / max(frames - 1, 1) * np.pi)
        lines.append(" ".join(map(str, values)))
    return "\n".join(lines) + "\n"


@pytest.fixture
def saved(tmp_path, monkeypatch):
    monkeypatch.delenv("KIMODO_CHARACTER_FBX", raising=False)
    monkeypatch.setattr(capture.FreeMoCapClient, "_request", lambda *a, **k: pytest.fail("Runtime request"))
    root = tmp_path / "data"
    settings = Settings(root, "unused")
    references, recordings = root / "references", root / "recordings"
    for path in (references, recordings, root / "freemocap/jobs", root / "character"):
        path.mkdir(parents=True)
    (root / "freemocap/runtime.json").write_text(
        json.dumps(
            {
                "base_url": "http://127.0.0.1:53117",
                "recordings_root": str(recordings),
                "references_root": str(references),
            }
        )
    )
    client = capture.FreeMoCapClient(settings)
    folder = recordings / "take"
    (folder / "synchronized_videos").mkdir(parents=True)
    (folder / "output_data").mkdir()
    (folder / "3d_models").mkdir()
    video = folder / "synchronized_videos/cam.mp4"
    video.write_bytes(b"synthetic video")
    video_entry = {
        "path": "synchronized_videos/cam.mp4",
        "bytes": video.stat().st_size,
        "sha256": capture._digest(video),
    }
    (folder / ".kimodo-freemocap.json").write_text(
        json.dumps({"recording_id": "take", "runtime_key": client.runtime_key, "videos": [video_entry]})
    )
    body = folder / "output_data/mediapipe_body_3d_xyz.npy"
    np.save(body, np.zeros((6, 37, 3)))
    entry = {
        "path": "output_data/mediapipe_body_3d_xyz.npy",
        "bytes": body.stat().st_size,
        "sha256": capture._digest(body),
    }
    process = {
        "schema_version": 1,
        "job_id": "a" * 32,
        "runtime_key": client.runtime_key,
        "recording_id": "take",
        "operation": "process",
        "status": "completed",
        "pipeline_completion_verified": True,
        "request": {"operation": "process", "recording_id": "take", "allow_planar": True},
        "sources": [video_entry],
        "outputs": [entry],
        "video_metadata": {"videos": [{"fps": 30, "frame_count": 6}]},
    }
    (root / "freemocap/jobs" / (process["job_id"] + ".json")).write_text(json.dumps(process))
    bvh = folder / "3d_models/take.bvh"
    bvh.write_text(synthetic_bvh())
    export = dict(
        process,
        job_id="b" * 32,
        operation="export",
        request={"operation": "export", "recording_id": "take"},
        outputs=[
            entry,
            {"path": "3d_models/take.bvh", "bytes": bvh.stat().st_size, "sha256": capture._digest(bvh)},
        ],
    )
    (root / "freemocap/jobs" / (export["job_id"] + ".json")).write_text(json.dumps(export))
    target = root / "character/target.fbx"
    target.write_bytes(b"synthetic registered FBX - not executed")
    (root / "character/target.json").write_text(json.dumps({"source_fbx": str(target)}))
    monkeypatch.setattr(retarget, "SOURCE_SHA256", capture._digest(target))
    monkeypatch.setattr(retarget, "validate_target", lambda path: Path(path).resolve(strict=True))
    return settings, export, process, bvh, target


def options(**kwargs):
    return retarget.CaptureRetargetRequest(source_scale_to_meters=1.0, source_rest_pose="tpose", **kwargs)


@pytest.mark.parametrize(
    "kwargs",
    [
        {"start_frame": True},
        {"root_motion": 1},
        {"source_scale_to_meters": float("nan")},
        {"rotation_smoothing_radius": 6},
        {"end_frame": 1},
        {
            "joint_corrections": [
                {"bone": "Root", "rotation_degrees": [1, 0, 0], "start_frame": 0, "end_frame": 3}
            ]
        },
    ],
)
def test_controls_are_strict_and_bounded(kwargs):
    data = {"source_scale_to_meters": 1.0, "source_rest_pose": "tpose", **kwargs}
    with pytest.raises(ValueError):
        retarget.CaptureRetargetRequest(**data)


def test_prepare_is_immutable_deduplicated_bound_and_offline(saved):
    settings, exported, process, bvh, target = saved
    before = (bvh.read_bytes(), target.read_bytes())
    result = retarget.prepare_capture_character_handoff(
        settings, exported["job_id"], "3d_models/take.bvh", options(start_frame=1, end_frame=5)
    )
    assert result["status"] == "prepared" and len(result["handoff_id"]) == 64
    directory, request, bones = retarget.verify_capture_preparation(settings, result["handoff_id"])
    assert request["frames"] == 4 and request["fps"] == 30 and len(bones) == 24
    assert retarget._read_bvh(directory / "input.bvh")["frames"] == 4
    assert retarget.prepare_capture_character_handoff(
        settings, exported["job_id"], "3d_models/take.bvh", options(start_frame=1, end_frame=5)
    )["reused"]
    other = retarget.prepare_capture_character_handoff(
        settings, exported["job_id"], "3d_models/take.bvh", options(), attempt=1
    )
    assert other["handoff_id"] != result["handoff_id"]
    assert before == (bvh.read_bytes(), target.read_bytes())


@pytest.mark.parametrize(
    "filename", ["input.bvh", "character_helper.py", "capture_character_handoff.py", "handoff.json"]
)
def test_preparation_tampering_is_rejected(saved, filename):
    settings, exported, *_ = saved
    result = retarget.prepare_capture_character_handoff(
        settings, exported["job_id"], "3d_models/take.bvh", options()
    )
    path = Path(result["directory"]) / filename
    if filename == "handoff.json":
        value = json.loads(path.read_text())
        value["options"]["root_motion"] = False
        path.write_text(json.dumps(value))
    else:
        path.write_text(path.read_text() + " ")
    with pytest.raises(ValueError):
        retarget.verify_capture_preparation(settings, result["handoff_id"])


def test_source_and_original_receipt_tampering_blocks_reuse(saved):
    settings, exported, _, bvh, _ = saved
    result = retarget.prepare_capture_character_handoff(
        settings, exported["job_id"], "3d_models/take.bvh", options()
    )
    bvh.write_text(bvh.read_text() + " ")
    with pytest.raises(ValueError):
        retarget.get_capture_character_report(settings, result["handoff_id"])


@pytest.mark.parametrize(
    "change",
    [
        lambda s: s.replace("forearm.L", "unknown.L"),
        lambda s: s.replace("Frames: 6", "Frames: 7"),
        lambda s: s.replace("0.0", "nan", 1),
        lambda s: s.replace("Frame Time: 0.0333333333333", "Frame Time: 0.0333667000334"),
    ],
)
def test_bvh_requires_known_complete_finite_rig(tmp_path, change):
    path = tmp_path / "clip.bvh"
    path.write_text(change(synthetic_bvh()))
    with pytest.raises(ValueError):
        retarget._read_bvh(path)


@pytest.mark.parametrize("fps", [24, 30, 60, 120])
def test_bvh_preserves_supported_integer_timing(tmp_path, fps):
    path = tmp_path / "clip.bvh"
    path.write_text(synthetic_bvh(fps=fps))
    assert retarget._read_bvh(path)["fps"] == fps


def test_smoothing_windows_noop_sign_equivalence_and_original_immutable():
    q = np.zeros((8, 2, 4))
    q[:, :, 0] = 1
    q[2:4, 0] *= -1
    root = np.zeros((8, 3))
    root[4, 0] = 2
    before = q.copy(), root.copy()
    value = retarget._resolved(
        options(
            start_frame=10,
            end_frame=18,
            smoothing_start_frame=12,
            smoothing_end_frame=16,
            rotation_smoothing_radius=2,
            root_smoothing_radius=1,
        ),
        20,
    )
    clean, position, report = cleanup_motion(q, root, ["Root", "Hips"], value)
    assert np.allclose(np.abs(clean[:, :, 0]), 1)
    assert np.array_equal(position[:2], root[:2]) and np.array_equal(position[6:], root[6:])
    assert position[4, 0] < root[4, 0] and report["max_root_change_m"] > 0
    assert np.array_equal(q, before[0]) and np.array_equal(root, before[1])


def test_correction_uses_local_xyz_and_stays_inside_original_window():
    q = np.zeros((6, 2, 4))
    q[:, :, 0] = 1
    root = np.zeros((6, 3))
    value = retarget._resolved(
        options(
            joint_corrections=[
                {
                    "bone": "Hips",
                    "rotation_degrees": [30, 0, 0],
                    "start_frame": 2,
                    "end_frame": 4,
                    "blend_frames": 0,
                }
            ]
        ),
        6,
    )
    changed, _, report = cleanup_motion(q, root, ["Root", "Hips"], value)
    assert np.array_equal(changed[:2], q[:2]) and np.array_equal(changed[4:], q[4:])
    assert np.allclose(changed[2, 1], [np.cos(np.pi / 12), np.sin(np.pi / 12), 0, 0])
    assert report["max_rotation_change_degrees"] == pytest.approx(30)


def test_no_output_overwrite_or_uncertain_automatic_retry(saved):
    settings, exported, *_ = saved
    result = retarget.prepare_capture_character_handoff(
        settings, exported["job_id"], "3d_models/take.bvh", options()
    )
    (Path(result["directory"]) / "bridge-dispatch.json").write_text("{}")
    repeated = retarget.prepare_capture_character_handoff(
        settings, exported["job_id"], "3d_models/take.bvh", options()
    )
    assert repeated["status"] == "outcome_unknown" and repeated["reused"]
