// Capture jobs use a different skeleton and provenance from Kimodo generation.
// Keep their selectors separate; an SMPL estimate is not a SOMA motion variant.
import { validatePreviewData } from './rig-data.js';

export const VIDEO_LIMIT = 512 * 1024 * 1024;
export const VIDEO_EXTENSIONS = ['mp4', 'mov', 'mkv', 'webm', 'avi', 'm4v'];
export const CAPTURE_TERMINAL = new Set(['completed', 'failed', 'cancelled', 'interrupted']);
export const CAPTURE_ARTIFACTS = { 'reference.mp4': 'Analyzed reference video', 'motion.npz': 'Captured SMPL24 motion', 'motion.bvh': 'SMPL24 animation BVH · meters', 'detections.json': '2D detections and confidence', 'smpl-parameters.json': 'Fitted SMPL parameters', 'timing.json': 'Source timing report', 'diagnostics.json': 'Capture diagnostics' };
const HASH = /^[a-f0-9]{64}$/;
const BODY25 = ['Nose', 'Neck', 'Right shoulder', 'Right elbow', 'Right wrist', 'Left shoulder', 'Left elbow', 'Left wrist', 'Mid hip', 'Right hip', 'Right knee', 'Right ankle', 'Left hip', 'Left knee', 'Left ankle', 'Right eye', 'Left eye', 'Right ear', 'Left ear', 'Left big toe', 'Left small toe', 'Left heel', 'Right big toe', 'Right small toe', 'Right heel'];

export function videoRequest(values) {
  const video_path = String(values.video_path ?? '').trim();
  if (!video_path) throw new Error('Choose a reference video or enter its full local path.');
  if (video_path.includes('://') || video_path.includes('\0')) throw new Error('Enter a local video path, not a web address.');
  if (['start_seconds', 'duration_seconds', 'fps'].some(key => values[key] === null || values[key] === undefined || String(values[key]).trim() === '')) throw new Error('Enter the start time, length and frame rate.');
  const start_seconds = Number(values.start_seconds), duration_seconds = Number(values.duration_seconds), fps = Number(values.fps);
  if (!Number.isFinite(start_seconds) || start_seconds < 0 || start_seconds > 86400) throw new Error('The start time must be between zero and 86,400 seconds.');
  if (!Number.isFinite(duration_seconds) || duration_seconds <= 0 || duration_seconds > 80) throw new Error('Choose a length above zero and up to 80 seconds.');
  const frames = Math.floor(duration_seconds * fps + 1e-8);
  if (!Number.isFinite(fps) || fps < 1 || fps > 60 || frames < 6 || frames > 2400) throw new Error('Choose 1 to 60 frames per second and 6 to 2,400 frames.');
  const detector = values.detector;
  if (!['mp-pose', 'yolo-hrnet'].includes(detector)) throw new Error('Choose an available body detector.');
  const max_gap_frames = values.max_gap_frames === undefined ? 0 : Number(values.max_gap_frames);
  if ((values.max_gap_frames !== undefined && String(values.max_gap_frames).trim() === '') || !Number.isInteger(max_gap_frames) || max_gap_frames < 0 || max_gap_frames > 15) throw new Error('Bridge zero to 15 missing frames; zero keeps strict detection.');
  return { video_path, start_seconds, duration_seconds, fps, max_frames: 2400, detector, max_gap_frames };
}

export function checkVideoFile(file) {
  const extension = String(file?.name ?? '').split('.').pop().toLowerCase();
  if (!VIDEO_EXTENSIONS.includes(extension)) throw new Error('Choose an MP4, MOV, MKV, WebM, AVI or M4V video.');
  if (!Number.isSafeInteger(file.size) || file.size < 1 || file.size > VIDEO_LIMIT) throw new Error('Choose a nonempty video up to 512 MB. For larger files, enter a local path.');
  return extension;
}

export function capturePreview(data, id, job = null) {
  if (data?.job_id !== id || data?.skeleton !== 'smpl24' || data?.source_kind !== 'video-mocap') throw new Error('Capture preview does not match this video job.');
  if (data?.joint_names?.length !== 24) throw new Error('Capture preview must contain all 24 body joints.');
  const expected = job?.output?.artifacts?.find(item => item.name === 'motion.npz')?.sha256;
  if (job && (job.source?.sha256 !== data.source_sha256 || !HASH.test(expected ?? '') || data.motion_sha256 !== expected)) throw new Error('Capture preview does not match the verified video and motion output.');
  if (!Array.isArray(data.source_time_seconds) || data.source_time_seconds.length !== data.frames || data.source_time_seconds.some((time, index, times) => !Number.isFinite(time) || time < 0 || (index && Math.abs(time - times[index - 1] - 1 / data.fps) > 1e-6))) throw new Error('Capture preview has invalid source timing.');
  if (!Array.isArray(data.keypoints2d) || data.keypoints2d.length !== data.frames || data.keypoints2d.some(points => !Array.isArray(points) || points.length !== 25 || points.some(point => !Array.isArray(point) || point.length !== 3 || point.some(value => !Number.isFinite(value)) || point[2] < 0 || point[2] > 1))) throw new Error('Capture preview has invalid detection confidence.');
  if (data.estimated_frames !== undefined && (!Array.isArray(data.estimated_frames) || new Set(data.estimated_frames).size !== data.estimated_frames.length || data.estimated_frames.some(frame => !Number.isInteger(frame) || frame < 0 || frame >= data.frames))) throw new Error('Capture preview has invalid estimated-frame flags.');
  return validatePreviewData({ ...data, sample_index: 0, variant_id: null,
    source_range: { start_frame: 0, end_frame_exclusive: data.frames } }, { job: id, sample: 0 });
}

export function captureFileUrl(id, name) {
  if (!/^[a-f0-9]{32}$/.test(id) || !Object.hasOwn(CAPTURE_ARTIFACTS, name)) return null;
  return `/api/video-mocap/${id}/files/${encodeURIComponent(name)}`;
}

export function captureFrame(time, frames, fps) {
  if (!Number.isFinite(time) || !Number.isInteger(frames) || frames < 1 || !Number.isFinite(fps) || fps <= 0) return 0;
  return Math.max(0, Math.min(frames - 1, Math.round(time * fps)));
}

export function captureEvidence(data, time) {
  const frame = captureFrame(time, data.frames, data.fps), points = data.keypoints2d[frame];
  return { frame, sourceTime: data.source_time_seconds[frame], estimated: (data.estimated_frames ?? []).includes(frame), mean: points.reduce((sum, point) => sum + point[2], 0) / points.length,
    usable: points.filter(point => point[2] > 0.05).length,
    points: points.map((point, index) => ({ name: BODY25[index], x: point[0], y: point[1], confidence: point[2] })) };
}

export function captureDiagnosticLines(job) {
  const diagnostics = job.diagnostics;
  if (!diagnostics) return [];
  const lines = [];
  if (Number.isInteger(diagnostics.frames)) lines.push(`${diagnostics.detected_frames ?? 0} of ${diagnostics.frames} sampled frames have usable detections.`);
  const problems = diagnostics.problem_frames ?? [];
  if (problems.length) {
    lines.push(`${diagnostics.problem_frame_count ?? problems.length} frames need attention.${job.status === 'failed' ? ' Fitting stopped instead of silently copying an earlier pose.' : ''}`);
    for (const problem of problems.slice(0, 24)) {
      const sourceTime = (job.request?.start_seconds ?? 0) + problem.frame / (job.request?.fps || 30);
      lines.push(`Frame ${problem.frame} · source ${sourceTime.toFixed(2)} s · ${String(problem.status).replaceAll('_', ' ')}`);
    }
    if (problems.length > 24) lines.push(`${problems.length - 24} more affected frames are recorded in the capture job details.`);
  }
  if (diagnostics.estimated_frames?.length) lines.push(`${diagnostics.estimated_frames.length} frames use explicitly bridged 2D estimates. Inspect each against the reference.`);
  if (diagnostics.note) lines.push(diagnostics.note);
  return lines;
}

// A late list/read response cannot change a newer selection, closed dialog or read.
export class CaptureSelection {
  constructor() { this.id = ''; this.epoch = 0; this.read = 0; }
  select(id) { this.id = id; this.epoch++; this.read++; return this.snapshot(); }
  invalidate() { this.epoch++; this.read++; }
  snapshot() { return { id: this.id, epoch: this.epoch }; }
  current(ticket) { return ticket.id === this.id && ticket.epoch === this.epoch; }
  beginRead() { return { ...this.snapshot(), read: ++this.read }; }
  currentRead(ticket) { return this.current(ticket) && ticket.read === this.read; }
}

export class CaptureGate {
  constructor() { this.revision = 0; this.plan = null; this.signature = null; }
  invalidate() { this.revision++; this.plan = null; this.signature = null; }
  begin(request) { this.invalidate(); return { revision: this.revision, signature: JSON.stringify(request) }; }
  accept(ticket, request, plan) {
    if (ticket.revision !== this.revision || ticket.signature !== JSON.stringify(request)) return false;
    this.plan = structuredClone(plan); this.signature = ticket.signature; return true;
  }
  ready(request) { return this.plan?.ready === true && HASH.test(this.plan.source?.sha256 ?? '') && this.signature === JSON.stringify(request); }
  consume(request) {
    if (!this.ready(request)) throw new Error('Check this video and its current settings first.');
    const args = { request, expected_source_sha256: this.plan.source.sha256 }; this.invalidate(); return args;
  }
}
