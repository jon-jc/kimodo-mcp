import pytest

from kimodo_mcp import props
from kimodo_mcp.config import Settings
from kimodo_mcp.studio_assets import atomic_json, import_studio_asset, read_json


@pytest.fixture
def fixture(tmp_path):
    settings = Settings(data_dir=tmp_path / "workspace", python="python")
    source = tmp_path / "weapon.obj"
    source.write_text("v 0 0 0\nv 1 0 0\nv 0 1 0\nf 1 2 3\n")
    asset = import_studio_asset(settings, str(source), "Blade")
    request = {"title": "Sword scene", "attachments": [{"asset_id": asset["asset_id"], "bone": "RightHand"}]}
    return settings, asset, request


def test_scene_immutable_dedup_and_asset_binding(fixture):
    settings, asset, request = fixture
    first = props.save_prop_scene(settings, request)
    assert props.save_prop_scene(settings, request) == first
    changed = request | {"title": "Other"}
    assert props.save_prop_scene(settings, changed)["scene_id"] != first["scene_id"]
    receipt = settings.data_dir / "prop-scenes" / first["scene_id"] / "scene.json"
    value = read_json(receipt)
    value["request"]["attachments"][0]["bone"] = "Head"
    atomic_json(receipt, value)
    with pytest.raises(ValueError, match="changed"):
        props.get_prop_scene(settings, first["scene_id"])


def test_scene_limits_and_metadata_history(fixture):
    settings, _, request = fixture
    with pytest.raises(ValueError):
        props.save_prop_scene(settings, request | {"attachments": request["attachments"] * 17})
    saved = props.save_prop_scene(settings, request)
    result = props.list_prop_scenes(settings)
    assert result["scenes"] == [
        {
            "scene_id": saved["scene_id"],
            "verification": "metadata_only",
            "title": request["title"],
            "attachments": 1,
        }
    ]
    assert props.list_prop_scene_handoffs(settings)["handoffs"] == []


@pytest.mark.parametrize(
    "change",
    [
        {"bone": "unknown"},
        {"scale": [0, 1, 1]},
        {"scale": [-1, 1, 1]},
        {"rotation_degrees": [361, 0, 0]},
        {"translation_m": [float("nan"), 0, 0]},
        {"translation_m": [True, 0, 0]},
        {"unexpected": "value"},
    ],
)
def test_attachment_bounds(fixture, change):
    _, asset, _ = fixture
    with pytest.raises(ValueError):
        props.PropAttachment.model_validate({"asset_id": asset["asset_id"], **change})


def test_source_selectors_strict():
    assert props.PropMotionSource(kind="kimodo", job_id="a" * 32).sample_index == 0
    assert props.PropMotionSource(kind="capture_character", capture_handoff_id="a" * 64)
    for value in [
        {"kind": "kimodo"},
        {"kind": "capture_character", "capture_handoff_id": "a" * 64, "job_id": "b" * 32},
        {"kind": "kimodo", "job_id": "a" * 32, "sample_index": True},
    ]:
        with pytest.raises(ValueError):
            props.PropMotionSource.model_validate(value)


def test_preparation_binds_source_and_refuses_uncertain_retry(fixture, monkeypatch):
    settings, _, request = fixture
    scene = props.save_prop_scene(settings, request)
    motion = {
        "source": {"kind": "kimodo", "job_id": "a" * 32},
        "frames": 12,
        "fps": 30,
        "blend_sha256": "b" * 64,
    }
    monkeypatch.setattr(props, "_motion", lambda *_: dict(motion))
    prepared = props.prepare_prop_scene_handoff(settings, scene["scene_id"], motion["source"])
    assert prepared["status"] == "prepared"
    directory = settings.data_dir / "prop-handoffs" / prepared["handoff_id"]
    atomic_json(directory / "dispatch.json", {})
    assert props.get_prop_scene_handoff(settings, prepared["handoff_id"])["status"] == "outcome_unknown"
    with pytest.raises(ValueError, match="dispatched"):
        props.execute_prop_scene_handoff(settings, prepared["handoff_id"])
    motion["blend_sha256"] = "c" * 64
    with pytest.raises(ValueError, match="changed"):
        props.get_prop_scene_handoff(settings, prepared["handoff_id"])


def test_character_asset_cannot_be_used_as_prop(tmp_path):
    settings = Settings(data_dir=tmp_path / "workspace", python="python")
    source = tmp_path / "body.obj"
    source.write_text("v 0 0 0")
    asset = import_studio_asset(settings, str(source), "Body", "character")
    with pytest.raises(ValueError, match="registered as props"):
        props.save_prop_scene(settings, {"title": "Bad", "attachments": [{"asset_id": asset["asset_id"]}]})
