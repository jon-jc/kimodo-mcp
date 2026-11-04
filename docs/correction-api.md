# Correction preview and revision API

`preview_motion_variant(job_id, edit, sample_index=0, frame_indices=[])` validates and computes exact source-skeleton corrections without saving files, creating jobs or starting an inference runtime or editor. Its math is the same forward kinematics used by `create_motion_variant`.

Provide a complete `variant_edit` and up to 16 unique **original-source frame indices** inside the retained trim. An empty list selects the first retained frame; input order is retained. A trimmed clip's frame zero is not necessarily original frame zero.

The response contains normalized `edit`, `predicted_variant_id`, source checksums, `fps`, retained `frames`, original `source_frames`, `source_range`, joint names/parents, contact status and adjusted-frame count. Each `frames_preview` entry contains:

| Field | Meaning |
| --- | --- |
| `original_frame` | Original sample index. |
| `frame_index`, `time_seconds` | Index and time relative to the retained clip. |
| `positions_m`, `global_rot_mats` | Corrected world-space positions and 3×3 joint rotation matrices. |
| `original_positions_m`, `original_global_rot_mats` | Unmodified original sample at the same original frame. |
| `joint_weights` | Each authored joint's amplitude taper at that frame. |

Coordinates are meters, Y-up, +Z forward; rotation matrices act on column vectors. Top-level `parent_local_offsets` supplies exact static bone offsets; `first_retained_root_position` supplies the original root at the trim start. `root_transform_matrix` maps corrected pre-placement coordinates to final world coordinates. Offsets are `null` and `joint_editing.supported=false` if the original skeleton cannot meet the native static-offset tolerance; placement-only preview may remain valid.

For original window `[a,b)` with blend length `n>0`, let `u=clamp(min(f-a,b-1-f)/n,0,1)`. Amplitude is `u²(3−2u)` inside the window and zero outside. With `n=0`, amplitude is one inside. The correction is `L_source @ Rx(weight*x) @ Ry(weight*y) @ Rz(weight*z)`. Zero-weight boundary frames cannot move. No inverse kinematics, anatomical solver or contact repair runs.

The predicted identifier becomes a usable `variant_id` only after explicit saving. Preview geometry describes the source skeleton, not fitted-character skin deformation.

## Partial-preserving revisions

Call `revise_motion_variant` with a verified variant and a partial revision:

```json
{
  "job_id": "<completed job id>", "variant_id": "<verified variant id>",
  "sample_index": 0,
  "revision": {"joint_updates": [{
    "joint_name": "RightArm", "local_xyz_radians": [0.0, 0.0, 0.1]
  }]},
  "frame_indices": [60], "save": false
}
```

Unmentioned trim, placement, joint windows, tapers and corrections remain unchanged. Existing-joint updates may set individual fields. New joints require angles and start/end bounds; taper defaults to three frames. `remove_joints` explicitly removes named existing corrections. Both lists permit at most 16 unique names and cannot contain the same joint. Omit unchanged fields rather than passing `null`.

Angles replace original-relative offsets; they are never stacked on the saved output. Trim changes must retain existing correction windows or explicitly revise/remove them. The service never silently shifts or clamps windows.

The default `save=false` returns the exact preview plus `changes`, `joint_changes` and `source_variant_id`. Repeat with `save=true` to save through the existing immutable pipeline; the response contains its verified receipt and `saved=true`. Equal resolved edits reuse the existing identifier. `source_variant_id` is invocation context, not a new persisted parent chain. Existing sources and receipt formats remain unchanged.

Removing all effective joint corrections restores original predicted contacts because results rebuild from the original sample. Effective joint corrections invalidate that channel. Neither outcome proves physically correct contact; inspect and assess the saved result before exporting.

Use `get_input_schema("variant_revision")` for partial revisions and `get_input_schema("variant_edit")` for complete edits. Studio routes are `POST /api/actions/preview_motion_variant` and `POST /api/actions/revise_motion_variant`, with the usual typed validation and Studio token. They expose backend functionality; direct viewport dragging is a separate UI integration.
