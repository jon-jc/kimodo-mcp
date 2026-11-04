// Browser-side draft helpers. The server remains the authority for request validation.
const clone = value => structuredClone(value);
const equal = (left, right) => JSON.stringify(left) === JSON.stringify(right);
export const NATIVE_DEFAULTS = Object.freeze({
  model: 'kimodo-soma-rp-v1.2', steps: 50, cfg_type: 'separated', cfg_regular: 2,
  cfg_text: 2, cfg_constraint: 2, transition_frames: 5, first_heading_angle: 0,
  root_margin: 0.04, postprocess: true, real_robot_rotations: false,
  formats: ['npz', 'bvh'], bvh_standard_tpose: true,
});
export const NATIVE_KEYS = Object.keys(NATIVE_DEFAULTS);
export function nativeSettings(request = {}) {
  return Object.fromEntries(NATIVE_KEYS.map(key => [key, clone(request[key] ?? NATIVE_DEFAULTS[key])]));
}
export function numericField(value, name, min, max, integer = false) {
  if ((typeof value !== 'number' && typeof value !== 'string') || String(value).trim() === '') throw new Error(`${name} needs a number.`);
  const number = Number(value);
  if (!Number.isFinite(number) || number < min || number > max || (integer && !Number.isInteger(number))) throw new Error(`${name} must be ${integer ? 'a whole number ' : ''}from ${min} to ${max}.`);
  return number;
}
export function composeNativeRequest(base, brief, settings, { constraints = null, clearConstraints = false, preserveInitialHeading = false } = {}) {
  if (!base?.segments || !Array.isArray(base.constraints)) throw new Error('Validate or load a complete generation request first.');
  const result = { ...clone(base), ...clone(settings), seed: brief.seed ?? base.seed, samples: brief.candidates };
  if (preserveInitialHeading) result.first_heading_angle = base.first_heading_angle;
  result.segments = clone(brief.beats ?? [{ text: brief.description, duration: brief.duration_seconds }]);
  const allocations = segments => segments.map(item => Math.floor(item.duration * 30));
  const timingChanged = !equal(allocations(base.segments), allocations(result.segments));
  if (base.constraints.length && (timingChanged || base.model !== result.model) && !clearConstraints) {
    throw new Error('Changing timing or model with saved constraints requires explicitly clearing the saved pose/path constraints first.');
  }
  result.constraints = clearConstraints ? [] : clone(constraints ?? base.constraints);
  return result;
}
export function parseNativeJson(text, kind = 'request') {
  if (typeof text !== 'string' || new TextEncoder().encode(text).length > 950_000) throw new Error('Choose JSON smaller than 950 KB.');
  let parsed;
  try { parsed = JSON.parse(text); } catch { throw new Error('The file is not valid JSON.'); }
  if (kind === 'constraints') {
    const constraints = Array.isArray(parsed) ? parsed : parsed?.constraints;
    if (!Array.isArray(constraints) || constraints.length > 32) throw new Error('A constraints file needs an array of at most 32 constraint sets.');
    return clone(constraints);
  }
  const request = parsed?.request ?? parsed;
  if (!request || typeof request !== 'object' || Array.isArray(request) || !Array.isArray(request.segments)) throw new Error('Choose a complete GenerateRequest or a saved plan containing request.');
  return clone(request);
}
export function parseFrameList(text, { max = 128, label = 'Frames' } = {}) {
  if (typeof text !== 'string' || !text.trim()) throw new Error(`${label} needs comma-separated frame numbers.`);
  const values = text.split(',').map(item => numericField(item, label, 0, 2399, true));
  if (values.length > max || values.some((frame, index) => index && frame <= values[index - 1])) throw new Error(`${label} needs up to ${max} unique frame numbers in increasing order.`);
  return values;
}
export function rootPathConstraint(rows, dense = false) {
  if (!Array.isArray(rows) || !rows.length || rows.length > 128) throw new Error('A root path needs 1 to 128 keyframes.');
  const points = rows.map(row => ({
    frame: numericField(row.frame, 'Path frame', 0, 2399, true),
    x: numericField(row.x, 'Path X', -1000, 1000), z: numericField(row.z, 'Path Z', -1000, 1000),
    heading: String(row.heading ?? '').trim() === '' ? null : numericField(row.heading, 'Heading', -360, 360),
  }));
  if (points.some((point, index) => index && point.frame <= points[index - 1].frame)) throw new Error('Root path frames must be unique and in increasing order.');
  const headings = points.some(point => point.heading !== null);
  if (headings && points.some(point => point.heading === null)) throw new Error('Give every path keyframe a heading, or leave all headings empty.');
  const values = [];
  for (let index = 0; index < points.length; index++) {
    const current = points[index], next = points[index + 1];
    values.push(current);
    if (dense && next) {
      if (points.at(-1).frame - points[0].frame + 1 > 512) throw new Error('A dense path may cover at most 512 constrained frames. Use a shorter interval or sparse keyframes.');
      let angle = next.heading - current.heading;
      angle = ((angle + 180) % 360 + 360) % 360 - 180;
      for (let frame = current.frame + 1; frame < next.frame; frame++) {
        const fraction = (frame - current.frame) / (next.frame - current.frame);
        values.push({ frame, x: current.x + (next.x - current.x) * fraction, z: current.z + (next.z - current.z) * fraction, heading: current.heading + angle * fraction });
      }
    }
  }
  return { type: 'root2d', frame_indices: values.map(value => value.frame), smooth_root_2d: values.map(value => [value.x, value.z]),
    ...(headings ? { global_root_heading: values.map(value => [Math.cos(value.heading * Math.PI / 180), Math.sin(value.heading * Math.PI / 180)]) } : {}) };
}
export function constraintSummary(constraint) {
  const frames = constraint?.frame_indices ?? [];
  return `${constraint?.type ?? 'Unknown'} · ${frames.length} frame(s)${frames.length ? ` · ${frames[0]}–${frames.at(-1)}` : ''}`;
}
export function appendConstraint(existing, constraint) {
  if (!Array.isArray(existing) || existing.length >= 32) throw new Error('Keep at most 32 constraint sets.');
  const next = [...clone(existing), clone(constraint)];
  if (next.reduce((count, item) => count + (item.frame_indices?.length ?? 0), 0) > 512) throw new Error('The complete request may constrain at most 512 frames across all sets.');
  return next;
}
export function bindExtractedConstraint(result, source, targetModel) {
  if (result?.source_job_id !== source.job_id || result?.source_sample !== source.sample_index || result?.model !== targetModel || !/^[a-f0-9]{64}$/.test(result?.source_sha256 ?? '') || !result?.constraint) throw new Error('The extracted pose does not match the selected source and target model. Choose a matching model or another source.');
  return clone(result.constraint);
}
export function exportOptions(format, values) {
  if (format === 'bvh') return { bvh_standard_tpose: Boolean(values.bvh_standard_tpose) };
  if (format === 'amass') return { amass_z_up: Boolean(values.amass_z_up) };
  if (format === 'csv') return { mujoco_rest_zero: Boolean(values.mujoco_rest_zero) };
  throw new Error('Choose a supported motion export format.');
}
export function nativeWorkspaceUrl(status) {
  if (status?.status !== 'ready' || status.owned !== true || !/^[a-f0-9]{32}$/.test(status.owned_run_id ?? '') || status.url !== 'http://127.0.0.1:8769') return null;
  return status.url;
}
