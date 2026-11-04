import test from 'node:test';
import assert from 'node:assert/strict';
import { CaptureGate, CaptureSelection, captureDiagnosticLines, captureEvidence, captureFileUrl, captureFrame, capturePreview, checkVideoFile, videoRequest } from '../src/video-data.js';

const values = { video_path: 'D:\\References\\walk.mp4', start_seconds: 2, duration_seconds: 4, fps: 30, detector: 'mp-pose' };

test('capture budgets and detector choices reject unbounded or ambiguous requests', () => {
  assert.equal(videoRequest(values).max_frames, 2400);
  for (const patch of [{ video_path: '' }, { start_seconds: -1 }, { start_seconds: '' }, { fps: '' }, { duration_seconds: 81 }, { duration_seconds: 80, fps: 60 }, { duration_seconds: 0.5, fps: 10 }, { detector: 'shell' }, { max_gap_frames: 16 }, { max_gap_frames: '' }]) {
    assert.throws(() => videoRequest({ ...values, ...patch }));
  }
  assert.equal(videoRequest({ ...values, fps: 29.97 }).fps, 29.97);
  assert.equal(videoRequest({ ...values, duration_seconds: 80, fps: 30.001 }).fps, 30.001);
  assert.equal(videoRequest({ ...values, duration_seconds: 0.1, fps: 60 }).duration_seconds, 0.1);
  assert.equal(videoRequest({ ...values, max_gap_frames: 4 }).max_gap_frames, 4);
});

test('changing the reference or trim invalidates a previously ready capture', () => {
  const gate = new CaptureGate(), request = videoRequest(values), first = gate.begin(request);
  gate.invalidate();
  assert.equal(gate.accept(first, request, { ready: true }), false);
  const second = gate.begin(request);
  assert.equal(gate.accept(second, request, { ready: true, source: { sha256: 'a'.repeat(64) } }), true);
  assert.equal(gate.ready(request), true);
  assert.equal(gate.ready({ ...request, start_seconds: 3 }), false);
  assert.equal(gate.accept(second, { ...request, detector: 'yolo-hrnet' }, { ready: true }), false);
});

test('capture links stay on their job route and uploads are bounded video files', () => {
  const id = 'a'.repeat(32);
  assert.equal(captureFileUrl(id, 'motion.bvh'), `/api/video-mocap/${id}/files/motion.bvh`);
  assert.equal(captureFileUrl(id, '../private.json'), null);
  assert.equal(captureFileUrl('other', 'motion.bvh'), null);
  assert.equal(captureFileUrl(id, 'runtime.json'), null);
  assert.equal(checkVideoFile({ name: 'WALK.MP4', size: 100 }), 'mp4');
  for (const file of [{ name: 'run.exe', size: 100 }, { name: 'x.mp4', size: 0 }, { name: 'x.mp4', size: 513 * 1024 * 1024 }]) assert.throws(() => checkVideoFile(file));
});

test('starting consumes the verified source so a lost response cannot enable an automatic retry', () => {
  const gate = new CaptureGate(), input = videoRequest(values), ticket = gate.begin(input);
  const plan = { ready: true, source: { sha256: 'a'.repeat(64) } };
  gate.accept(ticket, input, plan); plan.source.sha256 = 'b'.repeat(64);
  assert.deepEqual(gate.consume(input), { request: input, expected_source_sha256: 'a'.repeat(64) });
  assert.equal(gate.ready(input), false);
  assert.throws(() => gate.consume(input), /Check this video/);
  const next = gate.begin(input); gate.accept(next, input, { ready: true });
  assert.equal(gate.ready(input), false, 'readiness without source identity cannot dispatch');
});

test('capture reads and delayed submissions cannot replace a newer selection or a closed dialog', () => {
  const state = new CaptureSelection();
  state.select('first'); const submission = state.snapshot(), first = state.beginRead(), second = state.beginRead();
  assert.equal(state.currentRead(first), false);
  assert.equal(state.currentRead(second), true);
  state.select('second');
  assert.equal(state.current(submission), false);
  assert.equal(state.currentRead(second), false);
  const pending = state.beginRead(); state.invalidate();
  assert.equal(state.currentRead(pending), false);
});

function previewFixture() {
  const id = 'b'.repeat(32), sourceSha = 'c'.repeat(64), motionSha = 'd'.repeat(64);
  const data = { job_id: id, source_kind: 'video-mocap', source_sha256: sourceSha, motion_sha256: motionSha,
    skeleton: 'smpl24', model: 'easymocap-smpl', frames: 6, fps: 30,
    joint_names: Array.from({ length: 24 }, (_, index) => `Joint${index}`), joint_parents: [-1, ...Array(23).fill(0)],
    positions: Array.from({ length: 6 }, () => Array.from({ length: 24 }, () => [0, 1, 2])),
    source_time_seconds: Array.from({ length: 6 }, (_, frame) => 2 + frame / 30),
    keypoints2d: Array.from({ length: 6 }, () => Array.from({ length: 25 }, (_, index) => [index, index + 2, 0.8])), estimated_frames: [3] };
  const job = { source: { sha256: sourceSha }, output: { artifacts: [{ name: 'motion.npz', sha256: motionSha }] } };
  return { id, data, job };
}

test('review binds both original video and resulting motion checksums with complete confidence and timing', () => {
  const { id, data, job } = previewFixture();
  assert.equal(capturePreview(data, id, job).source_range.end_frame_exclusive, 6);
  for (const patch of [{ job_id: 'e'.repeat(32) }, { motion_sha256: 'f'.repeat(64) }, { source_sha256: 'f'.repeat(64) },
    { keypoints2d: [] }, { source_time_seconds: [0, 1] }, { estimated_frames: [6] }]) assert.throws(() => capturePreview({ ...data, ...patch }, id, job));
  const corrupt = structuredClone(data); corrupt.keypoints2d[0][0][2] = 1.5;
  assert.throws(() => capturePreview(corrupt, id, job), /confidence/);
});

test('selected-frame evidence labels interpolated frames without inventing 3D joint confidence', () => {
  const { data } = previewFixture(), evidence = captureEvidence(data, 0.1);
  assert.equal(evidence.frame, 3); assert.equal(evidence.estimated, true);
  assert.equal(evidence.sourceTime, 2.1); assert.equal(evidence.usable, 25);
  assert.equal(evidence.points[0].name, 'Nose'); assert.equal(evidence.points[0].confidence, 0.8);
  assert.equal(captureFrame(100, 6, 30), 5); assert.equal(captureFrame(-1, 6, 30), 0);
});

test('failed captures expose bounded actionable original-frame diagnostics', () => {
  const lines = captureDiagnosticLines({ status: 'failed', request: { start_seconds: 2, fps: 10 },
    diagnostics: { frames: 40, detected_frames: 10, problem_frame_count: 30,
      problem_frames: Array.from({ length: 30 }, (_, frame) => ({ frame, status: frame ? 'insufficient' : 'missing' })) } });
  assert.ok(lines.some(line => line.includes('Frame 0 · source 2.00 s · missing')));
  assert.ok(lines.some(line => line.includes('6 more affected')));
  assert.ok(lines.length < 30);
});
