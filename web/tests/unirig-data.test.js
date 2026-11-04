import test from 'node:test';
import assert from 'node:assert/strict';
import { UNIRIG_DEFAULTS, canCancelUniRig, uniRigArtifactUrl, uniRigPlanMatches, uniRigRegistration, uniRigRequest, uniRigRuntimeMessage, verifiedUniRigOutputs } from '../src/unirig-data.js';
import { PlanGate } from '../src/sequence.js';
const asset = 'a'.repeat(64), id = 'b'.repeat(32), sha = 'c'.repeat(64);
const fields = operation => ({ ...UNIRIG_DEFAULTS, operation });
const row = { asset_id: asset, rig_source: 'self' };
const job = () => ({ id, status: 'completed', output: { job_id: id, artifacts: [{ path: 'outputs/00/merged.glb', sha256: sha, bytes: 200, item_index: 0 }] } });
test('released operations preserve exact applicable controls and reset inactive controls', () => {
  const request = uniRigRequest({ ...fields('pipeline'), seed: '25', num_beams: '7', top_p: '.7', output_format: 'fbx', add_root: true }, [row]);
  assert.equal(request.num_beams, 7); assert.equal(request.seed, 25); assert.equal(request.output_format, 'fbx'); assert.equal(request.add_root, true);
  const preprocess = uniRigRequest({ ...fields('preprocess'), num_beams: '', output_format: 'fbx', add_root: true }, [row]);
  assert.equal(preprocess.num_beams, 15); assert.equal(preprocess.output_format, 'glb'); assert.equal(preprocess.add_root, false);
});
test('rig input requires exact immutable IDs and merge requires an explicit rig', () => {
  assert.throws(() => uniRigRequest(fields('merge'), [row]));
  const request = uniRigRequest(fields('skin'), [{ ...row, rig_source: 'job', source_job_id: id, source_item_index: '2', rig_output: 'skin' }]);
  assert.equal(request.assets[0].source_item_index, 2); assert.equal(request.assets[0].source_job_id, id);
  for (const rows of [[], [row, row], [{ ...row, asset_id: '../invalid' }]]) assert.throws(() => uniRigRequest(fields('pipeline'), rows));
});
test('decoding limits and positive sampling controls are enforced', () => {
  for (const change of [{ num_beams: 31 }, { seed: 1.5 }, { top_p: 0 }, { temperature: 6 }, { faces_target_count: 99 }]) assert.throws(() => uniRigRequest({ ...fields('pipeline'), ...change }, [row]));
});
test('checked plans bind all input values and the asset content checksum', () => {
  const request = uniRigRequest(fields('pipeline'), [row]);
  const plan = { ready: true, request: structuredClone(request), assets: [{ item_index: 0, mesh: { asset_id: asset, sha256: asset } }] };
  assert.equal(uniRigPlanMatches(plan, request), true);
  const changed = structuredClone(plan); changed.assets[0].mesh.sha256 = sha; assert.equal(uniRigPlanMatches(changed, request), false);
  changed.assets[0].mesh.sha256 = asset; changed.request.num_beams = 10; assert.equal(uniRigPlanMatches(changed, request), false);
});
test('editing or closing invalidates a checked rigging plan before any start', () => {
  const gate = new PlanGate(), request = uniRigRequest(fields('pipeline'), [row]), ticket = gate.begin(request);
  assert.equal(gate.accept(ticket, { ...request, seed: 1 }, {}), false);
  const next = gate.begin(request); assert.equal(gate.accept(next, request, { ready: true }), true); gate.invalidate(); assert.equal(gate.current(request), null);
});
test('only verified completed manifest outputs get download addresses', () => {
  assert.equal(verifiedUniRigOutputs(job()).length, 1);
  for (const change of [{ status: 'running' }, { verification_error: 'changed' }, { output: { job_id: 'd'.repeat(32), artifacts: job().output.artifacts } }]) assert.deepEqual(verifiedUniRigOutputs({ ...job(), ...change }), []);
  for (const name of ['../output.glb', 'outputs/../file.glb', 'outputs/a\\b.glb', 'https://example/a.glb']) assert.throws(() => uniRigArtifactUrl(id, name));
});
test('registering results preserves explicit job and file selection', () => {
  assert.deepEqual(uniRigRegistration(job(), 'outputs/00/merged.glb', 'Reviewed rig', 'character'), { job_id: id, name: 'outputs/00/merged.glb', label: 'Reviewed rig', kind: 'character' });
  assert.throws(() => uniRigRegistration(job(), 'outputs/other.glb', 'Rig', 'character'));
  assert.throws(() => uniRigRegistration(job(), 'outputs/00/merged.glb', ' ', 'character'));
});
test('runtime readiness copy does not claim GPU or weight availability and cancellation stays scoped', () => {
  assert.match(uniRigRuntimeMessage({ configuration_ready: true }), /not verified/);
  assert.equal(canCancelUniRig({ id, status: 'running' }), true); assert.equal(canCancelUniRig(job()), false);
});


test('native sampling and decoder token controls preserve explicit off and enforce bounds', () => {
  const request = uniRigRequest({ ...fields('pipeline'), do_sample: false, max_new_tokens: '512' }, [row]);
  assert.equal(request.do_sample, false); assert.equal(request.max_new_tokens, 512);
  assert.equal(request.top_k, 5);
  for (const change of [{ do_sample: 'false' }, { max_new_tokens: '' }, { max_new_tokens: 0 }, { max_new_tokens: 2049 }, { max_new_tokens: 1.5 }]) assert.throws(() => uniRigRequest({ ...fields('pipeline'), ...change }, [row]));
  const preprocess = uniRigRequest({ ...fields('preprocess'), do_sample: false, max_new_tokens: '' }, [row]);
  assert.equal(preprocess.do_sample, true); assert.equal(preprocess.max_new_tokens, 2048);
});
