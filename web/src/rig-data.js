// Source-rig controls share the server's bounded, immutable edit contract.
export const BODY_GROUPS = ['all', 'torso', 'head', 'left-arm', 'right-arm', 'left-hand', 'right-hand', 'left-leg', 'right-leg'];
const HASH = /^[0-9a-f]{64}$/;
const JOB = /^[0-9a-f]{32}$/;
const clone = value => structuredClone(value);

export function sourceKey(job, sample = 0, variant = null) { return `${job}:${sample}:${variant || 'original'}`; }
export function variantArgs(variant) { return variant ? { variant_id: variant } : {}; }
export function selectedPreview(job, sample, variant = null, cached = null) {
  const collection = variant ? job?.variant_previews : job?.previews;
  const saved = Array.isArray(collection) ? collection.filter(item => item.sample_index === sample && (item.variant_id ?? null) === variant).sort((a, b) => (b.attempt ?? 0) - (a.attempt ?? 0))[0] : null;
  if (cached && (cached.sample_index !== sample || (cached.variant_id ?? null) !== variant)) cached = null;
  if (!cached || (saved?.attempt ?? -1) > (cached.attempt ?? -1)) return saved ?? null;
  return { ...cached, exports: cached.exports?.length ? cached.exports : (saved?.attempt === cached.attempt ? saved.exports : []) };
}
export function selectedDownloads(job, sample, variant = null, preview = null) {
  const entries = [...(job?.downloads ?? []).filter(item => item.sample_index === sample && (item.variant_id ?? null) === variant), ...(preview?.exports ?? [])];
  const version = variant ? job?.variants?.find(item => item.variant_id === variant && item.sample_index === sample) : null;
  if (version) entries.push({ ...version.artifact, variant_id: variant, sample_index: sample, label: 'Edited motion NPZ', url: version.artifact?.url ?? version.url });
  if (preview?.url && !entries.some(item => item.url === preview.url)) entries.push({ label: 'Character GLB', url: preview.url });
  return entries.map(entry => ({ ...entry, label: artifactLabel({ ...entry, variant_id: entry.variant_id ?? variant }) }));
}
export function canEditRig(data) { return data?.editable?.supported === true && ['somaskel30', 'somaskel77'].includes(data.skeleton); }

export function artifactLabel(entry) {
  const character = entry.source_kind === 'character', skeleton = entry.source_kind === 'skeleton';
  let label = entry.label;
  if (entry.role === 'character_mesh' || label === 'Character FBX') label = 'Character mesh FBX';
  else if (entry.role === 'animation') label = `${skeleton ? 'Skeleton' : character ? 'Character' : 'Motion'} animation FBX`;
  else if (label === 'Animation FBX') label = 'Character animation FBX';
  else if (entry.role === 'blender_project') label = `${skeleton ? 'Skeleton' : 'Character'} Blender project`;
  else if (entry.role === 'character_preview' || label === 'Animated GLB') label = 'Animated character GLB';
  else if (entry.role === 'motion_variant') label = 'Edited motion NPZ';
  else if (entry.format === 'bvh') label = entry.options ? `BVH · ${entry.options.bvh_standard_tpose ? 'standard T-pose' : 'BONES-SEED rest pose'}` : 'Native BVH';
  else if (entry.format === 'amass' || entry.role === 'amass') label = `AMASS NPZ${entry.options ? entry.options.amass_z_up ? ' · Z up' : ' · Y up' : ''}`;
  else if (entry.format === 'csv') label = 'G1 motion CSV';
  else if (!label && entry.format === 'npz') label = 'Native motion NPZ';
  label ||= entry.path ?? entry.format?.toUpperCase() ?? 'Motion file';
  return `${label}${entry.variant_id ? ` · version ${entry.variant_id.slice(0, 8)}` : ''}`;
}
export function previewDataUrl(job, sample = 0, variant = null) {
  if (!JOB.test(job) || !Number.isInteger(sample) || sample < 0 || sample > 3 || (variant !== null && !HASH.test(variant))) throw new Error('Choose a valid saved motion source.');
  return `/api/jobs/${job}/motion-preview-data?sample_index=${sample}${variant ? `&variant_id=${variant}` : ''}`;
}

export function validatePreviewData(data, { job, sample, variant = null, expectedSha = null } = {}) {
  if (!data || data.job_id !== job || data.sample_index !== sample || (data.variant_id ?? null) !== variant) throw new Error('Rig preview belongs to a different motion selection.');
  if (!HASH.test(data.source_sha256) || !HASH.test(data.original_source_sha256 ?? data.source_sha256) || (expectedSha && data.source_sha256 !== expectedSha)) throw new Error('Rig preview source checksum does not match the selected motion.');
  if (!Number.isInteger(data.frames) || data.frames < 1 || data.frames > 2400 || !Number.isFinite(data.fps) || data.fps < 1 || data.fps > 240) throw new Error('Rig preview has unsupported frame timing.');
  const names = data.joint_names, parents = data.joint_parents;
  if (!Array.isArray(names) || !names.length || names.length > 77 || new Set(names).size !== names.length || names.some(name => typeof name !== 'string' || !name || name.length > 256)) throw new Error('Rig preview has invalid joint names.');
  if (!Array.isArray(parents) || parents.length !== names.length || parents.filter(index => index === -1).length !== 1 || parents.some(index => !Number.isInteger(index) || index < -1 || index >= names.length)) throw new Error('Rig preview has an invalid hierarchy.');
  parents.forEach((_, joint) => { const seen = new Set(); for (let index = joint; index !== -1; index = parents[index]) { if (seen.has(index)) throw new Error('Rig preview hierarchy contains a cycle.'); seen.add(index); } });
  const range = data.source_range;
  if (!range || !Number.isInteger(range.start_frame) || !Number.isInteger(range.end_frame_exclusive) || range.start_frame < 0 || range.end_frame_exclusive > 2400 || range.end_frame_exclusive - range.start_frame !== data.frames) throw new Error('Rig preview has an invalid original-frame range.');
  if (!Array.isArray(data.positions) || data.positions.length !== data.frames || data.positions.some(frame => !Array.isArray(frame) || frame.length !== names.length || frame.some(point => !Array.isArray(point) || point.length !== 3 || point.some(value => !Number.isFinite(value) || Math.abs(value) > 1e6)))) throw new Error('Rig preview contains invalid joint positions.');
  return data;
}

export function bodyGroup(name) {
  const side = /^(Left|L(?=[A-Z]))/.test(name) ? 'left' : /^(Right|R(?=[A-Z]))/.test(name) ? 'right' : null;
  if (/Head|Neck|Jaw|Eye|Face/i.test(name)) return 'head';
  if (side && /Thumb|Index|Middle|Ring|Pinky|Finger|Hand/i.test(name)) return `${side}-hand`;
  if (side && /Shoulder|Collar|Clavicle|Arm|Elbow|Wrist/i.test(name)) return `${side}-arm`;
  if (side && /Leg|Thigh|Shin|Knee|Foot|Toe|Ankle|Hip/i.test(name)) return `${side}-leg`;
  return 'torso';
}

export function jointChain(parents, selected) {
  const chain = new Set();
  if (!Number.isInteger(selected) || selected < 0 || selected >= parents.length) return chain;
  for (let index = selected; index !== -1; index = parents[index]) chain.add(index);
  parents.forEach((_, joint) => { for (let index = joint; index !== -1; index = parents[index]) { if (index === selected) { chain.add(joint); break; } } });
  return chain;
}

export function visibleJoints(data, { query = '', group = 'all', isolate = false, selected = null } = {}) {
  const chain = jointChain(data.joint_parents, selected), term = query.trim().toLowerCase();
  return data.joint_names.map((name, index) => (!term || name.toLowerCase().includes(term)) && (group === 'all' || bodyGroup(name) === group) && (!isolate || chain.has(index)));
}

// Searching is navigation, not a visibility edit. Only explicit body-region
// and chain-isolation controls remove surrounding context from the viewport.
export function viewerFilters(filters) { return { ...filters, query: '' }; }

// Labels remain inside the actual viewer, not the page viewport. Keep the
// selected joint first; suppress neighboring text instead of stacking it.
export function layoutJointLabels(candidates, width, height, selected = null) {
  const padding = 4, gap = 3;
  if (!Number.isFinite(width) || !Number.isFinite(height) || width <= padding * 2 || height <= padding * 2) return [];
  const ordered = [...candidates].sort((a, b) => Number(b.index === selected) - Number(a.index === selected));
  const placed = [];
  for (const item of ordered) {
    if (![item.x, item.y, item.width, item.height].every(Number.isFinite) || item.width <= 0 || item.height <= 0) continue;
    const boxWidth = Math.min(item.width, width - padding * 2), boxHeight = Math.min(item.height, height - padding * 2);
    const top = Math.max(padding, Math.min(item.y - boxHeight / 2, height - padding - boxHeight));
    // Prefer the usual right-hand label, then try its left-hand counterpart.
    for (const proposed of [item.x + 9, item.x - 9 - boxWidth]) {
      const left = Math.max(padding, Math.min(proposed, width - padding - boxWidth));
      if (placed.some(other => left < other.left + other.width + gap && left + boxWidth + gap > other.left
        && top < other.top + other.height + gap && top + boxHeight + gap > other.top)) continue;
      placed.push({ index: item.index, left, top, width: boxWidth, height: boxHeight });
      break;
    }
  }
  return placed;
}

export function beforeFrame(data, original, frame) {
  if (!data || !original || (data.original_source_sha256 ?? data.source_sha256) !== original.source_sha256 || JSON.stringify(data.joint_names) !== JSON.stringify(original.joint_names) || JSON.stringify(data.joint_parents) !== JSON.stringify(original.joint_parents) || data.fps !== original.fps) throw new Error('Before/after views require the exact original source and matching hierarchy.');
  if (!Number.isInteger(frame) || frame < 0 || frame >= data.frames) throw new Error('Preview frame is outside this version.');
  const index = data.source_range.start_frame + frame - original.source_range.start_frame;
  if (index < 0 || index >= original.frames) throw new Error('Before/after source range is outside the original motion.');
  return original.positions[index];
}

export function sourceBounds(data, original = null) {
  const min = [Infinity, Infinity, Infinity], max = [-Infinity, -Infinity, -Infinity];
  const include = frame => { for (const point of frame) for (let axis = 0; axis < 3; axis++) { min[axis] = Math.min(min[axis], point[axis]); max[axis] = Math.max(max[axis], point[axis]); } };
  data.positions.forEach((frame, index) => { include(frame); if (original) include(beforeFrame(data, original, index)); });
  return { min, max };
}

function number(value, min, max, label, integer = false) {
  const result = typeof value === 'number' ? value : typeof value === 'string' && value.trim() ? Number(value) : NaN;
  if (!Number.isFinite(result) || result < min || result > max || (integer && !Number.isInteger(result))) throw new Error(`${label} must be ${integer ? 'a whole number' : 'a number'} from ${min} to ${max}.`);
  return result;
}

export function freshEdit(frames) { return { start_frame: 0, end_frame_exclusive: frames, yaw_radians: 0, translation_xz: [0, 0], root_origin: 'world', joint_adjustments: [] }; }

export function adjustmentFromControls(values, data, edit) {
  const index = data.joint_names.indexOf(values.joint_name);
  if (!canEditRig(data)) throw new Error(data.editable?.reason ?? 'This source can be inspected but is not supported for corrections.');
  if (index < 0 || data.joint_parents[index] === -1) throw new Error('Choose a non-root SOMA joint. Move the root with clip placement controls.');
  const start = number(values.start_frame, edit.start_frame, edit.end_frame_exclusive - 1, 'Start frame', true);
  const end = number(values.end_frame_exclusive, start + 1, edit.end_frame_exclusive, 'End frame (exclusive)', true);
  const blend = number(values.blend_frames, 0, 30, 'Blend frames', true);
  if (end - start < 2 * blend + 1) throw new Error('The joint window needs at least 2 × blend frames + 1 samples. Widen it or reduce the blend.');
  const angles = values.degrees.map((value, axis) => number(value, -90, 90, `${'XYZ'[axis]} rotation`) * Math.PI / 180);
  if (angles.length !== 3) throw new Error('Choose all three local rotation values.');
  return { joint_name: values.joint_name, local_xyz_radians: angles, start_frame: start, end_frame_exclusive: end, blend_frames: blend };
}

export function upsertAdjustment(edit, adjustment) {
  const next = clone(edit), index = next.joint_adjustments.findIndex(item => item.joint_name === adjustment.joint_name);
  if (index < 0) { if (next.joint_adjustments.length >= 16) throw new Error('A version can adjust at most 16 unique joints.'); next.joint_adjustments.push(clone(adjustment)); }
  else next.joint_adjustments[index] = clone(adjustment);
  return next;
}

export function placementFromControls(values, edit, originalFrames) {
  const next = clone(edit);
  next.start_frame = number(values.start_frame, 0, originalFrames - 2, 'Trim start', true);
  next.end_frame_exclusive = number(values.end_frame_exclusive, next.start_frame + 2, originalFrames, 'Trim end (exclusive)', true);
  next.yaw_radians = number(values.yaw_degrees, -180, 180, 'Heading') * Math.PI / 180;
  next.translation_xz = [number(values.x, -1000, 1000, 'X position'), number(values.z, -1000, 1000, 'Z position')];
  if (!['world', 'first_frame'].includes(values.root_origin)) throw new Error('Choose a valid root origin.');
  next.root_origin = values.root_origin;
  if (next.joint_adjustments.some(item => item.start_frame < next.start_frame || item.end_frame_exclusive > next.end_frame_exclusive)) throw new Error('Keep every joint window inside the retained trim, or remove that joint correction first.');
  return next;
}

export class DraftHistory {
  constructor(edit) { this.reset(edit); }
  reset(edit) { this.entries = [clone(edit)]; this.index = 0; this.revision = (this.revision ?? -1) + 1; }
  get value() { return clone(this.entries[this.index]); }
  get canUndo() { return this.index > 0; }
  get canRedo() { return this.index < this.entries.length - 1; }
  push(edit) { if (JSON.stringify(edit) === JSON.stringify(this.entries[this.index])) return; this.entries = this.entries.slice(0, this.index + 1); this.entries.push(clone(edit)); if (this.entries.length > 50) this.entries.shift(); this.index = this.entries.length - 1; this.revision++; }
  undo() { if (this.canUndo) { this.index--; this.revision++; } return this.value; }
  redo() { if (this.canRedo) { this.index++; this.revision++; } return this.value; }
}
