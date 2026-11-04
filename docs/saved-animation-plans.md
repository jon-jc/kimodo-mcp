# Save an animation plan before generating

A saved animation plan retains the complete validated `GenerateRequest`, a title, authoring notes and optional parent-plan lineage. It gives a complex sequence a stable identity before spending time on inference. Saving, loading, listing and previewing revisions require no generation worker, model weights or editor connection.

Plans and motion are separate records. A plan contains instructions and predicted frame allocations. A completed job contains generated samples. An immutable motion variant contains a correction to an existing sample. Saving a plan does not create motion, and revising a plan does not alter any completed take.

## The five operations

| Tool | Result | Starts generation? |
| --- | --- | --- |
| `save_animation_plan(title, request, description='', parent_plan_id=null)` | A saved full request, `plan_id`, `request_sha256`, timeline and `reused` flag. | No |
| `get_animation_plan(plan_id)` | Verified saved settings, authoring metadata and current validated frame plan. | No |
| `list_animation_plans(limit=20, offset=0)` | Summaries, valid-record `total`, `invalid_count` and pagination fields. | No |
| `revise_animation_plan(plan_id, revision, title=null, save=false)` | An explicit changed-field preview; `save=true` saves a new child plan. | No |
| `generate_animation_plan(plan_id)` | A new durable job using the exact saved request, with plan provenance. | Yes |

Use `get_workflow("saved-plans")` for the ordered recipe. `get_input_schema("generate_request")` and `get_input_schema("animation_revision")` describe the full input objects; the MCP tool schema describes their outer arguments. No separate, reduced saved-plan request schema exists.

`get_workspace_state.saved_plans` exposes the bounded summary shape. Studio action routes call the same five MCP operations and retain the existing local-origin and request-token checks. A client should explicitly refresh the plan library with `list_animation_plans`; frequent `/api/state` polling does not rescan and verify every saved request. These API capabilities do not imply that every client supplies a dedicated saved-plan interface.

## Author self-contained beats

Describe one or two related behaviors in each beat. Include the actor's starting state and any persistent object or stance in each prompt. A later prompt such as “then stops” omits useful context; “A person carrying a small box slows down and comes to a balanced stop” is self-contained. A description stored beside a plan is authoring context, not text silently prepended to every model prompt.

This example is a complete `save_animation_plan` argument object:

```json
{
  "title": "Carry box — approach, stop, place",
  "description": "Three readable phases for a gameplay interaction. Review the stop and release boundaries before accepting a take.",
  "request": {
    "model": "kimodo-soma-rp-v1.2",
    "segments": [
      {"text": "A person walks forward carrying a small box steadily in both hands.", "duration": 3.0},
      {"text": "A person carrying a small box slows down and comes to a balanced stop.", "duration": 2.0},
      {"text": "A person standing with a small box bends forward, places the box on the ground, and releases it.", "duration": 3.0}
    ],
    "seed": 17,
    "samples": 2,
    "steps": 35,
    "cfg_text": 2.5,
    "cfg_constraint": 1.5,
    "transition_frames": 8,
    "first_heading_angle": 0.0,
    "root_margin": 0.04,
    "postprocess": true,
    "bvh_standard_tpose": true,
    "formats": ["npz", "bvh"],
    "constraints": []
  }
}
```

The box is prompt context: the bridge does not create an object, solve grasp contacts, synchronize a prop, or simulate its release. The example authors a request, not evidence that a generated performance meets those intentions.

Alternatively, call `plan_animation` with a natural brief and save its returned `request` unchanged. Natural planning chooses explicit presets. Once an advanced plan is loaded, keep its complete request rather than rebuilding it from a brief: custom values such as 35 sampling steps and unequal guidance weights cannot be represented by every brief control.

## Read the timeline precisely

Each segment is 0.2–10 seconds; a request contains at most eight segments and 2,400 frames. Planning quantizes each duration independently using `int(duration * 30)`. The returned `segment_frames`, not rounded display text or the total duration alone, define the planned allocation.

For the example above:

| Beat | Output action range | Incoming history generated for this beat | Requested action frames |
| --- | --- | --- | ---: |
| Approach | `[0, 90)` | None | 90 |
| Stop | `[90, 150)` | `[82, 90)` | 60 |
| Place | `[150, 240)` | `[142, 150)` | 90 |

All ranges are zero-based with an exclusive end. A later segment generates extra history and revises or blends the preceding segment's final eight samples. Its own requested action frames remain present. With postprocessing, upstream corrects the generated history and action; without it, the history is blended in motion-feature space. `segment_timeline` reports the actual history window, generation count and preceding-tail reprocessing.

The full clip has 240 samples, an eight-second playback duration and a first-to-last key span of 239/30 seconds. These counts do not predict when the model will complete a gesture or settle into the next action. Inspect the actual transition history and the beginning of each later action. Changing a later prompt can also change the preceding overlap; this is not isolated per-beat regeneration.

Planning also returns `constraint_density` and `advisories`. The sparse-key guidance recommends fewer than 20 authored frames per effective constraint type; dense root paths are exempt. Warnings do not silently change settings or reject an otherwise valid request. Use sparse meaningful anchors and leave room for natural movement, then review the generated result.

## Preserve settings when revising

First preview a revision without writing a new plan:

```json
{
  "plan_id": "REPLACE_WITH_RETURNED_PLAN_ID",
  "revision": {
    "beats": [
      {"text": "A person walks forward slowly carrying a small box steadily in both hands.", "duration": 3.0},
      {"text": "A person carrying a small box eases into a balanced stop without a sudden lean.", "duration": 2.0},
      {"text": "A person standing with a small box bends their knees, places the box on the ground, and releases it carefully.", "duration": 3.0}
    ]
  },
  "save": false
}
```

Replace the illustrative placeholder with the actual returned 64-character lowercase hexadecimal identifier. Inspect `changes`, `notes` and the returned request. Repeat with `save=true` to retain the child, optionally supplying a new `title`. The original remains available, and the new record identifies its `parent_plan_id`. Revision preserves the description unless a complete new plan is saved explicitly with different authoring metadata.

`AnimationRevision` can change a single-beat description or duration, replace all beats, select a sampling-effort preset, change candidates or seed, and explicitly clear constraints. It does not contain arbitrary partial request patches. To change advanced fields such as model, guidance or transition length, load the full request, edit the intended fields, validate it, and save a new plan with the original `parent_plan_id`.

Unmentioned settings remain unchanged, including model, custom sampling steps, guidance, transition length, heading, root margin, postprocessing, formats, BVH pose convention and complete constraint arrays. Choosing a new effort preset deliberately replaces custom steps. Multi-beat revisions require the complete replacement beat list; a single description cannot silently collapse the sequence.

Existing constraints remain at their exact indices when **every segment's quantized frame count is unchanged**. Text-only revisions therefore retain anchors. Even a small duration edit can retain them if it quantizes to the same allocation. If any segment gains or loses frames, use `clear_constraints=true` deliberately or save a new explicit request with rebuilt constraints. Equal total duration is insufficient when a boundary moves. Clearing constraints removes all of them and appears in `changes`; there is no automatic stretching, shifting or partial remapping.

Preserving an anchor's index does not establish that it still matches the revised action. Review semantic conflicts, reachability and the surrounding transition before generating. No tool guarantees that unchanged body parts will remain identical in a newly sampled performance.

## Identity, reuse and recovery

- `plan_id` hashes the canonical schema version, title, description, complete validated request and parent-plan ID. Timestamps are excluded from that identity.
- `request_sha256` hashes the generation request alone. Different titles or parent lineage can describe the same request hash.
- Saving identical content reuses the existing verified plan and returns `reused=true`. Different content creates a different ID; there is no in-place edit or delete tool.
- Loading verifies canonical data and receipt integrity before returning the request. A missing, malformed, redirected or changed receipt is an error, not an invitation to regenerate it silently.
- Listing returns valid summaries, newest first, and counts invalid records separately. `limit` and `offset` bound the returned page; use `get_animation_plan` to retrieve a selected full request.

Plan files live under the configured data directory's `animation-plans` folder, separate from job outputs. Callers select managed IDs, not arbitrary file paths. A title is 1–160 characters and must contain non-whitespace text; a description is at most 2,000 characters. The existing Studio action body limit is 1 MiB; a large advanced request may require a direct MCP call within its schema and saved-receipt limits.

Reproducibility means retaining the request, source constraints, model identity, batch seed, candidate count and sample index. It is not a bitwise-output guarantee across runtime versions, hardware or numerical kernels. A batch is seeded once; candidate number is not an arithmetic increment of the seed. Changing text, settings or batch size is a new experiment.

## Generate and hand off later

When generation is intended, check `get_workspace_state` and resolve runtime access or capacity issues first. Call `generate_animation_plan` with the chosen saved ID. The response returns a job ID; the job context retains `saved_plan_id` and `request_sha256`. Poll `get_job` until completed. Repeating the generation call submits another take; it does not return a cached generation. After an uncertain response, inspect recent jobs and their context before retrying.

Review completed samples using `inspect_motion`, `create_motion_review` and action-appropriate `assess_motion_quality` or `assess_motion_candidates` policies. A numerical pass is not artistic acceptance. Use immutable motion variants for bounded cleanup of an already good performance; revise a plan when a new performance is wanted.

Export only a completed selected sample or verified variant. The later `export-motion`, `export-skeleton`, `export-character` and `import-unreal` recipes use job/sample/version identities, not a plan ID. Blender and Unreal execution are separate explicit operations. See [agent workflows](agent-workflows.md), [precise motion corrections](rig-corrections.md) and [editor handoffs](editor-handoffs.md).

This workflow does not promise seamless clip stitching, automatic prop contacts, collision avoidance, production-ready animation or universal quality thresholds. Offline authoring and contract tests establish request preservation and scheduling behavior; they do not establish a newly generated clip's visual quality or engine acceptance.
