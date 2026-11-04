import test from 'node:test';
import assert from 'node:assert/strict';
import { appendConstraint, bindExtractedConstraint, composeNativeRequest, constraintSummary, exportOptions, nativeSettings, nativeWorkspaceUrl, numericField, parseFrameList, parseNativeJson, rootPathConstraint } from '../src/native-data.js';

const base = () => ({ model: 'kimodo-soma-rp-v1.2', segments: [{ text: 'Wave.', duration: 4 }], seed: 42, samples: 1,
  steps: 37, cfg_type: 'regular', cfg_regular: 1.8, cfg_text: 3.2, cfg_constraint: 1.2,
  transition_frames: 7, first_heading_angle: 0.123456789, root_margin: 0.07, postprocess: false,
  real_robot_rotations: false, formats: ['bvh', 'npz'], bvh_standard_tpose: false,
  constraints: [{ type: 'root2d', frame_indices: [0], smooth_root_2d: [[0, 1]] }],
});
const brief = () => ({ description: 'Wave.', duration_seconds: 4, quality: 'balanced', candidates: 1, seed: 42 });

test('complete native draft preserves exact custom settings and constraints on a text-only edit', () => {
  const original = base(), snapshot = structuredClone(original);
  const result = composeNativeRequest(original, { ...brief(), description: 'Wave slowly.' }, nativeSettings(original));
  assert.deepEqual(result, { ...original, segments: [{ text: 'Wave slowly.', duration: 4 }] });
  assert.deepEqual(original, snapshot);
  result.constraints[0].smooth_root_2d[0][0] = 2;
  assert.deepEqual(original, snapshot);
});

test('all exact guidance settings including inactive values survive mode switching', () => {
  const original = base();
  for (const cfg_type of ['regular', 'nocfg', 'separated']) {
    const result = composeNativeRequest(original, brief(), { ...nativeSettings(original), cfg_type, steps: 63 });
    assert.equal(result.cfg_type, cfg_type); assert.equal(result.steps, 63);
    assert.equal(result.cfg_regular, 1.8); assert.equal(result.cfg_text, 3.2); assert.equal(result.cfg_constraint, 1.2);
    assert.equal(result.first_heading_angle, original.first_heading_angle);
    assert.deepEqual(result.formats, ['bvh', 'npz']);
  }
});

test('an anchored starting pose retains its compiled heading unless explicitly overridden', () => {
  const original = base(), settings = { ...nativeSettings(original), first_heading_angle: 0 };
  assert.equal(composeNativeRequest(original, brief(), settings, { preserveInitialHeading: true }).first_heading_angle, original.first_heading_angle);
  assert.equal(composeNativeRequest(original, brief(), settings).first_heading_angle, 0);
});

test('timing and model changes require explicit clearing of saved constraints', () => {
  const original = base(), settings = nativeSettings(original);
  assert.throws(() => composeNativeRequest(original, { ...brief(), duration_seconds: 5 }, settings), /explicitly clearing/);
  assert.throws(() => composeNativeRequest(original, brief(), { ...settings, model: 'kimodo-g1-rp' }), /explicitly clearing/);
  const result = composeNativeRequest(original, { ...brief(), duration_seconds: 5 }, settings, { clearConstraints: true });
  assert.deepEqual(result.constraints, []); assert.equal(result.segments[0].duration, 5);
  const sameFrames = composeNativeRequest(original, { ...brief(), duration_seconds: 4.001 }, settings);
  assert.deepEqual(sameFrames.constraints, original.constraints);
});

test('per-action timing changes are guarded even when total duration stays identical', () => {
  const original = base(); original.segments = [{ text: 'Walk.', duration: 2 }, { text: 'Stop.', duration: 2 }];
  assert.throws(() => composeNativeRequest(original, { ...brief(), beats: [{ text: 'Walk.', duration: 3 }, { text: 'Stop.', duration: 1 }] }, nativeSettings(original)), /timing/);
});

test('explicit constraint replacement is immutable and clearing takes precedence', () => {
  const original = base(), staged = [rootPathConstraint([{ frame: 5, x: 1, z: 2 }])];
  const result = composeNativeRequest(original, brief(), nativeSettings(original), { constraints: staged });
  assert.deepEqual(result.constraints, staged); assert.notEqual(result.constraints, staged);
  assert.deepEqual(composeNativeRequest(original, brief(), nativeSettings(original), { constraints: staged, clearConstraints: true }).constraints, []);
});

test('root path keyframes and optional headings produce native world coordinates', () => {
  const result = rootPathConstraint([{ frame: '0', x: '-2', z: '3', heading: '90' }, { frame: '10', x: '2', z: '4', heading: '0' }]);
  assert.deepEqual(result.frame_indices, [0, 10]); assert.deepEqual(result.smooth_root_2d, [[-2, 3], [2, 4]]);
  assert.ok(Math.abs(result.global_root_heading[0][0]) < 1e-12); assert.equal(result.global_root_heading[0][1], 1);
  assert.equal(constraintSummary(result), 'root2d · 2 frame(s) · 0–10');
});

test('dense paths preserve endpoints and only fill internal frames with shortest heading rotation', () => {
  const result = rootPathConstraint([{ frame: 2, x: 0, z: 1, heading: 170 }, { frame: 4, x: 2, z: 3, heading: -170 }], true);
  assert.deepEqual(result.frame_indices, [2, 3, 4]); assert.deepEqual(result.smooth_root_2d, [[0, 1], [1, 2], [2, 3]]);
  assert.ok(Math.abs(result.global_root_heading[1][0] + 1) < 1e-12);
  const withoutHeading = rootPathConstraint([{ frame: 2, x: 0, z: 1 }, { frame: 4, x: 2, z: 3 }], true);
  assert.equal(withoutHeading.global_root_heading, undefined);
});

test('invalid or excessive path keys fail rather than fabricate coordinates', () => {
  const point = { frame: 0, x: 0, z: 0 };
  for (const bad of [[], [{ ...point, frame: '' }], [{ ...point, x: 'NaN' }], [{ ...point, z: 1001 }], [point, point], [{ ...point, heading: 0 }, { ...point, frame: 3 }]]) assert.throws(() => rootPathConstraint(bad));
  assert.throws(() => rootPathConstraint([point, { ...point, frame: 512 }], true), /512/);
  assert.equal(rootPathConstraint([point, { ...point, frame: 511 }], true).frame_indices.length, 512);
});

test('constraint frame mapping is strictly ordered, bounded and rejects empty list entries', () => {
  assert.deepEqual(parseFrameList('0, 15, 119'), [0, 15, 119]);
  for (const value of ['', '0,', '2,1', '0,0', '-1', '2400', '0.1', 'Infinity']) assert.throws(() => parseFrameList(value));
  assert.throws(() => parseFrameList(Array.from({ length: 129 }, (_, i) => i).join(',')));
});

test('constraint extraction must bind the exact source and target model before staging', () => {
  const source = { job_id: 'a'.repeat(32), sample_index: 2 }, model = 'kimodo-soma-rp-v1.2';
  const result = { source_job_id: source.job_id, source_sample: 2, model, source_sha256: 'c'.repeat(64), constraint: base().constraints[0] };
  assert.deepEqual(bindExtractedConstraint(result, source, model), result.constraint);
  for (const modified of [{ source_job_id: 'b'.repeat(32) }, { source_sample: 1 }, { source_sha256: 'invalid' }, { model: 'kimodo-g1-rp' }]) assert.throws(() => bindExtractedConstraint({ ...result, ...modified }, source, model));
});

test('complete and constraint JSON import is bounded, explicit, and leaves validation to server', () => {
  const value = base(); assert.deepEqual(parseNativeJson(JSON.stringify(value)), value);
  assert.deepEqual(parseNativeJson(JSON.stringify({ request: value, plan_id: 'x' })), value);
  assert.deepEqual(parseNativeJson(JSON.stringify(value.constraints), 'constraints'), value.constraints);
  assert.deepEqual(parseNativeJson(JSON.stringify({ constraints: value.constraints }), 'constraints'), value.constraints);
  for (const invalid of ['null', '[]', '{}', '{broken']) assert.throws(() => parseNativeJson(invalid));
  assert.throws(() => parseNativeJson(' '.repeat(950_001)));
  assert.throws(() => parseNativeJson(JSON.stringify(Array(33).fill({})), 'constraints'));
});

test('staging respects total constraints and frame row budget', () => {
  const one = rootPathConstraint([{ frame: 0, x: 0, z: 0 }]);
  assert.equal(appendConstraint([], one).length, 1);
  assert.throws(() => appendConstraint(Array(32).fill(one), one), /32/);
  assert.throws(() => appendConstraint([{ frame_indices: Array(512).fill(0) }], one), /512/);
});

test('export settings send only parameters applicable to the selected format', () => {
  const values = { bvh_standard_tpose: true, amass_z_up: false, mujoco_rest_zero: true };
  assert.deepEqual(exportOptions('bvh', values), { bvh_standard_tpose: true });
  assert.deepEqual(exportOptions('amass', values), { amass_z_up: false });
  assert.deepEqual(exportOptions('csv', values), { mujoco_rest_zero: true });
  assert.throws(() => exportOptions('glb', values));
});

test('numeric controls reject blank, nonfinite, array and fractional integer values', () => {
  assert.equal(numericField('37', 'Steps', 1, 100, true), 37);
  for (const value of ['', ' ', NaN, Infinity, [], null, '1.5', 101]) assert.throws(() => numericField(value, 'Steps', 1, 100, true));
});

test('native workspace link only opens the ready owned fixed loopback service', () => {
  const ready = { status: 'ready', owned: true, owned_run_id: 'a'.repeat(32), url: 'http://127.0.0.1:8769' };
  assert.equal(nativeWorkspaceUrl(ready), ready.url);
  for (const changed of [{ status: 'starting' }, { owned: false }, { owned_run_id: 'invalid' }, { url: 'https://example.com' }, { url: 'http://127.0.0.1:8769/evil' }]) assert.equal(nativeWorkspaceUrl({ ...ready, ...changed }), null);
});
