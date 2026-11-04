// Landmark captures retain their native units and missing data. They are not
// editable SOMA poses, and a valid processed point is not a confidence score.
const JOB = /^[a-f0-9]{32}$/;
const HASH = /^[a-f0-9]{64}$/;
const UNITS = new Set(['pixels', 'millimeters', 'calibration_units']);
export const DEFAULT_WINDOW = 240;

export function canReviewFreeMocap(job) {
  return JOB.test(job?.job_id ?? '') && ['process', 'export'].includes(job.operation)
    && job.status === 'completed' && job.pipeline_completion_verified === true;
}

export function landmarkPreviewUrl(job, start = 0, count = DEFAULT_WINDOW) {
  if (!JOB.test(job) || !Number.isInteger(start) || start < 0 || start >= 144000
    || !Number.isInteger(count) || count < 1 || count > 2400) throw new Error('Choose a valid capture and frame window.');
  return `/api/freemocap/${job}/preview?start_frame=${start}&count=${count}`;
}
export function landmarkArtifactUrl(job, path) {
  if (!JOB.test(job) || typeof path !== 'string' || !path || /[:\\\0]/.test(path)
    || path.split('/').some(part => !part || part === '.' || part === '..')) return null;
  return `/api/freemocap/${job}/files/${path.split('/').map(encodeURIComponent).join('/')}`;
}

export function validateLandmarkPreview(data, job, expectedSha = null) {
  if (!data || data.source_kind !== 'freemocap' || data.job_id !== job || !JOB.test(job)) throw new Error('Landmark preview belongs to a different capture.');
  if (!HASH.test(data.motion_sha256 ?? '') || (expectedSha && expectedSha !== data.motion_sha256)) throw new Error('Landmark preview checksum does not match the saved capture.');
  const count = data.detector === 'mediapipe' ? 37 : data.detector === 'rtmpose' ? 27 : 0;
  if (!count || data.skeleton !== `freemocap-${data.detector}-body${count}`) throw new Error('This body landmark layout is not supported.');
  if (!Number.isInteger(data.frames) || data.frames < 1 || data.frames > 2400
    || !Number.isInteger(data.total_frames) || data.total_frames < data.frames || data.total_frames > 144000
    || !Number.isInteger(data.start_frame) || data.start_frame < 0 || data.start_frame + data.frames > data.total_frames) throw new Error('Landmark preview has an invalid frame window.');
  if (data.fps !== null && (!Number.isFinite(data.fps) || data.fps < 1 || data.fps > 240)) throw new Error('Landmark timing is invalid.');
  if (!UNITS.has(data.coordinates?.units) || data.coordinates.up_axis !== 'Z') throw new Error('Landmark coordinate metadata is not supported.');
  const names = data.joint_names, parents = data.joint_parents;
  if (!Array.isArray(names) || names.length !== count || new Set(names).size !== count || names.some(name => typeof name !== 'string' || !name || name.length > 128)) throw new Error('Landmark names are invalid.');
  if (!Array.isArray(parents) || parents.length !== count || parents.filter(p => p === -1).length !== 1
    || parents.some(p => !Number.isInteger(p) || p < -1 || p >= count)) throw new Error('Landmark display hierarchy is invalid.');
  parents.forEach((_, joint) => { const seen = new Set(); for (let i = joint; i !== -1; i = parents[i]) { if (seen.has(i)) throw new Error('Landmark hierarchy contains a cycle.'); seen.add(i); } });
  if (!Array.isArray(data.tracked_mask) || data.tracked_mask.length !== count || data.tracked_mask.some(value => typeof value !== 'boolean')) throw new Error('Landmark types are invalid.');
  for (const name of ['frame_indices', 'source_time_seconds', 'positions', 'valid_mask']) if (!Array.isArray(data[name]) || data[name].length !== data.frames) throw new Error('Landmark preview arrays do not match its frame window.');
  for (let f = 0; f < data.frames; f++) {
    const index = data.start_frame + f;
    if (data.frame_indices[f] !== index) throw new Error('Landmark preview must preserve contiguous original frames.');
    const time = data.source_time_seconds[f];
    if (data.fps === null ? time !== null : !Number.isFinite(time) || Math.abs(time - index / data.fps) > 1e-6) throw new Error('Landmark source timing does not match its original frame.');
    const positions = data.positions[f], mask = data.valid_mask[f];
    if (!Array.isArray(positions) || positions.length !== count || !Array.isArray(mask) || mask.length !== count) throw new Error('Landmark frame dimensions are invalid.');
    positions.forEach((point, j) => {
      if (typeof mask[j] !== 'boolean' || !Array.isArray(point) || point.length !== 3
        || (mask[j] ? point.some(value => !Number.isFinite(value) || Math.abs(value) > 1e12) : point.some(value => value !== null))) throw new Error('Missing landmarks must remain explicitly absent.');
    });
  }
  return data;
}

// Rotate Z-up source coordinates for the Y-up viewer. Keep the scale unchanged.
export function landmarkDisplayPoint(point) { return [point[0], point[2], -point[1]]; }
export function landmarkBounds(data) {
  const min = [Infinity, Infinity, Infinity], max = [-Infinity, -Infinity, -Infinity]; let available = false;
  data.positions.forEach((frame, f) => frame.forEach((point, j) => {
    if (!data.valid_mask[f][j]) return;
    available = true; landmarkDisplayPoint(point).forEach((value, axis) => { min[axis] = Math.min(min[axis], value); max[axis] = Math.max(max[axis], value); });
  }));
  return available ? { min, max } : null;
}
export function adjacentLandmarkWindow(data, direction, count = DEFAULT_WINDOW) {
  if (direction !== -1 && direction !== 1) throw new Error('Choose the previous or next window.');
  return direction < 0 ? Math.max(0, data.start_frame - count) : Math.min(data.total_frames - 1, data.start_frame + data.frames);
}
export function landmarkFrameEvidence(data, localFrame, selected = null) {
  const frame = Math.max(0, Math.min(data.frames - 1, Math.round(localFrame)));
  const valid = data.valid_mask[frame];
  return { frame: data.frame_indices[frame], time: data.source_time_seconds[frame], valid: valid.filter(Boolean).length,
    total: valid.length, units: data.coordinates.units,
    selected: Number.isInteger(selected) && selected >= 0 && selected < valid.length ? {
      name: data.joint_names[selected], tracked: data.tracked_mask[selected], valid: valid[selected], position: data.positions[frame][selected],
    } : null };
}

// A window or quality response must never populate a newer selection or a closed dialog.
export class LandmarkReviewGate {
  constructor() { this.epoch = 0; this.job = null; this.request = 0; }
  select(job) { this.job = job; this.epoch++; this.request++; }
  close() { this.select(null); }
  begin() { return { job: this.job, epoch: this.epoch, request: ++this.request }; }
  current(ticket) { return ticket.job === this.job && ticket.epoch === this.epoch && ticket.request === this.request; }
}
