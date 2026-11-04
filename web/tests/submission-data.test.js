import test from 'node:test';
import assert from 'node:assert/strict';
import { selectSubmittedJob, submissionSelection } from '../src/submission-data.js';

const oldJob = 'a'.repeat(32), newJob = 'b'.repeat(32), version = 'c'.repeat(64);
const state = () => ({ selected: oldJob, sample: 0, variant: null, selectionEpoch: 4,
  selectionSignature: 'old render', draft: { title: 'Unsaved sword attack', translation: [1, 2, 3] } });

test('late generation responses retain a newly selected job, candidate or version', async () => {
  for (const change of [{ selected: 'd'.repeat(32) }, { sample: 1 }, { variant: version }]) {
    const current = state(), snapshot = submissionSelection(current);
    let resolve; const pending = new Promise(done => { resolve = done; }).then(result => selectSubmittedJob(current, result, snapshot));
    Object.assign(current, change); current.selectionEpoch++; const expected = structuredClone(current);
    resolve({ job_id: newJob }); assert.equal(await pending, false); assert.deepEqual(current, expected);
  }
});

test('returning to the same candidate still invalidates an earlier pending generation', () => {
  const current = state(), snapshot = submissionSelection(current);
  current.sample = 1; current.selectionEpoch++; current.sample = 0; current.selectionEpoch++;
  const expected = structuredClone(current);
  assert.equal(selectSubmittedJob(current, { job: { id: newJob } }, snapshot), false);
  assert.deepEqual(current, expected);
});

test('an unchanged submission selects each supported response shape without changing the draft', () => {
  for (const result of [{ id: newJob }, { job_id: newJob }, { job: { id: newJob } }]) {
    const current = state(), originalDraft = current.draft, snapshot = submissionSelection(current);
    assert.equal(selectSubmittedJob(current, result, snapshot), true);
    assert.equal(current.selected, newJob); assert.equal(current.sample, 0); assert.equal(current.variant, null);
    assert.equal(current.selectionEpoch, 5); assert.equal(current.selectionSignature, '');
    assert.equal(current.draft, originalDraft);
    assert.equal(selectSubmittedJob(current, result, snapshot), false);
  }
});

test('invalid submission IDs and missing snapshots never clear a current selection', () => {
  const current = state(), expected = structuredClone(current), snapshot = submissionSelection(current);
  for (const result of [null, {}, { job_id: 'not-a-job' }, { job: { id: version } }]) assert.equal(selectSubmittedJob(current, result, snapshot), false);
  assert.equal(selectSubmittedJob(current, { id: newJob }, null), false);
  assert.deepEqual(current, expected);
});
