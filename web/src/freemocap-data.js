// FreeMoCap stages are explicit operations against a separately configured runtime.
export const FREEMOCAP_STAGES = [
  { id: 'check_sync', title: 'Check videos', detail: 'Inspect frame counts and timing without processing.', run: 'Check these videos' },
  { id: 'synchronize', title: 'Synchronize', detail: 'Align multiple videos using shared audio or a visible flash.', run: 'Synchronize copies' },
  { id: 'import', title: 'Import a take', detail: 'Copy references into a new recording; originals stay intact.', run: 'Create recording copy' },
  { id: 'calibrate', title: 'Calibrate cameras', detail: 'Use a measured ChArUco board recording for the camera geometry.', run: 'Start calibration' },
  { id: 'process', title: 'Track & reconstruct', detail: 'Detect landmarks, reconstruct, interpolate and filter the motion.', run: 'Start motion processing' },
  { id: 'export', title: 'Export in Blender', detail: 'Create a Blender scene and FBX/BVH from supported MediaPipe output.', run: 'Export through Blender' },
];
const JOB = /^[0-9a-f]{32}$/;
const RECORDING = /^[A-Za-z0-9][A-Za-z0-9_-]{0,79}$/;
const ADVANCED = new Set(['rtmpose_model_name', 'rtmpose_confidence_threshold', 'mediapipe_detection_confidence', 'mediapipe_presence_confidence', 'mediapipe_tracking_confidence', 'filter_order', 'use_outlier_rejection', 'minimum_cameras', 'maximum_cameras_to_drop', 'target_reprojection_error']);
const key = value => JSON.stringify(value, (_, item) => item && typeof item === 'object' && !Array.isArray(item) ? Object.fromEntries(Object.keys(item).sort().map(name => [name, item[name]])) : item);
function number(value, minimum, maximum, label, integer = false) {
  const result = String(value ?? '').trim() ? Number(value) : NaN;
  if (!Number.isFinite(result) || result < minimum || result > maximum || (integer && !Number.isInteger(result))) throw new Error(`${label} must be ${integer ? 'a whole number' : 'a number'} from ${minimum} to ${maximum}.`);
  return result;
}
function reference(value) {
  const name = String(value ?? '').trim();
  if (!name || name.startsWith('/') || /[:\\\0]/.test(name) || name.split('/').some(part => !part || part === '.' || part === '..')) throw new Error('Use file names relative to the registered reference folder, with forward slashes.');
  return name;
}
export function validFreeMocapJobId(value) { return typeof value === 'string' && JOB.test(value); }
export function freeMocapStageReadiness(capabilities, operation) {
  if (!capabilities?.compatible) return { ready: false, note: 'Connect the reviewed external FreeMoCap runtime, then refresh to plan and run this stage.' };
  if (['calibrate', 'process', 'export'].includes(operation) && capabilities.status_extension?.available !== true) {
    return { ready: false, note: 'This stage needs the checked FreeMoCap status extension to verify completion and prevent overlapping writes. Start the external runtime with the operator guide’s launcher, then refresh. Video checks, synchronization and import remain available.' };
  }
  return { ready: true, note: ['calibrate', 'process', 'export'].includes(operation) ? 'Status extension connected. Completion is checked against this runtime instance and the recorded outputs.' : 'Base runtime connected. Review the inputs before running this stage.' };
}
export function freeMocapRequest(values) {
  const operation = values.operation;
  if (!FREEMOCAP_STAGES.some(item => item.id === operation)) throw new Error('Choose a FreeMoCap stage.');
  const request = { operation };
  if (['check_sync', 'synchronize', 'import'].includes(operation)) {
    const files = String(values.video_files ?? '').split(/\r?\n/).map(line => line.trim()).filter(Boolean).map(reference);
    if (!files.length || files.length > 8) throw new Error('Enter one to eight reference videos, one relative file name per line.');
    if (files.some(name => !/\.(mp4|mov|avi|mkv|webm)$/i.test(name))) throw new Error('Choose MP4, MOV, AVI, MKV or WebM videos.');
    const stems = files.map(name => name.split('/').pop().replace(/\.[^.]+$/, '').toLowerCase());
    if (new Set(stems).size !== stems.length) throw new Error('Every video needs a different file name stem, including across folders and formats.');
    if (operation === 'synchronize' && files.length < 2) throw new Error('Synchronization needs at least two cameras.');
    request.video_files = files;
  } else {
    const id = String(values.recording_id ?? '').trim();
    if (!RECORDING.test(id)) throw new Error('Choose an imported recording for this stage.');
    request.recording_id = id;
  }
  if (operation === 'synchronize') {
    if (!['audio', 'brightness'].includes(values.sync_method)) throw new Error('Choose audio or flash synchronization.');
    request.sync_method = values.sync_method;
    request.brightness_ratio_threshold = number(values.brightness_ratio_threshold ?? 1000, 1, 1_000_000, 'Brightness ratio');
  }
  if (operation === 'import') {
    const sync = String(values.sync_job_id ?? '').trim();
    if (sync && !validFreeMocapJobId(sync)) throw new Error('Use the 32-character synchronization receipt ID from this workspace.');
    if (sync) request.sync_job_id = sync;
    else request.synchronization_confirmed = Boolean(values.synchronization_confirmed);
  }
  if (operation === 'calibrate') {
    request.board = {
      squares_x: number(values.squares_x, 2, 30, 'Board columns', true),
      squares_y: number(values.squares_y, 2, 30, 'Board rows', true),
      square_length_mm: number(values.square_length_mm, 0.01, 1000, 'Measured square size'),
      marker_length_ratio: number(values.marker_length_ratio ?? 0.8, 0.01, 0.99, 'Marker-to-square ratio'),
      aruco_dictionary_enum: number(values.aruco_dictionary_enum ?? 2, 0, 20, 'ArUco dictionary ID', true),
    };
    request.use_groundplane = Boolean(values.use_groundplane);
  }
  if (operation === 'process') {
    if (!['mediapipe', 'rtmpose'].includes(values.detector_type)) throw new Error('Choose MediaPipe or RTMPose.');
    const text = String(values.process_advanced ?? '{}').trim() || '{}';
    if (text.length > 8000) throw new Error('Advanced processing settings are too large.');
    let advanced;
    try { advanced = JSON.parse(text); } catch { throw new Error('Advanced processing settings need valid JSON.'); }
    if (!advanced || typeof advanced !== 'object' || Array.isArray(advanced) || Object.keys(advanced).some(name => !ADVANCED.has(name))) throw new Error('Advanced settings contain an unsupported key; detector and cutoff are controlled by the fields above.');
    request.process_options = { ...advanced, detector_type: values.detector_type, cutoff_hz: number(values.cutoff_hz, 0.01, 100, 'Filter cutoff') };
    request.allow_planar = Boolean(values.allow_planar);
    if (values.calibration_source === 'reference') {
      request.calibration_file = reference(values.calibration_file);
      if (!request.calibration_file.toLowerCase().endsWith('.toml')) throw new Error('Select a calibration TOML reference.');
    } else if (values.calibration_source === 'recording') {
      if (!RECORDING.test(values.calibration_recording_id ?? '')) throw new Error('Choose the recording containing the completed camera calibration.');
      request.calibration_recording_id = values.calibration_recording_id;
    } else if (values.calibration_source !== 'none') throw new Error('Choose a calibration source.');
  }
  if (operation === 'export') {
    request.export_options = { formats: [values.fbx ? 'fbx' : null, values.bvh ? 'bvh' : null].filter(Boolean),
      rest_pose: values.rest_pose, apply_foot_locking: Boolean(values.apply_foot_locking),
      limit_hand_markers_range_of_motion: Boolean(values.limit_hand_markers_range_of_motion) };
    if (!['tpose', 'apose'].includes(values.rest_pose)) throw new Error('Choose a T-pose or A-pose rest rig.');
  }
  return request;
}

export class FreeMocapPlanGate {
  constructor() { this.revision = 0; this.plan = null; this.signature = null; }
  invalidate() { this.revision++; this.plan = null; this.signature = null; }
  begin(request) { this.invalidate(); return { revision: this.revision, signature: key(request) }; }
  accept(ticket, current, plan) {
    if (ticket.revision !== this.revision || ticket.signature !== key(current)) return false;
    if (plan?.operation !== current.operation || key(plan.request) !== key(current)) throw new Error('The plan does not match these exact settings. Review again before running.');
    this.plan = plan; this.signature = ticket.signature; return true;
  }
  ready(request) { return Boolean(this.plan && this.signature === key(request)); }
}

export function freeMocapJobState(job) {
  if (job?.status === 'completed' && job.pipeline_completion_verified === true) return 'Stage completed';
  if (job?.status === 'completed') return 'Completion reported — verification missing';
  if (job?.status === 'cancelled') return 'Cancelled';
  if (job?.status === 'failed') return 'Failed';
  if (job?.status === 'submission_unknown') return 'Submission outcome unknown — inspect before retrying';
  if (job?.status === 'submitted') return 'Submitted — completion not verified';
  return String(job?.status ?? 'No receipt selected');
}
export function canCancelFreeMocapJob(job) {
  return Boolean(validFreeMocapJobId(job?.job_id) && ['calibrate', 'process'].includes(job.operation)
    && job.upstream_pipeline_id && ['submitted', 'submitting', 'submission_unknown'].includes(job.status));
}
