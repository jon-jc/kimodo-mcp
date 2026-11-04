import test from 'node:test';
import assert from 'node:assert/strict';
import { RigPanel } from '../src/rig-panel.js';
import { DraftHistory, freshEdit } from '../src/rig-data.js';

const element = () => ({ value: '', disabled: false, textContent: '', children: [],
  replaceChildren(...items) { this.children = items; }, append(...items) { this.children.push(...items); },
  setAttribute() {}, addEventListener() {} });
function fixture() {
  const elements = new Map();
  globalThis.document = { getElementById(id) { if (!elements.has(id)) elements.set(id, element()); return elements.get(id); }, createElement: element };
  const $ = id => document.getElementById(id), panel = Object.create(RigPanel.prototype);
  const baseline = freshEdit(120), data = { skeleton: 'somaskel30', editable: { supported: true }, frames: 120, fps: 30,
    joint_names: ['Root', 'Arm', 'Hand'], joint_parents: [-1, 0, 1], source_range: { start_frame: 0, end_frame_exclusive: 120 },
    positions: Array.from({ length: 120 }, () => [[0, 0, 0], [1, 0, 0], [2, 0, 0]]), source_sha256: 'a'.repeat(64) };
  Object.assign(panel, { data, original: data, baseline, history: new DraftHistory(baseline), selected: 1, pendingForms: new Map(),
    inputRevision: 0, previewToken: 0, loadedKey: 'source', busy: false,
    selection: { job: { id: 'b'.repeat(32) }, sample: 0, variant: null, key: 'source' },
    callbacks: { onError(error) { throw error; }, onTime() {}, onDraftPreviewChange() {}, getSelectionEpoch: () => 7, onVariant() {} },
    viewer: { time: 0, duration: 119 / 30, setManipulator() {}, clearDraftPose() {},
      showDraftPose(pose) { this.draftPose = pose; } }, preview() {} });
  $('rig-edit-mode').value = 'rotate'; panel.renderDraft(); panel.fillJoint();
  return { panel, $ };
}

test('staging a joint preserves pending placement fields and staging placement preserves joint form values', () => {
  const { panel, $ } = fixture();
  $('trim-start').value = '10'; $('trim-end').value = '100'; panel.capturePlacement();
  $('joint-x').value = '15'; $('joint-start').value = '15'; $('joint-end').value = '80'; panel.captureJoint(); panel.stageJoint();
  assert.equal($('trim-start').value, '10'); assert.equal($('trim-end').value, '100');
  assert.equal(panel.history.value.start_frame, 0);
  $('joint-x').value = '22'; panel.captureJoint(); panel.stagePlacement();
  assert.equal($('joint-x').value, '22'); assert.equal(panel.history.value.start_frame, 10);
  assert.equal(panel.history.value.joint_adjustments[0].local_xyz_radians[0], Math.PI / 12);
  assert.equal($('apply-variant').disabled, true);
});

test('a late save keeps a newer draft selected and supplies source selection epoch', async () => {
  const { panel } = fixture(); let resolve, saved;
  const changed = panel.history.value; changed.translation_xz = [1, 0]; panel.history.push(changed);
  panel.callbacks.onVariant = (result, snapshot) => { saved = snapshot; };
  globalThis.fetch = () => new Promise(done => { resolve = value => done({ ok: true, json: async () => ({ result: value }) }); });
  const pending = panel.save();
  const newer = panel.history.value; newer.translation_xz = [3, 0]; panel.history.push(newer);
  resolve({ variant_id: 'c'.repeat(64) }); await pending;
  assert.equal(saved.should_select, false); assert.equal(saved.selection_epoch, 7);
  assert.deepEqual(panel.history.value.translation_xz, [3, 0]);
});

test('pending numeric input during save also prevents replacement, while an unchanged save may adopt', async () => {
  for (const editDuringSave of [false, true]) {
    const { panel, $ } = fixture(); let resolve, saved;
    const changed = panel.history.value; changed.translation_xz = [1, 0]; panel.history.push(changed);
    panel.callbacks.onVariant = (_, snapshot) => { saved = snapshot; };
    globalThis.fetch = () => new Promise(done => { resolve = value => done({ ok: true, json: async () => ({ result: value }) }); });
    const pending = panel.save();
    if (editDuringSave) { $('clip-x').value = '2'; panel.capturePlacement(); }
    resolve({ variant_id: 'c'.repeat(64) }); await pending;
    assert.equal(saved.should_select, !editDuringSave);
  }
});

test('a stale preview response cannot replace a newer source or draft', async () => {
  const { panel } = fixture(); let resolve, shown = false;
  panel.viewer.showDraftPose = () => { shown = true; };
  globalThis.fetch = () => new Promise(done => { resolve = value => done({ ok: true, json: async () => ({ result: value }) }); });
  const pending = RigPanel.prototype.preview.call(panel);
  panel.loadedKey = 'another-source'; resolve({ frames_preview: [] }); await pending;
  assert.equal(shown, false);
});

test('direct drag stages one undoable edit, uses a usable frame-zero window, and cancels after source change', () => {
  const { panel } = fixture(), identity = [[1, 0, 0], [0, 1, 0], [0, 0, 1]];
  panel.previewBundle = { joint_names: panel.data.joint_names, joint_parents: panel.data.joint_parents,
    parent_local_offsets: [[0, 0, 0], [1, 0, 0], [1, 0, 0]], first_retained_root_position: [0, 0, 0],
    frames_preview: [{ original_frame: 0, original_positions_m: panel.data.positions[0], original_global_rot_mats: [identity, identity, identity] }] };
  assert.equal(panel.startDrag(), true);
  assert.equal(panel.dragState.initial.joint_adjustments[0].blend_frames, 0);
  panel.drag({ position: [1, 0, 0], quaternion: [0, 0, Math.sin(Math.PI / 8), Math.cos(Math.PI / 8)] });
  panel.drag({ position: [1, 0, 0], quaternion: [0, 0, Math.sin(Math.PI / 4), Math.cos(Math.PI / 4)] });
  assert.equal(panel.history.canUndo, false); panel.endDrag(); assert.equal(panel.history.entries.length, 2);
  assert.ok(Math.abs(panel.history.value.joint_adjustments[0].local_xyz_radians[2] - Math.PI / 2) < 1e-12);
  panel.startDrag(); panel.loadedKey = 'different'; panel.endDrag(); assert.equal(panel.history.entries.length, 2);
});

test('cancelled pointer manipulation discards transient feedback without staging it', () => {
  const { panel } = fixture();
  panel.dragState = { latest: { ...panel.history.value, translation_xz: [5, 0] } };
  panel.cancelDrag(); panel.endDrag();
  assert.equal(panel.dragState, null); assert.equal(panel.history.canUndo, false);
  assert.deepEqual(panel.history.value.translation_xz, [0, 0]);
});

test('seeking or playing invalidates an in-flight single-frame draft preview', async () => {
  const { panel } = fixture(); let resolve, shown = false;
  panel.viewer.showDraftPose = () => { shown = true; };
  globalThis.fetch = () => new Promise(done => { resolve = value => done({ ok: true, json: async () => ({ result: value }) }); });
  const pending = RigPanel.prototype.preview.call(panel); panel.previewCleared();
  resolve({ frames_preview: [] }); await pending; assert.equal(shown, false);
});
