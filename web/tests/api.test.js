import test from 'node:test';
import assert from 'node:assert/strict';
import { anchorFrame, buildBrief, description, duration, jobId, revisionChanges, safeLocalUrl, sampleName, setToken, action } from '../src/api.js';

const values = { description: '  A person waves. ', duration: '4', quality: 'balanced', candidates: '2', seed: '' };

test('pose anchors use exact zero-based source frames and clamp the last key', () => {
  assert.equal(anchorFrame(2, 120, 30), 60);
  assert.equal(anchorFrame(4, 120, 30), 119);
  assert.equal(anchorFrame(-1, 120, 30), 0);
  assert.throws(() => anchorFrame(1, 0, 30));
});

test('brief retains deliberate settings without inventing an optional seed', () => {
  assert.deepEqual(buildBrief(values), { description: 'A person waves.', duration_seconds: 4, quality: 'balanced', candidates: 2 });
  assert.equal(buildBrief({ ...values, seed: '0' }).seed, 0);
});

test('revisions send only intentional edits and preserve untouched timing and constraints', () => {
  const previous = buildBrief({ ...values, seed: '42' });
  assert.deepEqual(revisionChanges({ ...previous, description: 'A person waves faster.' }, previous), { description: 'A person waves faster.' });
  assert.deepEqual(revisionChanges(previous, previous), {});
});

test('timed sequences keep explicit beats and omit the single-action duration', () => {
  const beats = [{ text: 'Walk.', duration: '3' }, { text: 'Stop.', duration: '2' }];
  const brief = buildBrief({ ...values, beats });
  assert.equal(brief.duration_seconds, undefined);
  assert.deepEqual(brief.beats, [{ text: 'Walk.', duration: 3 }, { text: 'Stop.', duration: 2 }]);
  assert.deepEqual(revisionChanges(structuredClone(brief), brief), {});
  assert.deepEqual(revisionChanges({ ...brief, description: 'Context', beats: [{ text: 'Walk slowly.', duration: 3 }, brief.beats[1]] }, brief), { beats: [{ text: 'Walk slowly.', duration: 3 }, brief.beats[1]] });
  for (const bad of [[], beats.slice(0, 1), Array(9).fill(beats[0]), [{ text: '', duration: 1 }, beats[0]], [{ text: 'Long', duration: 11 }, beats[0]]]) assert.throws(() => buildBrief({ ...values, beats: bad }));
});

test('invalid artist inputs cannot become an unbounded generation request', () => {
  for (const changes of [{ description: ' ' }, { duration: '11' }, { duration: 'NaN' }, { candidates: '1.5' }, { candidates: '8' }, { seed: '-1' }, { seed: '2.2' }, { quality: 'unbounded' }]) {
    assert.throws(() => buildBrief({ ...values, ...changes }));
  }
});

test('file navigation remains within the same-origin managed API', () => {
  const origin = 'http://127.0.0.1:8768';
  assert.equal(safeLocalUrl('/api/files/job/review.html', origin), `${origin}/api/files/job/review.html`);
  for (const input of ['https://outside.test/api/model.glb', '//outside.test/api/model.glb', 'javascript:alert(1)', '/api/../../private', 'file:///D:/model.glb', '/assets/executable.js', null]) assert.equal(safeLocalUrl(input, origin), null);
});

test('job summaries preserve source request and timing semantics', () => {
  const job = { id: 'abc', request: { segments: [{ text: 'Stand.', duration: 1 }, { text: 'Wave.', duration: 3 }] }, manifest: { frames: 120, fps: 30 } };
  assert.equal(jobId(job), 'abc'); assert.equal(description(job), 'Stand. Wave.'); assert.equal(duration(job), 4); assert.equal(sampleName(3), 'motion_03.npz');
});

test('mutations carry the server-issued token and preserve structured errors', async () => {
  const original = globalThis.fetch;
  try {
    setToken('local-token');
    globalThis.fetch = async (path, options) => {
      assert.equal(path, '/api/actions/plan_animation');
      assert.equal(options.headers['X-Studio-Token'], 'local-token');
      assert.equal(options.credentials, 'same-origin');
      assert.deepEqual(JSON.parse(options.body), { brief: 'x' });
      return { ok: true, json: async () => ({ result: { planned: true } }) };
    };
    assert.deepEqual(await action('plan_animation', { brief: 'x' }), { planned: true });
    globalThis.fetch = async () => ({ ok: false, status: 400, json: async () => ({ error: 'Unsupported rig' }) });
    await assert.rejects(action('plan_animation', {}), /Unsupported rig/);
  } finally { globalThis.fetch = original; }
});
