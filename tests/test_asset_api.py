"""Model import and prop/rigging boundaries shared by Studio and MCP clients."""

import asyncio
import json
import struct
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest
from starlette.testclient import TestClient

from kimodo_mcp import asset_api, props, studio_assets, unirig, unirig_api
from kimodo_mcp.config import Settings
from kimodo_mcp.handoff_scripts import asset_preview
from kimodo_mcp.server import create_server
from kimodo_mcp.studio import create_app


def glb_bytes(value=None):
    payload = json.dumps({"asset": {"version": "2.0"}} if value is None else value).encode()
    payload += b" " * (-len(payload) % 4)
    return struct.pack("<4sIIII", b"glTF", 2, 20 + len(payload), len(payload), 0x4E4F534A) + payload


@pytest.fixture
def settings(tmp_path):
    return Settings(data_dir=tmp_path / "data", python=sys.executable)


@pytest.fixture
def client(settings, tmp_path):
    web = tmp_path / "web"
    web.mkdir()
    (web / "index.html").write_text("<html>fixture</html>")
    with TestClient(create_app(settings, web), base_url="http://127.0.0.1:8768") as browser:
        token = browser.get("/api/state").json()["csrf_token"]
        yield browser, {"x-studio-token": token}


async def call(server, name, arguments):
    result = await server.call_tool(name, arguments)
    assert isinstance(result, tuple), "All shared operations must expose structured MCP results"
    return result[1]


def test_upload_source_mcp_and_scene_share_the_same_immutable_asset(client, settings, monkeypatch):
    browser, headers = client
    monkeypatch.setattr(studio_assets, "run_blender", lambda *_: pytest.fail("Implicit Blender launch"))
    contents = glb_bytes()
    response = browser.post("/api/assets/import?extension=glb&label=Sword&kind=prop", content=contents, headers=headers)
    assert response.status_code == 200, response.text
    asset = response.json()
    identifier = asset["asset_id"]
    server = create_server(settings)
    recovered = asyncio.run(call(server, "get_studio_asset", {"asset_id": identifier}))
    assert recovered["label"] == "Sword" and recovered["sha256"] == identifier
    assert asyncio.run(call(server, "list_studio_assets", {}))["assets"] == [recovered]
    scene = {"request": {"title": "Right hand sword", "attachments": [{"asset_id": identifier, "bone": "RightHand"}]}}
    saved = browser.post("/api/actions/save_prop_scene", json=scene, headers=headers)
    assert saved.status_code == 200, saved.text
    scene_id = saved.json()["result"]["scene_id"]
    assert asyncio.run(call(server, "get_prop_scene", {"scene_id": scene_id}))["request"]["title"] == "Right hand sword"
    assert asyncio.run(call(server, "list_prop_scenes", {}))["total"] == 1
    assert asyncio.run(call(server, "list_prop_scene_handoffs", {}))["total"] == 0
    download = browser.get(f"/api/assets/{identifier}/source")
    assert download.content == contents and download.headers["content-type"] == "model/gltf-binary"
    assert download.headers["x-content-type-options"] == "nosniff"
    assert download.headers["cache-control"] == "no-store"
    assert browser.get(f"/api/assets/{identifier}/preview/0").status_code == 404
    assert asyncio.run(call(server, "get_studio_asset_preview", {"asset_id": identifier}))["status"] == "not_prepared"
    assert not (settings.data_dir / "asset-previews").exists()
    assert not list((settings.data_dir / "asset-uploads").iterdir())
    Path(recovered["absolute_path"]).write_bytes(b"changed source")
    assert browser.get(f"/api/assets/{identifier}/source").status_code == 404
    with pytest.raises(Exception, match="changed"):
        asyncio.run(call(server, "get_prop_scene", {"scene_id": scene_id}))


def test_upload_requires_token_local_origin_bounded_size_and_unambiguous_parameters(client, settings, monkeypatch):
    browser, headers = client
    route = "/api/assets/import?extension=obj&label=Blade&kind=prop"
    assert browser.post(route, content=b"v 0 0 0").status_code == 403
    assert browser.post(route, content=b"v 0 0 0", headers=headers | {"origin": "https://evil.example"}).status_code == 403
    for suffix in ("&extension=glb", "&extra=ignored", "&kind=character"):
        assert browser.post(route + suffix, content=b"v 0 0 0", headers=headers).status_code == 400
    assert browser.post("/api/assets/import?extension=blend&label=Bad&kind=prop", content=b"blend", headers=headers).status_code == 400
    assert browser.post(route, content=b"", headers=headers).status_code == 400
    monkeypatch.setattr(studio_assets, "MAX_BYTES", 8)
    assert browser.post(route, content=b"x" * 9, headers=headers).status_code == 400
    assert browser.post(route, content=b"v 0 0 0", headers=headers).status_code == 200
    assert not list((settings.data_dir / "asset-uploads").iterdir())


@pytest.mark.parametrize("value", [[], 2, {"images": [None]}, {"images": "x"}, {"images": [{"uri": None}]}, {"nodes": {}}, {"buffers": [{"uri": "file:///secret"}]}])
def test_malformed_glb_upload_is_a_validation_error_not_server_failure(client, settings, value):
    browser, headers = client
    response = browser.post("/api/assets/import?extension=glb&label=Bad&kind=prop", content=glb_bytes(value), headers=headers)
    assert response.status_code == 400
    assert not (settings.data_dir / "assets").exists()
    assert not list((settings.data_dir / "asset-uploads").iterdir())


def test_truncated_glb_chunk_header_is_a_validation_error(client):
    browser, headers = client
    response = browser.post("/api/assets/import?extension=glb&label=Bad&kind=prop", content=struct.pack("<4sII", b"glTF", 2, 12), headers=headers)
    assert response.status_code == 400


def test_obj_import_omits_material_directives_with_all_whitespace(tmp_path, monkeypatch):
    source = tmp_path / "source.obj"
    source.write_text("mtllib\tprivate.mtl\n  mtllib  other.mtl\nusemtl\tsecret\n\nv 0 0 0\nv 1 0 0\nf 1 2 1\n")
    imported = []
    objects = []

    class Mesh:
        type = "MESH"
        data = SimpleNamespace(vertices=[1, 2])

    def load(filepath):
        imported.append(Path(filepath).read_text())
        objects.append(Mesh())

    monkeypatch.setitem(sys.modules, "bpy", SimpleNamespace(data=SimpleNamespace(objects=objects), ops=SimpleNamespace(wm=SimpleNamespace(obj_import=load))))
    assert len(asset_preview.import_asset(source, "obj", tmp_path)) == 1
    assert imported == ["\nv 0 0 0\nv 1 0 0\nf 1 2 1\n"]
    assert "mtllib" in source.read_text(), "Source model must remain unchanged"


async def test_typed_prop_prepare_execute_and_rigging_controls(settings, monkeypatch):
    server = create_server(settings)
    calls = []
    monkeypatch.setattr(props, "prepare_prop_scene_handoff", lambda settings, scene, source, attempt: calls.append((scene, source.model_dump(), attempt)) or {"handoff_id": "d" * 64, "status": "prepared"})
    monkeypatch.setattr(props, "execute_prop_scene_handoff", lambda settings, handoff: {"handoff_id": handoff, "status": "completed"})
    body = {"scene_id": "a" * 64, "source": {"kind": "kimodo", "job_id": "b" * 32, "sample_index": 2, "attempt": 1, "variant_id": "c" * 64}, "attempt": 3}
    assert (await call(server, "prepare_prop_scene_handoff", body))["status"] == "prepared"
    assert calls == [(body["scene_id"], props.PropMotionSource.model_validate(body["source"]).model_dump(), 3)]
    assert (await call(server, "execute_prop_scene_handoff", {"handoff_id": "d" * 64}))["status"] == "completed"
    for bad in (True, -1, 100, "1"):
        with pytest.raises(Exception):
            await call(server, "prepare_prop_scene_handoff", body | {"attempt": bad})
    assert len(calls) == 1
    requests = []
    monkeypatch.setattr(unirig, "plan_unirig", lambda settings, request: requests.append(request.model_dump()) or {"ready": True})
    monkeypatch.setattr(unirig, "start_unirig", lambda settings, request: requests.append(request.model_dump()) or {"id": "e" * 32, "status": "queued"})
    controls = {"operation": "pipeline", "assets": [{"asset_id": "a" * 64}], "num_beams": 4, "top_k": 3, "top_p": 0.7, "temperature": 1.1, "repetition_penalty": 2.0, "seed": 42, "add_root": True, "skin_backend": "open3d", "output_format": "fbx", "faces_target_count": 12000}
    assert (await call(server, "plan_unirig", {"request": controls}))["ready"]
    assert (await call(server, "start_unirig", {"request": controls}))["status"] == "queued"
    assert requests == [unirig.UniRigRequest.model_validate(controls).model_dump()] * 2
    with pytest.raises(Exception):
        await call(server, "start_unirig", {"request": controls | {"task": "arbitrary.yaml"}})
    assert len(requests) == 2


def test_registered_unirig_output_is_copied_verified_and_does_not_select_character(client, settings, tmp_path, monkeypatch):
    browser, headers = client
    output = tmp_path / "rig.glb"
    output.write_bytes(glb_bytes())
    calls = []

    def verified(settings, job_id, name):
        calls.append((job_id, name))
        if (job_id, name) != ("a" * 32, "outputs/item_00/rig.glb"):
            raise ValueError("Not a manifest output")
        return output

    monkeypatch.setattr(unirig, "get_unirig_artifact", verified)
    arguments = {"job_id": "a" * 32, "name": "outputs/item_00/rig.glb", "label": "Rigged model"}
    saved = browser.post("/api/actions/register_unirig_output", json=arguments, headers=headers)
    assert saved.status_code == 200, saved.text
    value = saved.json()["result"]
    assert value["registered_character_changed"] is False and value["kind"] == "character"
    assert Path(value["absolute_path"]).read_bytes() == output.read_bytes()
    assert len(calls) == 2, "Producer must be reverified after copying"
    assert not (settings.data_dir / "character").exists()
    artifact = asyncio.run(call(create_server(settings), "get_unirig_artifact", {"job_id": arguments["job_id"], "name": arguments["name"]}))
    assert artifact["sha256"] == value["asset_id"]
    route = f"/api/unirig/{arguments['job_id']}/files/"
    assert browser.get(route + arguments["name"]).content == output.read_bytes()
    assert browser.get(route + "request.json").status_code == 404
    output.write_bytes(glb_bytes({"images": [{"uri": "https://remote/secret"}]}))
    assert browser.get(route + arguments["name"]).status_code == 404


async def test_catalog_annotations_distinguish_reads_and_explicit_execution(settings):
    tools = {tool.name: tool for tool in await create_server(settings).list_tools()}
    for name in asset_api.TOOL_NAMES | unirig_api.TOOL_NAMES:
        assert tools[name].outputSchema is not None
        assert tools[name].annotations.openWorldHint is False
    for name in ("get_studio_asset_preview", "get_prop_scene_handoff", "get_unirig_job", "plan_unirig"):
        assert tools[name].annotations.readOnlyHint is True
    for name in ("build_studio_asset_preview", "execute_prop_scene_handoff", "start_unirig"):
        assert tools[name].annotations.readOnlyHint is False
        assert tools[name].annotations.idempotentHint is False


def test_preview_route_serves_only_verified_completed_conversion(client, settings, tmp_path, monkeypatch):
    browser, headers = client
    source = tmp_path / "model.glb"
    source.write_bytes(glb_bytes())
    asset = studio_assets.import_studio_asset(settings, str(source), "Model")
    calls = []

    def convert(script, request_path, directory):
        calls.append(script)
        request = studio_assets.read_json(request_path)
        output = directory / "preview.glb"
        output.write_bytes(glb_bytes({"asset": {"version": "2.0"}, "extras": "converted fixture"}))
        studio_assets.atomic_json(directory / "report.json", {
            "schema_version": 1, "status": "completed", "asset_id": asset["asset_id"],
            "script_sha256": request["script_sha256"], "sha256": studio_assets.digest(output),
        })

    monkeypatch.setattr(studio_assets, "run_blender", convert)
    arguments = {"asset_id": asset["asset_id"], "attempt": 1}
    built = browser.post("/api/actions/build_studio_asset_preview", json=arguments, headers=headers)
    assert built.status_code == 200 and len(calls) == 1
    result = built.json()["result"]
    route = f"/api/assets/{asset['asset_id']}/preview/1"
    assert browser.get(route).content == Path(result["absolute_path"]).read_bytes()
    assert browser.get(f"/api/assets/{asset['asset_id']}/preview/0").status_code == 404
    assert browser.get(f"/api/assets/{asset['asset_id']}/preview/bad").status_code == 404
    Path(result["absolute_path"]).write_bytes(b"changed")
    assert browser.get(route).status_code == 404
    assert len(calls) == 1, "File reads must never run converters"


def test_prop_artifacts_require_completed_bound_receipt_and_exact_filename(client, settings, tmp_path, monkeypatch):
    browser, headers = client
    source = tmp_path / "sword.obj"
    source.write_text("v 0 0 0\nv 1 0 0\nv 0 1 0\nf 1 2 3\n")
    asset = studio_assets.import_studio_asset(settings, str(source), "Sword")
    scene = props.save_prop_scene(settings, {"title": "Scene", "attachments": [{"asset_id": asset["asset_id"]}]})
    motion = {"source": {"kind": "kimodo", "job_id": "a" * 32}, "frames": 12, "fps": 30, "blend_sha256": "b" * 64}
    monkeypatch.setattr(props, "_motion", lambda *_: dict(motion))
    prepared = props.prepare_prop_scene_handoff(settings, scene["scene_id"], motion["source"])
    identifier = prepared["handoff_id"]
    directory = settings.data_dir / "prop-handoffs" / identifier
    route = f"/api/prop-scenes/{identifier}/files/"
    assert browser.get(route + "scene-preview.glb").status_code == 404
    outputs = {}
    for name in ("scene.blend", "scene-preview.glb", "attachment-recipe.json", "prop_00.fbx"):
        path = directory / name
        path.write_bytes(glb_bytes() if path.suffix == ".glb" else b"synthetic protocol fixture")
        outputs[name] = studio_assets.digest(path)
    studio_assets.atomic_json(directory / "report.json", {"schema_version": 1, "handoff_id": identifier, "status": "completed", "animation_preserved": True, "character_bones": list(props.landmarks()), "frames": 12, "fps": 30, "outputs": outputs})
    assert browser.get(route + "scene-preview.glb").content == glb_bytes()
    download = browser.get(route + "prop_00.fbx")
    assert download.status_code == 200 and "attachment" in download.headers["content-disposition"]
    for hidden in ("handoff.json", "report.json", "dispatch.json", "../report.json"):
        assert browser.get(route + hidden).status_code == 404
    body = {"handoff_id": identifier, "path": "scene.blend"}
    artifact = browser.post("/api/actions/get_prop_scene_artifact", json=body, headers=headers)
    assert artifact.status_code == 200 and artifact.json()["result"]["sha256"] == outputs["scene.blend"]
    (directory / "attachment-recipe.json").write_bytes(b"tampered recipe")
    assert browser.get(route + "scene-preview.glb").status_code == 404
