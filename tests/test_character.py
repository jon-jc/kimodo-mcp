"""Pure rig-profile checks; Blender roundtrip evidence is tested separately."""

import numpy as np
import pytest

from kimodo_mcp.handoff_scripts import character as module


def test_hierarchy_is_connected_and_lengths_positive():
    bones = module.landmarks()
    assert len(bones) == 24
    assert [name for name, (_, _, parent) in bones.items() if parent is None] == ["Root"]
    seen = set()
    for name, (head, tail, parent) in bones.items():
        assert parent is None or parent in seen
        assert np.linalg.norm(np.array(tail) - head) > 1e-4
        seen.add(name)


@pytest.mark.parametrize("category", ["body", "head", "sleeve", "skirt"])
def test_all_skin_regions_are_normalized_finite_and_bound(category):
    bones = module.landmarks()
    for x in np.linspace(-0.5, 0.5, 31):
        for z in np.linspace(0, 1, 51):
            weights = module.weights_for_vertex((x, -0.06, z), category)
            assert set(weights) <= set(bones) - {"Root"}
            assert all(np.isfinite(w) and 0 <= w <= 1 for w in weights.values())
            assert abs(sum(weights.values()) - 1) < 1e-10
            assert len(weights) <= 2


def test_hair_has_no_nearby_limb_influence():
    assert module.part_category(np.array([[-0.2, -0.06, 0.44], [0.02, 0.16, 0.92]])) == "head"
    assert module.weights_for_vertex((-0.2, 0.1, 0.44), "head") == {"Head": 1}


def test_mirrored_arms_map_to_matching_sides():
    assert set(module.weights_for_vertex((0.3, -0.05, 0.8), "sleeve")) == {"LeftForeArm", "LeftHand"}
    assert set(module.weights_for_vertex((-0.3, -0.05, 0.8), "sleeve")) == {"RightForeArm", "RightHand"}


def test_rejects_other_target_before_blender_import(tmp_path):
    other = tmp_path / "other.fbx"
    other.write_bytes(b"not the approved FBX")
    with pytest.raises(ValueError, match="inspected character"):
        module.validate_target(other)
