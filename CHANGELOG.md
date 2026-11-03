# Change log

Each delivery is reviewed as a pull request and merged after its required checks pass. Entries describe implemented behavior and validation separately from artistic or engine acceptance.

## 2026-11-03 — Immutable rig corrections with full MCP version selection

### Added

- `inspect_motion_rig` returns every source joint, parent/child links, body groups, frame-specific local axes, current bone lengths and verified edit eligibility. Agents can select actual joints and inspect neighboring chains without relying on screenshots or guessed names.
- `create_motion_variant` saves up to 16 unique nonroot local XYZ rotation corrections, bounded to ±90 degrees per axis. Each uses an explicit original-frame window and smoothstep edge blending. Forward kinematics rebuilds descendants while retaining constant bone lengths and untouched frames.
- `get_motion_variant` verifies the exact edit, original source, saved arrays and checksums. `get_job` discovers saved corrections; variants remain separate from generation jobs and original samples.
- Original-frame trimming, planar yaw/translation and first-frame root placement support clip cleanup without resampling or model inference. At least two frames remain; FPS is unchanged.
- A `variant_edit` schema and `correct-motion` recipe give agents complete typed controls and ordered verification guidance. Inventory is now 42 tools, eight schemas and twelve recipes.

### Integration and compatibility

- Optional `variant_id` selectors now propagate through inspection, frame/pose extraction, quality reports, assessment batches, comparisons, source-pose generation, format conversion, character preview, and every Blender/Unreal preparation, execution and receipt call.
- Each variant has isolated export and handoff provenance, attempt selection and retry locks. Trimmed timing comes from the selected variant. Derived skeleton handoffs require both its variant ID and exact BVH export ID.
- Native calls retain their defaults and original export identities. Native and corrected previews remain separate. Shared workspace validation binds original manifest/source, correction request/receipt/output, and downstream files; changed inputs invalidate cached verification.
- The studio receives bounded source-skeleton playback data and verified variant downloads through local routes. These backend controls support the accompanying rig interface and are independently accessible through MCP.

### Validation and boundaries

- 663 Python tests pass locally, including fresh stdio and loopback HTTP discovery. Ruff and whitespace checks pass.
- The saved forearm correction exported to BVH and passed a fresh background Blender 5.2.1 run: the corrected animated GLB, mesh FBX, animation FBX and Blender project all verified against the selected variant. The GLB contains one 120-frame animation and a 24-joint skin. This run did not import into Unreal or touch an active editor scene.
- Automated tests cover original preservation, taper and rotation order, constant bone lengths, untouched frames, trim timing, exact export identity compatibility, changed/cross-job/cross-sample sources, download containment, preview isolation, schema discovery and full agent correction/review/pose reuse.
- Four saved-wave edits (trim, placement, forearm correction and combined) were compared with the pinned upstream SOMA forward-kinematics implementation. Maximum position error stayed below one micrometer and the original motion/manifest hashes remained unchanged.
- Corrections are restricted to complete canonical SOMA sources with consistent local/global rotations and static offsets. Effective joint changes remove predicted contacts rather than retaining stale labels. No automatic IK, contact locking, retiming, rest-rig restructuring, skin-weight editing or aesthetic approval is claimed.
- The supplied 24-bone fitted character does not reproduce every joint of the 77-joint source skeleton, including independent fingers. Review the source rig and target character separately.

## 2026-11-03 — Explicit motion assessment and generation guidance

### Added

- `assess_motion_quality` evaluates one completed sample against caller-authored numerical limits. `assess_motion_candidates` evaluates up to eight distinct job/sample pairs under the same policy, retaining input order and source identity.
- Eleven checks cover speed, acceleration, jerk, angular speed, bone drift, contact sliding, a foot-floor proxy, and four explicit loop-boundary measurements. Each check reports pass, fail, or unavailable; missing evidence never counts as a pass.
- Reports bind the original source, generation request, sample and normalized policy by checksums. Bounded problem windows identify contributing source frames, peaks, affected-frame counts and truncation. Review frames guide subsequent exact-pose inspection and visual review.
- The `quality_policy` schema and `assess-candidates` recipe expose the complete controls to any compatible MCP client. Both assessment tools are also available through the studio action interface.
- Generation plans now expose exact segment timelines, incoming transition context, reprocessed tail ranges, per-type constraint density and actionable advisories without changing valid requests.

### Behavior and scope

- Assessment limits are explicit; there is no automatic artistic ranking, universal realism score or automatic winner. Loop checks require declared loop intent. Assessment ranges are inclusive; planning ranges are half-open, and both responses state their convention.
- Transition guidance follows the pinned upstream implementation: later segments generate extra history and can reprocess the preceding segment tail. Requested action frames are retained. Sparse-constraint guidance counts unique frames after effective type expansion; dense planar root paths are exempt.
- Original motion is read-only. Invalid sources fail verification. Contact labels, joint-pivot floor measurements, noisy numerical derivatives and endpoint-only loop checks retain explicit limitations.

### Validation

- Tests cover physical units, derivative frame support, loop boundaries, missing metadata, strict policy validation, deterministic hashes, source changes, managed tool calls, schema discovery and planning semantics.
- A saved 120-frame, 30 FPS SOMA wave was assessed without inference or source modification. It met the explicitly chosen bone-drift, contact-slide and jerk limits; this establishes only those numerical checks.
- The MCP inventory grows from 37 to 39 tools, with seven domain schemas and eleven workflow recipes. Reconnect clients that cache tool discovery after updating.
