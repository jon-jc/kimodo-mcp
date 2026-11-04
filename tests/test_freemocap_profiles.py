import json

import pytest

from kimodo_mcp.freemocap_profiles import SKELLYFORGE_REVISION, get_landmark_profile


@pytest.mark.parametrize("detector,body_count,face_count", [("mediapipe", 37, 136), ("rtmpose", 27, 68)])
def test_exact_saved_layout_and_virtual_order(detector, body_count, face_count):
    body = get_landmark_profile(detector, "body")
    assert len(body["names"]) == body_count
    assert body["names"][-4:] == ["head_center", "neck_center", "trunk_center", "hips_center"]
    assert body["tracked_flags"] == [True] * (body_count - 4) + [False] * 4
    assert body["parents"].count(-1) == 1
    assert body["parents"][-1] == -1
    assert body["names"][body["parents"][0]] == "head_center"
    for virtual in body["virtual_markers"]:
        assert sum(virtual["weights"]) == 1.0
        assert all(body["tracked_flags"][index] for index in virtual["source_indices"])
    assert get_landmark_profile(detector, "face")["count"] == face_count


@pytest.mark.parametrize("detector", ["mediapipe", "rtmpose"])
@pytest.mark.parametrize("aspect", ["body", "left_hand", "right_hand", "face"])
def test_profile_graphs_are_bounded_acyclic_and_json_safe(detector, aspect):
    profile = get_landmark_profile(detector, aspect)
    names = profile["names"]
    assert len(set(names)) == len(names)
    assert profile["source_revision"] == SKELLYFORGE_REVISION
    assert len(profile["tracked_flags"]) == len(names)
    for first, last in profile["edges"]:
        assert 0 <= first < len(names) and 0 <= last < len(names) and first != last
    for segment in profile["segments"]:
        assert 0 <= segment["proximal"] < len(names)
        assert 0 <= segment["distal"] < len(names)
    if profile["parents"] is not None:
        for index in range(len(names)):
            visited = set()
            while index >= 0:
                assert index not in visited
                visited.add(index)
                index = profile["parents"][index]
    json.dumps(profile, allow_nan=False)


def test_saved_face_indices_are_not_mislabeled_as_detector_ids():
    face = get_landmark_profile("mediapipe", "face")
    assert face["names"][1] == "face_0001"  # Detector contour index 1 instead names face_0007.
    assert face["names"][-1] == "face_0135"
    assert face["parents"] is None and face["edges"] == []
    assert face["name_semantics"] == "saved_output_index_not_detector_landmark_id"
    assert get_landmark_profile("rtmpose", "face")["names"][-1] == "face_0067"


def test_hand_output_names_and_display_graph_do_not_claim_rig_rotations():
    hand = get_landmark_profile("rtmpose", "left_hand")
    assert hand["count"] == 21 and hand["names"][0] == "hand_root"
    assert [hand["parents"][index] for index in (1, 5, 9, 13, 17)] == [0] * 5
    assert hand["graph_semantics"] == "original_display_tree_not_source_rig_hierarchy"
    assert get_landmark_profile("mediapipe", "right_hand")["names"][0] == "wrist"


def test_calls_do_not_share_mutable_profile_state():
    first = get_landmark_profile("mediapipe", "body")
    first["names"][0] = "changed"
    first["segments"][0]["name"] = "changed"
    first["virtual_markers"][0]["weights"][0] = 0
    first["notes"].clear()
    again = get_landmark_profile("mediapipe", "body")
    assert again["names"][0] == "nose" and again["segments"][0]["name"] == "head"
    assert again["virtual_markers"][0]["weights"] == [0.5, 0.5]
    assert again["notes"]


@pytest.mark.parametrize("detector,aspect", [("unknown", "body"), ("mediapipe", "wholebody"), ("MediaPipe", "body")])
def test_unknown_layout_is_rejected(detector, aspect):
    with pytest.raises(ValueError, match="reviewed"):
        get_landmark_profile(detector, aspect)
