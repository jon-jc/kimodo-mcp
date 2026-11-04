import test from 'node:test';
import assert from 'node:assert/strict';
import { FREEMOCAP_STAGES, FreeMocapPlanGate, canCancelFreeMocapJob, freeMocapJobState, freeMocapRequest, freeMocapStageReadiness } from '../src/freemocap-data.js';

test('six explicit stages submit only their relevant controls', () => {
  const common = { video_files: 'left.mp4\nright.mp4', recording_id: 'kimodo-abc', sync_method: 'audio', synchronization_confirmed: true,
    squares_x: 5, squares_y: 3, square_length_mm: 54, detector_type: 'mediapipe', cutoff_hz: 6, calibration_source: 'none', allow_planar: true,
    fbx: true, bvh: true, rest_pose: 'tpose' };
  for (const stage of FREEMOCAP_STAGES) {
    const result = freeMocapRequest({ ...common, operation: stage.id });
    assert.equal(result.operation, stage.id);
    assert.equal('video_files' in result, ['check_sync', 'synchronize', 'import'].includes(stage.id));
    assert.equal('export_options' in result, stage.id === 'export');
    assert.equal('process_options' in result, stage.id === 'process');
  }
});

test('registered reference names reject absolute paths, traversal, duplicate stems and excess cameras', () => {
  for (const video_files of ['D:/private.mp4', '../private.mp4', 'nested\\private.mp4', '/private.mp4', 'same.mp4\nsame.mov', 'clip.exe', Array.from({ length: 9 }, (_, i) => `${i}.mp4`).join('\n')]) {
    assert.throws(() => freeMocapRequest({ operation: 'check_sync', video_files }));
  }
  assert.deepEqual(freeMocapRequest({ operation: 'check_sync', video_files: ' session/left.mp4\n\nsession/right.mov ' }).video_files, ['session/left.mp4', 'session/right.mov']);
});

test('calibration requires measured geometry and processing never invents calibration or editor actions', () => {
  assert.throws(() => freeMocapRequest({ operation: 'calibrate', recording_id: 'take', squares_x: 5, squares_y: 3, square_length_mm: '' }), /Measured/);
  const request = freeMocapRequest({ operation: 'process', recording_id: 'take', detector_type: 'mediapipe', cutoff_hz: 6, calibration_source: 'none', allow_planar: false });
  assert.equal(request.allow_planar, false); assert.equal(request.calibration_file, undefined);
  assert.equal(request.export_options, undefined);
});

test('advanced settings cannot override paths, operation, detector or common cutoff controls', () => {
  const values = { operation: 'process', recording_id: 'take', detector_type: 'rtmpose', cutoff_hz: 6, calibration_source: 'reference', calibration_file: 'calibration/cameras.toml' };
  for (const process_advanced of ['[]', 'null', '{', '{"blenderExePath":"bad"}', '{"cutoff_hz":8}', '{"detector_type":"mediapipe"}']) assert.throws(() => freeMocapRequest({ ...values, process_advanced }));
  const result = freeMocapRequest({ ...values, process_advanced: '{"filter_order":3,"maximum_cameras_to_drop":1}' });
  assert.deepEqual(result.process_options, { filter_order: 3, maximum_cameras_to_drop: 1, detector_type: 'rtmpose', cutoff_hz: 6 });
  assert.equal(result.calibration_file, 'calibration/cameras.toml');
});

test('stage plans bind exact current inputs and late responses cannot re-enable Run', () => {
  const gate = new FreeMocapPlanGate(), first = { operation: 'check_sync', video_files: ['left.mp4'] }, second = { ...first, video_files: ['right.mp4'] };
  const ticket = gate.begin(first); gate.invalidate();
  assert.equal(gate.accept(ticket, first, { operation: first.operation, request: first }), false);
  const current = gate.begin(first);
  assert.equal(gate.accept(current, second, { operation: first.operation, request: first }), false);
  assert.throws(() => gate.accept(current, first, { operation: first.operation, request: second }), /exact settings/);
  assert.equal(gate.accept(current, first, { operation: first.operation, request: first }), true);
  assert.equal(gate.ready(first), true); assert.equal(gate.ready(second), false);
});

test('output readiness and accepted submissions never become false completion claims', () => {
  assert.equal(freeMocapJobState({ status: 'submitted', recording: { outputs: [1, 2, 3] } }), 'Submitted — completion not verified');
  assert.notEqual(freeMocapJobState({ status: 'completed', pipeline_completion_verified: false }), 'Stage completed');
  assert.equal(freeMocapJobState({ status: 'completed', pipeline_completion_verified: true }), 'Stage completed');
  assert.match(freeMocapJobState({ status: 'submission_unknown' }), /unknown/);
});

test('only an owned calibration or processing receipt with an upstream pipeline can be cancelled', () => {
  const job = { job_id: 'a'.repeat(32), operation: 'process', status: 'submitted', upstream_pipeline_id: 'pipeline' };
  assert.equal(canCancelFreeMocapJob(job), true);
  for (const extra of [{ operation: 'synchronize' }, { upstream_pipeline_id: null }, { status: 'completed' }, { job_id: '../other' }, { status: 'cancelled' }]) assert.equal(canCancelFreeMocapJob({ ...job, ...extra }), false);
});

test('synchronization receipt import and deliberate Blender cleanup remain explicit', () => {
  const id = 'b'.repeat(32);
  assert.deepEqual(freeMocapRequest({ operation: 'import', video_files: 'left.mp4\nright.mp4', sync_job_id: id, synchronization_confirmed: false }), { operation: 'import', video_files: ['left.mp4', 'right.mp4'], sync_job_id: id });
  const exported = freeMocapRequest({ operation: 'export', recording_id: 'take', fbx: true, rest_pose: 'apose' });
  assert.deepEqual(exported.export_options, { formats: ['fbx'], rest_pose: 'apose', apply_foot_locking: false, limit_hand_markers_range_of_motion: false });
});

test('status extension gates only processing, calibration and export, while base stages remain usable', () => {
  for (const operation of ['check_sync', 'synchronize', 'import']) {
    assert.equal(freeMocapStageReadiness({ compatible: true }, operation).ready, true);
    assert.equal(freeMocapStageReadiness({ compatible: false }, operation).ready, false);
  }
  for (const operation of ['calibrate', 'process', 'export']) {
    assert.equal(freeMocapStageReadiness({ compatible: true }, operation).ready, false);
    assert.match(freeMocapStageReadiness({ compatible: true, status_extension: { available: false } }, operation).note, /status extension/);
    assert.equal(freeMocapStageReadiness({ compatible: true, status_extension: { available: true } }, operation).ready, true);
  }
});
