import test from 'node:test';
import assert from 'node:assert/strict';
import { LandmarkReviewGate, adjacentLandmarkWindow, canReviewFreeMocap, landmarkArtifactUrl, landmarkBounds, landmarkDisplayPoint, landmarkFrameEvidence, landmarkPreviewUrl, validateLandmarkPreview } from '../src/freemocap-review-data.js';

const id = 'a'.repeat(32), sha = 'b'.repeat(64);
function fixture({ detector = 'mediapipe', start = 2600, frames = 3, total = 2800, fps = 30 } = {}) {
  const joints = detector === 'mediapipe' ? 37 : 27;
  return { source_kind: 'freemocap', job_id: id, motion_sha256: sha, detector, skeleton: `freemocap-${detector}-body${joints}`,
    frames, total_frames: total, start_frame: start, fps, frame_indices: Array.from({ length: frames }, (_, i) => start + i),
    source_time_seconds: Array.from({ length: frames }, (_, i) => fps === null ? null : (start + i) / fps),
    joint_names: Array.from({ length: joints }, (_, i) => `point_${i}`), joint_parents: Array.from({ length: joints }, (_, i) => i - 1),
    tracked_mask: Array.from({ length: joints }, (_, i) => i < joints - 4),
    positions: Array.from({ length: frames }, (_, f) => Array.from({ length: joints }, (_, j) => [j, f + 1, j + 2])),
    valid_mask: Array.from({ length: frames }, () => Array(joints).fill(true)), coordinates: { units: 'millimeters', up_axis: 'Z' } };
}

test('both pinned body layouts accept contiguous windows past frame2400 without relabeling source time', () => {
  for (const detector of ['mediapipe', 'rtmpose']) {
    const data = fixture({ detector });
    assert.equal(validateLandmarkPreview(data, id, sha), data);
    assert.equal(landmarkFrameEvidence(data, 2).frame, 2602);
    assert.equal(landmarkFrameEvidence(data, 2).time, 2602 / 30);
  }
});
test('wrong source, checksum, hierarchy, timing and decimated windows are refused', () => {
  const changes = [data => data.job_id = 'c'.repeat(32), data => data.motion_sha256 = 'd'.repeat(64),
    data => data.joint_parents[1] = 1, data => data.frame_indices[1]++, data => data.source_time_seconds[0] = 0,
    data => data.total_frames = 144001, data => data.coordinates.units = 'meters'];
  for (const change of changes) { const data = fixture(); change(data); assert.throws(() => validateLandmarkPreview(data, id, sha)); }
});
test('missing points remain null and absent from bounds, not visible origin placeholders', () => {
  const data = fixture({ start: 0, frames: 1, total: 1 });
  data.positions[0][0] = [null, null, null]; data.valid_mask[0][0] = false;
  validateLandmarkPreview(data, id);
  assert.equal(landmarkFrameEvidence(data, 0, 0).selected.valid, false);
  assert.equal(landmarkFrameEvidence(data, 0).valid, 36);
  assert.deepEqual(landmarkBounds(data).min, [1, 3, -1]);
  data.positions[0][0] = [0, 0, 0]; assert.throws(() => validateLandmarkPreview(data, id), /absent/);
});
test('a fully missing window has no fabricated bounds or numerical position', () => {
  const data = fixture(); data.positions.forEach((frame, f) => frame.forEach((_, j) => { data.positions[f][j] = [null, null, null]; data.valid_mask[f][j] = false; }));
  validateLandmarkPreview(data, id);
  assert.equal(landmarkBounds(data), null); assert.equal(landmarkFrameEvidence(data, 0).valid, 0);
});
test('legacy unknown frame rates allow frame inspection without invented seconds', () => {
  const data = fixture({ fps: null }); validateLandmarkPreview(data, id);
  assert.equal(landmarkFrameEvidence(data, 1).time, null);
  data.source_time_seconds[0] = 0; assert.throws(() => validateLandmarkPreview(data, id), /timing/);
});
test('display rotation preserves native scale and handedness', () => {
  assert.deepEqual(landmarkDisplayPoint([1000, 2000, 3000]), [1000, 3000, -2000]);
  assert.equal(Math.hypot(...landmarkDisplayPoint([2, 3, 4])), Math.hypot(2, 3, 4));
});
test('window navigation covers long clips and clamps the previous start to zero', () => {
  const data = fixture({ start: 240, frames: 240, total: 144000 });
  assert.equal(adjacentLandmarkWindow(data, 1), 480); assert.equal(adjacentLandmarkWindow(data, -1), 0);
  data.start_frame = 30; assert.equal(adjacentLandmarkWindow(data, -1), 0);
  assert.throws(() => adjacentLandmarkWindow(data, 0));
});
test('preview and artifact routes accept managed identities and reject path traversal', () => {
  assert.match(landmarkPreviewUrl(id, 143999, 1), /start_frame=143999&count=1$/);
  for (const args of [[id, -1], [id, 144000], [id, 0, 2401], ['../bad', 0]]) assert.throws(() => landmarkPreviewUrl(...args));
  assert.equal(landmarkArtifactUrl(id, 'output_data/body motion.npy'), `/api/freemocap/${id}/files/output_data/body%20motion.npy`);
  for (const path of ['../other.npy', '/outside.npy', 'D:/other.npy', 'output_data\\other.npy', 'output_data/./other.npy']) assert.equal(landmarkArtifactUrl(id, path), null);
});
test('only verified successful process or export receipts can open body review', () => {
  const job = { job_id: id, operation: 'process', status: 'completed', pipeline_completion_verified: true };
  assert.equal(canReviewFreeMocap(job), true);
  assert.equal(canReviewFreeMocap({ ...job, operation: 'export' }), true);
  for (const change of [{ operation: 'calibrate' }, { status: 'failed' }, { pipeline_completion_verified: false }, { job_id: '../bad' }]) assert.equal(canReviewFreeMocap({ ...job, ...change }), false);
});
test('late window responses cannot replace a newer window, receipt, or closed view', () => {
  const gate = new LandmarkReviewGate(); gate.select(id); const first = gate.begin(), second = gate.begin();
  assert.equal(gate.current(first), false); assert.equal(gate.current(second), true);
  gate.select('c'.repeat(32)); assert.equal(gate.current(second), false);
  const other = gate.begin(); gate.close(); assert.equal(gate.current(other), false);
});
