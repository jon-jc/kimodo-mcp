# Native controls and the combined workflow

Studio and MCP share managed requests, verified outputs and explicit execution. The integrated projects have different representations and purposes: Kimodo generates motion, FreeMoCap reconstructs landmarks, EasyMocap fits a body model to video, UniRig creates a static rig, and Blender/Unreal author and deliver assets. Animate supplies reviewed presentation ideas. Their native controls are not interchangeable.

The capability inventory identifies **available managed controls**, **external controls** that remain in the original application, and **unavailable controls**. It is read-only source coverage, not runtime readiness, and explicitly reports that full native parity is false. `get_input_schema` provides exact field types/ranges; `get_workflow` provides the ordered tools and required evidence for a selected path. Installed models, successful GPU execution and editor acceptance must be established separately.

## Choose the shortest valid path

| Starting point | Shared workflow | Review before continuing |
| --- | --- | --- |
| A text brief | Plan/generate → inspect/compare → precise correction → registered-character export | Timing, poses, contacts and the actual skinned preview |
| Synchronized camera videos | FreeMoCap import/calibrate/process → review → explicit BVH export → cleanup/character retarget | Calibration identity, missing landmarks, source units and rest pose |
| Ordinary single-person footage | EasyMocap trim/detect/fit → matched video/skeleton review → verified SMPL24 NPZ/BVH | Detection gaps, estimates, assumed camera and uncertain depth/scale |
| An unrigged mesh | Asset import → UniRig skeleton → Blender refinement if needed → skin → merge → library registration | Bone layout and skin deformation; retarget mapping remains explicit |
| A sword or other static prop | Asset import → attachment scene → select verified character motion → build/review → static prop and recipe exports | Bone axes, scale, grip, intersections and Unreal socket conversion |
| A finished character clip | Verified Blender character output → explicit intended Unreal project/rig/destination → import/report | Engine-side timing, hierarchy, mesh playback and acceptance report |

FreeMoCap landmark arrays are not rotations. Its addon BVH, EasyMocap's SMPL24 BVH, Kimodo's SOMA BVH and a UniRig skeleton are distinct representations. The current capture-character adapter supports the reviewed FreeMoCap addon layout and the registered 24-bone target; it does not convert arbitrary BVH files just because they use the same extension. The same restriction applies to rigged models: library registration does not automatically select a new target character.

## Source coverage matrix

| Reviewed source | Managed controls available in Studio/MCP | Native/external controls and boundaries |
| --- | --- | --- |
| [Kimodo `e1c219c`](https://github.com/nv-tlabs/kimodo/tree/e1c219c99b4b94ba48be1fb6a1cbc64ecee4c954) | Released generator, prompts/segments, seed/candidates, diffusion steps, transition overlap, three guidance modes, heading, postprocessing/root margin, G1 projection, pose/path/end-effector constraints, inspection/correction and format-specific export | Original visual constraint gestures, examples, motion-file loading, native body/robot visualizers, camera/opacity/contacts, screenshot/video rendering and larger native resource ranges remain in the explicitly started original workspace. Training/TMR evaluation are separate. |
| [FreeMoCap `700c861`](https://github.com/freemocap/freemocap/tree/700c8614d11d886dacfea8e52333d3c0b942ef52) | Reference metadata, audio/brightness synchronization, import, measured board calibration, MediaPipe/RTMPose controls, filtering/triangulation controls, explicit MediaPipe Blender/FBX/BVH export, body/hand/face review, bounded cleanup and fitted-character retarget | Original camera discovery, device configuration, live recording and Electron controls remain external. One camera produces planar pixel-space output. RTMPose Blender export and animated glTF export are not provided by this adapter. Export cleanup/rest-pose requests require visual verification. |
| [EasyMocap `e6006fd`](https://github.com/zju3dv/EasyMocap/tree/e6006fd3814d5f8ad45a7ce965beb9bcc90767f2) | Trim, FPS/frame limit, mp-pose/yolo-hrnet, optional bounded 2D short-gap estimation, fixed single-person SMPL fitting, matched video review, diagnostics and NPZ/BVH export | Image annotation, calibrated multi-view fitting, multi-person tracking, alternate body models/losses and research configs remain external. Assumed camera fitting does not recover hidden motion reliably or measure scale. SMPL24 does not enter the FreeMoCap-specific character handoff. |
| [UniRig `6793c66`](https://github.com/VAST-AI-Research/UniRig/tree/6793c6640ff01c8fb389f3993434124bb43d2933) | All five managed released rigging stages; seed/faces, sampling/token limit/beam/top-k/top-p/temperature/repetition, skin backend, edited/prior rig selection, GLB/FBX merge/root control, result registration | Alternate task/model/class/tokenizer/trainer configs, datasets, training and RigNet benchmark are operator workflows. Bone physics attributes and paper Rig-XL/VRoid checkpoints are absent at this revision. A predicted rig is static and has no automatic SOMA/Manny/character retarget map. |
| [Animate `7e5eb56`](https://github.com/cth9191/animate/tree/7e5eb56feb2dd573f890e1b7b34748af43d58263) | Original implementations informed by exact-frame rendering, contact sheets, synchronized comparison, motion beats and hash-based provenance | Its 2D style kits, polygon morphing, audio/voice assembly, web-video authoring and export tools are not installed as MCP operations. Decorative animation and image scores are not skeletal solvers or physical-quality checks. |
| Blender and Unreal editor integrations | Verified skeleton/character preparation and execution, saved reports, model previews, rigid prop scenes, explicit Unreal project/rig/destination and animation import | Weight painting, arbitrary rig changes, IK, Control Rig, Sequencer, sockets, physics, collision and gameplay remain deliberate editor/MCP operations. The bridge does not provide automatic universal retargeting, two-hand gripping or prop socket conversion. |

The complete original **Kimodo** UI can be launched from its native-workspace panel. Its URL alone does not indicate readiness: use the owned start/status result. Native UI state and its manually saved exports do not automatically become managed jobs. For the other projects, this inventory links their reviewed source and reports external controls without pretending to start or wrap their entire applications.

## Controls that matter for natural motion

Use small, reviewable edits. Keep the original clip and create a new variant or handoff attempt for a correction. Compare the same frame range and coordinate space before and after changing joint offsets, root placement or smoothing. Smoothing may remove jitter while also flattening a purposeful impact or weakening planted contacts; a numerical reduction in frame-to-frame change is not a quality verdict.

For a capture, inspect missing points and tracking swaps before smoothing. Confirm synchronization and calibration for measured multi-view reconstruction. Keep short-gap estimates visible, preserve source time and avoid extrapolating unsupported motion. The registered target has 24 bones; hand and face landmarks do not become articulated fingers or facial animation without a compatible target and separate mapping.

For a new model, inspect UniRig bones before skin prediction when anatomy is unusual. Check representative bending poses after weighting. For weapons, first establish a usable grip in the character motion, then place the rigid prop and review it through the whole action. Attachment does not solve the arm, fingers, contacts or collisions.

## Detailed contracts

- [Native Kimodo feature coverage and original-workspace lifecycle](native-feature-coverage.md)
- [FreeMoCap setup, stage controls and evidence](freemocap.md)
- [Capture review](capture-review.md) and [cleanup/character retarget](capture-retarget.md)
- [EasyMocap controls](easymocap.md) and [source audit](mocap-source-audit.md)
- [UniRig setup, inference controls and edited-rig loop](unirig.md)
- [Asset library, rigid props and export conventions](assets-and-props.md)
- [Animate source study](animate-review.md)
- [Blender/Unreal protocols and verified handoffs](editor-handoffs.md)

Coverage is tied to the pinned revisions above. A newer upstream release may add controls or change signatures; update its adapter and tests before treating that release as compatible. No runtime is installed or started by reading this inventory.
