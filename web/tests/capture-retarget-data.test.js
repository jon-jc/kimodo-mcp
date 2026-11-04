import test from 'node:test';
import assert from 'node:assert/strict';
import { CaptureGate, captureArtifactUrl, captureBvhOutputs, captureOptions, capturePreparationMatches, captureTarget, sameCaptureTarget, validateCaptureSource } from '../src/capture-retarget-data.js';
const id = 'a'.repeat(32), handoff = 'c'.repeat(64), sha = 'b'.repeat(64);
const source = () => ({ job_id: id, artifact_path: 'output_data/take.bvh', source_sha256: sha, source_frames: 2880, fps: 30, mapping: { Hips: 'pelvis', RightForeArm: 'forearm.R' } });
const fields = () => ({ start_frame: '200', end_frame: '2600', source_scale_to_meters: '0.01', source_rest_pose: 'tpose', root_motion: true, rotation_smoothing_radius: '2', root_smoothing_radius: '0', smoothing_start_frame: '', smoothing_end_frame: '', blend_frames: '3' });

test('retarget entry only accepts verified completed export BVH artifacts', () => {
  const output = { path: 'output_data/take.bvh', sha256: sha }, job = { job_id: id, operation: 'export', status: 'completed', pipeline_completion_verified: true, outputs: [output, { path: 'take.fbx', sha256: sha }] };
  assert.deepEqual(captureBvhOutputs(job), [output]);
  for (const changed of [{ operation: 'process' }, { status: 'failed' }, { pipeline_completion_verified: false }, { job_id: '../bad' }]) assert.deepEqual(captureBvhOutputs({ ...job, ...changed }), []);
});
test('source review binds artifact path and checksum, original frame count and FPS', () => {
  const value = source(), selection = { job_id: id, artifact_path: value.artifact_path, sha256: sha };
  assert.equal(validateCaptureSource(value, selection), value);
  for (const changed of [{ job_id: 'd'.repeat(32) }, { artifact_path: 'another.bvh' }, { source_sha256: 'e'.repeat(64) }, { source_frames: 0 }, { fps: null }, { mapping: {} }]) assert.throws(() => validateCaptureSource({ ...value, ...changed }, selection));
});
test('cleanup requires explicit scale and rest pose and preserves original BVH window indices', () => {
  const options = captureOptions(fields(), source());
  assert.equal(options.start_frame, 200); assert.equal(options.end_frame, 2600); assert.equal(options.smoothing_start_frame, 200); assert.equal(options.smoothing_end_frame, 2600); assert.equal(options.source_scale_to_meters, 0.01);
  for (const change of [{ source_scale_to_meters: '' }, { source_rest_pose: '' }, { end_frame: 2601 }, { start_frame: 2600 }, { rotation_smoothing_radius: 6 }, { root_smoothing_radius: 1.5 }, { smoothing_start_frame: 199 }, { smoothing_end_frame: 2601 }, { smoothing_start_frame: 700, smoothing_end_frame: 600 }]) assert.throws(() => captureOptions({ ...fields(), ...change }, source()));
});
test('local corrections are bounded, uniquely named, and contained in the retained trim', () => {
  const correction = { bone: 'RightForeArm', rotation_degrees: ['0', '0', '-5'], start_frame: '300', end_frame: '400', blend_frames: '4' };
  assert.deepEqual(captureOptions(fields(), source(), [correction]).joint_corrections[0], { bone: 'RightForeArm', rotation_degrees: [0, 0, -5], start_frame: 300, end_frame: 400, blend_frames: 4 });
  for (const change of [{ bone: 'Missing' }, { rotation_degrees: [46, 0, 0] }, { start_frame: 100 }, { end_frame: 3000 }, { blend_frames: 31 }]) assert.throws(() => captureOptions(fields(), source(), [{ ...correction, ...change }]));
  assert.throws(() => captureOptions(fields(), source(), [correction, correction]));
});
test('prepared execution binds exact source, resolved options and attempt regardless of JSON property order', () => {
  const input = { options: captureOptions(fields(), source()), attempt: 2 };
  const report = { handoff_id: handoff, request: { ...source(), handoff_id: handoff, options: Object.fromEntries(Object.entries(input.options).reverse()), attempt: 2 } };
  assert.equal(capturePreparationMatches(report, source(), input), true);
  for (const change of [{ attempt: 3 }, { source_sha256: 'e'.repeat(64) }, { job_id: 'e'.repeat(32) }, { artifact_path: 'changed.bvh' }, { options: { ...input.options, source_scale_to_meters: 1 } }]) assert.equal(capturePreparationMatches({ ...report, request: { ...report.request, ...change } }, source(), input), false);
});
test('capture gates reject late preparation after edits, newer preparation or close', () => {
  const gate = new CaptureGate(), input = { source: id, options: { scale: 0.01 } };
  const first = gate.begin(input); assert.equal(gate.accept(first, { ...input, source: 'other' }, {}), false);
  const second = gate.begin(input); assert.equal(gate.accept(first, input, {}), false);
  assert.equal(gate.accept(second, input, { handoff_id: handoff }), true); assert.equal(gate.get(input).handoff_id, handoff);
  assert.equal(gate.get({ ...input, options: { scale: 1 } }), null); gate.invalidate(); assert.equal(gate.get(input), null); assert.equal(gate.accept(second, input, {}), false);
});
test('artifact download addresses are constrained to verified managed file names', () => {
  assert.equal(captureArtifactUrl(handoff, 'character-preview.glb'), `/api/capture-character/${handoff}/files/character-preview.glb`);
  assert.throws(() => captureArtifactUrl(handoff, '../handoff.json')); assert.throws(() => captureArtifactUrl('invalid', 'animation.fbx'));
});
test('Unreal requires an explicit new name and keeps exact target identity through slash normalization', () => {
  const target = { project_file: 'D:/Project/AnimationLab.uproject', skeleton_path: '/Game/Character/Skeleton', skeletal_mesh_path: '/Game/Character/Mesh', destination_path: '/Game/Animations', asset_name: 'Capture_01' };
  assert.deepEqual(captureTarget(target), target); assert.throws(() => captureTarget({ ...target, asset_name: '' })); assert.throws(() => captureTarget({ ...target, asset_name: '../bad' }));
  assert.equal(sameCaptureTarget(target, { ...target, project_file: 'd:\\Project\\AnimationLab.uproject' }), true);
  assert.equal(sameCaptureTarget(target, { ...target, asset_name: 'Another' }), false); assert.equal(sameCaptureTarget(target, { ...target, project_file: 'D:/Other.uproject' }), false);
});
