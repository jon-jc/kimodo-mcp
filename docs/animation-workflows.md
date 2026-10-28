# Animation workflows

## Start with a reproducible clip

Call `list_models` and choose the exact identifier `kimodo-soma-rp-v1.2`, one sample, a fixed seed, and a five-second segment:

> A person walks forward at a relaxed pace and waves with their right hand.

Call `health_check` with `probe_runtime=true`, then pass the arguments in `examples/simple-motion.json` to `validate_motion`. Send the same arguments to `generate_motion`. Poll the returned ID with `get_job`; use `get_job_log` to diagnose a failed run. Once the job succeeds, inspect the animation's frame count, skeleton layout, finite values, and output files. Then preview it and judge the motion itself. Numeric validity does not establish that a gesture looks natural or matches its prompt.

The bridge accepts up to eight segments, four samples, 100 denoising steps, 32 constraint objects, and 512 constrained frames in one request. These are bounded service inputs, not an assertion that every extreme combination has equal motion quality. Use `kimodo://request-schema` for the complete current contract.

## Compose several actions

Use separate timed segments rather than packing many actions into one sentence:

| Segment | Duration | Prompt |
| --- | --- | --- |
| 1 | 3 seconds | A person walks forward carrying a small box with both hands. |
| 2 | 3 seconds | A person carrying a small box slows down and stops. |
| 3 | 4 seconds | A person bends forward and places a small box on the ground. |

Each prompt must describe its own starting context. Kimodo generates segments in sequence; transitions occupy part of the following segment. Each segment stays within 10 seconds. At 30 FPS, a five-second clip contains 150 frames indexed 0 through 149.

## Guide movement with waypoints

The request in `examples/root-waypoints.json` guides the smoothed root along the XZ ground plane. Positions are in meters. A frame-zero waypoint should be at the canonical origin. Root position is distinct from the instantaneous pelvis location, which can sway naturally around the smoothed path.

A dense path provides targets for each frame. Keep the trajectory reachable within the duration and consistent with the text. A person cannot naturally walk several meters in a few frames just because the target coordinates are valid.

## Add pose or end-effector constraints

Full-body constraints contain local axis-angle rotations for every skeleton joint and a hips position. Kimodo derives constrained positions through forward kinematics. Hand and foot constraints use the same pose structure, then select the relevant end-effectors. They are not free-form hand-position dictionaries.

For practical editing, generate or load a motion, select a pose, and reuse it as a constrained frame. When working with the upstream visual demo, save its constraints JSON and load that structured data through the bridge. Avoid inventing a 77-joint pose array without knowing the joint order.

## Select an export

| Skeleton | Interchange output | Convention |
| --- | --- | --- |
| SOMA | BVH | Full 77-joint hierarchy; centimeter offsets and root motion. |
| G1 | MuJoCo CSV | 36 columns; Z-up, +X-forward; root quaternion wxyz. |
| SMPL-X | AMASS NPZ | Body parameters; Z-up, +Y-forward. |
| All | Kimodo NPZ | Motion arrays in meters; Y-up, +Z-forward. |

For DCC software, prefer SOMA BVH with the standard T-pose option when the downstream rig expects that rest pose. Default BVH uses the BONES-SEED rest pose. Applying the file to a different character requires a separate rig mapping and retargeting step.

Conversion into Kimodo NPZ resamples to 30 Hz. Preserve source FPS when importing CSV, which has no frame-rate header. BVH import supports the Kimodo/BONES-SEED SOMA hierarchy; it is not a universal BVH retargeter.

## Work before model access is ready

The upstream repository ships 16 completed example animations. The upstream demo and converter can inspect and export them without claiming new text-to-motion inference. Label them as upstream examples. The bridge's request validation and dependency probe also work before generation. Model generation remains a separate verification step after dependencies, gated text-encoder access, and memory readiness are confirmed.

## Check the result

- Confirm the expected duration, sample count, skeleton, and file types.
- Look for foot sliding, penetrations, abrupt transitions, and unreachable constraints.
- Compare several seeds when the motion is valid but the performance is unsuitable.
- Keep seed, exact model, prompts, constraints, and generation settings alongside the artifact.
- Use benchmark metrics only with their required reference data and embeddings; do not interpret missing scores as perfect results.

## Review and compare candidates

Call `analyze_motion_quality` for the completed job and its NPZ artifact, then `create_motion_review` for the same artifact. Open the returned local HTML file. It contains a frame player and contact sheet with front, side, and three-quarter views. Uniform timeline samples and labeled diagnostic extrema use the exact source poses. Fixed whole-clip camera bounds keep translation and scale changes visible.

Inspect the frames around a reported maximum as well as the maximum itself. A large speed or jerk can be appropriate for a fast action; a contact-sliding measurement needs correct foot contact labels. These diagnostics are review aids with units and configurable sensitivities, not a universal score or automatic repair. Unavailable metrics state why they cannot be measured.

Generate another candidate when the performance needs improvement. Keep the model, durations, and constraints consistent to compare seeds, or change one prompt, constraint, or supported postprocessing setting at a time. Use `compare_motions` with `job_id`, `other_job_id`, and their artifact names. Both inputs need matching FPS, skeleton, joint names, and hierarchy. Set `offset_frames` explicitly: a positive value compares the first clip's frame `i` with the second clip's frame `i + offset_frames`.

Start with `space="world"` to preserve root movement. Use `space="root_relative"` to compare body poses after removing each frame's root translation; facing and scale remain unchanged. Both clips share the same camera bounds. Comparison does not retarget, resample, or blend the source motions.

After selecting a candidate, inspect the character deformation in Blender and the imported animation in Unreal. Skeletal review cannot reveal every skinning, clothing, collision, or gameplay issue. Preserve the chosen job's request, artifact hashes, and import report. [Motion review](animate-review.md) describes the metrics, timing conventions, and implementation limits.
