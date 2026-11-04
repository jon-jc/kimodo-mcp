# Native Kimodo feature coverage

The bridge offers two complementary workspaces. **Studio and MCP** use validated requests, managed jobs, immutable source versions, checksums and explicit editor handoffs. **The original Kimodo workspace** provides the upstream visualizers, constraint timeline, example browser, local motion loading and render controls in a separate session. Opening the original UI does not import its state or output files into managed job history.

This inventory is grounded in upstream revision [`e1c219c99b4b94ba48be1fb6a1cbc64ecee4c954`](https://github.com/nv-tlabs/kimodo/tree/e1c219c99b4b94ba48be1fb6a1cbc64ecee4c954). “Available in the native workspace” describes the reviewed upstream implementation; it is not a claim that every model, body asset, renderer or generated result has been executed on the current machine.

## Open the complete original workspace

1. Install the separately licensed Kimodo runtime, model dependencies and upstream `demo` extra in the operator's runtime environment. The demo depends on NVIDIA's Viser fork. Additional SOMA/SMPL-X body assets may be needed for their optional visualizers. The lightweight bridge installation does not install these dependencies.
2. Configure `KIMODO_REPO` to the clean pinned checkout and `KIMODO_PYTHON` to that runtime Python. Keep authored files in the data directory, outside the upstream checkout.
3. Call `get_native_workspace`. It reads bounded local receipts and checks the owned lease; it does not load a model.
4. Call `start_native_workspace` with an exact model identifier from `list_models` only when ready to load that model. Starting may require substantial CPU/GPU memory. Status is initially `starting`, not proof that a UI or model is ready.
5. When the owned worker reports `ready`, open the returned fixed URL, `http://127.0.0.1:8769`. The Studio native-controls panel provides the same explicit Start/Open/Stop flow.
6. Call `stop_native_workspace` when finished. It requests that the worker carrying the matching run identity stop itself. The bridge never accepts an arbitrary process ID to terminate.

Starting is always explicit. Downloads remain disabled unless the operator sets `KIMODO_ALLOW_DOWNLOADS=1`; missing cached models fail rather than silently changing that policy. The native process holds the shared generation lock for its entire lifetime, including idle periods, because its loaded models consume memory. Managed generation waits until it is stopped and can reach its configured waiting timeout.

The native server binds only to loopback. Readiness requires the owned worker's matching identity, a fresh heartbeat, its held lease, the actual Viser host/port and a listening socket. A different service occupying port 8769 is never adopted or stopped. Failed or unresponsive startup exposes a bounded log tail. Unexpected process termination is reported on subsequent status reads; a native session is not a persisted managed generation job.

The worker copies bundled examples into `<data_dir>/native-workspace/examples` before importing the demo. It does not move or overwrite upstream files or existing authored examples. The working directory is `<data_dir>/native-workspace`. The original UI's local-file controls retain their upstream behavior, including operator-selected paths; they are not the bridge's restricted managed-artifact API. Use trusted local inputs and export intentionally.

## Generation and request controls

| Native feature | Managed MCP / Studio support | Original workspace / boundary | Evidence |
| --- | --- | --- | --- |
| Dataset, skeleton and model version | `list_models`; exact `GenerateRequest.model`; native-controls model selector | Native dataset/skeleton/version selectors | `kimodo/model/registry.py`; bridge `catalog.py`; `test_schemas.py` |
| Nine released generation models | All reviewed SOMA RP/SEED, ArdyRep, SMPL-X RP and G1 RP/SEED identifiers | Native model picker; weights/access still external | Registry and catalog above |
| TMR model | No generation endpoint for TMR | Retrieval/evaluation model, not an additional text-to-motion generator | `kimodo/model/tmr.py`, `kimodo/metrics/tmr.py` |
| Single prompt and ordered prompts | `segments`, simple briefs and sequence composer; exact quantized frame plan | Native draggable prompt timeline | `kimodo/model/kimodo_model.py`; `schemas.py`, `planning.py`, `test_planning.py` |
| Seed and candidates | Explicit seed; 1–4 candidates | Native demo allows up to 10 samples outside hosted mode | `kimodo/demo/ui.py`; intentional managed resource bound |
| Diffusion steps | 1–100, rechecked against model schedule | Native slider offers 2–1000 | `worker.py`, `test_worker.py`; intentional managed bound |
| Separate text/constraint guidance | `cfg_type="separated"`; `cfg_text`, `cfg_constraint` | Native enabled-guidance toggle and two weights | `kimodo/model/cfg.py`; `test_native_controls.py` |
| Disable classifier-free guidance | `cfg_type="nocfg"`; weights retained but unused | Native guidance disabled | Same source/test |
| Combined classifier-free guidance | `cfg_type="regular"`, scalar `cfg_regular` | Native model API supports this mode; demo toggle exposes separated/no-CFG | Same source/test |
| Initial heading | `first_heading_angle` in radians | Native starting-direction controls | `kimodo/model/kimodo_model.py`; `schemas.py`, `worker.py` |
| Transition overlap | `transition_frames`; exact segment/overlap ranges and advisories | Native transition slider | `_multiprompt` in upstream model; `test_schemas.py` |
| Foot-skate/constraint postprocessing | `postprocess`, `root_margin`; effective state in plan and manifest | Native postprocess toggle; unavailable for G1 | `kimodo/postprocess.py`; `worker.py`, `test_worker.py` |
| G1 hinge-axis/limit projection | `real_robot_rotations=true` only for G1; consistent local/global rotations and positions in artifacts | Native “Real robot rotations” toggle | `kimodo/exports/mujoco.py`; `test_native_controls.py` |

Guidance weights are finite and bounded to 0–20. Inactive weights are preserved when switching modes or revising a complete request. `plan.guidance.weights` is `null` when guidance is disabled. This distinguishes “no CFG” from separated guidance with two zero weights, which is a different native denoiser branch.

G1 projection runs the native converter's one-axis-per-hinge projection, XML limit clamp and forward kinematics before publication. The worker replaces all four resulting kinematic arrays together. It retains the model's root trajectory, heading and predicted contact labels, as the demo does. Projection can change feet and constrained positions; it does **not** establish balance, collision avoidance, actuator feasibility or safe robot execution. `plan.advisories` and manifest warnings make that distinction explicit. The CSV rest-zero convention is a separate export setting.

Existing saved plans retain their original IDs and request checksums when a release adds defaulted request fields. Loading validates the old recorded fields; generation and revision apply current compatible defaults. New plans record the expanded request. A new CFG/projection setting never silently rewrites the old receipt.

## Constraints, editing and playback

| Feature | Managed support | Native workspace / remaining difference |
| --- | --- | --- |
| Root position/path and optional heading | Typed `root2d` arrays in `GenerateRequest.constraints`; source-frame extraction | Native editable waypoints and dense-path interpolation are provided by the original UI, not inferred by the MCP schema |
| Full-body keyframes | Typed axis-angle poses and root translations; `extract_pose_constraint`, `extract_motion_constraints` | Native full-body constraint track and visual pose controls |
| Hands and feet | Named `left-hand`, `right-hand`, `left-foot`, `right-foot` constraints; generic `end-effector` for reviewed names | Native end-effector interval/keyframe controls |
| Mixed and repeated constraints | Typed list, shape/frame bounds and overlap-conflict checks; sparse-keyframe advice | Native constraint tracks, interval dragging, snap/reset/undo and local/world editing |
| Editing a completed motion | SOMA source inspector, exact one-frame draft FK preview, trim/world placement/local offsets, immutable saved variants | This is an additional bridge workflow, not a replacement for native generation constraints or a general IK/retargeting solver |
| Playback, source skeleton and character | Studio source skeleton, fitted-character GLB where supported, frame controls, comparisons and read-only quality reports | Original SOMA skin/SomaLayer, SMPL-X skin, G1 meshes, foot-contact display, mesh opacity, camera FOV and native constraint overlays remain in the original UI |
| Bundled examples | Native workspace copies and exposes reviewed upstream example files | Managed Studio starters are authored text/beat templates, not an import of the entire upstream example session |
| Source motion import | Managed source tools operate on verified job artifacts | Original UI loads native NPZ, supported SOMA BVH, AMASS and G1 CSV; arbitrary file loading is deliberately not exposed as an MCP generation argument |

The typed constraint definitions are in `schemas.py`; conversion is in `worker.constraints_to_upstream`. Source extraction and validation are exercised by `tests/test_motion_access.py`, `tests/test_schemas.py`, `tests/test_worker.py` and the managed source/variant tests. Native editing semantics are implemented in upstream [`kimodo/viz/constraint_ui.py`](https://github.com/nv-tlabs/kimodo/blob/e1c219c99b4b94ba48be1fb6a1cbc64ecee4c954/kimodo/viz/constraint_ui.py) and [`kimodo/demo/ui.py`](https://github.com/nv-tlabs/kimodo/blob/e1c219c99b4b94ba48be1fb6a1cbc64ecee4c954/kimodo/demo/ui.py).

Upstream recommends sparse authored poses, generally fewer than 20 unique frames per constraint type, with dense root paths exempt. The bridge reports advice and per-segment density without rejecting valid dense requests. Later prompts generate extra history and revise the preceding segment's tail; the requested action frame allocation is preserved. A prompt-only revision keeps constraints at their original frame indices. Changing the per-segment quantized allocations requires explicitly clearing or rebuilding constraints.

## Exports and external pipelines

| Operation | Managed route | Native route / limitation |
| --- | --- | --- |
| Native NPZ | Requested during generation, hash-bound downloads | Native save/download |
| SOMA BVH | Generation or `export_motion`, native/standard-T-pose option; optional `export_id` for Blender | Native BVH save/download; coordinates/rest frames must be interpreted correctly |
| SMPL-X AMASS | Generation or completed-source export with explicit Z-up convention | Native AMASS export; model/body licenses remain separate |
| G1 CSV | Generation or completed-source export with explicit rest-zero option | Native MuJoCo qpos export; not a robot safety certification |
| PNG screenshot and MP4 render | No managed rendering tool claiming native equivalence | Original UI's Screenshot and Video export controls; actual renderer/dependencies must work locally |
| Blender/Unreal handoff | Fixed reviewed scripts, operator-selected targets, exact source/attempt receipts | Additional bridge capability; not an upstream universal retargeter |
| FreeMoCap body/hands/face review | Verified process/export receipts, exact selected landmarks, body QA and bounded viewer | Landmark coordinates are not native Kimodo rotations or an automatically compatible game rig |

See [completed-motion/editor handoffs](editor-handoffs.md), [FreeMoCap capture review](capture-review.md), [rig corrections](rig-corrections.md) and [agent workflows](agent-workflows.md) for the relevant contracts. A captured skeleton, a Kimodo output skeleton and an Unreal target skeleton are distinct representations. Cleanup, bone mapping, rest-pose alignment, scale, root motion and retargeting require explicit supported conversion and evidence; matching the word “skeleton” does not establish compatibility.

## Validation scope

`tests/test_native_controls.py` verifies strict request modes, scalar/list guidance dispatch, inactive-weight preservation, G1-only validation, projected-array publication/revalidation and old saved-plan identity compatibility. A separate CPU-only synthetic check against the pinned G1 implementation compared two six-frame samples with the native demo projection and independent forward kinematics; all maximum differences were zero. It loaded no generation model or capture/editor service.

`tests/test_native_demo.py` verifies explicit fixed-argument launch construction, nonce-bound stop requests, owned-heartbeat readiness, external-port refusal, generation-lock exclusion, bounded logs, pinned-checkout checks, preserved example copies and fake-runtime lifecycle. These are lifecycle tests, not a claim that the optional native demo and every visualizer were launched in this release. `tests/test_protocol.py` checks actual MCP discovery and contracts. The full upstream UI is accessed as its own workspace; individual native GUI gestures are not disguised as extra managed MCP tools.
