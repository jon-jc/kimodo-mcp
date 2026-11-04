// One-frame forward kinematics mirrors motion_variants.apply_edit. It never
// replaces the saved clip or invents a playable animation from one pose.
import { Euler, Matrix4, Quaternion, Vector3 } from 'three';

const clone = value => structuredClone(value);
const matrix = rows => new Matrix4().set(...rows[0], 0, ...rows[1], 0, ...rows[2], 0, 0, 0, 0, 1);
const rows = value => { const e = value.elements; return [[e[0], e[4], e[8]], [e[1], e[5], e[9]], [e[2], e[6], e[10]]]; };
const vector = value => new Vector3(...value);
const finiteVector = value => Array.isArray(value) && value.length === 3 && value.every(Number.isFinite);
const finiteMatrix = value => Array.isArray(value) && value.length === 3 && value.every(finiteVector);
export const editKey = value => JSON.stringify(value, function (_key, item) {
  return item && typeof item === 'object' && !Array.isArray(item)
    ? Object.fromEntries(Object.keys(item).sort().map(key => [key, item[key]])) : item;
});

export function jointWeight(adjustment, frame) {
  if (!adjustment || frame < adjustment.start_frame || frame >= adjustment.end_frame_exclusive) return 0;
  if (!adjustment.blend_frames) return 1;
  const t = Math.max(0, Math.min(1, (frame - adjustment.start_frame) / adjustment.blend_frames,
    (adjustment.end_frame_exclusive - 1 - frame) / adjustment.blend_frames));
  return t * t * (3 - 2 * t);
}

export function windowAroundFrame(frame, edit) {
  // Symmetric fades have zero influence at a window edge. A new edit at the
  // first/last retained frame therefore applies across the clip, avoiding an
  // abrupt cutoff halfway through it. Existing authored windows are preserved.
  if (frame === edit.start_frame || frame === edit.end_frame_exclusive - 1) {
    return { start_frame: edit.start_frame, end_frame_exclusive: edit.end_frame_exclusive, blend_frames: 0 };
  }
  const start = Math.max(edit.start_frame, frame - 15);
  const end = Math.min(edit.end_frame_exclusive, frame + 16);
  return { start_frame: start, end_frame_exclusive: end,
    blend_frames: Math.min(3, frame - start, end - 1 - frame) };
}

export function validateDraftPreview(result, { job, sample, sourceSha, frame, edit, names, parents }) {
  const pose = result?.frames_preview?.[0];
  if (result?.job_id !== job || result.sample_index !== sample || result.original_source_sha256 !== sourceSha
      || editKey(result.edit) !== editKey(edit) || result.frames_preview?.length !== 1 || pose?.original_frame !== frame
      || editKey(result.joint_names) !== editKey(names) || editKey(result.joint_parents) !== editKey(parents)) {
    throw new Error('Draft preview does not match the current source, frame and edit.');
  }
  for (const key of ['positions_m', 'original_positions_m']) {
    if (!Array.isArray(pose[key]) || pose[key].length !== names.length || !pose[key].every(finiteVector)) throw new Error('Draft preview has invalid joint positions.');
  }
  for (const key of ['global_rot_mats', 'original_global_rot_mats']) {
    if (!Array.isArray(pose[key]) || pose[key].length !== names.length || !pose[key].every(finiteMatrix)) throw new Error('Draft preview has invalid joint rotations.');
  }
  if (result.parent_local_offsets !== null && (!Array.isArray(result.parent_local_offsets)
      || result.parent_local_offsets.length !== names.length || !result.parent_local_offsets.every(finiteVector))) throw new Error('Draft preview has invalid bone offsets.');
  if (!finiteVector(result.first_retained_root_position)) throw new Error('Draft preview has no valid root placement reference.');
  return result;
}

export function draftPose(bundle, edit) {
  const source = bundle.frames_preview[0], names = bundle.joint_names, parents = bundle.joint_parents;
  const originalGlobals = source.original_global_rot_mats.map(matrix);
  const globals = [], positions = [];
  const adjustments = new Map(edit.joint_adjustments.map(item => [item.joint_name, item]));
  const active = edit.joint_adjustments.some(item => jointWeight(item, source.original_frame) > 0 && item.local_xyz_radians.some(Boolean));
  if (active && !bundle.parent_local_offsets) throw new Error(bundle.joint_editing?.reason ?? 'This source has no static offsets for joint manipulation.');
  const visit = joint => {
    if (globals[joint]) return;
    if (!active) { globals[joint] = originalGlobals[joint].clone(); positions[joint] = vector(source.original_positions_m[joint]); return; }
    const parent = parents[joint];
    if (parent < 0) { globals[joint] = originalGlobals[joint].clone(); positions[joint] = vector(source.original_positions_m[joint]); return; }
    visit(parent);
    const local = originalGlobals[parent].clone().invert().multiply(originalGlobals[joint]);
    const adjustment = adjustments.get(names[joint]), weight = jointWeight(adjustment, source.original_frame);
    if (weight) local.multiply(new Matrix4().makeRotationFromEuler(new Euler(...adjustment.local_xyz_radians.map(value => value * weight), 'XYZ')));
    globals[joint] = globals[parent].clone().multiply(local);
    positions[joint] = vector(bundle.parent_local_offsets[joint]).applyMatrix4(globals[parent]).add(positions[parent]);
  };
  names.forEach((_, index) => visit(index));
  const yaw = new Matrix4().makeRotationY(edit.yaw_radians);
  const pivot = edit.root_origin === 'first_frame' ? vector(bundle.first_retained_root_position).setY(0).applyMatrix4(yaw) : new Vector3();
  const shift = new Vector3(edit.translation_xz[0], 0, edit.translation_xz[1]).sub(pivot);
  return { positions_m: positions.map(point => point.applyMatrix4(yaw).add(shift).toArray()),
    global_rot_mats: globals.map(rotation => rows(yaw.clone().multiply(rotation))), original_frame: source.original_frame };
}

export function worldRotationToOffset(bundle, edit, joint, worldRotation) {
  const parent = bundle.joint_parents[joint];
  if (parent < 0) throw new Error('Use ground-plane movement for the root.');
  const adjustment = edit.joint_adjustments.find(item => item.joint_name === bundle.joint_names[joint]);
  const weight = jointWeight(adjustment, bundle.frames_preview[0].original_frame);
  if (weight < 0.02) throw new Error('This frame has no usable blend influence. Choose Edit around this frame or adjust the window.');
  const pose = draftPose(bundle, edit), source = bundle.frames_preview[0];
  const sourceLocal = matrix(source.original_global_rot_mats[parent]).invert().multiply(matrix(source.original_global_rot_mats[joint]));
  const offset = sourceLocal.invert().multiply(matrix(pose.global_rot_mats[parent]).invert()).multiply(matrix(worldRotation));
  const angles = new Euler().setFromRotationMatrix(offset, 'XYZ');
  return [angles.x, angles.y, angles.z].map(angle => Math.max(-Math.PI / 2, Math.min(Math.PI / 2, angle / weight)));
}

export function manipulateDraft(bundle, initial, { mode, selected, position, quaternion }) {
  const edit = clone(initial), pose = draftPose(bundle, initial), parents = bundle.joint_parents;
  if (mode === 'move' && parents[selected] < 0) {
    const delta = vector(position).sub(vector(pose.positions_m[selected]));
    edit.translation_xz = [Math.max(-1000, Math.min(1000, edit.translation_xz[0] + delta.x)),
      Math.max(-1000, Math.min(1000, edit.translation_xz[1] + delta.z))];
    return edit;
  }
  const target = mode === 'move' ? parents[selected] : selected;
  if (target < 0 || parents[target] < 0) throw new Error('This bone attaches to the root. Use Rotate joint, or move the root across the ground plane.');
  let rotation;
  if (mode === 'move') {
    const pivot = vector(pose.positions_m[target]);
    const from = vector(pose.positions_m[selected]).sub(pivot), to = vector(position).sub(pivot);
    if (from.lengthSq() < 1e-12 || to.lengthSq() < 1e-12) throw new Error('Choose a joint with a nonzero parent bone.');
    rotation = new Matrix4().makeRotationFromQuaternion(new Quaternion().setFromUnitVectors(from.normalize(), to.normalize()))
      .multiply(matrix(pose.global_rot_mats[target]));
  } else rotation = new Matrix4().makeRotationFromQuaternion(new Quaternion(...quaternion));
  const angles = worldRotationToOffset(bundle, edit, target, rows(rotation));
  const adjustment = edit.joint_adjustments.find(item => item.joint_name === bundle.joint_names[target]);
  if (!adjustment) throw new Error('Choose a correction window before manipulating this joint.');
  adjustment.local_xyz_radians = angles;
  return edit;
}

export function pixelJointPick(points, x, y, radius = 14) {
  return points.filter(point => point.visible && Number.isFinite(point.x) && Number.isFinite(point.y)
    && Number.isFinite(point.depth) && point.depth >= -1 && point.depth <= 1)
    .map(point => ({ ...point, distance: Math.hypot(point.x - x, point.y - y) }))
    .filter(point => point.distance <= radius)
    .sort((a, b) => a.distance - b.distance || a.depth - b.depth)[0]?.index ?? null;
}
