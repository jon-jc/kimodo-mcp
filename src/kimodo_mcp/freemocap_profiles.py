"""Reviewed landmark ordering and original display graphs for saved FreeMoCap data.

Names, counts and virtual-point ordering are format facts from the pinned
SkellyForge model definitions, not an imported tracker implementation. A graph
connects measured landmarks for inspection; it does not supply local rotations,
an articulated rig, anatomical acceptance or a retarget mapping.
"""

from typing import Literal

Detector = Literal["mediapipe", "rtmpose"]
Aspect = Literal["body", "left_hand", "right_hand", "face"]

FREEMOCAP_REVISION = "700c8614d11d886dacfea8e52333d3c0b942ef52"
SKELLYFORGE_REVISION = "b96272b1ef06546c1a55d404a5f4a92f1fad1b0f"
_BASE = f"https://github.com/freemocap/skellyforge/blob/{SKELLYFORGE_REVISION}/skellyforge/skellymodels"

_BODY_NAMES = {
    "mediapipe": (
        "nose", "left_eye_inner", "left_eye", "left_eye_outer", "right_eye_inner",
        "right_eye", "right_eye_outer", "left_ear", "right_ear", "mouth_left", "mouth_right",
        "left_shoulder", "right_shoulder", "left_elbow", "right_elbow", "left_wrist", "right_wrist",
        "left_pinky", "right_pinky", "left_index", "right_index", "left_thumb", "right_thumb",
        "left_hip", "right_hip", "left_knee", "right_knee", "left_ankle", "right_ankle",
        "left_heel", "right_heel", "left_foot_index", "right_foot_index",
    ),
    "rtmpose": (
        "nose", "left_eye", "right_eye", "left_ear", "right_ear", "left_shoulder", "right_shoulder",
        "left_elbow", "right_elbow", "left_wrist", "right_wrist", "left_hip", "right_hip",
        "left_knee", "right_knee", "left_ankle", "right_ankle", "left_big_toe", "left_small_toe",
        "left_heel", "right_big_toe", "right_small_toe", "right_heel",
    ),
}
_VIRTUAL_NAMES = ("head_center", "neck_center", "trunk_center", "hips_center")
_VIRTUAL_COMPONENTS = (
    (("left_ear", "right_ear"), (0.5, 0.5)),
    (("left_shoulder", "right_shoulder"), (0.5, 0.5)),
    (("left_shoulder", "right_shoulder", "left_hip", "right_hip"), (0.25, 0.25, 0.25, 0.25)),
    (("left_hip", "right_hip"), (0.5, 0.5)),
)
_HAND_NAMES = {
    "mediapipe": (
        "wrist", "thumb_cmc", "thumb_mcp", "thumb_ip", "thumb_tip",
        "index_finger_mcp", "index_finger_pip", "index_finger_dip", "index_finger_tip",
        "middle_finger_mcp", "middle_finger_pip", "middle_finger_dip", "middle_finger_tip",
        "ring_finger_mcp", "ring_finger_pip", "ring_finger_dip", "ring_finger_tip",
        "pinky_mcp", "pinky_pip", "pinky_dip", "pinky_tip",
    ),
    "rtmpose": (
        "hand_root", "thumb1", "thumb2", "thumb3", "thumb4",
        "forefinger1", "forefinger2", "forefinger3", "forefinger4",
        "middle_finger1", "middle_finger2", "middle_finger3", "middle_finger4",
        "ring_finger1", "ring_finger2", "ring_finger3", "ring_finger4",
        "pinky_finger1", "pinky_finger2", "pinky_finger3", "pinky_finger4",
    ),
}
_FACE_COUNTS = {"mediapipe": 136, "rtmpose": 68}


def _body(detector: Detector) -> dict:
    tracked = _BODY_NAMES[detector]
    names = [*tracked, *_VIRTUAL_NAMES]
    indices = {name: index for index, name in enumerate(names)}
    parent_names = {name: "head_center" for name in tracked[:11 if detector == "mediapipe" else 5]}
    parent_names.update({"trunk_center": "hips_center", "neck_center": "trunk_center",
                         "head_center": "neck_center"})
    segments = [
        {"name": "head", "proximal": indices["left_ear"], "distal": indices["right_ear"]},
        {"name": "neck", "proximal": indices["head_center"], "distal": indices["neck_center"]},
        {"name": "spine", "proximal": indices["neck_center"], "distal": indices["hips_center"]},
    ]
    for side in ("left", "right"):
        foot = f"{side}_foot_index" if detector == "mediapipe" else f"{side}_big_toe"
        parent_names.update({
            f"{side}_shoulder": "neck_center", f"{side}_elbow": f"{side}_shoulder",
            f"{side}_wrist": f"{side}_elbow", f"{side}_hip": "hips_center",
            f"{side}_knee": f"{side}_hip", f"{side}_ankle": f"{side}_knee",
            f"{side}_heel": f"{side}_ankle", foot: f"{side}_ankle",
        })
        connection_names = [
            ("shoulder", "neck_center", f"{side}_shoulder"),
            ("upper_arm", f"{side}_shoulder", f"{side}_elbow"),
            ("forearm", f"{side}_elbow", f"{side}_wrist"),
            ("pelvis", "hips_center", f"{side}_hip"),
            ("thigh", f"{side}_hip", f"{side}_knee"),
            ("shank", f"{side}_knee", f"{side}_ankle"),
            ("foot", f"{side}_ankle", foot),
            ("heel", f"{side}_ankle", f"{side}_heel"),
            ("foot_bottom", f"{side}_heel", foot),
        ]
        if detector == "mediapipe":
            for finger in ("pinky", "index", "thumb"):
                parent_names[f"{side}_{finger}"] = f"{side}_wrist"
            connection_names.append(("hand", f"{side}_wrist", f"{side}_index"))
        else:
            parent_names[f"{side}_small_toe"] = f"{side}_ankle"
            connection_names.append(("toes", f"{side}_ankle", f"{side}_small_toe"))
        segments.extend({"name": f"{side}_{name}", "proximal": indices[first], "distal": indices[last]}
                        for name, first, last in connection_names)
    parents = [indices[parent_names[name]] if name in parent_names else -1 for name in names]
    return {
        "names": names, "parents": parents,
        "edges": [[parent, child] for child, parent in enumerate(parents) if parent >= 0],
        "segments": segments, "tracked_flags": [True] * len(tracked) + [False] * 4,
        "virtual_markers": [
            {"name": name, "index": indices[name], "source_indices": [indices[item] for item in members],
             "weights": list(weights)}
            for name, (members, weights) in zip(_VIRTUAL_NAMES, _VIRTUAL_COMPONENTS, strict=True)
        ],
        "graph_semantics": "reviewed_landmark_hierarchy_not_local_rotations",
        "name_semantics": "saved_aspect_landmark_names",
    }


def _hand(detector: Detector) -> dict:
    names = list(_HAND_NAMES[detector])
    # An original display tree: each of the five four-point finger chains
    # branches from the wrist. These are spatial connections, not rig rotations.
    parents = [-1] + [0 if (index - 1) % 4 == 0 else index - 1 for index in range(1, 21)]
    edges = [[parent, child] for child, parent in enumerate(parents) if parent >= 0]
    return {
        "names": names, "parents": parents, "edges": edges,
        "segments": [{"name": f"{names[first]}_to_{names[last]}", "proximal": first, "distal": last}
                     for first, last in edges],
        "tracked_flags": [True] * 21, "virtual_markers": [],
        "graph_semantics": "original_display_tree_not_source_rig_hierarchy",
        "name_semantics": "saved_aspect_landmark_names",
    }


def _face(detector: Detector) -> dict:
    count = _FACE_COUNTS[detector]
    return {
        "names": [f"face_{index:04d}" for index in range(count)],
        "parents": None, "edges": [], "segments": [],
        "tracked_flags": [True] * count, "virtual_markers": [],
        "graph_semantics": "no_verified_saved_face_topology",
        "name_semantics": "saved_output_index_not_detector_landmark_id",
    }


def get_landmark_profile(detector: Detector, aspect: Aspect) -> dict:
    """Return fresh, JSON-safe data for one exact pinned saved-output layout.

    ``tracked_flags`` separates detector-derived points from virtual averages.
    It does not assert that every frame was observed: upstream saved trajectories
    have already undergone interpolation, filtering and optional alignment.
    Units are deliberately absent; a profile cannot establish calibration scale.
    """
    if detector not in _BODY_NAMES or aspect not in {"body", "left_hand", "right_hand", "face"}:
        raise ValueError("Choose a reviewed mediapipe/rtmpose body, hand or face profile.")
    profile = _body(detector) if aspect == "body" else _face(detector) if aspect == "face" else _hand(detector)
    profile.update({
        "profile_id": f"skellyforge-{SKELLYFORGE_REVISION[:12]}-{detector}-{aspect}-v1",
        "detector": detector, "aspect": aspect, "count": len(profile["names"]),
        "source_revision": SKELLYFORGE_REVISION, "freemocap_revision": FREEMOCAP_REVISION,
        "sources": [f"{_BASE}/tracker_info/{detector}_model_info.yaml",
                    f"{_BASE}/models/trajectory.py", f"{_BASE}/managers/actor.py"],
        "notes": [
            "Detector-derived labels are not per-frame visibility or confidence evidence.",
            "Saved trajectories may be interpolated, filtered, ground-aligned or wrist-adjusted upstream.",
            "Profiles do not infer coordinate units, joint rotations, contacts or retarget compatibility.",
        ],
    })
    if aspect == "face":
        profile["notes"].append(
            "Face labels identify saved array columns; they are not full MediaPipe mesh IDs, "
            "the separate RTMPose 106-point detector, facial blendshapes or a rig."
        )
    return profile
