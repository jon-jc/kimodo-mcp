import test from 'node:test';
import assert from 'node:assert/strict';
import { adjustmentFromControls, artifactLabel, beforeFrame, bodyGroup, canEditRig, DraftHistory, freshEdit, jointChain, placementFromControls, previewDataUrl, selectedDownloads, selectedPreview, sourceBounds, sourceKey, upsertAdjustment, validatePreviewData, variantArgs, viewerFilters, visibleJoints } from '../src/rig-data.js';

const job = 'a'.repeat(32), hash = 'b'.repeat(64), variant = 'c'.repeat(64), changedHash = 'd'.repeat(64);
function fixture() {
  return { job_id: job, sample_index: 0, variant_id: null, source_sha256: hash, original_source_sha256: hash,
    fps: 30, frames: 12, skeleton: 'somaskel77', model: 'fixture', editable: { supported: true }, joint_names: ['Hips', 'Spine', 'LeftArm', 'LeftHand', 'RightArm', 'RightHand', 'LeftFoot'],
    joint_parents: [-1, 0, 1, 2, 1, 4, 0], positions: Array.from({ length: 12 }, (_, frame) => Array.from({ length: 7 }, (_, joint) => [frame, joint, 0])),
    source_range: { start_frame: 0, end_frame_exclusive: 12 } };
}
const context = { job, sample: 0, variant: null, expectedSha: hash };
const controls = { joint_name: 'LeftArm', degrees: ['-90', '0', '45'], start_frame: '0', end_frame_exclusive: '12', blend_frames: '3' };

test('source API requests never accept arbitrary identifiers or paths', () => {
  assert.equal(previewDataUrl(job, 2, variant), `/api/jobs/${job}/motion-preview-data?sample_index=2&variant_id=${variant}`);
  assert.deepEqual(variantArgs(null), {}); assert.deepEqual(variantArgs(variant), { variant_id: variant });
  assert.notEqual(sourceKey(job, 0), sourceKey(job, 0, variant));
  for (const [id, sample, version] of [['../escape', 0, null], [job, true, null], [job, 4, null], [job, 0, '../file']]) assert.throws(() => previewDataUrl(id, sample, version));
});

test('rig data rejects foreign candidate/version/hash and malformed hierarchy or numeric payload', () => {
  assert.equal(validatePreviewData(fixture(), context).frames, 12);
  const invalid = [
    data => { data.job_id = 'f'.repeat(32); }, data => { data.sample_index = 1; }, data => { data.variant_id = variant; },
    data => { data.source_sha256 = changedHash; }, data => { data.joint_names[1] = 'Hips'; },
    data => { data.joint_parents[1] = 2; }, data => { data.joint_parents[1] = -1; }, data => { data.joint_parents[1] = 99; },
    data => { data.positions[0][0][0] = Infinity; }, data => { data.positions.pop(); }, data => { data.positions[0][0].pop(); },
    data => { data.source_range.start_frame = 1; }, data => { data.fps = 0; }, data => { data.frames = 2401; },
  ];
  for (const change of invalid) { const data = fixture(); change(data); assert.throws(() => validatePreviewData(data, context)); }
});

test('before/after ghost maps trimmed frame zero to exact original frame and rejects unrelated sources', () => {
  const original = fixture(), after = fixture();
  after.variant_id = variant; after.source_sha256 = changedHash; after.frames = 8; after.positions = after.positions.slice(3, 11); after.source_range = { start_frame: 3, end_frame_exclusive: 11 };
  assert.equal(beforeFrame(after, original, 0), original.positions[3]);
  assert.equal(beforeFrame(after, original, 7), original.positions[10]);
  assert.throws(() => beforeFrame(after, original, 8));
  after.original_source_sha256 = changedHash; assert.throws(() => beforeFrame(after, original, 0));
});

test('body filters and isolation preserve selected chain without including the opposite arm', () => {
  const data = fixture();
  assert.equal(bodyGroup('LeftHandIndex1'), 'left-hand'); assert.equal(bodyGroup('RightUpLeg'), 'right-leg');
  assert.equal(bodyGroup('LeftShin'), 'left-leg'); assert.equal(bodyGroup('RightShin'), 'right-leg');
  assert.deepEqual([...jointChain(data.joint_parents, 2)].sort(), [0, 1, 2, 3]);
  assert.deepEqual(visibleJoints(data, { group: 'left-hand' }), [false, false, false, true, false, false, false]);
  assert.deepEqual(visibleJoints(data, { query: 'aRm' }), [false, false, true, false, true, false, false]);
  assert.equal(visibleJoints(data, { isolate: true, selected: 2 })[4], false);
});

test('joint search narrows navigation while viewport isolation remains an explicit choice', () => {
  const data = fixture(), filters = { query: 'LeftArm', group: 'all', isolate: false, selected: 2 };
  assert.equal(visibleJoints(data, filters).filter(Boolean).length, 1);
  assert.equal(visibleJoints(data, viewerFilters(filters)).filter(Boolean).length, data.joint_names.length);
  assert.deepEqual(visibleJoints(data, viewerFilters({ ...filters, isolate: true })), [true, true, true, true, false, false, false]);
  assert.deepEqual(visibleJoints(data, viewerFilters({ ...filters, group: 'right-arm' })), [false, false, false, false, true, false, false]);
});

test('local degree controls produce precise radians and original-frame windows with smoothstep allowance', () => {
  const result = adjustmentFromControls(controls, fixture(), freshEdit(12));
  assert.deepEqual(result, { joint_name: 'LeftArm', local_xyz_radians: [-Math.PI / 2, 0, Math.PI / 4], start_frame: 0, end_frame_exclusive: 12, blend_frames: 3 });
  for (const change of [{ joint_name: 'Hips' }, { joint_name: 'Invented' }, { degrees: ['91', 0, 0] }, { degrees: ['', 0, 0] }, { start_frame: 8 }, { blend_frames: 7 }, { blend_frames: -1 }, { end_frame_exclusive: 13 }]) assert.throws(() => adjustmentFromControls({ ...controls, ...change }, fixture(), freshEdit(12)));
  const other = fixture(); other.skeleton = 'smplx'; assert.throws(() => adjustmentFromControls(controls, other, freshEdit(12)));
});

test('draft corrections replace a joint instead of duplicating it and cannot exceed sixteen joints', () => {
  const adjustment = adjustmentFromControls(controls, fixture(), freshEdit(12)), original = freshEdit(12);
  const a = upsertAdjustment(original, adjustment), b = upsertAdjustment(a, { ...adjustment, local_xyz_radians: [0, 0.1, 0] });
  assert.equal(original.joint_adjustments.length, 0); assert.equal(b.joint_adjustments.length, 1);
  assert.equal(a.joint_adjustments[0].local_xyz_radians[0], -Math.PI / 2);
  const full = freshEdit(12); full.joint_adjustments = Array.from({ length: 16 }, (_, index) => ({ ...adjustment, joint_name: `Joint${index}` }));
  assert.throws(() => upsertAdjustment(full, adjustment), /16/);
});

test('trim and placement retain original coordinates and reject windows outside the retained clip', () => {
  const edited = upsertAdjustment(freshEdit(12), adjustmentFromControls(controls, fixture(), freshEdit(12)));
  const values = { start_frame: 0, end_frame_exclusive: 12, yaw_degrees: 90, x: 2, z: -3, root_origin: 'first_frame' };
  const result = placementFromControls(values, edited, 12);
  assert.equal(result.yaw_radians, Math.PI / 2); assert.deepEqual(result.translation_xz, [2, -3]);
  assert.equal(result.joint_adjustments[0].start_frame, 0);
  assert.throws(() => placementFromControls({ ...values, start_frame: 1 }, edited, 12), /inside/);
  for (const change of [{ end_frame_exclusive: 1 }, { yaw_degrees: 181 }, { x: 1001 }, { root_origin: 'unknown' }]) assert.throws(() => placementFromControls({ ...values, ...change }, freshEdit(12), 12));
});

test('undo and redo affect independent draft copies and new edits discard only redo history', () => {
  const initial = freshEdit(12), history = new DraftHistory(initial);
  const next = freshEdit(12); next.yaw_radians = 1; history.push(next); next.yaw_radians = 2;
  assert.equal(history.value.yaw_radians, 1); assert.equal(history.undo().yaw_radians, 0);
  assert.equal(history.redo().yaw_radians, 1); history.undo();
  const branch = freshEdit(12); branch.translation_xz = [1, 2]; history.push(branch);
  assert.equal(history.canRedo, false); assert.deepEqual(initial.translation_xz, [0, 0]);
  const external = history.value; external.translation_xz[0] = 20; assert.equal(history.value.translation_xz[0], 1);
});

test('character previews never substitute original or another variant and choose latest numeric attempt', () => {
  const original = { sample_index: 0, attempt: 99, url: '/api/original' };
  const older = { sample_index: 0, variant_id: variant, attempt: 2, url: '/api/older', exports: [{ url: '/api/older-fbx' }] };
  const latest = { ...older, attempt: 10, url: '/api/latest', exports: [{ url: '/api/latest-fbx' }] };
  const data = { previews: [original], variant_previews: [older, latest] };
  assert.equal(selectedPreview(data, 0, variant).url, '/api/latest');
  assert.equal(selectedPreview(data, 0, variant, older).url, '/api/latest');
  assert.equal(selectedPreview(data, 0, variant, original).url, '/api/latest');
  assert.equal(selectedPreview(data, 0, changedHash), null);
  assert.equal(selectedPreview(data, 0).url, '/api/original');
  assert.equal(selectedPreview(data, 1, variant), null);
});

test('downloads are scoped to exact candidate and immutable version', () => {
  const data = { downloads: [{ sample_index: 0, path: 'original' }, { sample_index: 0, variant_id: variant, path: 'corrected' }, { sample_index: 1, variant_id: variant, path: 'other-candidate' }] };
  assert.deepEqual(selectedDownloads(data, 0, variant).map(item => item.path), ['corrected']);
  assert.deepEqual(selectedDownloads(data, 0).map(item => item.path), ['original']);
  data.variants = [{ sample_index: 0, variant_id: variant, artifact: { path: 'variant-native.npz', url: '/api/verified-variant' } }];
  assert.deepEqual(selectedDownloads(data, 0, variant).map(item => item.path), ['corrected', 'variant-native.npz']);
});

test('inspectable SOMA metadata does not override backend edit eligibility', () => {
  const data = fixture(); assert.equal(canEditRig(data), true);
  data.editable = { supported: false, reason: 'Missing native auxiliary channels' };
  assert.equal(canEditRig(data), false);
  assert.throws(() => adjustmentFromControls(controls, data, freshEdit(12)), /Missing native/);
  delete data.editable; assert.equal(canEditRig(data), false);
});

test('comparison camera includes a translated ghost only over retained original frames', () => {
  const original = fixture(), after = fixture();
  after.variant_id = variant; after.source_sha256 = changedHash; after.frames = 4;
  after.source_range = { start_frame: 3, end_frame_exclusive: 7 };
  after.positions = original.positions.slice(3, 7).map(frame => frame.map(point => [point[0] + 100, point[1], point[2]]));
  assert.deepEqual(sourceBounds(after), { min: [103, 0, 0], max: [106, 6, 0] });
  assert.deepEqual(sourceBounds(after, original), { min: [3, 0, 0], max: [106, 6, 0] });
});

test('verified downloads distinguish meshes, character and skeleton animations, rest pose and version', () => {
  assert.equal(artifactLabel({ role: 'character_mesh', format: 'fbx' }), 'Character mesh FBX');
  assert.equal(artifactLabel({ role: 'animation', source_kind: 'character', format: 'fbx' }), 'Character animation FBX');
  assert.equal(artifactLabel({ role: 'animation', source_kind: 'skeleton', format: 'fbx' }), 'Skeleton animation FBX');
  assert.equal(artifactLabel({ format: 'bvh', options: { bvh_standard_tpose: true } }), 'BVH · standard T-pose');
  assert.equal(artifactLabel({ role: 'motion_variant', variant_id: variant }), 'Edited motion NPZ · version cccccccc');
});
