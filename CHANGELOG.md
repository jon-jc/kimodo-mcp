# Change log

Each delivery is reviewed as a pull request and merged after its required checks pass. Entries describe implemented behavior and validation separately from artistic or engine acceptance.

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
