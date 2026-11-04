"""Read-only inventory of managed controls and upstream-only capabilities.

This is a source-coverage inventory, not a runtime health check. Keep the
distinction visible to clients so a discoverable feature is never mistaken for
an installed model, an executing camera service, or a verified editor result.
"""


def get_native_control_coverage() -> dict:
    return {
        "schema_version": 1,
        "executes_actions": False,
        "full_native_parity": False,
        "notes": [
            "Available controls are implemented bridge operations, subject to runtime setup and their typed input bounds.",
            "External controls remain in the original application or research workflow; this inventory does not execute them.",
            "Unavailable controls are unsupported by the reviewed integration or absent from the reviewed upstream release.",
            "Use get_input_schema for exact request fields and get_workflow for ordered, explicit actions.",
        ],
        "sources": [
            {
                "id": "kimodo", "title": "Kimodo generation and native workspace",
                "reviewed_revision": "e1c219c99b4b94ba48be1fb6a1cbc64ecee4c954",
                "source_url": "https://github.com/nv-tlabs/kimodo/tree/e1c219c99b4b94ba48be1fb6a1cbc64ecee4c954",
                "native_url": "http://127.0.0.1:8769",
                "managed_workflows": ["generate", "saved-plans", "reuse-pose", "correct-motion", "export-motion", "native-workspace"],
                "available_controls": [
                    "Released generator selection, prompts and ordered segments, seed, candidates, diffusion steps and transition frames",
                    "Separated, regular or disabled guidance; heading, root margin, postprocessing and G1 rotation projection",
                    "Root paths, full-body poses and named end-effector constraints with exact frame references",
                    "Completed-motion inspection, comparisons, immutable corrections and native NPZ/BVH/AMASS/G1 CSV exports",
                    "Explicit start, status and stop for the original native workspace",
                ],
                "external_controls": [
                    "Original visual constraint timeline, waypoint gestures, example browser and local motion-file loading",
                    "SOMA/SMPL-X/G1 native visualizers, foot contacts, opacity and camera controls",
                    "Original screenshot and video rendering; native sample and diffusion ranges beyond managed bounds",
                    "TMR retrieval/evaluation and model training",
                ],
                "unavailable_controls": ["Automatic synchronization of native UI state or exports into managed job history"],
                "notes": ["The native URL is usable only after an explicit successful owned workspace start; this inventory does not check readiness."],
            },
            {
                "id": "freemocap", "title": "FreeMoCap capture, cleanup and character handoff",
                "reviewed_revision": "700c8614d11d886dacfea8e52333d3c0b942ef52",
                "source_url": "https://github.com/freemocap/freemocap/tree/700c8614d11d886dacfea8e52333d3c0b942ef52",
                "native_url": None,
                "managed_workflows": ["freemocap", "capture-character"],
                "available_controls": [
                    "Reference-video metadata, audio/brightness synchronization, explicit new recording import and recording inspection",
                    "ChArUco board geometry, dictionary, measured calibration and ground-plane choice",
                    "MediaPipe confidence controls or three reviewed RTMPose models and confidence threshold",
                    "Low-pass cutoff/order, outlier rejection, camera-count/drop limits and target reprojection error",
                    "Explicit MediaPipe Blender/FBX/BVH export, rest pose, requested foot locking and hand-marker limits",
                    "Body/hand/face frame inspection, missing-data masks and body quality diagnostics",
                    "Verified BVH trim, source units/rest pose, root motion, bounded rotation/root smoothing and joint offsets before fitted-character export",
                ],
                "external_controls": [
                    "Original camera/device discovery, live previews, capture/recording and device settings",
                    "Original Electron workspace, camera hardware setup and native addon editing",
                ],
                "unavailable_controls": [
                    "Metric depth reconstruction from a single FreeMoCap camera; that upstream path is planar pixel-space output",
                    "RTMPose Blender export and animated glTF export in the reviewed adapter",
                    "General hand/face retargeting to the registered 24-bone target",
                ],
                "notes": ["Calibration, processing and export require the separate compatible service and durable status extension. Native export settings are requested, not certified by file existence."],
            },
            {
                "id": "easymocap", "title": "EasyMocap single-video body fitting",
                "reviewed_revision": "e6006fd3814d5f8ad45a7ce965beb9bcc90767f2",
                "source_url": "https://github.com/zju3dv/EasyMocap/tree/e6006fd3814d5f8ad45a7ce965beb9bcc90767f2",
                "native_url": None,
                "managed_workflows": ["video-mocap"],
                "available_controls": [
                    "Video trim, sample rate and frame limit, mp-pose or yolo-hrnet detector",
                    "Explicit bounded 2D short-gap estimation with preserved observations and estimated-frame labels",
                    "Fixed SMPL single-person fitting, exact video/skeleton frame review, diagnostics, NPZ/BVH exports and cancellation",
                ],
                "external_controls": [
                    "Image/keypoint annotation, camera intrinsics/extrinsics calibration and calibrated multi-view triangulation",
                    "Multi-person tracking/association, other body models and research fitting/loss configurations",
                ],
                "unavailable_controls": [
                    "Reliable recovery of hidden motion or measured scale/depth from arbitrary footage",
                    "Direct SMPL24-to-registered-character retarget through the FreeMoCap-specific handoff",
                ],
                "notes": ["The managed path uses assumed camera intrinsics and one person. Detection confidence is not 3D reconstruction certainty."],
            },
            {
                "id": "unirig", "title": "UniRig model rigging",
                "reviewed_revision": "6793c6640ff01c8fb389f3993434124bb43d2933",
                "source_url": "https://github.com/VAST-AI-Research/UniRig/tree/6793c6640ff01c8fb389f3993434124bb43d2933",
                "native_url": None,
                "managed_workflows": ["unirig", "asset-props"],
                "available_controls": [
                    "Preprocess, skeleton prediction, skin prediction, merge, and complete pipeline for 1–4 registered assets",
                    "Seed, target face count, sampling toggle, decoder token limit, beam count, top-k/top-p, temperature and repetition penalty",
                    "Pyrender/open3d skin backend, existing or edited rig source, prior verified skeleton/skin selection",
                    "GLB/FBX merged output, optional root bone, explicit job control, verified output registration and preview",
                ],
                "external_controls": [
                    "Skeleton refinement in Blender before predicting weights",
                    "Dataset preparation, training, RigNet benchmarking, alternate class/tokenizer/model/task configuration",
                    "Dependency/CUDA installation, checkpoint provisioning and VRM addon setup",
                ],
                "unavailable_controls": [
                    "Bone physics/stiffness prediction and Rig-XL/VRoid paper-model checkpoints in the reviewed release",
                    "Automatic motion generation or universal mapping of generated rigs to SOMA, Manny or the registered character",
                ],
                "notes": ["The released Articulation-XL models create static rigs. Usable deformation and animation compatibility still require review.",
                          "UniRig requires geometry-only OBJ without material directives; prefer self-contained GLB for materials and reproducible source identity."],
            },
            {
                "id": "animate", "title": "Animate-inspired review and motion planning",
                "reviewed_revision": "7e5eb56feb2dd573f890e1b7b34748af43d58263",
                "source_url": "https://github.com/cth9191/animate/tree/7e5eb56feb2dd573f890e1b7b34748af43d58263",
                "native_url": None,
                "managed_workflows": ["assess-candidates", "inspect", "compare", "saved-plans"],
                "available_controls": [
                    "Original bridge implementations of exact-frame review, contact sheets, fixed camera views and synchronized comparison",
                    "Motion beats, duration planning, shared rendering and source/content hashes",
                ],
                "external_controls": [
                    "Animate's 2D style kits, polygon morphing, audio/voice assembly, beat analysis and web-video authoring/export tools",
                ],
                "unavailable_controls": ["Using decorative 2D transitions, image scores or audio-tail folding as a skeletal solver or physical-quality proof"],
                "notes": ["This repository supplied reviewed design ideas. Its tools and assistant skill are not installed or copied into the MCP server."],
            },
            {
                "id": "editors", "title": "Blender, Unreal and prop assembly",
                "reviewed_revision": None,
                "source_url": "https://www.blender.org/lab/mcp-server/",
                "native_url": None,
                "managed_workflows": ["export-skeleton", "export-character", "capture-character", "asset-props", "import-unreal"],
                "available_controls": [
                    "Explicit verified skeleton/registered-character Blender handoffs and reports",
                    "GLB/FBX/OBJ/VRM library import, explicit preview conversion and immutable scenes with 1–16 static props",
                    "Bone or world attachment, translation, rotation and scale; animated scene preview, separate prop FBX and attachment recipe",
                    "Exact Unreal project/skeleton/mesh/destination selection, explicit animation import and acceptance reports",
                ],
                "external_controls": [
                    "Blender's modeling, weight painting, rig editing, constraints, IK and animation authoring through its own editor/MCP",
                    "Unreal's IK Rig/Retargeter, Control Rig, Sequencer, sockets, collision, physics and gameplay integration through the intended editor/MCP",
                ],
                "unavailable_controls": [
                    "Automatic Unreal prop socket conversion/import from a Blender attachment recipe",
                    "General retargeting of arbitrary skeletons, automatic two-hand gripping or physical contact repair",
                ],
                "notes": ["The tested editor protocols and versions are documented in editor-handoffs.md. An open unrelated editor is never an implied destination."],
            },
        ],
    }
