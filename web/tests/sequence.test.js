import test from 'node:test';
import assert from 'node:assert/strict';
import { duplicateBeat, firstBeatIssue, friendlyPlanningError, makeBeats, moveBeat, needsFreshSeed, PlanGate, removeBeat, requestBeats, savedPlanMetadata, savedPlanRoute, savedSequenceNotes, SequenceHistory, sequenceTimeline, sharedFrameTime, STARTER_SEQUENCES, WORKFLOW_STEPS, workflowKey, workflowState } from '../src/sequence.js';
import { SequenceComposer } from '../src/sequence-composer.js';

const identities = () => { let id = 0; return () => `beat-${++id}`; };
const beats = () => makeBeats(STARTER_SEQUENCES.greeting.beats, identities());
const saved = { plan_id: 'a'.repeat(64), title: 'Greeting', description: 'Footwork study', request: { model: 'Kimodo-SOMA-RP-v1', steps: 37, seed: 42, constraints: [{ frame: 0 }], smooth: false, formats: ['npz', 'bvh'] } };
const baseline = { model: saved.request.model, description: 'A person waves.', duration_seconds: 4, quality: 'balanced', candidates: 1, seed: 42 };

test('starter sequences are editable valid prompts, without sharing mutable identities', () => {
  for (const starter of Object.values(STARTER_SEQUENCES)) {
    const draft = makeBeats(starter.beats, identities());
    assert.equal(draft.length, 3);
    assert.equal(requestBeats(draft).length, 3);
    draft[0].text = 'Changed';
    assert.notEqual(starter.beats[0].text, 'Changed');
  }
  assert.throws(() => makeBeats([{ text: 'Only one' }]));
  assert.throws(() => makeBeats(STARTER_SEQUENCES.greeting.beats, () => 'duplicate'));
});

test('reorder, duplicate and remove preserve stable identities and do not alter the input', () => {
  const original = beats(), snapshot = structuredClone(original);
  const moved = moveBeat(original, 'beat-2', -1);
  assert.deepEqual(moved.map(item => item.id), ['beat-2', 'beat-1', 'beat-3']);
  assert.deepEqual(moveBeat(moved, 'beat-2', -1), moved);
  const duplicated = duplicateBeat(moved, 'beat-1', () => 'copy');
  assert.deepEqual(duplicated.map(item => item.id), ['beat-2', 'beat-1', 'copy', 'beat-3']);
  assert.equal(duplicated[1].text, duplicated[2].text);
  duplicated[2].text = 'Independent copy';
  assert.notEqual(duplicated[1].text, duplicated[2].text);
  assert.deepEqual(removeBeat(duplicated, 'copy'), moved);
  assert.deepEqual(original, snapshot);
  assert.throws(() => duplicateBeat(original, 'beat-1', () => 'beat-2'));
  assert.throws(() => duplicateBeat(Array(8).fill(original[0]), 'beat-1'));
  assert.throws(() => removeBeat(original.slice(0, 2), 'beat-1'));
  assert.throws(() => moveBeat(original, 'missing', 1));
});

test('request actions remove UI identities and reject incomplete collapsed rows', () => {
  assert.deepEqual(requestBeats([{ id: 'a', text: '  Walk.  ', duration: '1.23' }]), [{ text: 'Walk.', duration: 1.23 }]);
  for (const duration of ['', ' ', 'NaN', 'Infinity', -1, 11]) assert.throws(() => requestBeats([{ text: 'Walk.', duration }]));
  assert.throws(() => requestBeats([{ text: 'Walk.', duration: 2 }, { text: '', duration: 2 }]), /Action 2/);
});

test('validated timeline uses backend allocations and treats transition tails as revisited history', () => {
  const draft = makeBeats([{ text: 'Walk.', duration: 4.1 }, { text: 'Stop.', duration: 1 }], identities());
  const plan = { fps: 30, segment_frames: [122, 30], total_frames: 152, segment_timeline: [
    { start_frame: 0, end_frame_exclusive: 122, tail_reprocessed_frames: 5 },
    { start_frame: 122, end_frame_exclusive: 152, incoming_transition_start_frame: 117, incoming_transition_end_frame_exclusive: 122, tail_reprocessed_frames: 0 },
  ] };
  assert.equal(sequenceTimeline(draft).status, 'estimate');
  const result = sequenceTimeline(draft, plan);
  assert.equal(result.status, 'validated');
  assert.equal(result.frames, 152);
  assert.equal(result.seconds, 152 / 30);
  assert.equal(result.segments[0].requestedSeconds, 4.1);
  assert.equal(result.segments[0].frames, 122);
  assert.equal(result.segments[0].tailFrames, 5);
  assert.equal(result.segments[1].incomingStart, 117);
  assert.equal(result.segments[1].incomingEnd, 122);
  assert.equal(result.segments[1].start, 122);
  assert.throws(() => sequenceTimeline([...draft, draft[0]], plan), /match/);
  assert.throws(() => sequenceTimeline(draft, { ...plan, total_frames: 157 }), /total/);
  assert.throws(() => sequenceTimeline(draft, { ...plan, segment_timeline: [plan.segment_timeline[0], { start_frame: 127, end_frame_exclusive: 157 }] }), /contiguous/);
  assert.equal(sequenceTimeline([{ id: 'a', text: '', duration: '' }]).status, 'invalid');
});

test('late preview responses cannot restore stale validation or override newer validation', () => {
  const gate = new PlanGate(), input = { brief: baseline, saved_plan_id: saved.plan_id, start_from: null };
  const old = gate.begin(input), latest = gate.begin(input);
  assert.equal(gate.accept(old, input, { request: { seed: 1 } }), false);
  assert.equal(gate.accept(latest, input, { request: saved.request }), true);
  const returned = gate.current(input); returned.request.steps = 99;
  assert.equal(gate.current(input).request.steps, 37);
  gate.invalidate();
  assert.equal(gate.current(input), null);
  assert.equal(gate.accept(latest, input, {}), false);
});

test('validation binds the exact saved version and selected pose, even for identical visible text', () => {
  const gate = new PlanGate();
  const input = { brief: baseline, saved_plan_id: saved.plan_id, start_from: { job_id: 'job', sample_index: 0, variant_id: 'c'.repeat(64), frame: 17 } };
  const ticket = gate.begin(input);
  assert.equal(gate.accept(ticket, { ...input, saved_plan_id: 'b'.repeat(64) }, {}), false);
  assert.equal(gate.accept(ticket, { ...input, start_from: { ...input.start_from, frame: 18 } }, {}), false);
  assert.equal(gate.accept(ticket, { ...input, start_from: { ...input.start_from, variant_id: null } }, {}), false);
  assert.equal(gate.accept(ticket, input, {}), true);
});

test('a pending load preserves newer incomplete edits, titles, library selection and new-brief actions', () => {
  const gate = new PlanGate(), snapshot = { revision: 12, title: 'Draft', selected: saved.plan_id, start_from: null };
  const ticket = gate.begin(snapshot);
  for (const changes of [{ revision: 13 }, { title: 'New title' }, { selected: 'b'.repeat(64) }, { start_from: { frame: 3 } }]) assert.equal(gate.accept(ticket, { ...snapshot, ...changes }, saved), false);
  assert.equal(gate.accept(ticket, snapshot, saved), true);
});

test('unchanged saved routing uses the exact version and leaves advanced request values intact', () => {
  const snapshot = structuredClone(saved);
  assert.deepEqual(savedPlanRoute(saved, { ...baseline }, baseline), { kind: 'exact', plan_id: saved.plan_id });
  assert.deepEqual(savedPlanRoute(saved, { ...baseline, description: 'A person bows.' }, baseline), { kind: 'revision', plan_id: saved.plan_id, revision: { description: 'A person bows.' } });
  assert.deepEqual(savedPlanRoute(saved, { ...baseline, quality: 'detailed' }, baseline).revision, { quality: 'detailed' });
  assert.deepEqual(saved, snapshot);
  assert.throws(() => savedPlanRoute(saved, { ...baseline, model: 'different-model' }, baseline), /cannot change its model/);
});

test('sequence text edits preserve constraints unless clearing is explicitly selected', () => {
  const previous = { ...baseline, beats: requestBeats(beats()) }; delete previous.duration_seconds;
  const changed = { ...previous, description: 'Edited context', beats: previous.beats.map((item, index) => ({ ...item, text: index ? item.text : 'Walk slowly.' })) };
  assert.deepEqual(savedPlanRoute(saved, changed, previous).revision, { beats: changed.beats });
  assert.deepEqual(savedPlanRoute(saved, changed, previous, true).revision, { beats: changed.beats, clear_constraints: true });
  assert.deepEqual(savedPlanRoute(saved, previous, previous, true).revision, { clear_constraints: true });
});

test('clearing a populated seed invalidates exact routing, while originally unseeded plans stay exact', () => {
  const unseeded = { ...baseline }; delete unseeded.seed;
  assert.throws(() => savedPlanRoute(saved, unseeded, baseline), /fresh seed/);
  assert.equal(needsFreshSeed(unseeded, saved, baseline), true);
  assert.equal(needsFreshSeed(unseeded, saved, unseeded), false);
  assert.equal(needsFreshSeed(unseeded, null, null), true);
  assert.equal(needsFreshSeed({ ...unseeded, seed: 0 }, saved, baseline), false);
  assert.equal(savedPlanRoute(saved, unseeded, unseeded).kind, 'exact');
  assert.deepEqual(savedPlanRoute(saved, { ...unseeded, seed: 17 }, baseline).revision, { seed: 17 });
});

test('single-action saves preserve separate agent notes when prompt or seed changes', () => {
  const result = savedPlanMetadata(saved, { title: saved.title, sequence: false, notes: 'A person runs.' }, 'A person waves.');
  assert.deepEqual(result, { title: saved.title, description: 'Footwork study', changed: false });
});

test('empty sequence notes and padded titles preserve exact stored metadata until deliberately edited', () => {
  const record = { ...saved, title: ' Greeting ', description: '' };
  assert.deepEqual(savedPlanMetadata(record, { title: record.title, sequence: true, notes: record.title }, 'Greeting'), { title: record.title, description: '', changed: false });
  assert.deepEqual(savedPlanMetadata(record, { title: record.title, sequence: true, notes: 'Different study' }, 'Greeting'), { title: record.title, description: 'Different study', changed: true });
  assert.deepEqual(savedPlanMetadata(record, { title: 'New title', sequence: true, notes: record.title }, 'Greeting'), { title: 'New title', description: '', changed: true });
  assert.deepEqual(savedPlanMetadata(null, { title: ' New plan ', sequence: true, notes: ' Notes ' }), { title: 'New plan', description: 'Notes', changed: false });
});

test('whitespace-only stored notes have a compilable visible fallback without altering saved metadata', () => {
  for (const title of ['Greeting', ' ']) {
    const record = { ...saved, title, description: '   ' }, notes = savedSequenceNotes(record);
    assert.ok(notes.trim().length);
    assert.equal(savedPlanMetadata(record, { title, sequence: true, notes }, notes.trim()).description, '   ');
  }
  assert.equal(savedSequenceNotes(saved), 'Footwork study');
});

test('constraint timing errors give a specific actionable instruction while other errors remain intact', () => {
  const message = friendlyPlanningError('Error executing tool revise_animation_plan: Changing timing with existing constraints requires clear_constraints=true or a new explicit request.');
  assert.match(message, /original action durations/);
  assert.match(message, /Clear saved pose\/path constraints/);
  assert.doesNotMatch(message, /clear_constraints=true|executing tool/);
  assert.equal(friendlyPlanningError('Source checksum changed.'), 'Source checksum changed.');
});

test('workflow navigation preserves draft, version, source pose and saved request across every step', () => {
  const initial = { workflow: 'review', focusRig: true, selected: 'job', sample: 2, variant: 'b'.repeat(64), time: 1.25, savedPlan: saved, draft: { beats: beats(), seed: 42 }, viewerMode: 'source', anchor: { frame: 38 } };
  const snapshot = structuredClone(initial);
  let current = initial;
  for (const step of ['export', 'create', 'review', 'create']) {
    current = workflowState(current, step);
    assert.equal(current.workflow, step);
    assert.equal(current.selected, initial.selected);
    assert.equal(current.sample, initial.sample);
    assert.equal(current.variant, initial.variant);
    assert.equal(current.time, initial.time);
    assert.equal(current.savedPlan, initial.savedPlan);
    assert.equal(current.draft, initial.draft);
    assert.equal(current.anchor, initial.anchor);
    assert.equal(current.viewerMode, 'source');
    assert.equal(current.focusRig, false);
    assert.equal(current.inspectTab, step === 'export' ? 'export' : 'details');
  }
  assert.deepEqual(initial, snapshot);
  assert.throws(() => workflowState(initial, 'unknown'));
  for (const step of WORKFLOW_STEPS) assert.equal(workflowState({ selected: null }, step).workflow, step);
});

test('workflow keyboard navigation wraps and exposes first and last steps without activating unrelated keys', () => {
  assert.equal(workflowKey('create', 'ArrowLeft'), 'export');
  assert.equal(workflowKey('export', 'ArrowRight'), 'create');
  assert.equal(workflowKey('review', 'ArrowRight'), 'export');
  assert.equal(workflowKey('export', 'Home'), 'create');
  assert.equal(workflowKey('create', 'End'), 'export');
  assert.equal(workflowKey('review', 'Tab'), null);
});

test('a slower saved-library page cannot replace a newer page or saved-version refresh', () => {
  const gate = new PlanGate(), page0 = { offset: 0, preferredId: null }, page1 = { offset: 20, preferredId: null };
  const first = gate.begin(page0), second = gate.begin(page1);
  assert.equal(gate.accept(second, page1, { plans: ['newer-page'] }), true);
  assert.equal(gate.accept(first, page0, { plans: ['late-first-page'] }), false);
  assert.deepEqual(gate.current(page1), { plans: ['newer-page'] });
});

test('representation changes keep the same source frame instead of an independent viewer clock', () => {
  const timing = { fps: 30, frames: 120 }, end = 119 / 30;
  assert.equal(sharedFrameTime(1, timing, end), 1);
  assert.equal(sharedFrameTime(2.01, timing, end), 2);
  assert.equal(sharedFrameTime(0.002, timing, end), 0);
  assert.equal(sharedFrameTime(4, timing, end), end);
  assert.equal(sharedFrameTime(-1, timing, end), 0);
  assert.equal(sharedFrameTime(2, timing, 1.5), 1.5);
  for (const invalid of [{ frames: 0, fps: 30 }, { frames: 120, fps: 0 }, null]) assert.equal(sharedFrameTime(1, invalid, end), null);
});

test('sequence history groups typing in one field and separates later visits or structural edits', () => {
  const initial = { beats: beats(), selected: 'beat-1', field: 'text' }, history = new SequenceHistory(initial);
  const first = structuredClone(initial); first.beats[0].text = 'W'; history.apply(first, 'visit-1');
  const typed = structuredClone(first); typed.beats[0].text = 'Walk slowly.'; history.apply(typed, 'visit-1');
  assert.equal(history.past.length, 1);
  history.commit();
  const nextVisit = structuredClone(typed); nextVisit.beats[0].text = 'Walk briskly.'; history.apply(nextVisit, 'visit-2');
  assert.equal(history.past.length, 2);
  const reordered = { ...nextVisit, beats: moveBeat(nextVisit.beats, 'beat-1', 1) }; history.apply(reordered);
  assert.deepEqual(history.undo().beats, nextVisit.beats);
  assert.equal(history.undo().beats[0].text, 'Walk slowly.');
  assert.deepEqual(history.undo(), initial);
  assert.equal(history.undo(), null);
  assert.equal(history.redo().beats[0].text, 'Walk slowly.');
  assert.equal(history.redo().beats[0].text, 'Walk briskly.');
  assert.deepEqual(history.redo().beats, reordered.beats);
  assert.deepEqual(initial.beats, beats());
});

test('history restores stable identities, active field and deleted actions without sharing snapshots', () => {
  const original = { beats: beats(), selected: 'beat-2', field: 'duration' }, history = new SequenceHistory(original);
  const changed = structuredClone(original); changed.beats[1].duration = '4.125'; history.apply(changed, 1); history.commit();
  history.focus('beat-2', 'duration');
  const removed = { beats: removeBeat(changed.beats, 'beat-2'), selected: 'beat-3', field: 'text' }; history.apply(removed);
  const restored = history.undo();
  assert.equal(restored.selected, 'beat-2'); assert.equal(restored.field, 'duration');
  assert.deepEqual(restored.beats.map(item => item.id), ['beat-1', 'beat-2', 'beat-3']);
  assert.equal(restored.beats[1].duration, '4.125');
  restored.beats[1].text = 'External mutation';
  assert.notEqual(history.present.beats[1].text, 'External mutation');
  assert.deepEqual(history.undo(), original);
});

test('new edits discard only redo and history is bounded; load or starter resets the session', () => {
  const initial = { beats: beats(), selected: 'beat-1', field: 'duration' }, history = new SequenceHistory(initial, 3);
  for (let value = 1; value <= 5; value++) { const draft = structuredClone(initial); draft.beats[0].duration = value; history.apply(draft); }
  assert.equal(history.past.length, 3);
  history.undo(); assert.equal(history.canRedo, true);
  const branch = structuredClone(initial); branch.beats[0].duration = 7; history.apply(branch);
  assert.equal(history.canRedo, false); assert.equal(history.past.length, 3);
  const starter = { beats: makeBeats(STARTER_SEQUENCES.jump.beats, identities()), selected: 'beat-1', field: 'text' };
  history.reset(starter);
  assert.equal(history.canUndo, false); assert.equal(history.canRedo, false); assert.equal(history.undo(), null);
  assert.deepEqual(history.present, starter);
});

test('first invalid action identifies the exact collapsed field, including malformed duration and Unicode prompts', () => {
  const draft = beats(); draft[1].duration = '';
  assert.deepEqual(firstBeatIssue(draft), { index: 1, id: 'beat-2', field: 'duration', message: 'Action 2 needs a duration from 0.2 to 10 seconds.' });
  draft[0].text = '   ';
  assert.equal(firstBeatIssue(draft).field, 'text'); assert.equal(firstBeatIssue(draft).index, 0);
  draft[0].text = '人が歩く'; draft[1].duration = 1.5;
  assert.equal(firstBeatIssue(draft), null);
  draft[2].text = '!!!'; assert.equal(firstBeatIssue(draft).index, 2);
  assert.throws(() => requestBeats(draft), error => error.beatIndex === 2 && error.field === 'text');
});

test('composer validation opens and focuses the invalid field before showing its message', () => {
  const draft = beats(); draft[2].duration = '99';
  const calls = [], input = { setCustomValidity: value => calls.push(['message', value]), setAttribute: (name, value) => calls.push([name, value]), reportValidity: () => calls.push(['report']) };
  const fixture = { enabled: true, beats: draft, nodes: new Map([['beat-3', { input }]]), focus: (id, field) => calls.push(['focus', id, field]), onError: error => calls.push(['notice', error.message]) };
  assert.equal(SequenceComposer.prototype.reportValidity.call(fixture), false);
  assert.deepEqual(calls[0], ['focus', 'beat-3', 'duration']);
  assert.ok(calls.some(item => item[0] === 'report'));
  draft[2].duration = 2; calls.length = 0;
  assert.equal(SequenceComposer.prototype.reportValidity.call(fixture), true); assert.equal(calls.length, 0);
});
