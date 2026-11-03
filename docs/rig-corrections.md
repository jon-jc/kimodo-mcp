# Inspect and correct an existing motion

A small joint correction should preserve the performance around it. Kimodo MCP exposes the actual source rig, selected frames and precise edit controls so a compatible assistant can make deliberate changes without regenerating the whole clip. Corrections create separate saved versions and use the same backend as the studio.

## Agent workflow

1. Call `get_job` for a completed job and choose the exact sample. Saved corrections are listed in `variants`; they are not new generation jobs.
2. Call `inspect_motion_rig` with the job/sample and a useful `frame_index`. Read the full joint hierarchy, body groups, local axes and `editing.supported`. Choose named joints from this response.
3. Use `inspect_motion_frames` and `create_motion_review` to inspect the problem and neighboring frames. Establish an explicit numerical policy with `assess_motion_quality` when relevant.
4. Read `get_input_schema("variant_edit")`, then call `create_motion_variant` with the full intended edit. Keep the returned `variant_id` and source/output hashes.
5. Inspect and assess the selected variant. Use `compare_motions` for before/after review and build the target character preview to check deformation.
6. Keep the same `variant_id` through export and every subsequent editor call. Inspect the resulting Blender/Unreal receipts and the actual target character before accepting the animation.

`get_workflow("correct-motion")` exposes these steps through MCP. Discovery, rig inspection, corrections and numerical review need no inference worker or model weights. Format conversion needs the configured export runtime. Editor execution requires the corresponding editor connection.

## Example: adjust a forearm briefly

The following is a five-degree local Z correction over original frames 30–89, with five-frame smooth transitions at both ends. The joint name must exist in the inspected rig; the example is not an anatomical prescription.

```json
{
  "tool": "create_motion_variant",
  "arguments": {
    "job_id": "<completed-job-id>",
    "sample_index": 0,
    "edit": {
      "joint_adjustments": [{
        "joint_name": "RightForeArm",
        "local_xyz_radians": [0, 0, 0.08726646259971647],
        "start_frame": 30,
        "end_frame_exclusive": 90,
        "blend_frames": 5
      }]
    }
  }
}
```

Read the result through `get_motion_variant(job_id, variant_id, sample_index)` to verify it again. To refine it, submit the **complete combined edit against the original sample**. A variant cannot be used as a new edit source; this avoids accumulated numerical drift and ambiguous frame numbering. An identical normalized edit reuses a verified result. Choosing the original means omitting `variant_id`; no file is overwritten or deleted.

## Controls and conventions

| Control | Meaning |
| --- | --- |
| `start_frame`, `end_frame_exclusive` | Keep this original-source frame interval. Start defaults to 0, end defaults to the original frame count. At least two frames must remain. |
| `joint_adjustments` | Up to 16 distinct nonroot joints, each with one explicit interval inside the retained trim. |
| `local_xyz_radians` | Local X, Y, Z offsets, each within ±π/2. The operation is `L_source @ Rx(x) @ Ry(y) @ Rz(z)`. These are offsets, not replacement Euler angles. |
| `blend_frames` | 0–30; default 3. Smoothstep scales the offsets from zero at the first frame to full strength and back to zero at the last frame. The interval must contain at least `2*blend_frames+1` frames. Zero deliberately permits hard boundaries. |
| `yaw_radians` | Planar world yaw within ±π, applied after local corrections. |
| `translation_xz` | X/Z translation in meters, applied after the chosen root origin and yaw. |
| `root_origin` | `world` rotates around the world origin. `first_frame` subtracts the retained first root's X/Z position before rotating and translating. Root height is preserved. |

All edit intervals are **zero-based original-source frames, with an exclusive end**. After trimming `[20, 100)`, inspection frame 0 is original frame 20, and the new clip has 80 frames. Re-editing still uses original-frame intervals. Assessment issue windows use their separately declared inclusive convention.

Local axes returned by `inspect_motion_rig` are X/Y/Z basis directions in world space at the inspected frame. Rotation limits are tool bounds, not an anatomical model. Use small changes and inspect the entire affected chain. Parent corrections move descendants through forward kinematics; their bone lengths are preserved. Root rotation is controlled by planar placement rather than the joint-adjustment list.

## Compare the correct frames

`compare_motions` accepts `variant_id` for A and `other_variant_id` for B. A positive offset compares A frame `i` with B frame `i + offset_frames`. Therefore, when A is the original and B is a variant trimmed from source frame 20, use `offset_frames=-20`. World comparison retains placement differences; root-relative comparison removes translation only.

Pass `variant_id` to `inspect_motion`, `inspect_motion_rig`, `inspect_motion_frames`, `preview_motion`, both constraint-extraction tools, `analyze_motion_quality`, `assess_motion_quality`, and `create_motion_review`. Candidate entries for `assess_motion_candidates` also accept it. `PoseReference.variant_id` lets a new generation start from an actual corrected pose; this does not match velocity or guarantee a seamless transition.

## Export and editor handoff

- Call `export_motion(job_id, sample_index, variant_id, format="bvh")` for a corrected SOMA clip. Preserve the returned `export_id`.
- For generic skeleton export, pass **both** `variant_id` and `export_id` to `prepare_blender_handoff`, then use the same job/sample/variant/attempt with execution and receipt tools.
- For the registered fitted character, `build_character_preview` accepts `variant_id`; it prepares, executes and verifies a separate character preview. Lower-level character preparation also accepts the selector.
- For Unreal, keep `variant_id`, `source_kind`, sample, attempt and explicit target consistent through preparation, execution and report retrieval. A version's trimmed timing is validated independently of the original generation manifest.
- `list_job_outputs` and the job's `variant_previews` identify verified files by selected version. Native `previews` remain separate. Never infer which version an export contains from its display name alone.

## What is preserved and what needs review

The implementation validates complete canonical SOMA30/SOMA77 channels, local/global rotation consistency and constant parent-local offsets before correction. It updates posed joints and rotations together. Frames outside effective joint windows retain their source values; trims slice time-varying channels consistently. Placement updates root position, smooth root position and heading consistently. Identical edits and derived outputs are verified before reuse; an incomplete attempt is not blindly overwritten.

Any effective joint correction discards the existing predicted foot-contact channel because it no longer describes the edited motion reliably. Contact-dependent checks then report unavailable. Pure trimming and planar placement preserve the sliced source predictions. Neither case proves actual sole contact, collision-free movement or natural acting.

The source rig is distinct from the fitted character. The supplied character has 24 fitted bones and no independent finger, facial, hair or cloth animation. Finger edits in a SOMA77 source can be reviewed on that source skeleton but may not transfer to this character. Rest-bone lengths, hierarchy and skin weights are intentionally outside these motion-edit controls. Automatic IK, foot locking, motion stitching, arbitrary retiming and production approval require additional work and verification.
