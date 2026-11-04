# Change log

Each delivery is reviewed as a pull request and merged after its required checks pass. Entries describe implemented behavior and validation separately from artistic or engine acceptance.

## 2026-11-03 — FreeMoCap as a core Studio and MCP capture workflow

### Added

- FreeMoCap is the primary recorded-performance entry in Create, alongside Kimodo text animation and EasyMocap single-video fitting. The built-in workspace guides video checks, audio/flash synchronization, new recording import, measured camera calibration, detailed landmark tracking/reconstruction and explicit Blender export.
- Seven default-registered MCP tools expose the same stages to Claude Code, Codex and other clients. The live catalog now has 64 tools, 15 ordered recipes and 11 complete input schemas; agents can discover every supported stage and advanced parameter without a browser.
- A pinned local HTTP adapter for FreeMoCap v2.0.0-alpha.25 supports MediaPipe and RTMPose detection controls, temporal filtering, triangulation/outlier controls, explicit calibration selection and scoped cancellation. Reference paths and recording roots are operator-registered; fixed upstream operations do not accept arbitrary commands or destinations.
- Original optional runtime status extension preserves upstream progress delivery while recording terminal events. Completion binds service instance, pipeline, recording, stopped workers and verified outputs. Missing manager entries or merely present files do not establish success. The explicit launcher checks the clean reviewed revision and uses loopback Host/Origin restrictions without terminating another service on the port.
- Durable receipts preserve source/output fingerprints and uncertain-submission states. Cross-stage writes are blocked while prior work remains unresolved; calibration recording reuse requires a verified calibration receipt, export requires verified processing, and completed exports are rechecked before reuse.
- Responsive six-stage workspace includes reviewed-plan gating, stage-specific readiness, recording inspection, receipt loading from agents, explicit calibration selection and export settings. Advanced settings cannot replace common controls or registered paths. Missing runtime and missing status extension have separate explanations.
- Updated README with current Studio screenshots, a three-route comparison, detailed capture instructions, the complete FreeMoCap tool group, an agent example, fidelity/retargeting boundaries and links to source and operator guides. Updated MCP recipe/schema documentation and change log.

### Validation and boundaries

- Full validation passes 885 Python tests and 80 frontend tests, Ruff and the production build. Coverage includes typed contracts, upstream request mappings, fake-service stage execution, terminal observation, liveness/cancellation races, source/output tampering, calibration provenance, cross-stage conflicts, discovery and UI request/plan gating. Desktop and narrow-screen browser checks confirm actual controls and setup gating.
- FreeMoCap's external environment was not installed or launched, and real footage was not processed. No camera, Kimodo inference worker, Blender export or Unreal import ran during this integration. Runtime prerequisites and source/model licensing remain separate.
- One-camera FreeMoCap output is explicitly planar pixel space, not inferred metric depth. Detailed landmarks do not establish articulated finger/facial animation. The pinned Blender path supports MediaPipe only, excludes animated glTF and may fall back to addon defaults; requested export settings remain unverified until inspected. Captured landmarks require explicit character retargeting before Unreal use.

## 2026-11-03 — Video reference capture through EasyMocap

### Added

- Eight shared MCP/Studio operations cover preflight, explicit start, durable job listing/status, bounded logs, cancellation, source-frame inspection and verified artifact retrieval. Discovery now contains 57 tools, 14 recipes and 10 schemas; video jobs remain separate from Kimodo generation.
- An original adapter calls audited stages from pinned EasyMocap `e6006fd3814d5f8ad45a7ce965beb9bcc90767f2`: bounded FFmpeg sampling, MediaPipe or YOLO/HRNet BODY25 detection, assumed-camera initialization, robust SPIN/SMPL fitting with temporal regularization, normalization and numerical BVH export. Runtime/models stay external; fixed argument lists and blocked external-stage networking avoid wrapper quoting and implicit-download problems.
- Immutable source/request/asset/output receipts, one active capture lock, process-creation identity checks, owned-child cancellation and orphan detection. Starts can bind to the previously checked video hash. Missing/changed sources, assets and outputs fail verification.
- Explicit optional short-gap 2D interpolation (0–15 frames; strict by default) preserves original observations, time and low-confidence estimate flags. It rejects ambiguous identities, extrapolation, long gaps, gross discontinuity and collapsed intermediate bodies.
- Correct SMPL24 normalization includes shaped-pelvis root translation, camera-to-viewer rotation and full upstream FK agreement. BVH preserves hierarchy traversal, meter translations, frame rate and continuous rotation branches. The known upstream root-export bug is not reused.
- A responsive video dialog provides bounded local file copying, trim/detector/gap settings, actionable readiness, stage/log/cancel controls, synchronized reference/skeleton playback, problem-frame diagnostics, BODY25 confidence and verified downloads. Stale selection/plan responses cannot replace newer work; a submitted plan is consumed before dispatch.
- Detailed runtime/setup guide and source audit distinguish integrated functions, adjacent upstream features, licensing, uncertainty and retargeting requirements.

### Validation and boundaries

- Python validation includes independent FK and BVH reimport, mocked complete pipeline and cancellation, gap policies, receipt tampering, source-bound dispatch, HTTP upload/download guards and actual MCP discovery. Frontend tests cover request bounds, hashes, confidence, races and submission gating; the production build passes.
- Live browser checks copied a synthetic reference and showed the missing-runtime setup result. A separate synthetic fixture exercised completed skeleton/video review, shared scrubbing and explicitly marked low-confidence frames. This is interface validation, not a real motion-capture reconstruction.
- Real inference was not run. Ordinary footage does not guarantee correct depth, scale, identity, hidden limbs or contacts. Captures use SMPL24 and require explicit character retargeting before Unreal use; automatic SOMA handoffs, facial/finger capture and production animation acceptance are not implied.

## 2026-11-03 — Direct skeleton manipulation and reliable editing drafts

### Added

- **Edit skeleton** opens the source rig and desktop focus layout. Screen-space joint picking, visible translation/rotation handles, planar root movement and parent-bone aiming make small corrections directly manipulable. Rotation stays local and bone lengths remain fixed; this is not multi-joint IK.
- Responsive one-frame forward kinematics during dragging, exact backend verification on release, and a clearly marked unsaved-pose preview. Each drag stages one undoable edit; saving creates the playable immutable version. Seeking/playback clears the pose preview without discarding the draft.
- Explicit correction-window controls around the current frame. New endpoint edits span the retained clip to avoid a zero-fade cutoff inside it; existing authored windows remain unchanged. Exports and pose anchors distinguish saved motion from the displayed unsaved pose.
- Sequence undo/redo with 50 states, grouped field typing, stable card identities, keyboard shortcuts, and focus restoration. Invalid collapsed action fields open and receive focus before preview/save/generate.

### Fixed

- Pending joint and placement fields no longer overwrite each other when staging. Fractional drag values remain valid numeric input. Delayed saves and previews cannot replace newer selections, edits or playback.
- Pointer cancellation restores orbit and discards only the incomplete drag. Hidden/clipped joints cannot be picked. The fitted-character bone overlay now has a separate clearly named source-editing entry point.
- Rig-focus grid rows retain their content height, so candidates and diagnostics cannot overlap the viewport or playback. Its controls scroll independently; direct-edit controls wrap on narrow screens.

### Validation and scope

- 63 frontend tests cover FK, bone preservation, original-frame weights, placement, picking, cancellation, draft races, field preservation, sequence history and validation; production build passed.
- Live Studio checks clicked a source joint, dragged root/arm handles, rotated an arm, undid a drag and saved a two-joint version. Separate checks confirmed pending form preservation and desktop/narrow-screen layout. Exact client/backend FK agreement on an existing motion was within 1.3e-7 meters for positions and 2.8e-7 for rotation components.
- Added an actual editor screenshot and detailed workflow documentation. Validation used existing motions; no inference worker, model generation, Blender execution or Unreal import was run. Test corrections demonstrate edit mechanics, not accepted production animation quality.

## 2026-11-03 — Exact correction previews and preserved partial revisions

### Added

- `preview_motion_variant` computes exact source-skeleton forward kinematics without writing artifacts or creating jobs. Up to 16 unique original-source frame samples include corrected/original positions and rotations, exact static bone offsets, root placement and per-joint taper weights for interactive controls.
- `revise_motion_variant` previews partial trim, placement and named-joint changes while preserving all unmentioned settings. Explicit removals and `save=true` produce an immutable original-relative version through the existing pipeline; angles never stack on previous output.
- Both operations are exposed through MCP and the Studio action API, documented in the correction API guide, and discoverable in the correction workflow. The catalog now has 49 tools and nine domain schemas, including `variant_revision`.

### Validation and boundaries

- Tests compare preview samples exactly with saved SOMA30/SOMA77 outputs, verify source immutability and no-write preview behavior, exercise strict bounds and partial preservation, and check Studio/MCP discovery and protocol exposure.
- Source checksums are verified before and after preview. Source-frame windows never shift silently; effective joint edits invalidate predicted contacts. Removing all effective joint edits restores original predictions, not a guarantee of physical contact.
- No inference or editor launch is required or performed by these operations. Direct viewport dragging is separate UI work; previews do not claim anatomical correctness, inverse kinematics or fitted-character deformation quality.

## 2026-11-03 — Focused studio workflow, sequence composer and layout repairs

### Added

- Persistent **Create**, **Review & fix** and **Export** work areas expose the tools needed for each stage. Navigation preserves the authoring draft, loaded plan, candidate, correction version, staged joint edits and playback position. Animation history is a collapsible strip available throughout the workflow.
- An action-card sequence composer supports two to eight independently described beats, keyboard-accessible reordering, duplication, removal and timeline selection. Three editable starters provide approach/greet/settle, crouch/jump/land and turn/walk/stop sequences. Applying a starter explicitly replaces the visible actions.
- The timeline distinguishes estimated allocations from server-validated frame ranges. Shaded tails identify preceding samples revisited by the next action; they do not remove the requested next-action frames. Timing previews explicitly identify the 3D view as an existing saved animation.
- A paginated reusable-plan library shares complete requests with MCP clients. Loading retains custom sampling settings, model identity, metadata, constraints and export options. Deliberate changes save an immutable child version; unchanged loaded plans generate through their saved identity. Blank-seed revisions must resolve a new seed before saving.
- Updated README and studio guide explain the complete three-stage workflow with actual desktop, rig-review and narrow-screen screenshots.

### Fixed

- Replaced overlapping desktop layout rules with measured header/navigation rows, independently scrolling creation/view panels, a separate history strip and stacked narrow-screen layouts. Status messages occupy a bounded layout area instead of covering playback and action buttons.
- Hardened wrapping and minimum widths for action buttons, version selection, rig corrections, frame fields, download links and editor settings. Sequence authoring uses compact notes and a wider desktop column; mobile export controls no longer reserve an unnecessary vertical gap.
- Rig labels prioritize the selected joint, choose available left/right positions, remain inside the viewport and suppress crowded neighbors. Resizing recalculates placement; long labels truncate with the full name available in a tooltip.
- Selecting the current candidate retains its correction version. Switching workflow areas retains playback time; changing between matching source and fitted representations transfers the nearest source frame. Mobile navigation returns to the active screen's top without seeking playback.
- Stale preview, plan-load and library-list responses cannot replace newer authoring state. Late generation/retry responses cannot replace a candidate explicitly selected while submission was pending, including selecting another source and returning to the original.
- Saved-plan text edits preserve anchored constraints at unchanged frame allocations. Changed allocations display a plain explanation and require explicit clearing; a successful later preview dismisses the earlier error. Advanced custom sampling values remain unchanged until their control is deliberately changed.

### Validation and scope

- All 43 frontend tests pass, covering composer identity/order/limits, exact timing, advanced saved-request routes, metadata preservation, blank-seed handling, stale-response gates, workflow state, shared-frame transfer and collision-free label placement. The production build and syntax checks pass.
- Browser checks exercised shared-plan loading, text-only revision/save, timing-conflict rejection, explicit constraint-clearing preview, seed reset, candidate/version retention, workflow switching, responsive controls and actual rig labels. A fresh MCP session independently verified that the UI-saved child retained custom 37-step sampling, seven transition frames and its root constraint while changing only the intended prompt.
- These changes were tested against saved animations. No inference worker was started, new generation submitted, Blender conversion executed or Unreal import performed for this delivery. The UI improves authoring and inspection; visual motion quality, contacts, deformation and engine acceptance still require review of generated results.

## 2026-11-03 — Saved animation plans and constraint-preserving beat revisions

### Added

- Five shared MCP and Studio API operations save, load, list, revise and explicitly generate an animation plan. The running catalog now contains 47 tools, 13 workflow recipes and the existing eight complete domain schemas.
- Immutable saved plans retain the entire validated generation request, including custom sampling steps, both guidance weights, transition length, initial heading, root margin, postprocessing, export formats and pose convention, and complete constraint arrays. Titles and authoring notes do not silently alter prompts.
- Content-derived plan identities, separate request hashes, verified receipts, parent-plan lineage and identical-content reuse. Changed content creates a new plan; originals remain available. Missing or invalid receipts are not silently repaired, and listing reports invalid records separately from valid summaries.
- Revision previews show changed fields without saving or submitting. An explicit `save=true` retains a child plan; the separate `generate_animation_plan` call submits its exact request and records saved-plan provenance on the job.
- Bounded paginated plan summaries in MCP workspace state, plus all five actions through the existing protected local Studio API. Frequent Studio state polling does not rescan the plan library; clients explicitly refresh it through the list operation.
- A `saved-plans` discovery recipe and a detailed authoring guide covering practical self-contained beats, exact frame timelines, advanced-setting preservation, deliberate constraint rebuilding, reproducibility limits and later completed-motion review/export.

### Changed

- Text-only replacement beats retain existing constraints when every segment keeps the same quantized frame count. Duration edits that quantize to the same allocation also preserve anchors. A changed segment allocation still requires explicit constraint clearing or a new request with rebuilt constraints; equal total duration alone is insufficient.
- Corrected transition documentation: each later segment generates extra history and may reprocess the preceding segment's tail. Requested action frames remain present; semantic settling time and seamless continuity are not predicted or guaranteed.

### Validation and scope

- Offline tests cover exact advanced-request round trips, receipt identity and integrity, lineage, duplicate reuse, bounded listing, revision preservation, changed-boundary rejection, explicit submission provenance and shared MCP/Studio access. Route tests retain request-token checks and verify that authoring never submits a generation job; submission is captured by a test stub.
- A fresh real MCP stdio connection discovered all 47 tools, saved and reloaded a three-beat request with frame allocations `[45, 90, 60]`, and reused its identical saved content. No generation jobs were added or changed during that check.
- No new inference, worker startup, Blender execution or Unreal import was performed for this delivery. Saved plans establish reproducible instructions, not proof of natural motion, prop interaction, target-engine acceptance or production quality. Repeated explicit generation creates another take; it is not a cached or idempotent generation lookup.

## 2026-11-03 — Interactive rig inspection and correction studio

### Added

- A source-skeleton viewer with exact frame playback, clickable joints, searchable names, anatomical groups, selected-chain highlighting/isolation, labels, fixed camera views and keyboard navigation. Search preserves full skeleton context unless isolation is explicitly selected.
- Desktop focus mode places the viewport beside correction controls; narrow layouts stack accessibly. Original and fitted-character views remain distinct and use independently verified source identities.
- Local degree controls, original-frame windows, smooth edge blending, trimming and planar placement feed the existing typed MCP correction operation. Draft undo/redo/reset and per-joint removal prepare a complete original-relative edit before saving a new version.
- Version selection and exact original-frame ghosts support before/after review, including translated and trimmed clips. Switching versions retains the selected joint and original frame where available. Camera bounds include both compared sources.
- Selected-version diagnostics, pose anchoring, comparisons, conversion, downloads, character previews and Blender/Unreal controls. File labels distinguish mesh/animation roles and conversion options; edited preview readiness and trimmed timing are explicit.
- Detailed usage, keyboard shortcuts, MCP parity and real desktop/comparison/narrow-screen screenshots in the README and studio guide. Generation plans now display their transition and constraint-density advisories.

### Validation and scope

- 22 frontend tests pass, covering source/version rejection, hierarchy and frame validation, trimmed ghost alignment, search versus isolation, degree conversion, edit bounds, draft history, backend edit eligibility and exact preview/download selection. Production build succeeds with bundled dependencies.
- The actual studio saved a new five-degree forearm correction after stage/undo/redo. Its immutable receipt and output were independently verified through a fresh MCP stdio session exposing all 42 tools; repeating the same edit reused the same verified version.
- Browser checks covered desktop focus mode, source/character selection, exact frame scrubbing, retained joint/frame selection across trims, chain isolation, original ghosts, contact-unavailable diagnostics and a narrow 390-pixel viewport without horizontal document overflow. The final checked page reported no browser errors.
- Saving a draft performs real backend forward kinematics; the viewer never fabricates an unsaved pose. Existing sources remain unchanged. Natural appearance, contacts and fitted-character deformation still require visual review; no automatic production approval is claimed.

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
