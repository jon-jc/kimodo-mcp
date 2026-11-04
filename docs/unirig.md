# Model rigging with UniRig

The Studio **Rig a model** workspace and the MCP tools expose the released UniRig preprocessing, skeleton prediction, skin prediction and merge stages. They share registered assets, saved jobs and verified output files. A rig is the skeleton and its mesh weights; it is not an animation, a retarget map or proof that a character will deform naturally.

The adapter targets the clean [UniRig checkout at `6793c6640ff01c8fb389f3993434124bb43d2933`](https://github.com/VAST-AI-Research/UniRig/tree/6793c6640ff01c8fb389f3993434124bb43d2933). At this revision, skeleton and skin checkpoints trained on Articulation-XL2.0 are released. The paper's Rig-XL/VRoid model checkpoints and bone-attribute/stiffness prediction are not released. Training and RigNet evaluation code exist upstream but are separate operator workflows; this adapter does not accept arbitrary task files or training commands.

## Set up a separate runtime

Install the reviewed upstream runtime separately from the lightweight MCP environment and the Kimodo generator environment. The upstream instructions specify Python 3.11 and a compatible PyTorch/CUDA stack. Its requirements include Blender's `bpy` package, model and geometry libraries, FlashAttention, spconv, torch-scatter and torch-cluster. Binary wheels must match the selected Python, Torch and CUDA versions. A working desktop Blender installation alone does not satisfy this environment.

Configure the bridge process with operator-owned absolute paths:

```powershell
$env:KIMODO_UNIRIG_REPO = 'D:/MotionRuntime/UniRig'
$env:KIMODO_UNIRIG_PYTHON = 'D:/MotionRuntime/unirig-env/Scripts/python.exe'
```

Keep the upstream checkout clean at the reviewed commit. Each job receives its own copied configuration tree and output directory; the bridge does not edit the checkout. VRM support additionally requires the modified addon documented by upstream to be installed in the separate `bpy` runtime. The bridge does not install dependencies, register addons or download checkpoints during discovery or planning.

`get_unirig_capabilities` reads configuration and checkout evidence without importing a model. `plan_unirig` also discovers the required packages in the selected interpreter. A successful plan establishes configuration and module presence, **not** CUDA execution, enough GPU memory, cached checkpoint availability or expected rig quality. Preprocessing and merge have a smaller package check than inference, but still require the upstream geometry/Blender runtime.

Downloads are disabled unless the operator explicitly sets `KIMODO_ALLOW_DOWNLOADS=1` before starting the bridge. With downloads disabled, stage processes use offline Hugging Face settings and reject Python socket connections. Provision the released weights in the runtime's normal Hugging Face cache or permit downloads intentionally. When inference runs, the bridge records hashes of the actual checkpoints returned by upstream's loader. Models and datasets are not bundled with this project. UniRig's source uses the MIT license; assets, model files and dependencies retain their own terms.

## Choose a useful path

| Operation | Inputs and result | When to use it |
| --- | --- | --- |
| `preprocess` | Registered mesh → normalized `raw_data.npz` | Check that the upstream mesh preprocessing path accepts an asset. No rig is predicted. |
| `skeleton` | Registered mesh → `skeleton.fbx` | Predict bones, then inspect missing limbs, hierarchy and placement in Blender before skinning. |
| `skin` | An existing rigged input, a registered edited rig, or a prior verified skeleton → `skin.fbx` | Predict weights after the skeleton is satisfactory. |
| `merge` | Original registered mesh plus explicit rig source → `rigged.glb` or `rigged.fbx` | Transfer the selected skeleton/skin result onto the original mesh. |
| `pipeline` | Registered mesh → skeleton, skin and merged files | Run all released stages without an intermediate manual edit. Review every resulting rig. |

The complete pipeline reimports the predicted skeleton FBX before skin prediction. This follows upstream's edited-rig path and does not assume that skeleton inference produced an intermediate NPZ. If a tail, wing or other important bone is missing, use the separate skeleton stage, edit its output in Blender, register that edited FBX, and provide its `rig_asset_id` to the skin stage. After skinning, merge the verified skin output onto the original model. Merging a skeleton-only output does not invent skin weights.

Each request contains 1–4 distinct mesh asset IDs. The input formats are GLB, FBX, OBJ and VRM. Each file is limited to 512 MiB and all selected mesh/rig inputs together to 1 GiB. A batch runs its items sequentially. Item `n` uses `(seed + n) mod 2^32`; multiple stochastic variations of one mesh should be separate jobs because duplicate mesh IDs are rejected within a batch.

UniRig requires geometry-only OBJ files: `mtllib` and `usemtl` directives are rejected during planning and checked again before staging, including whitespace/case variants. OBJ lines are limited to 64 KiB. Export a self-contained GLB when materials matter. This preserves the registered OBJ bytes rather than silently stripping materials from the selected source. FBX uses the trusted upstream importer; disabling its image search is not a guarantee that explicit external texture references cannot be read. Such sidecars are not copied or fingerprinted by this adapter, so prefer embedded textures or self-contained GLB for reproducible results.

## Native inference controls

`get_input_schema("unirig_request")` returns the exact complete typed schema. Controls are preserved in the request receipt and copied native configs:

| Field | Default and managed range | Applies to |
| --- | --- | --- |
| `seed` | 12345; integer 0–4294967295 | Every operation |
| `faces_target_count` | 50000; integer 100–100000 | Mesh extraction when that stage runs; it does not simplify the final original mesh during merge |
| `do_sample` | `true`; strict boolean | Skeleton, pipeline |
| `max_new_tokens` | 2048; integer 1–2048 | Skeleton, pipeline |
| `num_beams` | 15; integer 1–30 | Skeleton, pipeline |
| `top_k` | 5; integer 1–100 | Sampling in skeleton/pipeline |
| `top_p` | 0.95; greater than 0 and at most 1 | Sampling in skeleton/pipeline |
| `temperature` | 1.5; greater than 0 and at most 5 | Sampling in skeleton/pipeline |
| `repetition_penalty` | 3.0; 0.1–10 | Skeleton, pipeline |
| `skin_backend` | `pyrender` or `open3d` | Skin, pipeline |
| `output_format` | `glb` or `fbx` | Merge, pipeline |
| `add_root` | `false`; strict boolean | Merge, pipeline |

With `do_sample=false`, native decoding does not use top-k, top-p or temperature. Their requested values remain in the receipt so switching sampling back on is explicit. The token cap is a decoder limit, not a requested bone count; lowering it can truncate the output or cause prediction to fail. Larger beam counts and face targets can increase work without guaranteeing better deformation.

Nondefault controls for an inapplicable operation are rejected. The reviewed class conditioning, tokenizer, checkpoint references, model architecture, precision and accelerator remain fixed. The adapter selects one device, one node, one result per item and zero loader workers, and removes optional training/logging callbacks. It does not claim arbitrary native YAML configuration parity.

## Studio and MCP sequence

1. Import a mesh in **Assets & props** or call `import_studio_asset`. A library character entry does not replace the registered animation target.
2. Open **Rig a model**, choose the operation and 1–4 meshes, and set applicable controls. For skin or merge, choose an existing rig asset or the exact item/output from a completed UniRig job.
3. Run **Check rigging plan** or `plan_unirig`. Resolve failed runtime checks. Changing the request requires a new plan.
4. Run **Start checked rigging** or `start_unirig` with that same request. Submission returns a job ID; it does not mean the mesh is rigged.
5. Poll `get_unirig_job`. `list_unirig_jobs` restores history after an agent/browser restart. Read `get_unirig_log` for the selected job, or explicitly request `cancel_unirig_job` if needed.
6. Require `status="completed"`, a verified `output` manifest and no `verification_error`. Use `get_unirig_artifact` with an exact manifest path; do not infer output filenames from a failed job.
7. Review the GLB in Studio where available and inspect bones/weights in Blender. `register_unirig_output` copies a verified GLB/FBX back into the asset library while preserving the current character registration.

Example complete request after importing a mesh:

```json
{
  "operation": "pipeline",
  "assets": [{"asset_id": "<64-character registered asset ID>"}],
  "seed": 42,
  "faces_target_count": 50000,
  "do_sample": true,
  "max_new_tokens": 2048,
  "num_beams": 15,
  "skin_backend": "pyrender",
  "output_format": "glb",
  "add_root": false
}
```

To use edited bones for skinning, choose `operation="skin"` and set `assets[0].rig_asset_id` to the registered edited file. To merge its eventual skin result, choose `operation="merge"` with the original mesh ID, `source_job_id` of the completed skin job, `source_item_index=0` and `rig_output="skin"`. A registered rig and prior job source cannot be supplied together.

## Preservation and failure handling

Jobs live under `<KIMODO_DATA_DIR>/unirig/jobs/<job_id>`. Input copies, request identity, configuration hashes, source revision, seeds, checkpoint observations and output checksums are recorded. Sources and configurations are verified before publication; completed artifact access checks the saved request/manifest and recorded files again. A changed output is not served as a verified success.

The supervisor rechecks the saved request and clean pinned runtime after acquiring the shared generation lease, before and after each stage, and before publication. Each external stage also validates its request against the checksum passed by the supervisor before using the runtime paths. A checkout change while waiting or processing therefore fails the job instead of publishing output under a stale reviewed revision.

The supervisor runs fixed Python entry points rather than upstream shell launchers. It shares the generation lease with Kimodo, so another loaded native session can make it wait. Cancellation stops only the stage process owned by this supervisor. Failed, cancelled or interrupted jobs retain evidence and never restart automatically. If a supervisor exits while an owned stage survives, a new job is blocked until that stage exits; inspect the local processes and evidence before operator recovery.

Format/header/hash checks establish output identity and basic container structure. They do not certify bone placement, weight sums across every vertex, material fidelity, facial rigs, collision, contact, or engine acceptance. Imported sidecar textures are not bundled. UniRig's geometric merge can alter alignment; review orientation, scale, hierarchy and deformation with representative poses. An arbitrary generated skeleton is not automatically compatible with the existing fitted-character handoff, SOMA or Unreal Manny.

## Verification scope

Tests cover strict controls, fixed stage arguments, pipeline order, source and configuration preservation, missing/truncated exports, changed receipts, cancellation and interrupted workers using synthetic data and fake runtimes. The configuration transformation was also run on a copy of all 26 YAML files from the real pinned checkout, including the skin backend and decoding controls. That check loaded no model and performed no rig prediction. Actual CUDA inference, new-model deformation and Unreal acceptance remain separate runtime/asset validation steps.
