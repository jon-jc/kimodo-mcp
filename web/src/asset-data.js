import { numericField } from './native-data.js';

const SHA = /^[a-f0-9]{64}$/;
export const PROP_BONES = Object.freeze(['Root', 'Hips', 'Spine1', 'Spine2', 'Chest', 'Neck1', 'Neck2', 'Head',
  ...['Left', 'Right'].flatMap(side => ['Shoulder', 'Arm', 'ForeArm', 'Hand', 'Leg', 'Shin', 'Foot', 'ToeBase'].map(part => side + part))]);

export function propSceneRequest(title, rows) {
  if (typeof title !== 'string' || !title.trim() || title.trim().length > 160) throw new Error('Give the scene a title of 1 to 160 characters.');
  if (!Array.isArray(rows) || rows.length < 1 || rows.length > 16) throw new Error('Add 1 to 16 props from the library.');
  const attachments = rows.map((row, index) => {
    if (!SHA.test(row.asset_id ?? '')) throw new Error(`Prop ${index + 1} needs a registered asset.`);
    const bone = row.bone || null;
    if (bone !== null && !PROP_BONES.includes(bone)) throw new Error(`Choose a supported attachment bone for prop ${index + 1}.`);
    const result = { asset_id: row.asset_id, bone };
    for (const [field, label, min, max] of [['translation_m', 'Position', -10000, 10000], ['rotation_degrees', 'Rotation', -360, 360], ['scale', 'Scale', .001, 100]]) {
      if (!Array.isArray(row[field]) || row[field].length !== 3) throw new Error(`${label} needs X, Y and Z values.`);
      result[field] = row[field].map((value, axis) => numericField(value, `Prop ${index + 1} ${label.toLowerCase()} ${'XYZ'[axis]}`, min, max));
    }
    return result;
  });
  return { title: title.trim(), attachments };
}

export function propMotionSource(fields) {
  if (fields.kind === 'capture_character') {
    if (!SHA.test(fields.capture_handoff_id ?? '')) throw new Error('Choose a completed capture character handoff.');
    return { kind: 'capture_character', capture_handoff_id: fields.capture_handoff_id };
  }
  if (fields.kind !== 'kimodo' || !/^[a-f0-9]{32}$/.test(fields.job_id ?? '')) throw new Error('Choose a completed Kimodo job.');
  const result = { kind: 'kimodo', job_id: fields.job_id,
    sample_index: numericField(fields.sample_index, 'Candidate index', 0, 3, true),
    attempt: numericField(fields.attempt, 'Character export attempt', 0, 99, true) };
  if (fields.variant_id) {
    if (!SHA.test(fields.variant_id)) throw new Error('Use the full 64-character motion version ID.');
    result.variant_id = fields.variant_id;
  }
  return result;
}

// The server verifies the stored blend and its producing report. Only exact
// candidate/version matches may supply an export attempt to this draft.
export function matchingCharacterExports(result, source) {
  if (result?.job_id !== source.job_id || !Array.isArray(result.outputs)) return [];
  const attempts = new Set();
  return result.outputs.filter(output => {
    if (output.verified !== true || output.source_kind !== 'character' || output.role !== 'blender_project'
      || output.format !== 'blend' || !SHA.test(output.sha256 ?? '') || output.sample_index !== source.sample_index
      || (output.variant_id ?? null) !== (source.variant_id ?? null)
      || !Number.isInteger(output.attempt) || output.attempt < 0 || output.attempt > 99 || attempts.has(output.attempt)) return false;
    attempts.add(output.attempt); return true;
  }).sort((left, right) => right.attempt - left.attempt);
}
