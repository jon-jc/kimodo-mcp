# EasyMocap integration source audit

This audit concerns EasyMocap commit `e6006fd3814d5f8ad45a7ce965beb9bcc90767f2` and the bounded local video workflow described in [EasyMocap setup](easymocap.md). It identifies implemented entry points and adjacent upstream capabilities. It is not a claim that every upstream application has been integrated or that ordinary footage produces accurate motion.

## Implemented source mapping

All upstream links below are pinned to the reviewed commit. Adapter modules are original wrappers and numerical conversion code; licensed model data is supplied separately by the operator.

| Operation | Reviewed upstream source | Adapter behavior |
| --- | --- | --- |
| Decode selected video | [`apps/preprocess/extract_image.py`](https://github.com/zju3dv/EasyMocap/blob/e6006fd3814d5f8ad45a7ce965beb9bcc90767f2/apps/preprocess/extract_image.py) | Uses its own fixed-argument FFmpeg process instead of the upstream shell command. Records constant-rate sampling and exact reference images. |
| BODY25 detection | [`apps/preprocess/extract_keypoints.py`](https://github.com/zju3dv/EasyMocap/blob/e6006fd3814d5f8ad45a7ce965beb9bcc90767f2/apps/preprocess/extract_keypoints.py), [`mediapipe_wrapper.py`](https://github.com/zju3dv/EasyMocap/blob/e6006fd3814d5f8ad45a7ce965beb9bcc90767f2/easymocap/estimator/mediapipe_wrapper.py) | Fixed `mp-pose` or `yolo-hrnet`, one selected image subdirectory; no arbitrary executable or configuration arguments. |
| Assumed camera | [`apps/calibration/create_blank_camera.py`](https://github.com/zju3dv/EasyMocap/blob/e6006fd3814d5f8ad45a7ce965beb9bcc90767f2/apps/calibration/create_blank_camera.py) | Creates the documented default intrinsics and identity extrinsics from extracted image dimensions. This is not camera calibration. |
| Monocular fitting | [`apps/fit/fit.py`](https://github.com/zju3dv/EasyMocap/blob/e6006fd3814d5f8ad45a7ce965beb9bcc90767f2/apps/fit/fit.py), [`config/fit/1v1p.yml`](https://github.com/zju3dv/EasyMocap/blob/e6006fd3814d5f8ad45a7ce965beb9bcc90767f2/config/fit/1v1p.yml) | Fixed SMPL model, MultiVideo reader and 1v1p stages; rendering disabled; reviewed joint reprojection loss override uses Geman–McClure with weight 100. |
| Initialization | [`easymocap/multistage/init_cnn.py`](https://github.com/zju3dv/EasyMocap/blob/e6006fd3814d5f8ad45a7ce965beb9bcc90767f2/easymocap/multistage/init_cnn.py) | SPIN body initialization with shared shape. Adapter checks all detections before entry to prevent missing-frame previous-pose substitution. |
| Fitted result layout | [`easymocap/datasets/base.py`](https://github.com/zju3dv/EasyMocap/blob/e6006fd3814d5f8ad45a7ce965beb9bcc90767f2/easymocap/datasets/base.py) | Reads each `smplfull/video/<frame>.json`, requires one person and full parameter coverage. |
| SMPL reconstruction | [`easymocap/bodymodel/smpl.py`](https://github.com/zju3dv/EasyMocap/blob/e6006fd3814d5f8ad45a7ce965beb9bcc90767f2/easymocap/bodymodel/smpl.py), [`lbs.py`](https://github.com/zju3dv/EasyMocap/blob/e6006fd3814d5f8ad45a7ce965beb9bcc90767f2/easymocap/bodymodel/lbs.py) | Obtains the fitted-shape neutral 24 joints and actual posed model joints, then verifies independent forward kinematics against them. BODY25 image landmarks are not substituted for model joints. |
| Export and receipt | [`video_mocap_math.py`](../src/kimodo_mcp/video_mocap_math.py), [`video_mocap_worker.py`](../src/kimodo_mcp/video_mocap_worker.py), [`video_mocap.py`](../src/kimodo_mcp/video_mocap.py) | Original normalized NPZ and BVH conversion, manifest verification, bounded processes, cancellation and managed artifact retrieval. |

## Numerical decisions and rejected shortcuts

EasyMocap's model applies global rotation Rh and translation Th after linear-blend skinning. A nonzero shaped pelvis J0 therefore gives world root `C @ (R(Rh) @ J0 + Th)`, where `C=diag(1,-1,-1)`. Using Th alone loses the root offset. Child rotations remain in model-local coordinates; the normalized root orientation includes C. The conversion requires static shape, unit scale, the exact SMPL24 parent hierarchy and zero root entries in the expanded 72-value body pose. Unsupported layouts fail explicitly.

The upstream [`scripts/postprocess/convert2bvh.py`](https://github.com/zju3dv/EasyMocap/blob/e6006fd3814d5f8ad45a7ce965beb9bcc90767f2/scripts/postprocess/convert2bvh.py) contains a marked root-motion issue where global Rh is inserted into the pose root. The adapter does not use it as a correctness oracle. The `convert_to_standard_smpl` helper in the pinned SMPL module also references a `delta_center` value whose assignment is commented out; it is not called.

The original converter uses stable Rodrigues matrices, forward kinematics and intrinsic XYZ BVH decomposition. It handles small rotations, half turns, gimbal cases and neighboring Euler branches. BVH channels follow the emitted hierarchy's traversal order, which differs from SMPL's array order. Fitted-shape offsets preserve bone lengths. Every normalized frame must agree with upstream model joints within 0.0001 meter before publication.

Synthetic tests compare independent homogeneous-transform FK, reparse exported BVH and cover malformed dimensions, dynamic shapes, unexpected root pose, non-unit scale and nonfinite numbers. A separate synthetic 64-frame comparison against the pinned Torch LBS arithmetic measured a maximum joint error of approximately `2.61e-8` meter. This comparison used constructed data, not licensed body assets or reconstructed video, and establishes arithmetic consistency only.

The fixed upstream temporal losses use four offsets and second differences. Exactly five frames can create an empty tensor and undefined loss; the adapter requires at least six samples. This protects the selected pipeline rather than claiming arbitrary upstream configurations are safe.

## Other upstream capabilities and safe extension boundaries

| Upstream area | Relevant source | Current exposure and bounded next step |
| --- | --- | --- |
| Interactive image annotation | [`apps/annotation/annot_keypoints.py`](https://github.com/zju3dv/EasyMocap/blob/e6006fd3814d5f8ad45a7ce965beb9bcc90767f2/apps/annotation/annot_keypoints.py), `annot_track.py` | Not launched by the adapter. A future immutable landmark patch could bind frame/person/joint edits to source hashes and require explicit refitting. |
| Detection filtering | [`easymocap/dataset/filter.py`](https://github.com/zju3dv/EasyMocap/blob/e6006fd3814d5f8ad45a7ce965beb9bcc90767f2/easymocap/dataset/filter.py) | Adapter currently reports coverage and rejects unusable frames. Future filters should copy observations, preserve original confidence and explicitly handle all-missing frames. |
| Temporal smoothing | [`myeasymocap/operations/smooth.py`](https://github.com/zju3dv/EasyMocap/blob/e6006fd3814d5f8ad45a7ce965beb9bcc90767f2/myeasymocap/operations/smooth.py) | No optional cleanup tool is exposed. Arithmetic axis-angle averaging fails across angle branches, and averaged rotation matrices require projection back onto valid rotations. Do not label blanket smoothing as contact repair. |
| Camera calibration | [`apps/calibration/calib_intri.py`](https://github.com/zju3dv/EasyMocap/blob/e6006fd3814d5f8ad45a7ce965beb9bcc90767f2/apps/calibration/calib_intri.py), `calib_extri.py`, `check_calib.py` | Not integrated. A bounded next step is calibration-file validation and reprojection reporting before any calibrated fitting workflow. |
| Calibrated triangulation | [`myeasymocap/operations/triangulate.py`](https://github.com/zju3dv/EasyMocap/blob/e6006fd3814d5f8ad45a7ce965beb9bcc90767f2/myeasymocap/operations/triangulate.py), `iterative_triangulate.py` | Not integrated. Requires synchronized views, established camera calibration and consistent person association; ordinary single-view files do not provide these inputs. |
| Tracking and association | [`apps/demo/auto_track.py`](https://github.com/zju3dv/EasyMocap/blob/e6006fd3814d5f8ad45a7ce965beb9bcc90767f2/apps/demo/auto_track.py), `apps/preprocess/extract_track.py` | Not integrated. Current capture cannot choose among multiple tracked people. Person identity and per-frame ambiguity must be explicit before exposing this workflow. |

The bridge includes an original optional short-gap **2D** interpolation step before fitting. It preserves detector observations and timing, marks every estimate, caps confidence and refuses ambiguous identities, extrapolation, long gaps and degenerate/discontinuous results. It does not interpolate output rotations or repair contacts. A future 3D cleanup stage would need rotation-manifold interpolation and bone-length preservation; averaging axis-angle values or compressing failed frames out of time would conceal missing evidence.

## Provenance and operational boundaries

The runtime is operator-selected trusted local software. The adapter checks the pinned checkout and known asset fingerprints, uses fixed subprocess argument lists and disables Python-stage network connections. It does not sandbox arbitrary third-party software or claim that hashing a pickle makes it safe; body-model files are installed by the operator, not accepted as uploaded job inputs.

Completed artifact hashes establish which files belong to a request. They do not attest to physical correctness, model licensing, or footage ownership. Review detection coverage, source timing and the exact analyzed video before downstream editing. SMPL24 output remains distinct from Kimodo's SOMA rig; no automatic character retarget, physics/contact pass, Unreal import or NeRF workflow is implied.
