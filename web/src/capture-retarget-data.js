import { numericField } from './native-data.js';
const copy = value => structuredClone(value);
const ordered = value => Array.isArray(value) ? value.map(ordered) : value && typeof value === 'object' ? Object.fromEntries(Object.keys(value).sort().map(name => [name, ordered(value[name])])) : value;
const key = value => JSON.stringify(ordered(value));
export const CAPTURE_FILES = Object.freeze({ 'character-preview.glb': 'Animated character GLB', 'animation.fbx': 'Character animation FBX', 'character.fbx': 'Character mesh FBX', 'character.blend': 'Blender project', 'cleanup-report.json': 'Cleanup evidence', 'cleaned-motion.npz': 'Cleaned source motion' });
export function captureBvhOutputs(job) {
  return job?.operation === 'export' && job.status === 'completed' && job.pipeline_completion_verified === true && /^[a-f0-9]{32}$/.test(job.job_id ?? '')
    ? (job.outputs ?? []).filter(item => typeof item.path === 'string' && item.path.toLowerCase().endsWith('.bvh') && /^[a-f0-9]{64}$/.test(item.sha256 ?? '')) : [];
}
export function captureArtifactUrl(id, path) {
  if (!/^[a-f0-9]{64}$/.test(id ?? '') || !Object.hasOwn(CAPTURE_FILES, path)) throw new Error('Choose a verified capture-character artifact.');
  return `/api/capture-character/${id}/files/${encodeURIComponent(path)}`;
}
export function validateCaptureSource(result, selection) {
  if (!/^[a-f0-9]{32}$/.test(result?.job_id ?? '') || result.job_id !== selection.job_id || result.artifact_path !== selection.artifact_path || result.source_sha256 !== selection.sha256 || !Number.isInteger(result.source_frames) || result.source_frames < 2 || result.source_frames > 144000 || !Number.isFinite(result.fps) || result.fps <= 0 || !result.mapping || !Object.keys(result.mapping).length) throw new Error('Source inspection does not match the selected verified BVH.');
  return result;
}
export function captureOptions(fields, source, corrections = []) {
  if (!source?.source_frames) throw new Error('Inspect a verified BVH source first.');
  const start = numericField(fields.start_frame, 'Trim start', 0, source.source_frames - 2, true);
  const end = numericField(fields.end_frame, 'Trim end', 2, source.source_frames, true);
  if (end - start < 2 || end - start > 2400) throw new Error('Keep 2 to 2400 original BVH frames in this version.');
  if (!['tpose', 'apose'].includes(fields.source_rest_pose)) throw new Error('Choose the rest pose used when this BVH was exported.');
  const smoothingStart = String(fields.smoothing_start_frame ?? '').trim() === '' ? start : numericField(fields.smoothing_start_frame, 'Smoothing start', start, end - 1, true);
  const smoothingEnd = String(fields.smoothing_end_frame ?? '').trim() === '' ? end : numericField(fields.smoothing_end_frame, 'Smoothing end', start + 1, end, true);
  if (smoothingEnd <= smoothingStart) throw new Error('Smoothing end must be after its start.');
  if (corrections.length > 16 || new Set(corrections.map(item => item.bone)).size !== corrections.length) throw new Error('Use at most 16 unique target bones.');
  const joint_corrections = corrections.map(item => {
    if (!Object.hasOwn(source.mapping, item.bone)) throw new Error('Choose a mapped character bone.');
    const first = numericField(item.start_frame, 'Joint window start', start, end - 1, true), last = numericField(item.end_frame, 'Joint window end', start + 1, end, true);
    if (last <= first) throw new Error('Joint correction end must be after its start.');
    return { bone: item.bone, rotation_degrees: item.rotation_degrees.map(value => numericField(value, 'Local rotation degrees', -45, 45)), start_frame: first, end_frame: last, blend_frames: numericField(item.blend_frames, 'Joint blend frames', 0, 30, true) };
  });
  return { start_frame: start, end_frame: end, source_scale_to_meters: numericField(fields.source_scale_to_meters, 'Source scale to meters', 0.000001, 10), source_rest_pose: fields.source_rest_pose,
    root_motion: Boolean(fields.root_motion), rotation_smoothing_radius: numericField(fields.rotation_smoothing_radius, 'Rotation smoothing radius', 0, 5, true), root_smoothing_radius: numericField(fields.root_smoothing_radius, 'Root smoothing radius', 0, 5, true),
    smoothing_start_frame: smoothingStart, smoothing_end_frame: smoothingEnd, blend_frames: numericField(fields.blend_frames, 'Smoothing blend frames', 0, 30, true), joint_corrections };
}
export function capturePreparationMatches(report, source, input) {
  const request = report?.request;
  return /^[a-f0-9]{64}$/.test(report?.handoff_id ?? '') && request?.handoff_id === report.handoff_id && request.job_id === source?.job_id && request.artifact_path === source.artifact_path && request.source_sha256 === source.source_sha256 && request.attempt === input.attempt && key(request.options) === key(input.options);
}
export function captureTarget(fields) {
  const result = {};
  for (const name of ['project_file', 'skeleton_path', 'skeletal_mesh_path', 'destination_path', 'asset_name']) { result[name] = String(fields[name] ?? '').trim(); if (!result[name]) throw new Error('Complete every Unreal target field.'); }
  if (!/^[A-Za-z_][A-Za-z0-9_]{0,63}$/.test(result.asset_name)) throw new Error('Use a new animation name with letters, numbers and underscores, beginning with a letter or underscore.');
  return result;
}
export function sameCaptureTarget(left, right) {
  const project = value => /^[A-Za-z]:[\\/]/.test(value ?? '') ? value.replaceAll('\\', '/').toLowerCase() : value;
  return project(left?.project_file) === project(right?.project_file) && ['skeleton_path', 'skeletal_mesh_path', 'destination_path', 'asset_name'].every(name => left?.[name] === right?.[name]);
}
export class CaptureGate {
  constructor() { this.epoch = 0; this.prepared = null; }
  invalidate() { this.epoch++; this.prepared = null; }
  begin(value) { this.invalidate(); return { epoch: this.epoch, key: key(value) }; }
  current(ticket, value) { return ticket.epoch === this.epoch && ticket.key === key(value); }
  accept(ticket, value, result) { if (!this.current(ticket, value)) return false; this.prepared = { key: ticket.key, result: copy(result) }; return true; }
  get(value) { return this.prepared?.key === key(value) ? copy(this.prepared.result) : null; }
}
