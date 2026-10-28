# Kimodo MCP

Give an MCP-compatible assistant a controlled interface to [NVIDIA Kimodo](https://github.com/nv-tlabs/kimodo) for creating and inspecting skeletal animation. The bridge runs locally and uses Kimodo's model runtime for generation; it does not require an OpenAI or Anthropic API key.

Kimodo produces motion on SOMA human, Unitree G1 robot, and SMPL-X body skeletons. It does not generate a new character mesh or automatically retarget animation onto an arbitrary rig.

## What you can do

- Discover supported models, skeleton layouts, and export formats.
- Describe an action or a sequence of timed actions.
- Add root waypoints, paths, full-body keyframes, and hand or foot constraints.
- Submit generation as a background job, inspect its status, and cancel it.
- Inspect motion arrays and retrieve generated artifacts.
- Export supported skeletons as BVH, MuJoCo CSV, or AMASS NPZ.
- Connect through local stdio or loopback Streamable HTTP.

## Install the bridge

Use Python 3.11 or newer in a dedicated environment. From this repository:

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -e ".[dev]"
```

On macOS or Linux, use `.venv/bin/python` in place of `.venv\Scripts\python.exe`.

The lightweight bridge and the Kimodo runtime can use different Python environments. The bridge exposes its tools, validates animation requests, and reports runtime prerequisites before model weights are available.

For a complete Windows environment, install `uv`, Git, CMake, and a C++ compiler, then run the provided setup script. It installs Python packages and a pinned upstream checkout without downloading model weights:

```powershell
.\scripts\setup_runtime.ps1 -DataDirectory "D:\KimodoData"
```

If CMake's dependency download fails, rerun with `-NativeDependencyFallback`. The fallback installs pybind11 2.11.1 and Eigen 3.4.0 into the external data directory, builds the native extension separately, and uses upstream's supported option to skip rebuilding that extension during the Kimodo package install. A missing compiler still needs to be installed separately.

## Connect your assistant

Follow [client setup](docs/clients.md) for Codex, Claude Code, Claude Desktop, and other MCP clients. Set these environment variables in the client's server configuration:

| Variable | Purpose |
| --- | --- |
| `KIMODO_REPO` | Absolute path to the Kimodo source checkout. |
| `KIMODO_PYTHON` | Absolute path to Python in the environment that has Kimodo installed. |
| `KIMODO_DATA_DIR` | Directory for jobs and output artifacts, outside this repository. |
| `TEXT_ENCODER_DEVICE` | Use `cpu` to reduce GPU memory use. |
| `HF_HOME` | Optional external Hugging Face cache directory. |
| `KIMODO_ALLOW_DOWNLOADS` | Set to `1` to allow first-run model downloads; default is offline. |
| `KIMODO_JOB_TIMEOUT` | Maximum seconds waiting for a generation slot and maximum execution seconds; default `3600`. |

Start a local stdio server with the bridge environment's Python:

```powershell
.\.venv\Scripts\python.exe -m kimodo_mcp
```

No terminal banner is expected in stdio mode: the process waits for an MCP client.

Generation also requires an independent worker. After setup, start it once from PowerShell:

```powershell
.\scripts\start_worker.ps1 -DataDirectory "D:\KimodoData" -UpstreamDirectory "D:\KimodoData\upstream-kimodo"
```

Add `-AllowDownloads` when first downloading authorized models. Without that flag the worker stays offline. The worker continues processing when an MCP client closes. Restart it after reboot or configuration changes; this script does not install a Windows service. `health_check` reports whether the worker is available, and generation refuses submissions when it is offline.

On any platform, the equivalent is to run the bridge Python with `-m kimodo_mcp.worker_service` in a separate terminal using the same source, runtime, cache, data-directory, and download settings. Keep that terminal running.

## Prepare Kimodo for generation

Clone Kimodo separately, install a CUDA-compatible PyTorch build, and follow its [installation instructions](https://github.com/nv-tlabs/kimodo/blob/main/docs/source/getting_started/installation.md). Kimodo's native motion-correction extension needs CMake and a working C++ compiler. The demo additionally uses a customized Viser dependency.

Generation uses a gated Llama text encoder. Your Hugging Face account must have access to `meta-llama/Meta-Llama-3-8B-Instruct`, and the runtime must be signed in. Configure credentials locally with Hugging Face tooling; do not put tokens in this repository or tool arguments.

After obtaining access, enable first-run downloads with `-AllowDownloads` in the worker startup script, or `KIMODO_ALLOW_DOWNLOADS=1` in the independent worker's environment. Once the required models are cached, restart the worker in its default offline mode to use them without fetching weights. A cached token alone does not establish access to a gated model.

Upstream reports about 17 GB VRAM for all-GPU generation and under 3 GB GPU memory when text encoding runs on CPU. The CPU encoder still needs substantial system memory. A successful MCP connection alone does not establish model access, sufficient memory, or successful inference.

## Animation workflow

Ask your assistant:

> Check Kimodo's runtime readiness, then plan a five-second animation of a person walking forward and waving. Validate the request, generate one reproducible SOMA sample, inspect the output, and export a standard T-pose BVH.

For a longer scene, describe self-contained segments of at most 10 seconds each. See [animation workflows](docs/animation-workflows.md) for coordinates, constraints, examples, and export choices.

## MCP tools

| Tool | Purpose |
| --- | --- |
| `list_models` | Exact model identifiers and supported skeleton/export combinations. |
| `health_check` | Bridge configuration; set `probe_runtime=true` for local dependency and GPU checks without downloading weights. |
| `validate_motion` | Check a structured request and return its frame plan. |
| `generate_motion` | Submit that request and receive a durable job ID. |
| `get_job`, `list_jobs` | Inspect progress and recent jobs across clients sharing a data directory. |
| `cancel_job` | Request cancellation while preserving completed outputs. |
| `get_job_log` | Read a bounded diagnostic tail. |
| `get_artifact` | Resolve an output listed in a completed job's manifest. |
| `inspect_motion` | Check NPZ arrays, skeleton hierarchy, timing, motion bounds, and contact data. |
| `preview_motion` | Create a local interactive skeleton preview from a completed NPZ artifact. |

The `kimodo://request-schema` resource exposes the full input schema. The `animation_workflow` prompt guides an assistant through validation and generation. Files in `examples/` are complete arguments for `validate_motion` or `generate_motion`.

## Development

```powershell
.\.venv\Scripts\python.exe -m pytest
```

[Architecture](docs/architecture.md) explains the process boundary and artifact lifecycle. [Upstream review](docs/upstream-review.md) records the source revision, subsystem map, and limitations used to design this adapter.
