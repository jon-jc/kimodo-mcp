# Connect an MCP client

Install the bridge first. Use absolute executable and directory paths so a desktop client's working directory cannot change which Python or source checkout it launches. Replace the example paths below with your own.

## Start the generation worker

All clients share one independently started worker using the same `KIMODO_DATA_DIR`. On Windows, run this from the bridge checkout after installation:

```powershell
.\scripts\start_worker.ps1 -DataDirectory "D:\KimodoData" -UpstreamDirectory "C:\tools\kimodo"
```

Add `-AllowDownloads` for authorized first-run model downloads. The script starts a hidden background process and detects an already running worker. Restart the worker after reboot or changes to its runtime, cache, or download configuration. On other platforms, start `python -m kimodo_mcp.worker_service` in a separate terminal with the same environment as the bridge. Keep that process running independently of the client.

The default worker mode is `service`; `health_check` exposes its availability. A successful client connection can list and validate tools without a worker, but generation requires one. The optional `KIMODO_WORKER_MODE=child` mode belongs to the client process tree and may terminate when a Windows client closes; it does not provide the independent worker's lifetime guarantee.

## Codex

Register a user-level stdio entry:

```powershell
codex mcp add kimodo --env "KIMODO_REPO=C:\tools\kimodo" --env "KIMODO_PYTHON=C:\tools\kimodo-runtime\Scripts\python.exe" --env "KIMODO_DATA_DIR=D:\KimodoData" --env "TEXT_ENCODER_DEVICE=cpu" -- "C:\tools\kimodo-mcp\.venv\Scripts\python.exe" -m kimodo_mcp
codex mcp get kimodo
```

The entry is stored in the user's Codex configuration. Open a new client session if the current session has already loaded its tool list. Registration confirms configuration; call `health_check` with `probe_runtime=true` before generating. See [Codex MCP documentation](https://learn.chatgpt.com/docs/extend/mcp).

## Claude Code

```powershell
$KimodoEntry = @{
    type = 'stdio'
    command = 'C:\tools\kimodo-mcp\.venv\Scripts\python.exe'
    args = @('-m', 'kimodo_mcp')
    env = @{
        KIMODO_REPO = 'C:\tools\kimodo'
        KIMODO_PYTHON = 'C:\tools\kimodo-runtime\Scripts\python.exe'
        KIMODO_DATA_DIR = 'D:\KimodoData'
        TEXT_ENCODER_DEVICE = 'cpu'
    }
} | ConvertTo-Json -Depth 4 -Compress
claude mcp add-json --scope user kimodo $KimodoEntry
claude mcp get kimodo
```

Use `/mcp` in Claude Code to inspect the connection. The JSON form preserves Python's `-m` argument when registering from Windows PowerShell. User scope keeps machine-specific settings outside the project. See [Claude Code MCP documentation](https://code.claude.com/docs/en/mcp).

## Claude Desktop and clients using `mcpServers`

Merge the following `kimodo` entry into the existing `mcpServers` object; retain other configured servers. Claude Desktop on Windows keeps its configuration at `%APPDATA%\Claude\claude_desktop_config.json`.

```json
{
  "mcpServers": {
    "kimodo": {
      "command": "C:\\tools\\kimodo-mcp\\.venv\\Scripts\\python.exe",
      "args": ["-m", "kimodo_mcp"],
      "env": {
        "KIMODO_REPO": "C:\\tools\\kimodo",
        "KIMODO_PYTHON": "C:\\tools\\kimodo-runtime\\Scripts\\python.exe",
        "KIMODO_DATA_DIR": "D:\\KimodoData",
        "TEXT_ENCODER_DEVICE": "cpu"
      }
    }
  }
}
```

Completely quit and reopen Claude Desktop after changing its configuration. Other clients may use a different top-level key; use their documented schema while retaining the same executable, arguments, and environment. See the [MCP local-server guide](https://modelcontextprotocol.io/docs/develop/connect-local-servers).

## Shared local HTTP

Run the bridge in a terminal with the same environment variables:

```powershell
& "C:\tools\kimodo-mcp\.venv\Scripts\python.exe" -m kimodo_mcp --transport streamable-http --port 8767
```

Then register the local endpoint:

```powershell
codex mcp add kimodo --url http://127.0.0.1:8767/mcp
claude mcp add --transport http --scope user kimodo http://127.0.0.1:8767/mcp
```

Choose either stdio or HTTP for a client entry named `kimodo`.

Keep this endpoint on loopback. This adapter does not provide a multi-user authorization service or a production remote deployment. Local stdio is usually easier because the client starts and stops the bridge process.

## Blender and Unreal connections

Configure `kimodo`, `blender`, and `unreal` as separate MCP entries in each client. Kimodo prepares motion and checked handoff scripts; the editor connections operate their respective applications. Registering Kimodo alone does not install either editor connector. Verify all three entries in the client's MCP status and verify the open scene or project before executing a handoff.

The fitted-character workflow also requires an operator-selected source FBX. Set `KIMODO_CHARACTER_FBX`, or create `character/target.json` inside the configured data directory with `schema_version: 1` and an absolute `source_fbx` path. The supported profile checks the source's exact content hash; choosing another character file does not automatically fit a new rig. Keep this registration and the original assets outside the repository.

An Unreal connector may expose native import operations without a general Python executor. In that case, execute the prepared script through the editor's Python facility, or use native import tools and verify the same rig, scale, timing, and asset paths. A native connector's tool response does not create the prepared script's report automatically; do not claim that script completed unless its managed report exists.

## Troubleshooting

| Symptom | Check |
| --- | --- |
| Server fails to start | The configured command must be the bridge Python, with `kimodo_mcp` installed. |
| Server connects, generation fails | `KIMODO_PYTHON` must point to the runtime Python with Kimodo, PyTorch, and native dependencies installed. |
| Generation reports worker unavailable | Start the independent worker with the same data directory; inspect `worker-service-error.log` if startup fails. |
| Model download is denied | Check Hugging Face account access and authentication inside the runtime environment. |
| GPU out of memory | Set `TEXT_ENCODER_DEVICE=cpu`, reduce samples, and recheck available system memory. |
| Output not found | Inspect job status and artifact metadata; a submitted job is not a completed animation. |
| Tools absent after installation | Restart the client session and inspect its MCP status. |
