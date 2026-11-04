# FreeMoCap post-processing

The FreeMoCap adapter exposes explicit stages for local reference videos: inspect synchronization metadata, synchronize, import a new recording, calibrate, process landmarks and export through FreeMoCap's Blender integration. It talks to an independently installed loopback service. It never starts cameras, installs dependencies, starts the service automatically or calls a stop-all endpoint.

The reviewed source is [FreeMoCap `700c8614d11d886dacfea8e52333d3c0b942ef52`](https://github.com/freemocap/freemocap/tree/700c8614d11d886dacfea8e52333d3c0b942ef52), version `v2.0.0-alpha.25`. This is the FastAPI/Electron v2 implementation. Other releases have different routes and processing behavior. The service's actual preferred port is 53117; older README port examples are not the adapter contract.

## What the output means

One-camera FreeMoCap processing places image detections on a flat XZ plane, with Y equal to zero and pixel-scale magnitudes. It does not infer monocular depth or metric 3D. Set `allow_planar=true` only when that result is intended. Its constructed zero reprojection errors do not establish accuracy.

Metric multi-camera reconstruction requires synchronized views, matching camera identities and an explicit measured calibration. Equal frame counts alone do not establish synchronization. Occlusion, calibration error, synchronization error and tracking mistakes still affect reconstructed motion. Ground-plane and cleanup options are processing choices, not evidence of animation quality.

FreeMoCap landmarks are not SOMA joint rotations or a neutral skeleton. They cannot be passed directly to Kimodo pose anchors, correction variants or the registered-character handoff. Game-engine use requires an explicit retarget setup and visual verification. This integration makes no automatic Unreal compatibility, contact-quality or production-quality claim.

## Operator setup

Use a separate FreeMoCap environment and the reviewed checkout. Its dependency graph includes its own tracking, camera, filtering, synchronization and Blender export packages; it should not be installed into the Kimodo model environment. Consult the pinned upstream installation and lockfile for platform-specific CPU/CUDA choices and licensed dependencies. Tracking in this external runtime may download model files. Planning does not download them.

Create existing recording and reference directories. Configure `<KIMODO_DATA_DIR>/freemocap/runtime.json` with exactly these fields:

```json
{
  "base_url": "http://127.0.0.1:53117",
  "recordings_root": "D:/MotionCapture/recordings",
  "references_root": "D:/MotionCapture/references"
}
```

The adapter accepts only a loopback HTTP origin. It disables environment proxies and redirects. It resolves `localhost` to literal loopback. Roots must be absolute existing directories. Agent-facing video and calibration names are relative references within the configured roots, using forward slashes. They are not arbitrary absolute file paths or URLs.

### Durable status extension

The upstream HTTP API returns pipeline IDs for calibration and processing, but does not expose durable completion. Its WebSocket stream shares consumable progress queues with the existing UI. Connecting another client merely to observe completion can consume those events; the adapter does not do that.

The original optional module `kimodo_mcp.freemocap_status_extension` solves this at the runtime boundary. It mirrors aggregator terminal messages when they are published, preserving delivery to the original queue. It stores atomic observations in an operator-selected directory and adds bounded status routes. No FreeMoCap source is copied or edited. A completed observation requires an actual matching terminal event and stopped pipeline workers. The controller additionally verifies recording identity, source checksums and result files.

An operator must launch the runtime with this extension before planning or starting calibration, processing or export. The basic metadata, synchronization and import operations can use the compatible upstream service without it. Example PowerShell setup, using installation-specific paths:

```powershell
$env:PYTHONPATH = 'D:/Code/kimodo-mcp/src'
& 'D:/MotionRuntime/freemocap-env/Scripts/python.exe' -m kimodo_mcp.freemocap_status_extension --repo 'D:/Code/FreeMoCap' --status-directory 'D:/MotionCapture/status' --port 53117
```

The launch module imports only standard-library code until invoked. Use the bridge source on `PYTHONPATH` or an installed bridge package visible to the separate runtime. The launcher verifies the exact clean checkout, imported version and hook contract before installation. It binds `127.0.0.1` and fails normally on an occupied port; it does not use upstream's entry point that can terminate a process on the preferred port. The service's own startup and telemetry settings still apply.

An outer guard covers both the observer and upstream HTTP/WebSocket routes. It requires a loopback client and the configured local Host/port. Requests without a browser Origin and same-origin browser requests are accepted; foreign origins and `null` origins are rejected. For a separate local web frontend, the operator can explicitly add `--allow-origin http://localhost:3000`. This accepts a specific loopback HTTP origin, not a wildcard. Native Electron `file:`/`null` origins are not automatically trusted. These protections belong to this optional launcher; an independently started unmodified upstream service retains its own policies.

The observer retains the actual pipeline object through verified worker exit. It does not infer death merely because upstream removed a pipeline from its manager during cancellation or cleanup. Records are kept under a unique service-instance subdirectory. A restarted service cannot satisfy an unverified prior instance's receipt; already verified receipts remain usable while their output checksums still match.

`get_freemocap_capabilities` reports base `compatible` separately from `status_extension.available`. Base compatibility checks the reported version and required route presence; the extension's launcher checks the actual source revision. Neither check proves model readiness, camera calibration or capture accuracy. No installed runtime or real video processing is claimed by this release's synthetic tests.

## Shared stage and review operations

| Tool | Purpose |
| --- | --- |
| `get_freemocap_capabilities()` | Read configured service compatibility, status-extension availability and limitations. |
| `list_freemocap_recordings(limit=20)` | List at most 100 recordings contained in the operator root. |
| `inspect_freemocap_recording(recording_id)` | Read upstream readiness and contained output paths, sizes and hashes. File presence alone is not pipeline success. |
| `plan_freemocap_capture(request)` | Validate a stage and inspect its fixed upstream payload without submitting that stage. |
| `start_freemocap_capture(request)` | Explicitly submit that stage and retain a local receipt. |
| `get_freemocap_job(job_id)` | Poll this adapter's receipt and, where available, bound completion evidence. |
| `cancel_freemocap_job(job_id)` | Cancel only the exact calibration/processing pipeline in this adapter's receipt. |
| `inspect_freemocap_frames(job_id, frame_indices=[], aspect="body")` | Read up to 16 original body, hand or face frames from verified completed outputs, preserving missing-point masks and native units. |
| `analyze_freemocap_quality(job_id)` | Read body output availability, consecutive gaps, adjacent-frame movement and suggested review frames without a quality score. |
| `get_freemocap_artifact(job_id, path)` | Verify and locate an exact output path listed in a completed process/export receipt. This does not run an export. |

Job IDs are 32 lowercase hexadecimal characters. Recording IDs are bounded identifiers, not paths. Cancellation is unavailable for synchronization because the pinned upstream has no scoped synchronization cancel route. It never shuts down the service or stops all pipelines.

## Review saved results with the service stopped

Use `list_motion_sources(source_kind="freemocap", limit=30, offset=0)` to recover shared stage history, including receipts created by another browser or agent. This metadata-only index does not contact the capture service or verify outputs. Keep `source_kind` with `source_id`, inspect the returned source-specific receipt, and distinguish recorded status from current verification. `get_workflow("resume-motion")` provides the complete cross-source recovery recipe.

Verified completed process/export receipts support offline frame inspection, body diagnostics and output retrieval. The existing operator registration, original sources, calibration evidence and saved files must still match. Pending or uncertain outcomes require the original service evidence and are not completed by offline review.

In **Create → Open capture workspace**, selecting a completed processed take opens the read-only body landmark viewer. Load a contiguous window, scrub original frames, select a point to inspect its native coordinates, and follow the numerical review's suggested frames. The viewer shows body points and virtual centers. Hands and face are available through `inspect_freemocap_frames` using the appropriate `aspect`; they are not currently rendered in this body viewer.

The review preserves missing points as `null` triplets with a validity mask. It distinguishes planar `pixels`, proven measured `millimeters` and otherwise unspecified `calibration_units`; no meter scale is guessed. New receipts preserve nominal source FPS, while legacy receipts without timing remain frame-based. Diagnostics describe processed output after interpolation/filtering, not raw tracking confidence, contact accuracy or anatomical acceptance.

See [capture review, profiles and agent examples](capture-review.md) for exact tools, bounded preview/download routes, body/hand/face ordering, virtual-point semantics, source citations and verification limits. Offline inspection never runs tracking or Blender; producing a new export remains a separate explicit stage.

## Stage-by-stage workflow

Use the same request for plan and explicit submission. Only fields applicable to the selected operation may be supplied; omit irrelevant options instead of copying every default field from a schema.

**Inspect timing or synchronize.** Select at most eight reference videos with unique filename stems:

```json
{"operation":"check_sync","video_files":["walk/cam0.mp4","walk/cam1.mp4"]}
```

Planning already reads video metadata. Submitting `check_sync` retains that check as a receipt; it does not alter the videos. For synchronization use `operation="synchronize"` and `sync_method="audio"` or `"brightness"`. The latter accepts `brightness_ratio_threshold` from 1 to 1000000, default 1000. Poll the returned synchronization receipt to completion.

References must be nonempty supported video files, at most 4 GiB each and 16 GiB combined. Videos are bounded to ten minutes, 144000 frames and 240 fps, with matching frame rates within 0.01 fps. Explicit resampling belongs outside this adapter. A matching frame count is reported as metadata, not verified temporal alignment.

**Import a new take.** Supply the same original references and the completed adapter synchronization receipt:

```json
{"operation":"import","video_files":["walk/cam0.mp4","walk/cam1.mp4"],"sync_job_id":"<32-character adapter job id>"}
```

For already synchronized multi-camera footage, omit `sync_job_id` and explicitly set `synchronization_confirmed=true`; frame counts must still match. The adapter assigns a unique recording name, verifies imported files and records ownership. It preserves original references. Existing external recordings may be inspected, but mutation requires an adapter-imported recording belonging to this configured runtime.

**Calibrate a separate board recording.** Import the synchronized calibration footage as its own take, then provide measured ChArUco geometry:

```json
{
  "operation":"calibrate", "recording_id":"<imported calibration recording>",
  "board":{"squares_x":5,"squares_y":7,"square_length_mm":40,"marker_length_ratio":0.8,"aruco_dictionary_enum":2},
  "use_groundplane":false
}
```

Board dimensions permit 2–30 squares per axis, square length up to 1000 mm, marker ratio strictly between zero and one and dictionary enum 0–20. The fixed solver is Anipose. Calibration writes both local results and the upstream global calibration library/last-successful calibration; submitting it is an explicit configuration-affecting action. Poll for verified successful completion before using it elsewhere.

**Process the subject recording.** Select either a relative calibration TOML within the reference root or `calibration_recording_id` from a completed calibration take:

```json
{
  "operation":"process", "recording_id":"<imported subject recording>",
  "calibration_recording_id":"<completed calibration recording>",
  "process_options":{"detector_type":"mediapipe","cutoff_hz":6,"filter_order":4}
}
```

Calibration camera IDs must match the imported video IDs. A single-camera planar request must instead omit calibration and explicitly set `allow_planar=true`. Processing forces upstream automatic Blender export and editor opening off.

The detector choices are MediaPipe and RTMPose. RTMPose exposes three reviewed model variants and a confidence threshold; MediaPipe exposes detection, presence and tracking confidence. Filtering uses the actual metadata frame rate and requires cutoff strictly below half that rate; filter order is 1–8. Triangulation controls bound minimum cameras to 2–8 and maximum cameras to drop to 0–3. The target reprojection error is in undistorted normalized coordinates, not pixels. These controls do not expose an independent interpolation or filtering stage.

**Export explicitly.** After verified processing completion, plan and submit a separate export:

```json
{
  "operation":"export", "recording_id":"<successfully processed recording>",
  "export_options":{"formats":["fbx","bvh"],"rest_pose":"tpose","apply_foot_locking":false,"limit_hand_markers_range_of_motion":false}
}
```

Export invokes background Blender through FreeMoCap without opening an editor window. This upstream export supports MediaPipe output only; RTMPose export is rejected. Animated glTF is not supported. The adapter checks the `.blend` result and requested nonempty FBX/BVH artifacts, but reports `export_settings_verified=false`: upstream may fall back to addon defaults when loading configuration. Requested rest pose or cleanup is not independently certified by file existence. Inspect the resulting rig and motion before engine use.

## Receipts, failures and preservation

Local receipts live at `<KIMODO_DATA_DIR>/freemocap/jobs/<job_id>.json`. They bind source fingerprints, the selected operation, configured service identity and any upstream pipeline ID. The extension additionally binds calibration/processing observations to its service instance, recording and pipeline type. A restart before verified completion cannot silently turn a stale receipt into success or cancel an unrelated pipeline.

`submitted` means accepted, not completed. `submission_unknown` means a transport or verification failure left the outcome uncertain; do not automatically repeat it. Stages writing the same recording are blocked while previous work lacks verified terminal evidence. Export requires a verified successful processing receipt. Completed output hashes are checked again when the receipt is reused. Failed or cancelled work may leave partial files; import a new take rather than overwriting them.

`pipeline_completion_verified=true` describes evidence for the terminal status: always check `status` too, because a verified failure remains a failure. `inspect_freemocap_recording` reports observed readiness separately and does not claim pipeline completion. Results include NPY landmark arrays, CSV/Parquet data, calibration TOML and optional Blender/FBX/BVH files as actually present. The upstream tracker-schema metadata has a known RTMPose-label issue after MediaPipe processing; it is not sufficient proof of skeletal layout or a conversion mapping.

The external [FreeMoCap source license](https://github.com/freemocap/freemocap/blob/700c8614d11d886dacfea8e52333d3c0b942ef52/LICENSE) is AGPL-3.0; its dependencies retain their own terms. The bridge does not bundle upstream implementation or models. Tests use synthetic files, fake HTTP responses and publication-queue fixtures. They verify adapter behavior and evidence handling, not tracking or calibration performance on actual footage.
