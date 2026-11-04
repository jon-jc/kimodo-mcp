import test from 'node:test';
import assert from 'node:assert/strict';
import { PROP_BONES, matchingCharacterExports, propMotionSource, propSceneRequest } from '../src/asset-data.js';

const id = 'a'.repeat(64), job = 'b'.repeat(32), version = 'c'.repeat(64);
const prop = () => ({ asset_id: id, bone: 'RightHand', translation_m: ['0', '.2', '-.1'], rotation_degrees: ['90', '0', '0'], scale: ['1', '1', '1'] });
const source = () => ({ kind: 'kimodo', job_id: job, sample_index: '0', attempt: '1', variant_id: '' });
const output = change => ({ verified: true, source_kind: 'character', role: 'blender_project', format: 'blend', sha256: id, sample_index: 0, attempt: 1, variant_id: null, ...change });

test('prop offsets preserve exact bone axes and numeric values', () => {
  assert.equal(PROP_BONES.length, 24); assert.equal(new Set(PROP_BONES).size, 24);
  const result = propSceneRequest(' Sword grip ', [prop()]);
  assert.deepEqual(result, { title: 'Sword grip', attachments: [{ asset_id: id, bone: 'RightHand', translation_m: [0, .2, -.1], rotation_degrees: [90, 0, 0], scale: [1, 1, 1] }] });
  assert.equal(propSceneRequest('World prop', [{ ...prop(), bone: '' }]).attachments[0].bone, null);
});

test('empty transforms never become zero and invalid values cannot create scenes', () => {
  for (const [field, values] of [['translation_m', ['', 0, 0]], ['rotation_degrees', [Infinity, 0, 0]], ['scale', [0, 1, 1]], ['scale', [101, 1, 1]], ['rotation_degrees', [361, 0, 0]]]) {
    assert.throws(() => propSceneRequest('Sword', [{ ...prop(), [field]: values }]));
  }
  for (const rows of [[], Array.from({ length: 17 }, prop), [{ ...prop(), bone: 'UnknownBone' }], [{ ...prop(), asset_id: '../model' }]]) assert.throws(() => propSceneRequest('Sword', rows));
  assert.throws(() => propSceneRequest(' ', [prop()]));
});

test('motion sources keep exact candidate version and attempt without coercing empty values', () => {
  assert.deepEqual(propMotionSource({ ...source(), variant_id: version }), { kind: 'kimodo', job_id: job, sample_index: 0, attempt: 1, variant_id: version });
  assert.deepEqual(propMotionSource({ kind: 'capture_character', capture_handoff_id: id }), { kind: 'capture_character', capture_handoff_id: id });
  for (const change of [{ attempt: '' }, { sample_index: '' }, { sample_index: 4 }, { attempt: 100 }, { variant_id: 'partial' }, { job_id: id }]) assert.throws(() => propMotionSource({ ...source(), ...change }));
});

test('character discovery selects the newest verified export only for the selected motion identity', () => {
  const expected = output(), result = { job_id: job, outputs: [output({ attempt: 0 }), expected, output({ attempt: 2, sample_index: 1 }), output({ attempt: 3, variant_id: version }), output({ attempt: 5, verified: false }), output({ attempt: 6, role: 'animation' }), output({ attempt: 7, source_kind: 'skeleton' })] };
  assert.deepEqual(matchingCharacterExports(result, propMotionSource(source())), [expected, output({ attempt: 0 })]);
  assert.deepEqual(matchingCharacterExports({ ...result, job_id: 'd'.repeat(32) }, propMotionSource(source())), []);
  assert.equal(matchingCharacterExports(result, propMotionSource({ ...source(), variant_id: version }))[0].attempt, 3);
});
