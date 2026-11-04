"""Immutable, content-addressed local assets and explicit isolated preview conversion."""

from __future__ import annotations

import hashlib
import itertools
import json
import os
import re
import shutil
import struct
import subprocess
from pathlib import Path

from filelock import FileLock

from .jobs import atomic_json

MAX_BYTES = 512 * 1024 * 1024
FORMATS = {"glb", "fbx", "obj", "vrm"}
NOTES = [
    "Source bytes are preserved. Re-importing the same bytes retains the first label and kind.",
    "External texture/material sidecars are not copied into the library; prefer embedded materials. OBJ previews omit material directives.",
    "Preview geometry is not a rigging or animation-compatibility certification. VRM behavior is not preserved.",
]


def digest(path):
    result = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            result.update(block)
    return result.hexdigest()


def canonical(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def read_json(path):
    path = Path(path)
    if path.is_symlink() or not path.is_file() or path.stat().st_size > 2 * 1024 * 1024:
        raise ValueError("Expected a bounded managed JSON receipt")
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError("Expected an object receipt")
    return value


def managed_directory(settings, category, identifier):
    if not isinstance(identifier, str) or not re.fullmatch(r"[0-9a-f]{64}", identifier):
        raise ValueError("Expected a lowercase SHA256 identifier")
    root = settings.data_dir.resolve() / category
    directory = root / identifier
    if root.resolve() != root or directory.resolve() != directory:
        raise ValueError("Managed asset path escapes its storage")
    return directory


def _file(path, expected=None):
    if path.is_symlink() or not path.is_file() or not 0 < path.stat().st_size <= MAX_BYTES:
        raise ValueError("Expected a regular asset of 1 byte to 512 MiB")
    checksum = digest(path)
    if expected is not None and checksum != expected:
        raise ValueError("Asset content changed")
    return checksum


def inspect_glb(path):
    """Validate self-contained GLB storage without loading textures or geometry."""
    path = Path(path)
    _file(path)
    with path.open("rb") as stream:
        header = stream.read(12)
        if len(header) != 12:
            raise ValueError("Invalid GLB header")
        magic, version, total = struct.unpack("<4sII", header)
        if magic != b"glTF" or version != 2 or total != path.stat().st_size:
            raise ValueError("Expected GLB version 2")
        chunk_header = stream.read(8)
        if len(chunk_header) != 8:
            raise ValueError("Invalid GLB JSON chunk header")
        length, kind = struct.unpack("<II", chunk_header)
        if kind != 0x4E4F534A or length > min(total - 20, 16 * 1024 * 1024):
            raise ValueError("Invalid or excessive GLB JSON")
        value = json.loads(stream.read(length))
    if not isinstance(value, dict):
        raise ValueError("GLB JSON must contain an object")
    for collection in ("buffers", "images", "meshes", "skins", "animations", "nodes"):
        records = value.get(collection, [])
        if not isinstance(records, list) or any(not isinstance(item, dict) for item in records):
            raise ValueError(f"GLB {collection} must contain a list of objects")
        if collection in {"buffers", "images"}:
            for item in records:
                if "uri" in item and (not isinstance(item["uri"], str) or not item["uri"].startswith("data:")):
                    raise ValueError("GLB external resource references are not supported")
    return {
        "meshes": len(value.get("meshes", [])),
        "skins": len(value.get("skins", [])),
        "animations": len(value.get("animations", [])),
        "nodes": len(value.get("nodes", [])),
    }


def import_studio_asset(settings, source_path, label, kind="prop"):
    if not isinstance(label, str) or not label.strip() or len(label) > 160:
        raise ValueError("Asset label must contain 1 to 160 characters")
    if kind not in {"prop", "character"}:
        raise ValueError("Asset kind must be prop or character")
    source = Path(source_path)
    if not source.is_absolute() or source.is_symlink():
        raise ValueError("Asset source must be an absolute regular file")
    source = source.resolve(strict=True)
    extension = source.suffix.lower().lstrip(".")
    if extension not in FORMATS:
        raise ValueError("Supported asset formats: GLB, FBX, OBJ, VRM")
    checksum = _file(source)
    if extension in {"glb", "vrm"}:
        inspect_glb(source)
    directory = managed_directory(settings, "assets", checksum)
    directory.parent.mkdir(parents=True, exist_ok=True)
    with FileLock(str(directory.parent / (checksum + ".lock")), timeout=10):
        if directory.exists():
            return resolve_asset(settings, checksum) | {"reused": True}
        directory.mkdir()
        destination = directory / ("source." + extension)
        shutil.copyfile(source, destination)
        _file(destination, checksum)
        _file(source, checksum)
        receipt = {
            "schema_version": 1,
            "asset_id": checksum,
            "sha256": checksum,
            "bytes": destination.stat().st_size,
            "format": extension,
            "label": label.strip(),
            "kind": kind,
            "source_name": source.name,
            "notes": NOTES,
        }
        atomic_json(directory / "asset.json", receipt)
    return resolve_asset(settings, checksum) | {"reused": False}


def resolve_asset(settings, asset_id):
    directory = managed_directory(settings, "assets", asset_id)
    value = read_json(directory / "asset.json")
    if (
        value.get("schema_version") != 1
        or value.get("asset_id") != asset_id
        or value.get("sha256") != asset_id
        or value.get("format") not in FORMATS
        or value.get("kind") not in {"prop", "character"}
        or not isinstance(value.get("label"), str)
        or not 1 <= len(value["label"]) <= 160
    ):
        raise ValueError("Invalid asset receipt")
    source = directory / ("source." + value["format"])
    _file(source, asset_id)
    if source.stat().st_size != value.get("bytes"):
        raise ValueError("Asset size changed")
    return value | {"absolute_path": str(source)}


def list_studio_assets(settings, limit=20, offset=0):
    if type(limit) is not int or not 1 <= limit <= 100 or type(offset) is not int or not 0 <= offset <= 10000:
        raise ValueError("Invalid asset pagination")
    root = settings.data_dir.resolve() / "assets"
    if root.resolve() != root:
        raise ValueError("Managed asset path escapes its storage")
    records, invalid = [], 0
    truncated = False
    if root.exists():
        entries = list(itertools.islice(root.iterdir(), 20001))
        truncated = len(entries) > 20000
        for path in sorted(entries[:20000]):
            if not re.fullmatch(r"[0-9a-f]{64}", path.name):
                continue
            try:
                # Scan small receipts, not potentially terabytes of asset data.
                if path.resolve() != path:
                    raise ValueError("Managed asset path escapes its storage")
                value = read_json(path / "asset.json")
                if value.get("asset_id") != path.name or value.get("sha256") != path.name:
                    raise ValueError("Invalid asset metadata")
                records.append(value)
                if len(records) >= 10000:
                    truncated = True
                    break
            except (OSError, ValueError, KeyError):
                invalid += 1
    selected = []
    for record in records[offset : offset + limit]:
        try:
            selected.append(resolve_asset(settings, record["asset_id"]))
        except (OSError, ValueError, KeyError):
            invalid += 1
    return {
        "assets": selected,
        "total": len(records),
        "invalid_count": invalid,
        "limit": limit,
        "offset": offset,
        "scan_truncated": truncated,
        "verification": "Returned page source bytes verified; other entries are receipt metadata only",
    }


def blender_executable():
    candidate = os.environ.get("KIMODO_BLENDER") or shutil.which("blender")
    if not candidate and os.name == "nt":
        paths = sorted(Path("C:/Program Files/Blender Foundation").glob("Blender */blender.exe"))
        candidate = str(paths[-1]) if paths else None
    if not candidate or not Path(candidate).is_file():
        raise ValueError("Configure KIMODO_BLENDER with the Blender executable path")
    return str(Path(candidate).resolve())


def run_blender(script, request_path, directory):
    command = [
        blender_executable(),
        "--background",
        "--factory-startup",
        "--disable-autoexec",
        "--python",
        str(script),
        "--",
        str(request_path),
    ]
    with (directory / "blender.log").open("xb") as stream:
        result = subprocess.run(
            command,
            stdin=subprocess.DEVNULL,
            stdout=stream,
            stderr=subprocess.STDOUT,
            timeout=180,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            cwd=directory,
        )
    if result.returncode:
        raise ValueError("Blender asset conversion failed; inspect its managed log")


def _preview_request(settings, asset_id, attempt):
    if type(attempt) is not int or not 0 <= attempt <= 99:
        raise ValueError("Attempt must be an integer from 0 to 99")
    asset = resolve_asset(settings, asset_id)
    script = Path(__file__).parent / "handoff_scripts" / "asset_preview.py"
    identity = {"asset_id": asset_id, "script_sha256": digest(script), "attempt": attempt}
    preview_id = canonical(identity)
    directory = managed_directory(settings, "asset-previews", preview_id)
    request = identity | {
        "source": asset["absolute_path"],
        "format": asset["format"],
        "output": str(directory / "preview.glb"),
    }
    return directory, request, script


def get_studio_asset_preview(settings, asset_id, attempt=0):
    directory, request, _ = _preview_request(settings, asset_id, attempt)
    base = {"asset_id": asset_id, "preview_id": directory.name, "status": "not_prepared", "attempt": attempt}
    if not directory.exists():
        return base
    stored = read_json(directory / "request.json")
    if stored != request:
        raise ValueError("Preview request differs from its content-addressed source")
    if not (directory / "report.json").exists():
        return base | {"status": "outcome_unknown", "note": "Inspect this attempt before creating a new one"}
    report = read_json(directory / "report.json")
    if (
        report.get("asset_id") != asset_id
        or report.get("script_sha256") != request["script_sha256"]
        or report.get("status") != "completed"
        or report.get("schema_version") != 1
        or not isinstance(report.get("sha256"), str)
        or not re.fullmatch(r"[0-9a-f]{64}", report["sha256"])
    ):
        raise ValueError("Preview receipt differs from its source or converter")
    path = directory / "preview.glb"
    _file(path, report["sha256"])
    structure = inspect_glb(path)
    resolve_asset(settings, asset_id)
    if read_json(directory / "request.json") != stored or read_json(directory / "report.json") != report:
        raise ValueError("Preview provenance changed during verification")
    return (
        report
        | base
        | {
            "status": "completed",
            "absolute_path": str(path),
            "preview_path": str(path),
            "structure": structure,
        }
    )


def build_studio_asset_preview(settings, asset_id, attempt=0):
    directory, request, script = _preview_request(settings, asset_id, attempt)
    directory.parent.mkdir(parents=True, exist_ok=True)
    with FileLock(str(directory.parent / (directory.name + ".lock")), timeout=10):
        if not directory.exists():
            directory.mkdir()
            atomic_json(directory / "request.json", request)
            run_blender(script, directory / "request.json", directory)
        result = get_studio_asset_preview(settings, asset_id, attempt)
        if result["status"] != "completed":
            raise ValueError(
                "This preview attempt was dispatched without a completed receipt; use a new attempt"
            )
        return result
