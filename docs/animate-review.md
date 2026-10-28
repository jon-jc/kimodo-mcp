# Motion review and Animate source study

The motion review tools help select and diagnose Kimodo output. They do not change model weights, modify motion arrays, or automatically improve a generated performance. Actual generation controls remain the model, prompts and segment durations, seed and sample count, constraints, denoising settings, and supported native postprocessing. Character fitting, retargeting, and editor import remain separate stages.

The design study used [cth9191/animate at revision 7e5eb56](https://github.com/cth9191/animate/tree/7e5eb56feb2dd573f890e1b7b34748af43d58263). Animate is a procedural Canvas 2D animation and video toolchain. It contains useful timing, review, composition, and reproducibility ideas, but no human motion model, skeleton solver, skinning system, or Blender/Unreal retargeter. The bridge implements its own review and numerical analysis code; no Animate source, plugin configuration, or skill files are bundled.

## Candidate review

1. Generate a reproducible candidate and inspect its completed NPZ artifact.
2. Call `analyze_motion_quality` to identify diagnostic extrema and unavailable measurements.
3. Call `create_motion_review` and open the returned HTML. Inspect the labeled frames, nearby frames, and the full performance.
4. Generate another candidate with a deliberate change. Call `compare_motions` to compare matching rigs and frame rates using an explicit frame offset.
5. Choose the performance that satisfies the action and constraints, then inspect its actual character deformation and final editor import.

These tools resolve completed artifacts through their job manifests. The review page is standalone, works offline, and contains the motion data it displays. It does not load a CDN or execute code from the NPZ archive. Do not treat a review page as a substitute for the original motion artifact.

## Measurements and their limits

| Diagnostic | Interpretation and limits |
| --- | --- |
| Joint speed, acceleration, jerk | Finite differences of positions in meters at the recorded FPS. Fast intended movement can produce high values. Acceleration and jerk amplify noise. |
| Angular speed | Relative rotation angle per second from available joint rotation matrices. Near-valid matrices are projected to a valid rotation only in a diagnostic copy; saved data and hashes remain unchanged. Missing or invalid rotation data makes this metric unavailable. |
| Bone-length drift | Change in each parent-child segment length relative to its first frame. This can reveal inconsistent joint positions; it does not inspect a skinned surface. |
| Contact sliding | Horizontal foot-joint speed only for supported SOMA contact mappings and intervals whose two endpoints are labeled in contact. Incorrect model contact labels remain a source of error. |
| Floor proxy | Supported foot-joint height relative to the configured flat floor. A joint is not the sole of a shoe; this is not mesh penetration, terrain, or collision detection. |
| Loop seam | Endpoint pose, root, and derivative differences. A deliberate non-looping action need not have a small seam. This does not close the loop. |
| Paired position difference | Aligned joint-position distance and RMSE over the overlap. This measures difference between candidates, not which performance is more natural. |

Reports include units, mean, 95th percentile, maximum, eligible value counts, the worst joint and frame, and configured sensitivities where applicable. A threshold crossing requests inspection; it is not a physical plausibility verdict. Missing information produces an explicit unavailable result, not a zero or perfect score. No composite quality score is calculated.

Frame indices are zero-based. The worst frame for a forward difference is the ending frame of its measurement window. In a paired report, `position_difference.worst_frame_index` belongs to the first source timeline. A positive offset compares first frame `i` with second frame `i + offset_frames`.

## Exact frames and stable framing

The HTML shows front, side, and three-quarter orthographic views. Every view fits all joints across the entire source clip; a comparison fits both entire clips to the same bounds, including frames outside their overlapping interval. The camera does not follow individual poses. This keeps root movement, drift, and scale differences visible while adapting to portrait, square, and landscape layouts.

The contact sheet combines uniform timeline coverage, endpoints, and diagnostic frames, with at most 24 selected instants. The player retains every source frame. It supports frame scrubbing, previous/next controls, Space to play or pause, and Home/End at the frame slider. Playback starts paused and stops at the final frame. The URL can select an initial frame and aspect, for example `?frame=42&aspect=portrait`. The sheet can be printed using the browser's print controls.

Rendering is a function of the requested integer frame. `window.renderFrame(frame)` and `window.renderReviewCard(index)` support visual regression checks. Drawing a frame after later frames must reproduce its pixels in the same browser environment. Browser pixel identity is a renderer check, not proof of physical correctness or cross-browser pixel equivalence.

The page records SHA-256 hashes of its source archives. Numerical reports also include canonical per-frame SHA-256 hashes and a content hash over the supported motion channels and rig metadata. Single-clip review checks a supplied report's sample, timing, and content hashes when present. Comparison checks its supplied timing and alignment metadata. Archive hashes identify the exact files; canonical hashes allow numerical comparisons despite archive packaging differences. Hash equality does not establish aesthetic quality or equality of uninspected custom channels.

A 120-sample motion at 30 FPS has four seconds of sample playback and a final key at `119 / 30 = 3.966667` seconds. Both quantities are labeled. Unreal reports the key span; comparing it directly with sample playback duration would create an apparent one-frame error.

Comparisons require matching FPS, skeleton identifiers, joint names, and hierarchy. They use the overlapping exact frames without implicit resampling. World mode retains translation. Root-relative mode subtracts each pose's root translation only; it does not align facing, scale, or limbs. Review refuses inputs above its two-million-coordinate budget, including both clips in a pair, rather than silently skipping or interpolating diagnostic frames.

## What transfers from Animate

Paths in this table are relative to Animate's `plugins/animate/skills/animate/` directory. The study read all 105 tracked text files, including seven style kits and their demos, templates, examples, grammar, tools, and repository metadata. Its nine PNG references were inventoried; the combined style gallery and storyboard were visually inspected, while seven individual style sample PNGs were not separately inspected. Reading its external authoring instructions did not install them or adopt their approval workflow.

| Source area | Relevant idea and treatment |
| --- | --- |
| `kit/core.js`, `kit/score-head.js`, `kit/score-tail.js` | Frame-quantized playback, keyed randomness, and functions of absolute time motivate deterministic review. Audio tail folding is unrelated to pose-loop continuity. Decorative two-frame jitter and spring overshoot are excluded from skeletal tracks. |
| `tools/framehash.mjs`, `tools/tile.mjs` | Selected-frame hashing and reverse-order rendering motivate deterministic browser checks. The bridge uses full SHA-256 numerical hashes and responsive layouts; it does not port the fixed portrait tile geometry. |
| `tools/review.mjs`, `tools/storyboard.mjs`, `tools/still.mjs`, `kit/board.js` | Contact sheets, exact time labels, boundary frames, and using the same renderer for previews and inspection inform the new review page. Three-view skeletal inspection and physical-unit diagnostics are original additions. Pixel contrast, music-cut alignment, and dead-beat heuristics do not assess body motion. |
| `tools/compare.mjs` | Side-by-side reference inspection informs synchronized candidate comparison. Palette and proportional image comparisons are replaced with matching-rig, matching-FPS numerical comparisons and shared camera bounds. |
| `tools/textcheck.mjs`, `grammar/FRAME.md` | Safe composition and legible annotations inform responsive labels and camera fitting. Text-polygon overlap checks cannot detect body contact or mesh penetration. |
| `grammar/STORY.md`, `grammar/FORMATS.md`, `craft.md`, templates and examples | Clear beats, duration planning, and explicit review criteria help structure prompts and segments. Editorial rules for short 2D videos are not universal requirements for human motion. Metadata and prose are checked against executable content when they disagree. |
| `tools/beats.mjs` | Its constant-tempo estimate could support future user-provided audio annotations. No audio-analysis dependency or automatic retiming is added to this bridge. |
| `kit/morph.js` and style transition hooks | Polygon morphing and editorial transitions may help a future presentation layer. They are not a bone solver or safe motion interpolation method and are excluded from source poses. |
| `styles/math`, `styles/isometric` | Absolute-time presentation, fixed projections, screen-space captions, and measured readouts inform stable diagnostic views. Isometric painter ordering is not true depth rendering; its Z-up convention differs from Kimodo's Y-up. Springs are suitable only for optional camera/UI animation, not planted feet. The current review cameras stay fixed. |
| `styles/cut-paper`, `crosshatch`, `riso`, `sketchbook`, `pixel` | These implement material texture, line wobble, color registration, mascots, or intentionally discrete pixel motion. They are excluded from diagnostic rendering because they can obscure small motion changes. |
| `tools/build.mjs`, `tools/export.mjs` | Build and export share a deterministic renderer and explicit FPS. The bridge keeps its own bounded numeric loader and standalone HTML; it does not accept arbitrary piece JavaScript or add ffmpeg, motion blur, audio stems, or MP4 export. |
| `tools/capture.mjs`, `tools/gallery.mjs`, `tools/gpu.mjs`, `tools/measure/refs.mjs`, `tools/measure/shotlog.py` | Website capture, style galleries, browser-GPU setup, palette measurement, and reference-video shot analysis serve a different pipeline. They do not establish CUDA readiness, correct 3D motion, or final engine performance and are not added. |
| `tools/voice.mjs`, `tools/voice_words.py` | Voice assembly and transcript alignment are separate narration features. They add no direct skeletal animation improvement and are excluded. |
| `SKILL.md`, `intake.md`, `new-style.md`, plugin manifests | These describe the external author's assistant workflow and distribution. They remain source-study material and are not copied into this repository. |

## Upstream review and export edge cases

The following are static findings at the pinned Animate revision, not claims that its entire export pipeline was executed here:

- `review.mjs` prints several failed checks without propagating every failure into its process exit status. A successful command exit alone is not a complete review pass.
- Some contact-sheet frame paths use the unsuffixed `frames` directory even for alternate formats, while caption burning in `export.mjs` uses `final.mp4`. Alternate-format outputs need explicit verification.
- `tile.mjs` assumes portrait dimensions and fixed safe-zone coordinates. The bridge computes aspect-aware framing instead.
- Partial exports retain existing interior frame files without proving that they came from the same source build. A mixed stale/new frame set can look complete; source identity must be recorded independently.
- Selected PNG hashes use a truncated MD5 value. They cover those rendered frames in that environment, not all motion or physical quality.
- `measure/shotlog.py` rounds video FPS and uses image-difference heuristics. It is unsuitable as the time base for exact skeletal comparison.
- The mathematical plotting helper can connect across nonfinite samples, and filled plots assume finite points exist. Any future metric graphs should preserve unavailable intervals explicitly.

The [Animate MIT license](https://github.com/cth9191/animate/blob/7e5eb56feb2dd573f890e1b7b34748af43d58263/LICENSE) credits cth9191, 2026. This document attributes design concepts; the bridge's implementation is original. Any future source reuse must retain the required copyright and permission notice.
