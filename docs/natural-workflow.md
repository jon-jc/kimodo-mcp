# Describe, review, and revise an animation

Use `plan_animation` to turn a description into a reviewable request, then `generate_animation` to submit it. The connected assistant interprets what you mean and supplies the action description; the bridge preserves that text and validates the timing and settings. Planning does not load models or start generation.

For example, ask:

> Plan a four-second motion where the character stands in place and slowly waves with the right hand. Use one draft candidate first. Show me the settings before generating it.

The corresponding brief is:

```json
{
  "description": "A person stands in place and slowly waves with the right hand.",
  "duration_seconds": 4.0,
  "quality": "draft",
  "candidates": 1,
  "seed": 0,
  "model": "kimodo-soma-rp-v1.2"
}
```

Without overrides, planning chooses four seconds, one candidate, seed zero, the SOMA RP v1.2 model, 50 sampling steps, native postprocessing, and NPZ plus standard-T-pose BVH exports. Other selected models receive their supported export formats; G1 postprocessing stays disabled.

| Effort | Sampling steps | Use |
| --- | ---: | --- |
| `draft` | 20 | Explore the action before spending more time. |
| `balanced` | 50 | Default sampling effort. |
| `detailed` | 80 | Try more sampling steps on a selected idea. |

These presets change computation, not a guaranteed realism level. More candidates provide alternatives rather than improving one candidate automatically. Generation uses the configured local runtime and requires the independent worker to be running.

## Give longer actions explicit beats

Each beat has its own complete action description and duration of 0.2–10 seconds. Up to eight beats are supported. If `duration_seconds` is also supplied, it must equal their sum. The overall description is retained as context; it is not silently prepended to each prompt.

```json
{
  "description": "A relaxed greeting after approaching a friend.",
  "beats": [
    {"text": "A person walks forward at a relaxed pace.", "duration": 3.0},
    {"text": "The person stands in place and waves with the right hand.", "duration": 3.0}
  ]
}
```

The returned `plan.segment_frames` records the exact frame counts at 30 FPS. Fractional durations are quantized to whole frames. A clip with 120 samples at 30 FPS plays for four seconds, while its first-to-last sample span is 119/30 seconds. Review both motion and transitions rather than assuming the text fixes exact event timing.

## Revise without losing the original

Call `revise_animation` with a job ID and an explicit revision. It returns a proposed request and changed fields by default. Set `submit=true` to create a new job; the original request and artifacts remain available.

```json
{
  "job_id": "YOUR_COMPLETED_JOB_ID",
  "revision": {
    "description": "A person stands in place and makes a smaller, slower wave with the right hand.",
    "quality": "balanced"
  },
  "submit": false
}
```

Supply a rewritten action, rather than only an instruction such as “make it better.” Model, seed, guidance, exports, and constraints remain unchanged unless an allowed revision explicitly changes them. For multi-beat clips, replace the beats explicitly instead of collapsing them into one description. A changed prompt creates a new performance; unmentioned body parts are not guaranteed to stay identical.

Changing duration or replacing beats when constraints exist requires `clear_constraints=true`, or a new explicit `GenerateRequest` with deliberately chosen constraint timing. Clearing constraints is a visible revision. They are never silently shifted or stretched.

## Reuse a selected pose

In the studio, select a completed candidate, load its character preview, pause on a source frame, and enable **Start from the current pose** while creating a new animation. MCP clients can pass `start_from: {job_id, sample_index, frame}` alongside `brief` to `plan_animation` or `generate_animation`. The server resolves the verified source artifact, anchors the body and hand/foot orientations at frame zero, and derives its starting heading. It retains the source world position; it does not blend clips or match their velocities. Planning records the source checksum and performs no inference.

`extract_pose_constraint` reads a selected frame from a completed SOMA sample and returns a validated constraint for a new request. Source and destination frame indices are zero-based. For example, source frame 119 is the last sample of a 120-frame clip; target frame 0 anchors the start of the new clip.

Add the returned constraint to the destination request, call `validate_motion`, and submit with `generate_motion`. The destination must have enough frames and must not contain conflicting constraints. Pose reuse preserves the selected root position in meters in Kimodo's Y-up coordinates. A hand or foot constraint also controls the planar root, following upstream behavior.

Only explicitly identified canonical SOMA30 and SOMA77 archives with local rotations are supported. The model's own loader converts the 77-joint pose to its 30-joint representation. Detailed finger motion is not independently controlled by that model. Full-body constraints control joint positions and heading; terminal-joint twist and velocity continuity are not guaranteed. This is a pose anchor, not automatic seamless stitching.

## Choose using the actual output

Inspect a completed job with `inspect_motion`, `analyze_motion_quality`, and `create_motion_review`. Use `compare_motions` to compare compatible samples at an explicit frame offset, in world coordinates or relative to each frame's root. World-space review shows drift that root-relative review intentionally removes.

Diagnostic thresholds highlight possible problems; they do not provide a universal animation-quality score. Check the rendered character, especially shoulders, cloth, hair, feet, and the transition into any neighboring clip. The supported fitted character currently has body animation without independent finger, facial, hair, or cloth simulation. See [animation workflows](animation-workflows.md) for structured constraints and [editor handoffs](editor-handoffs.md) for Blender and Unreal exports.
