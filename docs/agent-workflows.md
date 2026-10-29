# Discover and use the complete MCP interface

An assistant can discover the supported animation workflows through MCP itself. It does not need a copy of this repository, a browser session, or an installed model to read the catalog and input schemas. Generating motion still requires the configured runtime and worker; editor operations require their respective local connections.

Start with these calls:

```json
{"tool": "get_capabilities", "arguments": {}}
```

```json
{"tool": "get_workspace_state", "arguments": {"probe_runtime": true, "recent_jobs": 5}}
```

These examples show a tool name and its arguments; use the client's normal MCP tool invocation mechanism. They are not shell commands or a separate HTTP request format.

`get_capabilities` derives its tool inventory and count from the running server. Each tool appears in one group with its registered description and annotations. The response includes the model catalog, input-schema identifiers, workflow identifiers, limits, defaults, coordinates, prerequisites, and unsupported operations. `get_workspace_state` adds the worker state, memory advisory, local editor reachability, registered character, saved Unreal target, recent jobs, and any registered opening preview.

Discovery success is not proof that inference can run. A TCP listener does not prove that the intended editor project is open. A registered opening preview is an existing asset and is explicitly identified as such; it is not the result of the current prompt.

## Workflow recipes

Call `get_workflow` with one of these identifiers:

| Identifier | Purpose |
| --- | --- |
| `generate` | Plan a natural brief, submit it, track completion, and review candidates. |
| `revise` | Change selected fields while preserving the original request and outputs. |
| `reuse-pose` | Inspect a source pose and anchor a new animation to it. |
| `inspect` | Read timing, selected frames, physical diagnostics, and a visual review. |
| `compare` | Compare compatible samples with explicit frame alignment. |
| `export-motion` | Convert completed managed motion to a compatible interchange format without inference. |
| `export-skeleton` | Prepare and execute the fixed Blender SOMA skeleton export. |
| `export-character` | Build or reuse the registered fitted-character GLB and exports. |
| `import-unreal` | Import a verified animation into an explicit matching project and rig. |
| `recover-job` | Inspect status and logs, cancel when intended, or deliberately retry. |

For example:

```json
{"tool": "get_workflow", "arguments": {"workflow": "export-character"}}
```

A recipe contains required inputs, prerequisites, ordered steps, expected results, failure guidance, and completion evidence. In a step, `arguments` contains literal values and `arguments_from` describes values obtained from inputs or previous results. For example, `steps.submit.id` means the job identifier returned by the step named `submit`. Bracket notation means construct a list. These references are guidance for the client, not executable expressions; retrieving a recipe never starts any action. Conditional steps should run only when their stated condition applies.

Keep the actual job, sample, `source_kind`, and attempt consistent across preparation, execution, and report retrieval. Pass them explicitly: the character execution tool and generic report tools do not all share the same defaults.

## Input schemas and resources

`get_input_schema` returns the current complete JSON Schema for a domain input:

```json
{"tool": "get_input_schema", "arguments": {"schema_name": "generate_request"}}
```

| Schema identifier | Describes |
| --- | --- |
| `generate_request` | Advanced model, segments, sampling, exports, and kinematic constraints. |
| `animation_brief` | Natural description, duration or beats, effort, candidates, seed, and model. |
| `animation_revision` | Explicit changes to an existing request. |
| `pose_reference` | A source job, sample index, and zero-based frame. |
| `quality_thresholds` | Diagnostic sensitivities in physical units. |
| `unreal_target` | Exact project, Skeleton, SkeletalMesh, destination directory, and asset name. |

For a tool's outer argument names, use its `tools/list` input schema. For example, `generate_motion` wraps a `generate_request` object in `request`; `generate_animation` wraps an `animation_brief` in `brief`. Optional `start_from` sits alongside `brief`.

Clients that expose MCP resources can read the same information:

| URI | Content |
| --- | --- |
| `kimodo://capabilities` | Capability catalog and complete registered tool inventory. |
| `kimodo://workspace` | Current workspace state. |
| `kimodo://models` | Exact supported model identifiers and formats. |
| `kimodo://request-schema` | Existing compatibility URI for the advanced request schema. |
| `kimodo://schemas/{schema_name}` | One of the domain input schemas above. |
| `kimodo://workflows/{workflow}` | One of the ordered workflow recipes above. |
| `kimodo://jobs/{job_id}` | Durable job status, request, and manifest. |
| `kimodo://jobs/{job_id}/outputs` | Verified output metadata for a job. |

These resources return JSON and have discoverable descriptions. The callable discovery tools remain useful in clients that do not automatically expose resources. The `animation_workflow` prompt provides a creative-brief starting point; recipes cover the more specific workflows.

## Describe and revise naturally

Use `plan_animation` before submitting a new brief when a reviewable plan is useful. It returns the exact low-level request and frame counts without loading models. `generate_animation` accepts the same brief and optional source pose, then returns a durable job identifier. Poll `get_job`; only `completed` means the generation artifacts are available.

Natural briefs default to four seconds, one candidate, seed zero, the current SOMA model, balanced effort, and the selected model's native plus interchange formats. Effort presets select 20, 50, or 80 sampling steps; they do not promise a realism level. The advanced request defaults differ: NPZ only, 50 steps, and `bvh_standard_tpose=false`. Read the plan or schema instead of assuming that both entry points have identical defaults.

For more than ten seconds, provide explicit timed beats. Each beat is a separate complete model prompt; the overall description is retained as context and is not silently prepended. The service supports up to eight beats of 0.2–10 seconds, four candidates, 100 sampling steps, 32 constraint objects, and 512 constrained frame entries in a request.

Use `revise_animation` with a rewritten description or explicit replacement beats. Its default `submit=false` only plans the change. With `submit=true`, it creates a new job and records parent lineage. Unmentioned settings remain unchanged. Timing changes never silently shift existing constraints; deliberately clear or rebuild them. An exact retry uses the saved `get_job.request` with `generate_motion`.

See [natural workflow details](natural-workflow.md) and [advanced animation controls](animation-workflows.md).

## Work with actual frames and constraints

`inspect_motion_frames` exposes selected poses as bounded numeric data. Supply 1–16 unique, increasing frame indices, an existing sample index, optional joint names, and `include_rotations` when rotations are needed. Explicit joint selections accept up to 32 unique names. A response contains at most 2,048 selected joint poses for positions, or 256 when rotations are requested; this is the selected frame count multiplied by the selected joint count. With all 77 SOMA joints, rotations fit three frames per call. Select fewer joints or split the frame range when a larger inspection exceeds the limit. Inspect the returned conventions rather than assuming that an array is an arbitrary DCC's joint order or angle representation.

For a new motion beginning at a chosen pose, pass `start_from: {job_id, sample_index, frame}` alongside `brief` to `plan_animation` or `generate_animation`. The source must be a supported canonical SOMA archive. Its world root position and heading are retained. This does not match velocity or guarantee seamless blending.

For more explicit constraints, use:

- `extract_pose_constraint` for one source frame and one destination frame.
- `extract_motion_constraints` for corresponding source and destination frame lists, up to 128 source frames.

Both operate on completed managed samples. Add the returned constraint data to an explicit `GenerateRequest`, then call `validate_motion` before `generate_motion`. Choose destination frames deliberately and keep them inside the new clip. Hand and foot constraints also constrain planar root motion under upstream semantics. Do not invent a large pose array or silently discard conflicting constraints.

Native motion uses meters, Y-up, +Z-forward, with the ground plane in XZ. Frames are zero-based. At 30 FPS, 120 samples have a four-second playback duration, while their first-to-last key span is 119/30 seconds. BVH uses centimeters; the prepared Blender script handles that conversion.

## Review and select candidates

Use `inspect_motion`, `analyze_motion_quality`, and `create_motion_review` to inspect the completed sample. A review combines fixed-camera views, an exact-frame player, a contact sheet, and diagnostic extrema. Read unavailable-metric reasons. Sliding, jerk, and bone drift can identify useful review frames, but none is a universal quality score or a substitute for seeing the motion.

`compare_motions` compares two completed samples with matching FPS and skeleton structure. The jobs may be identical when comparing two candidates from one run. A positive offset compares first-clip frame `i` with second-clip frame `i + offset_frames`. Start in `world` space to see translation; `root_relative` removes each frame's root translation while retaining heading and scale. The comparison does not retarget, resample, repair, or blend.

Choose a candidate based on observed motion, intended action, and downstream deformation. Keep the original request, model, seed, output hashes, and the reason for the selection. [Review methodology](animate-review.md) explains the diagnostics and their limits.

## Use shared studio settings and verified exports

The studio and MCP use the same workspace, jobs, character registration, saved Unreal target, and character-preview workflow. `get_unreal_target` reads the saved target. `save_unreal_target` replaces it with explicit validated fields; saving settings does not open an editor or import anything.

`list_job_outputs` returns verified file metadata, including native artifacts, completed format conversions, and the latest valid derived skeleton and character outputs for each sample, with sample, export, and attempt identity where applicable. Use the listed files rather than guessing paths. `get_artifact` remains the narrower resolver for an exact native generation-manifest entry. Review tools and execution receipts also return their managed output references. Local paths and file URIs require access to the same machine; the MCP server is not a public artifact-hosting service.

For the registered character, call `build_character_preview`. It reuses a valid completed preview or selects an unused attempt under a shared lock. It prepares and executes the fixed character export, verifies the result, and reports the attempt. If dispatch is uncertain, it refuses to silently start a duplicate. Inspect the matching receipt before considering a fresh attempt.

For a generic SOMA skeleton, use `prepare_blender_handoff`, `execute_blender_handoff` with `source_kind="skeleton"`, then `get_handoff_report` with the same arguments. The generic script uses the stable `KimodoRig` name and requires a session without a conflicting object. It preserves existing user content and does not automatically rig an arbitrary mesh.

The fitted-character profile is specific to the separately supplied source asset and checks its exact checksum. It has 24 body bones, without independent facial/finger animation or cloth/hair physics. No character source asset or model weights are bundled. See [editor setup and handoffs](editor-handoffs.md) and the [studio guide](studio.md).

## Export existing motion without inference

Call `export_motion` with a completed job and sample to export its native NPZ. The tool resolves the source through the original manifest, checks its hash and metadata, and invokes a fixed converter in the configured Kimodo/PyTorch runtime. It does not accept arbitrary source files or output paths. No diffusion inference, resampling, postprocessing, weight download, generation worker, or editor is involved; runtime dependencies and canonical skeleton assets must already be installed.

```json
{
  "tool": "export_motion",
  "arguments": {
    "job_id": "YOUR_COMPLETED_JOB_ID",
    "sample_index": 0,
    "format": "bvh",
    "bvh_standard_tpose": true
  }
}
```

`format` defaults to `bvh`. `sample_index` defaults to zero and must identify an existing source sample. Each option below is a strict boolean. Changed non-applicable options are rejected; irrelevant options left at their defaults are allowed.

| Format | Compatible source | Relevant option | Export convention |
| --- | --- | --- | --- |
| `bvh` | Canonical SOMA30 or SOMA77 | `bvh_standard_tpose=false` | Y-up, centimeter offsets/root motion, BONES-SEED rest pose. True selects standard T-pose conversion. |
| `amass` | Canonical SMPL-X22 | `amass_z_up=true` | Meters and Z-up by default; false retains Y-up. AMASS body parameters include the source FPS. |
| `csv` | Canonical G1/34 joints | `mujoco_rest_zero=false` | Meters, Z-up, +X-forward, wxyz root quaternion, 36 values per frame. True subtracts the MuJoCo rest configuration from joint DOFs. |

The source must provide explicit model, FPS, joint names, hierarchy, and proper local rotation matrices. Names/order/hierarchy must match the runtime's canonical skeleton. Format mismatch fails before publishing a completed export. Native-source and output checks include timing, array/channel structure, finite values, size, and digest; receipt verification repeats the relevant checks on reuse.

The result is a completed conversion receipt rather than a new generation job. Its `export_id` binds the source SHA-256, sample, options, exporter version, and reviewed upstream revision. Derived files live inside the original job's managed export directory. The receipt records model/skeleton, source timing, options, coordinate/rest conventions, and output checksum. `list_job_outputs` advertises only conversions whose source, assigned location, receipt, and output verify against that job. The original request, motion, and generation manifest are preserved.

An identical call revalidates and reuses a completed export. A format or option change creates a different export identity. A shared lock serializes identical conversions. If a call fails or times out, the managed attempt and log remain available for diagnosis; incomplete attempts are not automatically rerun. Resolve the actual failure with the operator before retrying. There is no arbitrary log-path reader or cleanup tool.

Use the receipt's FPS for CSV because that format has no frame-rate header. Do not claim that conversion is lossless across representations: SOMA30 expansion fills relaxed hand detail, AMASS uses default shape/hands/eyes/jaw for absent parameters, and G1 projects rotations into its joint degrees of freedom. A G1 animation export is not robot dynamics or safety validation. None of these exporters retargets an unrelated rig.

For Blender skeleton export from an NPZ-only SOMA job, first obtain a BVH receipt. Pass its exact 64-character hexadecimal `export_id` to `prepare_blender_handoff` together with the same job and sample and an explicit handoff attempt. Preparation reports `source_export_id` and `export_options`, binding that verified derived BVH. Then call `execute_blender_handoff` and `get_handoff_report` with `source_kind="skeleton"` and the same job/sample/attempt. `export_id` selects the conversion; `attempt` selects the separate editor handoff. A previously occupied attempt with a different source is not overwritten. Omit `export_id` when deliberately using the original manifest-listed BVH. AMASS and G1 CSV conversions remain downloadable artifacts rather than inputs to this SOMA handoff.

## Import into Unreal with the matching rig

First import the matching skeletal mesh and create its Skeleton through the editor's own tools. That initial mesh import is separate from this server's animation-only import. Inspect the intended open project and ensure that the local engine registration and Python transport are configured.

Use `prepare_unreal_handoff`, then `execute_unreal_handoff`, and finally `get_unreal_report`. Every call must use the same exact `target`, source kind, sample, and attempt. The target contains an absolute `.uproject` path, explicit Skeleton and SkeletalMesh content paths, a destination directory, and a new asset name. Execution checks project and rig compatibility; it does not overwrite an existing animation asset.

A successful preparation creates a script, not an imported animation. Completion requires the saved import receipt and inspection of the actual editor asset. Do not attach SOMA motion directly to Manny or another unrelated rig; perform an explicit retargeting workflow first. After a timeout, read the existing receipt before attempting another import.

## What remains outside the interface

The MCP connection exposes the implemented motion workflow, not every command or demo in upstream Kimodo. It does not provide arbitrary mesh generation, general-purpose character fitting, initial Unreal mesh import, arbitrary external file conversion/import, model training, benchmark execution, video/audio rendering, arbitrary code execution, or automatic motion repair and stitching. Compatible export of completed managed NPZ samples is supported through `export_motion`.

Dependency installation, authentication, model-download permission, worker startup, editor startup, and character-source registration are operator setup. The server does not accept arbitrary executable or read/write paths to bypass those boundaries. The Unreal target's explicit project/content paths are part of its bounded import contract.

The browser still owns interactive viewing controls such as orbiting, scrubbing, playback speed, and wireframe display. An agent can create and locate the same verified previews through MCP without pretending that a background tool call is a visual inspection. Discovery and recipe retrieval do not alter motion, load model weights, or execute editor scripts.

If a client has an older cached tool list, reconnect it or start a new client session and call `get_capabilities` again. Compare the returned inventory with the operations you intend to use; do not invent a tool because it appears in an upstream README. See [client configuration](clients.md) for stdio and local Streamable HTTP connections.
