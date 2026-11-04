import test from 'node:test';
import assert from 'node:assert/strict';
import { sourceIdentity, sourcePage, SourceReadGate, retainSelectedJob } from '../src/source-library-data.js';
const source = (kind = 'kimodo') => ({ source_kind: kind, source_id: 'a'.repeat(32), title: 'Take', recorded_status: 'completed', verification: 'not_checked' });
test('equal IDs from different workflows remain distinct and arbitrary routes are rejected', () => {
  assert.notEqual(sourceIdentity(source()), sourceIdentity(source('freemocap')));
  assert.throws(() => sourceIdentity({ ...source(), source_id: '../outside' }));
  assert.throws(() => sourceIdentity(source('untrusted')));
});
test('history validation binds filter and page without upgrading recorded completion', () => {
  const page = { source_kind: 'all', offset: 0, total: 2, metadata_only: true, next_offset: null, sources: [source(), source('freemocap')] };
  assert.equal(sourcePage(page, 'all', 0).sources[0].verification, 'not_checked');
  assert.throws(() => sourcePage(page, 'freemocap', 0));
  assert.throws(() => sourcePage({ ...page, sources: [source(), source()] }, 'all', 0));
  assert.throws(() => sourcePage({ ...page, next_offset: 0 }, 'all', 0));
});
test('late history reads cannot overwrite a changed filter or closed library', () => {
  const gate = new SourceReadGate(), first = gate.begin(), second = gate.begin();
  assert.equal(gate.current(first), false); assert.equal(gate.current(second), true);
  gate.invalidate(); assert.equal(gate.current(second), false);
});
test('opening older generated takes survives periodic recent-history replacement', () => {
  const older = { id: 'old', manifest: { frames: 60 } }, fresh = { id: 'new' };
  assert.deepEqual(retainSelectedJob([fresh], 'old', [older]), [fresh, older]);
  assert.deepEqual(retainSelectedJob([{ id: 'old', status: 'completed' }], 'old', [older]), [{ id: 'old', status: 'completed' }]);
  assert.deepEqual(retainSelectedJob([fresh], null, [older]), [fresh]);
});
