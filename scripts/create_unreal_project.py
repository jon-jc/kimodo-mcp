"""Create a separate content-only Unreal project with native animation MCP tools.

Run with --directory pointing to a new folder. Existing folders are never
overwritten, and this helper does not launch Unreal or edit other projects.
"""

from __future__ import annotations

import argparse
import json
import re
from pathlib import Path

PLUGINS = (
    "ModelContextProtocol",
    "ToolsetRegistry",
    "EditorToolset",
    "AnimationAssistantToolset",
    "PythonScriptPlugin",
    "EditorScriptingUtilities",
)

ENGINE_SETTINGS = """[HTTPServer.Listeners]
DefaultBindAddress=127.0.0.1

[/Script/PythonScriptPlugin.PythonScriptPluginSettings]
bRunPipInstallOnStartup=False
bRemoteExecution=True
RemoteExecutionMulticastBindAddress=127.0.0.1
RemoteExecutionMulticastTtl=0

[/Script/EngineSettings.GameMapsSettings]
EditorStartupMap=/Engine/Maps/Entry

[/Script/Engine.RendererSettings]
r.DynamicGlobalIlluminationMethod=0
r.ReflectionMethod=0
r.Nanite.ProjectEnabled=False
r.Shadow.Virtual.Enable=0
"""


def create_project(directory: Path, name: str, engine_version: str) -> Path:
    """Create a new project; require installed engine plugins at editor startup."""
    if not re.fullmatch(r"[A-Za-z][A-Za-z0-9_]{0,63}", name):
        raise ValueError("Project name must be a simple identifier starting with a letter.")
    if not re.fullmatch(r"[0-9]+\.[0-9]+", engine_version):
        raise ValueError("Use an engine association such as 5.8.")
    directory = directory.expanduser().resolve()
    if directory.exists():
        raise FileExistsError(f"Refusing to replace an existing directory: {directory}")
    directory.mkdir(parents=True, exist_ok=False)
    (directory / "Config").mkdir()
    (directory / "Content").mkdir()
    project_file = directory / f"{name}.uproject"
    project_file.write_text(
        json.dumps(
            {
                "FileVersion": 3,
                "EngineAssociation": engine_version,
                "Category": "Animation",
                "Description": "Isolated character animation import and validation workspace",
                "Plugins": [{"Name": plugin, "Enabled": True} for plugin in PLUGINS],
            },
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    (directory / "Config/DefaultEngine.ini").write_text(ENGINE_SETTINGS, encoding="utf-8")
    return project_file


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--directory", required=True, type=Path, help="A new project folder")
    parser.add_argument("--name", default="AnimationLab", help="Project filename without extension")
    parser.add_argument("--engine-version", default="5.8", help="Installed engine association")
    arguments = parser.parse_args()
    project_file = create_project(arguments.directory, arguments.name, arguments.engine_version)
    print(json.dumps({"project_file": str(project_file), "plugins": PLUGINS}, indent=2))


if __name__ == "__main__":
    main()
