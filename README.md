# Kimodo MCP & Motion Studio

**Describe a movement, generate skeletal animation locally, inspect the actual result, and bring it into Blender or Unreal Engine.**

Kimodo MCP connects [NVIDIA Kimodo](https://github.com/nv-tlabs/kimodo) to MCP-compatible assistants, including Codex and Claude, and provides a browser studio for the same workflow. It combines explicit animation planning, durable background jobs, reproducible candidates, motion diagnostics, interactive character previews, and checked editor handoffs.

The bridge uses a locally configured Kimodo runtime. It requires no OpenAI or Anthropic API key for motion generation. Your assistant client, model downloads, and editor installations are configured separately.

![Create screen with a timed sequence composer, validated frame timeline and an existing character preview](docs/images/studio-sequence-composer.jpg)

*The studio separates authoring a plan from viewing an existing animation. A validated sequence describes future generation; it does not replace the character's currently selected saved motion. Character assets, model weights, credentials, and local project files are not included in this repository.*

[Getting started](#getting-started-on-windows) · [Studio workflow](#work-in-the-browser-studio) · [Video references](#capture-a-video-reference) · [MCP tools](#mcp-tools) · [Agent workflows](docs/agent-workflows.md) · [Architecture](#how-it-works) · [Limitations](#scope-and-limitations) · [Troubleshooting](#troubleshooting)

## What this project provides

| Capability | What you can do |
| --- | --- |
| Guided studio workflow | Move between Create, Review & fix, and Export while retaining the current draft, selected motion version and playback position. |
| Natural-language planning | Describe an action, choose duration and generation effort, and inspect an explicit request before submitting it. |
| Timed sequences | Supply up to eight self-contained action descriptions with explicit durations and exact planned frame counts. |
| Saved animation plans | Save, load and revise the complete request before any generation; preserve advanced settings, frame anchors and parent-plan lineage. |
| Reproducible candidates | Generate one to four samples while preserving model, seed, prompts, constraints, and effective settings. |
| Saved revisions | Create a new version while retaining every setting you did not explicitly change. |
| Pose reuse | Select a completed SOMA frame and anchor another generation's initial pose and heading. |
| Background generation | Submit, follow stages, cancel, inspect logs, and return later without an open browser or assistant session. |
| Interactive viewer | Orbit, pan, zoom, fit the camera, play, pause, scrub, change speed, and toggle wireframe or skeleton display. |
| Motion review | Inspect exact frames from three fixed views, with contact sheets, hashes, physical-unit diagnostics, and explicit unavailable results. |
| Candidate comparison | Align compatible clips by frame offset and compare world positions or poses relative to the root. |
| Editor integration | Execute fixed Blender and Unreal handoffs, then check exported files, rig compatibility, and animation timing. |
| Downloads | Retrieve original motion, animated GLB, skeletal mesh/animation FBX, and Blender projects when available. |
| Precise rig corrections | Inspect all source joints, groups and local axes; save bounded local rotation corrections with smooth frame-window blends, trimming and planar placement. |
| Numerical assessment | Apply explicit project limits to up to eight takes; inspect problem windows and unavailable evidence without an automatic quality ranking. |
| Video reference capture | Use a separate EasyMocap runtime for bounded single-video body fitting, explicit short-occlusion estimates, confidence review and verified SMPL24/BVH outputs. |
| MCP interface | Use 57 typed tools over stdio or loopback Streamable HTTP, with a discoverable capability catalog, workflow recipes, input schemas, and shared local jobs. |

**Character scope:** Kimodo generates motion on supported skeletons; it does not generate a new mesh. The fitted-character pipeline is tied to one inspected asset and its exact checksum. It is not a general automatic rigging system. New users can generate and review skeletal motion without that character. Unrelated characters need an appropriate rig and a separately validated retargeting workflow.

## Getting started on Windows

### 1. Install prerequisites

| Component | Requirement |
| --- | --- |
| PowerShell | Version 7 or newer for setup and launcher scripts. |
| Git, `uv`, CMake | Available on `PATH` for sources, Python environments, and native builds. |
| C++ toolchain | A working compiler, such as Visual Studio C++ build tools, for Kimodo's motion-correction extension. |
| Node.js | Version 22.12 or newer to build the studio. CI uses Node 24. |
| NVIDIA runtime | Compatible GPU/driver for the selected PyTorch build. Setup defaults to PyTorch 2.11.0 with CUDA 12.8 wheels. |
| Model access | Hugging Face access to the gated `meta-llama/Meta-Llama-3-8B-Instruct` encoder. |
| Memory and disk | Capacity for environments, checkpoints, cache, and outputs; sufficient available RAM and commit capacity for inference. |
| Optional editors | Blender with its local MCP connection; Unreal with the required project plugins and Python transport. |

The interface and bridge are lightweight compared with inference. A working page does not establish that the machine can load the encoder. Read [memory readiness](#memory-and-model-readiness) before downloading or generating.

### 2. Clone and install the runtime

```powershell
git clone https://github.com/jon-jc/kimodo-mcp.git
cd kimodo-mcp

.\scripts\setup_runtime.ps1 -DataDirectory "D:\KimodoData"
```

Setup creates separate `mcp-env` and `runtime` environments, installs the bridge, clones Kimodo outside this repository, and probes runtime imports. It does **not** download model weights or establish successful inference. Upstream is pinned to [`e1c219c99b4b94ba48be1fb6a1cbc64ecee4c954`](https://github.com/nv-tlabs/kimodo/tree/e1c219c99b4b94ba48be1fb6a1cbc64ecee4c954); an existing checkout must match and remain clean.

If native dependency fetching fails:

```powershell
.\scripts\setup_runtime.ps1 -DataDirectory "D:\KimodoData" -NativeDependencyFallback
```

After the normal build fails, this option installs pybind11 2.11.1 and pinned Eigen 3.4.0 sources externally, builds the extension separately, and uses upstream's extension-skip installation option. It does not replace a missing compiler or edit upstream source files.

Use `-UpstreamDirectory` for an existing reviewed checkout. `-PythonVersion` defaults to `3.12`; `-TorchVersion` defaults to `2.11.0`.

### 3. Authorize the encoder and first-run downloads

Obtain access on the [Llama encoder model page](https://huggingface.co/meta-llama/Meta-Llama-3-8B-Instruct), then sign in locally:

```powershell
$env:HF_HOME = "D:\KimodoData\cache\huggingface"
& "D:\KimodoData\runtime\Scripts\hf.exe" auth login

.\scripts\start_worker.ps1 -DataDirectory "D:\KimodoData" -UpstreamDirectory "D:\KimodoData\upstream-kimodo" -AllowDownloads
```

The worker fetches required models when a generation needs them. Authentication alone does not grant gated access. Keep credentials in local Hugging Face configuration, outside prompts, tool arguments, examples, and Git.

After the weights are cached, restart the worker without `-AllowDownloads` for offline operation. Download permission belongs to the worker environment. Starting an already running worker again does not change its settings.

### 4. Open the studio

```powershell
.\scripts\start_studio.ps1 -DataDirectory "D:\KimodoData" -UpstreamDirectory "D:\KimodoData\upstream-kimodo" -StartWorker -OpenBrowser
```

Open **[http://127.0.0.1:8768](http://127.0.0.1:8768)** if needed. The launcher builds missing interface files and starts the local server in the background. Use `-Build` after interface changes or `-Port 8769` for another port.

**This launcher expects both environments created by the full setup script.** A standalone `.venv` installation uses the manual route below. The launcher does not start Blender or Unreal or install an operating-system service. Restart the worker and studio after reboot or configuration changes.

## Bridge-only and manual installation

Use this route for adapter development, MCP connections, or an independently installed Kimodo runtime:

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -e ".[dev]"

cd web
npm ci
npm run build
cd ..
```

The bridge supports Python 3.11 and newer. On macOS/Linux, use `.venv/bin/python` and install inference dependencies using the [pinned upstream guide](https://github.com/nv-tlabs/kimodo/blob/e1c219c99b4b94ba48be1fb6a1cbc64ecee4c954/docs/source/getting_started/installation.md). The complete inference/editor workflow was exercised on Windows; bridge CI does not establish compatibility for every GPU/editor combination.

Configure the same environment for the worker, studio, and MCP clients:

| Variable | Meaning |
| --- | --- |
| `KIMODO_DATA_DIR` | Shared jobs, reports, exports, and registrations outside this repository. |
| `KIMODO_REPO` | Absolute path to the reviewed upstream checkout. |
| `KIMODO_PYTHON` | Absolute path to Python with Kimodo and inference dependencies. |
| `TEXT_ENCODER_DEVICE` | `cpu` reduces GPU use by moving the encoder into system memory. |
| `HF_HOME` | Optional shared Hugging Face cache and local authentication directory. |
| `KIMODO_ALLOW_DOWNLOADS` | `1` permits first-run downloads; ordinary worker startup is offline. |
| `KIMODO_WORKER_MODE` | `service` is the independent worker; optional `child` mode follows the client process tree. |
| `KIMODO_JOB_TIMEOUT` | Waiting and execution limits, applied separately; default `3600` seconds. |
| `KIMODO_CHARACTER_FBX` | Optional source for the supported fitted-character profile. |
| `KIMODO_UNREAL_ENGINE` | Optional Unreal installation root containing `Engine`. |

Run these commands in separate terminals with those settings:

```powershell
# Terminal 1: keep the worker independent of the browser and assistant.
.\.venv\Scripts\python.exe -m kimodo_mcp.worker_service

# Terminal 2: from the repository root.
.\.venv\Scripts\python.exe -m kimodo_mcp.studio --web-directory web/dist
```

The default data directory is `%LOCALAPPDATA%\kimodo-mcp` on Windows or `~/.local/share/kimodo-mcp` otherwise. Use one shared directory for clients on the same GPU so they share the queue and generation lock.

## Work in the browser studio

Use the three persistent workflow buttons:

| Screen | Work here |
| --- | --- |
| **Create** | Describe one action or compose timed beats, preview the frame plan, save a reusable request, and explicitly generate when ready. The existing 3D preview remains available as a reference. |
| **Review & fix** | Select a completed candidate/version, inspect its source rig or fitted character, compare motion, review diagnostics, and save bounded corrections. |
| **Export** | Download the selected version's verified files, convert motion, or explicitly run a matching Blender/Unreal handoff. |

Switching screens preserves the authoring draft, selected candidate/version and current viewing time. It does not generate, save a correction or run an editor. **Animation history** is a collapsible strip available in every screen; selecting a take opens Review & fix, while **Revise** returns its request to Create. Status messages occupy their own layout space instead of covering playback or action controls. Desktop authoring and viewing panels scroll independently; narrow windows stack the content.

In Create, enable **Sequence of timed actions** for two to eight beats. Edit a card's description and 0.2–10-second duration, move it up/down, duplicate it, or add/remove an action. Editable starters provide an approach/greet/settle, crouch/jump/land, or turn/walk/stop outline. Each beat remains a complete model prompt; sequence notes are authoring context. Clicking a timeline segment selects its card.

**Preview the plan** validates the exact frame allocation, seed, candidate count and sampling settings without inference. Estimates become a validated timeline; shaded tails mark earlier frames revisited by the next action's transition history. The 3D viewer still shows the existing selected animation. A blank studio seed becomes a fresh seed during planning; direct MCP briefs default to zero.

Open **Reusable plans**, enter a title and choose **Validate & save version** to retain the complete request. Load a saved version to preserve custom guidance, sampling steps, transition settings, formats and pose/path constraints. Text-only revisions keep constraints when every beat retains its quantized frame count. Changed allocations require deliberate clearing or rebuilt constraints. Save a changed plan before using **Generate saved plan**. Planning, loading and saving are usable while the worker and editors are stopped.

Once a submitted job completes, use Review & fix to inspect its actual motion. **Build character preview** executes the supported character's checked Blender handoff; it is a separate explicit action. Review a source skeleton without that character, compare compatible clips, or pause a SOMA source and return to Create to anchor another generation. Pose anchoring does not guarantee matching velocity or a seamless transition.

**Edit skeleton** opens the source rig and its direct controls. Click a joint, choose **Move joint** to aim its parent bone without stretching, or **Rotate joint** to adjust local orientation. Dragging the root moves the clip across the ground plane. Each completed drag is undoable and previews exact source-skeleton math at the current frame. Choose **Save & inspect new version** for a playable corrected clip; originals remain saved. This is a one-bone adjustment tool, not a full-body IK or automatic contact solver.

![Direct source-rig editing with local rotation rings and independently scrolling controls](docs/images/studio-direct-rig-editing.jpg)

The action composer also supports **Undo actions**, **Redo actions**, and Ctrl/Cmd+Z. Validation opens the first incomplete action and focuses its field. Correction forms retain pending values when another part of the draft is staged; a delayed save cannot replace a newer draft or selection. See the [detailed editing guide](docs/studio.md#direct-viewport-corrections).

Draft, balanced, and detailed select **20**, **50**, and **80** sampling steps. More steps or candidates do not guarantee a better performance. The planner preserves descriptions and supplied beats; it does not contain a hidden language model that invents choreography or interprets vague edit requests.

<details>
<summary>View the responsive mobile layout</summary>

<img src="docs/images/studio-sequence-mobile.jpg" alt="Narrow-screen Create workflow with persistent navigation, timed action cards and reusable-plan controls" width="340" />

The interface adapts to narrow browser windows. It remains a local desktop workflow, not a remote phone-hosting service.

</details>

See [the full studio guide](docs/studio.md), [natural-language workflow](docs/natural-workflow.md), and [structured animation workflows](docs/animation-workflows.md).

## Capture a video reference

Open **Create → From video · EasyMocap**. Choose a local file (the browser copies up to 512 MiB into the local reference library), or enter a full path to a video up to 2 GiB. Select a start time, duration, frame rate and body detector, then **Check video & setup**. The checker identifies missing runtime/model files without downloading or processing them. A ready plan enables **Capture this motion**; starting rechecks the source checksum and creates a separate durable video job.

The fixed pipeline samples 6–2,400 frames, detects BODY25 landmarks, initializes and fits an SMPL body with temporal regularization, normalizes its actual shaped skeleton, and writes a meter-based BVH with the requested frame rate. Captures do not occupy or start the Kimodo generation worker. Progress, bounded logs, cancellation, detected/problem frames, confidence, reference video, skeleton review and verified downloads are available through both the Studio and MCP.

For imperfect footage, **Brief occlusions** can explicitly bridge up to 15 missing internal frames using low-confidence 2D interpolation. The default is strict detection. Estimates retain their original detection records, source times and inference flags; ambiguous people, long gaps, endpoints and unusable geometry remain unresolved. This is an aid for short gaps, not reliable recovery of arbitrary unseen action.

The review pairs the exact analyzed video with the SMPL24 skeleton. Scrubbing and playback share time; confidence identifies BODY25 image observations and bridged frames, rather than inventing certainty for the fitted 3D joints. Downloads are bound to a completed receipt and source/output hashes. Use the BVH in Blender for explicit retargeting, then validate your character's scale, rest pose, contacts and root motion before Unreal import. The existing automatic SOMA character handoff does not accept SMPL24 captures.

![Video review interface exercised with a synthetic test-pattern video and synthetic skeleton fixture](docs/images/studio-video-review-fixture.jpg)

*This screenshot shows an isolated synthetic interface test. The color bars and skeleton were supplied as test data; they are not a real video reconstruction.*

| Capture tool | Purpose |
| --- | --- |
| `plan_video_mocap` | Check source, bounded settings, pinned external runtime, dependencies and model assets. |
| `start_video_mocap` | Explicitly start a capture; pass the checked `expected_source_sha256`. |
| `get_video_mocap_job` / `list_video_mocap_jobs` | Read stage, diagnostics, separate capture history and completion receipt. |
| `get_video_mocap_log` / `cancel_video_mocap_job` | Inspect bounded logs or request scoped cancellation. |
| `inspect_video_mocap_frames` | Inspect up to 16 poses with source times, 2D confidence and estimated-frame flags. |
| `get_video_mocap_artifact` | Retrieve a verified reference, NPZ, BVH, parameters, detections, timing or diagnostic file. |

Use `get_workflow("video-mocap")` and `get_input_schema("video_mocap_request")` for the exact agent contract. See [EasyMocap installation, examples and limitations](docs/easymocap.md) and the [source-by-source integration audit](docs/mocap-source-audit.md).

**Runtime and evidence:** EasyMocap and its separately licensed body models are not bundled. The integration has synthetic numerical, lifecycle, protocol and browser-fixture validation; real footage reconstruction requires the configured runtime and assets. Monocular scale, depth and camera/world motion remain estimates. The reviewed EasyMocap license is [Project Registration License v1.0](https://github.com/zju3dv/EasyMocap/blob/e6006fd3814d5f8ad45a7ce965beb9bcc90767f2/LICENSE), which requires registration for organizational projects; body models and weights have separate terms.

## Connect Codex, Claude, or another MCP client

Launch MCP using the **bridge Python**, pointing it to the heavy runtime through environment settings. Clients using `mcpServers` can merge this entry into their existing configuration:

```json
{
  "mcpServers": {
    "kimodo": {
      "command": "D:\\KimodoData\\mcp-env\\Scripts\\python.exe",
      "args": ["-m", "kimodo_mcp"],
      "env": {
        "KIMODO_REPO": "D:\\KimodoData\\upstream-kimodo",
        "KIMODO_PYTHON": "D:\\KimodoData\\runtime\\Scripts\\python.exe",
        "KIMODO_DATA_DIR": "D:\\KimodoData",
        "TEXT_ENCODER_DEVICE": "cpu",
        "HF_HOME": "D:\\KimodoData\\cache\\huggingface"
      }
    }
  }
}
```

[Client setup](docs/clients.md) includes tested Codex registration, Windows-friendly Claude Code `add-json`, Claude Desktop settings, and troubleshooting. Keep machine-specific configurations outside Git.

Stdio is the default. For shared local HTTP:

```powershell
& "D:\KimodoData\mcp-env\Scripts\python.exe" -m kimodo_mcp --transport streamable-http --port 8767
```

Connect to `http://127.0.0.1:8767/mcp`. MCP uses **8767**; the studio uses **8768**. Both are local interfaces and provide no public multi-user authorization service.

Example assistant request:

> Check runtime readiness. Plan a four-second animation of a person standing in place and slowly waving with the right hand. Use one draft candidate and seed 42. Show the plan, generate it, inspect the completed motion, and create a frame review. Preserve the original request and artifacts when revising.

Blender and Unreal are separate connections. Registering `kimodo` alone does not install their connectors. A client can use all three; the studio invokes the bridge's fixed handoff operations.

## MCP tools

The server exposes **57 tools**, **14 workflow recipes** and **10 input schemas**. Start with `get_capabilities` for the running server's complete inventory and `get_workspace_state` for worker, memory, editor, character, target, saved-plan, native-job and separate video-capture state. `get_workflow` provides ordered recipes with completion evidence and failure guidance; `get_input_schema` exposes complete domain contracts, including advanced constraints. These discovery operations do not start inference or editor work. Submission is distinct from completed animation.

| Tool | Purpose |
| --- | --- |
| `get_capabilities` | Discover every registered tool, workflow, schema, model, limit, and implementation boundary. |
| `get_workflow` | Read an ordered workflow recipe without executing any actions. |
| `get_input_schema` | Read the complete schema for briefs, revisions, requests, pose references, diagnostics, or Unreal targets. |
| `get_workspace_state` | Read shared readiness, memory advisory, editor reachability, registered assets, saved target, saved plans, and recent jobs. |
| `list_models` | Exact model IDs, skeletons, formats, and reviewed revision. |
| `health_check` | Configuration and worker status; optional runtime/GPU probe without weight downloads. |
| `plan_animation` | Compile a description/timed beats into explicit settings, optionally anchored to a source pose. |
| `generate_animation` | Submit a brief with transparent effort, seed, and candidate settings. |
| `revise_animation` | Preview or submit a new version, retaining unchanged settings and parent-job lineage. |
| `save_animation_plan` | Save an exact validated request under an immutable identifier without starting generation. |
| `get_animation_plan` | Verify and load a saved request, its hashes, lineage and frame timeline. |
| `list_animation_plans` | List bounded saved-plan summaries with pagination and an invalid-record count. |
| `revise_animation_plan` | Preview explicit changes or save a child plan while preserving unmentioned settings. |
| `generate_animation_plan` | Explicitly submit the exact saved request and attach its plan provenance to a new job. |
| `extract_pose_constraint` | Extract a validated full-body or limb constraint from a canonical SOMA frame. |
| `extract_motion_constraints` | Map up to 128 actual SOMA source frames to explicit destination constraint frames. |
| `validate_motion` | Check a structured request, frame plan, compatibility, and constraint shapes. |
| `generate_motion` | Submit an explicit request and return a durable job ID. |
| `get_job` | Read status, stage, request, context, and completed manifest. |
| `list_jobs` | List recent jobs shared through the data directory. |
| `cancel_job` | Request cancellation while preserving completed outputs. |
| `get_job_log` | Read bounded worker diagnostics with supported credential redaction. |
| `get_artifact` | Resolve a manifest-listed output with checksum and local reference. |
| `list_job_outputs` | List verified native files, format conversions, and skeleton/character exports with source, sample, attempt, and checksum identity. |
| `export_motion` | Export completed managed NPZ to compatible BVH, AMASS NPZ, or G1 CSV without inference or downloads; verify or reuse its receipt. |
| `inspect_motion` | Inspect arrays, hierarchy, timing, bounds, and contact metadata. |
| `inspect_motion_frames` | Read selected frame positions and optional rotations without inventing missing metadata. |
| `preview_motion` | Create a local interactive skeleton preview from completed NPZ. |
| `inspect_motion_rig` | Read every joint, parent, body group, bone length, local axis and edit eligibility. |
| `create_motion_variant` | Save precise source-relative SOMA corrections without inference or overwriting the original. |
| `preview_motion_variant` | Preview exact original and corrected joint poses without creating files or jobs. |
| `revise_motion_variant` | Preview or save a partial correction revision, preserving unmentioned settings. |
| `get_motion_variant` | Verify a correction receipt, timing, edit settings and output checksum. |
| `assess_motion_quality` | Check explicit physical limits and return source-bound problem-frame windows. |
| `assess_motion_candidates` | Assess up to eight takes under the same reproducible policy, preserving input order. |
| `analyze_motion_quality` | Speed, acceleration, jerk, angular speed, bone drift, contact sliding, floor proxies, and seams. |
| `create_motion_review` | Offline three-view player, contact sheet, and numerical report. |
| `compare_motions` | Numerical and visual comparison at an explicit offset with matching rig/FPS. |
| `get_character_profile` | Describe the fitted rig and verify its operator-registered source. |
| `build_character_preview` | Reuse a verified preview or prepare, execute, and verify a new fitted-character export under a shared lock. |
| `prepare_blender_handoff` | Prepare fixed Blender import/export from native SOMA BVH or an explicitly selected verified derived BVH. |
| `prepare_character_handoff` | Prepare fitting, skinning, and retargeting for the supported profile. |
| `execute_blender_handoff` | Execute the verified prepared script and validate exported results. |
| `get_handoff_report` | Read Blender export, timing, hierarchy, and deformation evidence. |
| `get_unreal_target` | Read the same saved project, rig, and destination used by the studio. |
| `save_unreal_target` | Save explicit target fields without opening a project or importing assets. |
| `prepare_unreal_handoff` | Prepare animation-only import into an explicit project and existing rig. |
| `execute_unreal_handoff` | Run the verified script in the matching editor and verify the saved animation. |
| `get_unreal_report` | Read final asset, skeleton, timing, and track verification. |

The studio and MCP share character-preview execution and Unreal target settings. `build_character_preview` is available directly as an MCP tool; the browser calls the same operation. It chooses an unused attempt or reuses a valid receipt, and blocks automatic retries after uncertain editor dispatch. Inspect the receipt before retrying manually.

Frame inspection accepts 1–16 increasing source frames. An explicit joint selection accepts up to 32 unique names. The total response is limited to 2,048 selected joint poses for positions, or 256 when rotations are requested: selected frames multiplied by selected joints. For example, a 77-joint skeleton with rotations fits three complete frames; choose fewer joints to inspect more frames. Constraint extraction accepts up to 128 corresponding source/destination frames and still requires full request validation before generation.

| Resource | Content |
| --- | --- |
| `kimodo://capabilities` | Complete registered tool inventory, grouped capabilities, and workflow/schema index. |
| `kimodo://workspace` | Current shared workspace state. |
| `kimodo://models` | Supported model identifiers and export formats. |
| `kimodo://request-schema` | Advanced generation request schema, retained for compatibility. |
| `kimodo://schemas/{schema_name}` | One of eight named domain input schemas. |
| `kimodo://workflows/{workflow}` | One of thirteen ordered workflow recipes. |
| `kimodo://jobs/{job_id}` | Durable job status, saved request, and completed manifest. |
| `kimodo://jobs/{job_id}/outputs` | Verified output metadata for the selected job. |

The `animation_workflow` prompt guides assistants through the lifecycle. The [agent workflow guide](docs/agent-workflows.md) lists every recipe and schema identifier, explains actual frame/constraint access, and distinguishes implemented MCP operations from upstream-only features and operator setup. Discovery tools remain usable when a client does not expose MCP resources. Reconnect a client with a cached tool list after upgrading.

## Request examples

### Save a sequence before generating

Use `save_animation_plan` with a title and a complete `GenerateRequest`, including the `request` returned by `plan_animation` when starting from a brief. Loading it preserves custom sampling steps, both guidance weights, transition length, heading, root margin, postprocessing, export settings, and exact constraint arrays. The saved plan has its own immutable `plan_id`; `request_sha256` identifies the generation settings independently of the title and parent lineage.

`revise_animation_plan` previews changes by default. Set `save=true` to retain a child plan; neither action starts inference. Text-only beat revisions keep existing constraints when every beat retains the same quantized frame count. A changed segment allocation requires deliberate clearing or a new explicit request with rebuilt constraints. `generate_animation_plan` is the separate submission step and creates a new job with saved-plan provenance.

Read the [saved animation plan guide](docs/saved-animation-plans.md) or call `get_workflow("saved-plans")` for complete examples, transition-window interpretation, reproducibility limits and the later review/export workflow. Saved requests can be authored while the generation worker and editors are stopped.

### Start from a natural brief

Pass this object to `plan_animation`, then `generate_animation` when ready:

```json
{
  "brief": {
    "description": "A person stands in place and slowly waves with the right hand.",
    "duration_seconds": 4.0,
    "quality": "draft",
    "candidates": 2,
    "seed": 42
  }
}
```

Give longer actions explicit beats:

```json
{
  "brief": {
    "description": "An approach followed by a friendly greeting.",
    "beats": [
      { "text": "A person walks forward at a relaxed pace.", "duration": 3.0 },
      { "text": "A person stands in place and waves with the right hand.", "duration": 3.0 }
    ],
    "quality": "balanced",
    "candidates": 1,
    "seed": 42
  }
}
```

For lower-level control, these files contain full `validate_motion`/`generate_motion` arguments:

| Example | Purpose |
| --- | --- |
| [simple-motion.json](examples/simple-motion.json) | Reproducible SOMA motion with NPZ and standard-T-pose BVH. |
| [action-sequence.json](examples/action-sequence.json) | Several timed prompts. |
| [root-waypoints.json](examples/root-waypoints.json) | Frame-indexed root waypoints. |
| [robot-motion.json](examples/robot-motion.json) | A G1 request with supported exports. |

## Models, coordinates, and formats

The catalog contains nine exact generation identifiers; `kimodo-soma-rp-v1.2` is the default.

| Skeleton | Model identifiers | Native output |
| --- | --- | --- |
| SOMA | `kimodo-soma-rp-v1`, `kimodo-soma-rp-v1.1`, `kimodo-soma-rp-v1.2`, `kimodo-soma-rp-ardyrep`, `kimodo-soma-seed-v1`, `kimodo-soma-seed-v1.1` | Kimodo NPZ, BVH. |
| Unitree G1 | `kimodo-g1-rp`, `kimodo-g1-seed` | Kimodo NPZ, MuJoCo CSV. |
| SMPL-X | `kimodo-smplx-rp` | Kimodo NPZ, AMASS NPZ. |

- Canonical motion uses **meters, +Y up, +Z forward** and zero-based frame indices.
- Generation plans use **30 FPS**. Requests allow eight segments, four samples, 100 steps, 32 constraint objects, and 512 constrained frames.
- SOMA predicts a reduced 30-joint representation and exports 77 joints. Expansion fills relaxed hand detail; it does not independently generate articulated fingers.
- BVH offsets/root translation use centimeters, with corresponding Blender scale conversion.
- FBX, GLB, and `.blend` come from the editor handoff, not the native generator.
- A 120-sample clip at 30 FPS has **4.0 seconds of sample playback** and a **3.966667-second first-to-last key span**. Compare Unreal's sequence span with the latter.

The [upstream review](docs/upstream-review.md) explains representation differences, benchmark limits, and source coverage. Single-clip review does not implicitly run the research benchmark.

### Export an existing motion

Use `export_motion` when a completed job has a native NPZ and you need a compatible interchange file or another supported rest-pose/axis option. This converts the existing sample without generating motion, resampling, postprocessing, or downloading weights. It needs the configured Kimodo/PyTorch runtime and canonical skeleton assets, but no generation worker or open editor.

For a SOMA sample, pass these tool arguments:

```json
{
  "job_id": "YOUR_COMPLETED_JOB_ID",
  "sample_index": 0,
  "format": "bvh",
  "bvh_standard_tpose": true
}
```

| Format | Source | Option and default |
| --- | --- | --- |
| `bvh` | SOMA30 or SOMA77 | `bvh_standard_tpose=false`: BONES-SEED rest pose; true selects standard T-pose. Output remains Y-up, in centimeters. |
| `amass` | SMPL-X22 | `amass_z_up=true`: Z-up output; false retains Y-up. SMPL-X shape, hands, eyes, and jaw use upstream defaults. |
| `csv` | G1/34 joints | `mujoco_rest_zero=false`: raw joint DOFs; true uses MuJoCo rest-relative angles. Output is Z-up/+X-forward with wxyz root quaternions. |

The format defaults to `bvh`; choose the correct format for the source. Options are strict booleans. Changing an option that does not apply to the chosen format is rejected. BVH's standalone export default differs from natural-brief generation, which requests standard T-pose BVH.

The receipt records source and output hashes, timing, options, and coordinate conventions. Identical requests reuse a verified export; changed options create a separate derived file. Original motion and generation manifests remain unchanged. `list_job_outputs` discovers verified derived exports. An incomplete attempt is retained for inspection and is not automatically rerun.

For an NPZ-only SOMA job, pass the returned `export_id` to `prepare_blender_handoff` to use that exact derived BVH, then execute and read the skeleton handoff with the same sample and attempt. No new inference is needed. The [export workflow guide](docs/agent-workflows.md#export-existing-motion-without-inference) covers receipt reuse, representation limits, and downstream selection.

Read the [detailed change log](CHANGELOG.md) for numerical assessment and generation-planning improvements.

## Find and fix joints in the studio

![Review and fix screen showing the selected source rig and motion-correction controls](docs/images/studio-workflow-review.jpg)

Choose **Review & fix**, select a completed candidate and choose **Source rig**. Click a joint in the viewer or search by name. Search narrows the joint list while keeping the skeleton visible. Use body-region filters, **Isolate selected chain**, joint labels and front/side/top camera views to inspect specific parts. Labels avoid colliding on screen, with the selected joint taking priority. On desktop, **Focus rig editor** places the viewer beside the controls; **Exit rig focus** returns to the regular Review layout. History remains accessible, and the workflow buttons take you back to Create or Export.

For a small correction, enter local X/Y/Z offsets in degrees, choose the original start/end frames and blend length, then **Stage joint correction**. Undo, redo or reset the draft before **Save & inspect new version**. A direct drag previews an explicitly marked unsaved pose at the current frame; the backend verifies it on release. Saving creates the playable corrected clip. Seeking or playing returns to the selected saved version until you save the draft. Every version remains selectable. **Original ghost** compares matching original frames, including after trimming; **Fitted character** separately shows the selected version's verified character preview when available.

The review screen uses actual saved motion. A newly authored sequence is not a new animation until its generation completes. Read the [studio guide](docs/studio.md#inspect-and-correct-the-source-rig) for controls and keyboard shortcuts, and the [MCP correction guide](docs/rig-corrections.md) to perform the same edits through an assistant.

## Correct an existing animation through MCP

Use `inspect_motion_rig` to identify the actual source joints and their local axes, then `inspect_motion_frames` to examine the problem in context. `create_motion_variant` accepts explicit local rotation offsets, a source-frame window and smooth blending at its edges. It creates a separate corrected version without rerunning the model. Trimming and planar placement are also supported. The original stays available.

Call `get_workflow("correct-motion")` and `get_input_schema("variant_edit")` for the full agent interface. Keep the returned `variant_id` on subsequent inspection, assessment, comparison, pose reuse, exports, character previews and Blender/Unreal calls. `get_job` lists verified saved versions. The [correction guide](docs/rig-corrections.md) explains controls, examples, frame alignment and validation.

These are controlled motion corrections on canonical SOMA30/SOMA77 sources. They do not change rest-bone lengths, hierarchy or skin weights, solve contacts automatically, or establish production quality. Effective joint corrections discard stale predicted contact labels; review foot contact visually. The supplied character has 24 fitted bones, so use the source rig to inspect finger edits that it cannot reproduce.

## Blender and Unreal workflows

### Blender

For a generic SOMA skeleton, prepare a completed BVH, execute the fixed handoff, and inspect its report. Omit `export_id` to use the original manifest-listed BVH, or supply the exact ID from `export_motion` to select a verified derived BVH. It creates a separate scene and exports only its armature. The generic template requires a fresh session if `KimodoRig` already exists.

For the supported character, register its original FBX outside Git, then prepare and execute its handoff. **Build character preview** performs this managed workflow in the studio. Results include animated GLB, rest-pose mesh FBX, animation-only FBX, a separate Blender project, and a verification report. Originals remain preserved.

Changing the registration path does not make an unknown mesh supported. The demonstration character is not distributed. See [editor handoffs](docs/editor-handoffs.md) for registration, Blender MCP setup, units, and export verification.

### Unreal Engine

Import the exported character mesh first, establishing its actual Skeleton and SkeletalMesh. Configure the intended `.uproject`, those assets, a destination, and a new animation name. Prepare and execute the animation-only handoff using the same exact target.

The executor checks project/engine identity, checksums, rig hierarchy, destination, sampled keys, and tracks. It does not retarget an unrelated Skeleton or overwrite existing animations. Native Unreal MCP and Python remote execution are separate interfaces; native endpoint reachability does not prove Python import readiness.

The [editor guide](docs/editor-handoffs.md) covers plugins, loopback configuration, engine registration, and a separate validation-project helper. Manny, MetaHuman, Mixamo, and other rigs require explicit retargeting before matching-skeleton import.

## How it works

```mermaid
flowchart LR
    UI[Local browser studio] --> API[Loopback studio API]
    A[Codex / Claude / MCP client] --> MCP[MCP server]
    API --> T[Shared typed operations]
    MCP --> T
    T --> J[SQLite jobs + saved requests]
    J --> Q[Independent queue worker]
    Q --> K[Kimodo inference runtime]
    K --> F[Motion files + checksum manifest]
    F --> R[Inspection + reviews + comparisons]
    T --> R
    F --> B[Verified Blender handoff]
    B --> G[GLB / FBX / blend + receipt]
    G --> U[Matching Unreal project + rig]
```

The bridge, studio, and inference runtime are separate processes. Discovery and planning do not load model weights. One worker consumes the shared queue, while a generation lock serializes GPU work. SQLite stores requests and lineage; completed artifacts receive model, timing, effective-settings, and checksum metadata.

The external data directory contains `jobs.sqlite3`, `jobs/<job_id>/`, worker status/logs, operator registrations, and optional caches. Jobs retain requests, progress, native files, reviews, and managed editor handoffs. Closing a client does not cancel independent-worker jobs. Stopping that worker or rebooting still ends its process lifetime; it is not an installed system service.

See [architecture](docs/architecture.md), [studio setup](docs/studio.md), and [motion review](docs/animate-review.md).

## Safeguards

- **Typed, bounded requests:** model IDs, timing, finite values, samples, and constraint shapes are checked before expensive work.
- **Managed artifacts:** completed files remain within job storage and have manifest identity. Review/preview provenance is validated before presenting results.
- **Fixed editor execution:** tool arguments cannot contain arbitrary shell commands, executable paths, or Python; checked templates consume separate JSON data.
- **Explicit targets:** editor handoffs verify the intended project/rig and new output locations. Inspect receipts after uncertain timeouts before retrying.
- **Preserved originals:** revisions create new jobs; timing changes do not silently move constraints, and character work writes separate exports.
- **Local browser access:** loopback binding, host/origin checks, per-process action tokens, and allowlisted downloads. This is not a public multi-user hosting service.
- **Operator-controlled downloads:** ordinary workers are offline; model credentials stay outside tool arguments.

These boundaries describe this adapter. They do not establish the security of unrelated third-party MCP servers or externally installed editors.

## Memory and model readiness

[Upstream runtime notes](https://github.com/nv-tlabs/kimodo/blob/e1c219c99b4b94ba48be1fb6a1cbc64ecee4c954/README.md) report approximately 17 GB VRAM for all-GPU operation and under 3 GB GPU memory with text encoding on CPU. That moves the encoder requirement to system RAM.

The default 8B encoder's bfloat16 weights alone require roughly 16 GB, before overhead. Windows commit headroom can also be limiting. The studio displays an advisory below an estimated 20 GiB available-memory/commit budget for the CPU encoder. This estimate is not a guarantee or hard generation block.

Shorter clips and fewer steps do not remove that encoder requirement. The worker has no persistent text-embedding cache. A connected interface or successful import probe does not establish model access or successful inference.

## Scope and limitations

- This integrates Kimodo; it is not a new foundation model or mesh-generation service.
- The asset-specific fitted profile has 24 body bones, without independent fingers, facial animation, or cloth/hair simulation.
- Prompts and constraints influence motion but do not guarantee realism, exact choreography, unchanged body parts, or seamless stitching.
- Diagnostics are read-only review aids. Jerk amplifies noise, contact sliding depends on labels, and joint floor proxies do not detect skinned-mesh collision. There is no universal score or automatic winner.
- Root-relative comparison removes translation only, without heading/scale normalization or implicit resampling.
- HTML review allows two million coordinates across inputs and up to 24 contact-sheet instants, retaining every supported source frame in its player.
- Source review is not a benchmark reproduction. Weights, dependencies, body models, and datasets have separate requirements.
- Gameplay behavior, engine performance, collisions, and visual acceptance still require evaluation in the target project.

## What has been verified

| Area | Evidence and boundary |
| --- | --- |
| New motion | A completed local Kimodo generation produced the four-second, 120-frame wave used here. It was not substituted with an upstream example. |
| Blender | Blender 5.2.1 executed the checked character handoff and exported the fitted rig, textured GLB, FBXs, and separate project. Reference roundtrip evidence is in the editor guide. |
| Completed-motion conversion | Both BVH rest-pose options were exported from a real completed SOMA sample; both AMASS and G1 option variants were exercised with synthetic canonical fixtures. These checks verify conversion without new inference. |
| Unreal | Unreal 5.8.3 saved animation on the matching 24-bone rig: 24 tracks, 120 keys, 119 intervals, 30 FPS. Native editor inspection independently checked the result. |
| Live studio | Actual local API/GLB checks covered playback, textured rendering, planning, preserved revisions, pose anchors, timed beats, reviews, offset comparison, checksum-verified downloads, logs, Blender receipt reuse, and Unreal import. |
| Presentation | Desktop/narrow layouts were visually inspected. Final live checks had zero browser console errors and page errors. |
| Queue | Submission, cancellation before inference, disconnection behavior, serialization, and request preservation have dedicated checks. |
| Automated checks | CI runs Python tests/Ruff on Windows/Linux with Python 3.11/3.12, plus frontend tests/builds on Node 24. Wheel packaging includes the built interface and renderer notice. |

The final studio/publication checks reused the completed motion. They did **not** run fresh inference while available memory was insufficient. Validating a new anchored or multi-beat plan does not establish the quality of an animation that has not been generated.

## Troubleshooting

| Symptom | Check |
| --- | --- |
| Launcher cannot find Python | Use full Windows setup first, or the manual `.venv` route. The helper expects `mcp-env` and `runtime` under the data directory. |
| Missing/stale interface | Build with `npm ci` and `npm run build` in `web`, or use `-Build`; check `--web-directory`. |
| Connected client cannot generate | Check independent worker, runtime Python, model access/cache, and memory. |
| Worker unavailable | Start with the same data directory and inspect `worker-service-error.log`; restart after reboot/configuration changes. |
| Native dependency fetch fails | Try `-NativeDependencyFallback`; verify Git/network access and the compiler. |
| Model access denied | Check gated encoder access, local login, worker cache, and explicit download permission. |
| Allocation failure | Check free RAM and commit headroom. Draft mode still loads the encoder; no other applications are closed automatically. |
| Character preview unavailable | Verify the exact supported profile, completed source, Blender connection, and report. Arbitrary FBXs are not automatically supported. |
| Untextured preview | Rebuild/restart the current studio and check the browser console. Embedded GLB textures need the included local `blob:` resource policy. |
| Generic export finds `KimodoRig` | Use a suitable fresh Blender session; do not delete an unrelated object automatically. |
| Unreal target rejected | Verify the open project, engine, Python transport, matching imported mesh/Skeleton, and unused name. |
| Handoff timeout | Read the report before retrying; the editor may have completed later. |
| Comparison rejected | Match skeleton, hierarchy, joint names, FPS, and an offset with overlap. |

## Development and documentation

```powershell
python -m pip install -e ".[dev]"
python -m ruff check .
python -m pytest -q

cd web
npm ci
npm test
npm run build
```

Build the frontend before building a distributable Python wheel. It writes `web/dist` and the ignored package directory `src/kimodo_mcp/studio_web`. Runtime browser dependencies are bundled locally, with no CDN.

| Guide | Contents |
| --- | --- |
| [Studio](docs/studio.md) | Controls, setup, registration, memory, and recovery. |
| [Clients](docs/clients.md) | Codex, Claude Code/Desktop, and local HTTP. |
| [Natural-language workflow](docs/natural-workflow.md) | Briefs, presets, beats, revisions, and pose reuse. |
| [Animation workflows](docs/animation-workflows.md) | Coordinates, constraints, reviews, and formats. |
| [Editor handoffs](docs/editor-handoffs.md) | Blender/Unreal setup, compatibility, conversion evidence. |
| [Architecture](docs/architecture.md) | Processes, lifecycle, concurrency, storage, transport. |
| [Motion review](docs/animate-review.md) | Diagnostic limits, deterministic rendering, Animate study. |
| [Upstream review](docs/upstream-review.md) | Pinned analysis, benchmarks, and [coverage inventory](docs/source-review.csv). |

## Licenses and attribution

The bridge and studio use the [MIT License](LICENSE). Kimodo source uses [Apache-2.0](https://github.com/nv-tlabs/kimodo/blob/e1c219c99b4b94ba48be1fb6a1cbc64ecee4c954/LICENSE); checkpoints, encoder, body assets, and datasets have separate publisher terms. This repository's license does not replace them.

The viewer uses [Three.js](https://threejs.org/) under MIT, with [complete dependency notices](web/public/third-party-licenses.txt) included in source and builds. Those notices also preserve the Vite and Rolldown runtime and third-party attributions bundled by the build tools. The review study credits [cth9191/animate](https://github.com/cth9191/animate) for deterministic-timing and visual-review concepts. The implementation is original; external assistant/plugin instruction files are not bundled.

The demonstration character is a separately provided local asset. Its mesh, textures, source files, and generated motion are not redistributed here. Screenshots illustrate the workflow; the MIT code license does not grant rights to the depicted character artwork. This project is an independent integration, not an official NVIDIA, Blender, Unreal Engine, OpenAI, or Anthropic product.
