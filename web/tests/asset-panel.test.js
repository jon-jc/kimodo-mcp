import test from 'node:test';
import assert from 'node:assert/strict';
import { register } from 'node:module';
register('./helpers/css-loader.mjs', import.meta.url);
const { AssetPanel } = await import('../src/asset-panel.js');
const id = 'a'.repeat(64), second = 'b'.repeat(64), job = 'c'.repeat(32);

function fixture() {
  const elements = new Map();
  const $ = name => {
    if (!elements.has(name)) elements.set(name, { value: '', disabled: false, textContent: '', options: [], replaceChildren() {}, append() {} });
    return elements.get(name);
  };
  const panel = Object.assign(Object.create(AssetPanel.prototype), {
    $, epoch: 1, assetRevision: 0, dialog: { open: true }, prepared: null,
    assets: [{ asset_id: id, label: 'Sword', format: 'fbx', kind: 'prop' }],
    message(value) { this.lastMessage = value; },
    viewer: { clear() {} },
    applyScene(value) { this.appliedScene = value; this.invalidate(); },
    async loadViewer(url) { this.loadedUrl = url; },
  });
  $('as-library').value = id; $('as-preview-attempt').value = '0'; $('as-source-kind').value = 'kimodo';
  $('as-job').value = job; $('as-sample').value = '0'; $('as-character-attempt').value = '0';
  $('as-variant').value = ''; $('as-attempt').value = '0';
  return { panel, $ };
}
function delayedResponse() {
  let resolve;
  globalThis.fetch = () => new Promise(done => { resolve = (result, ok = true) => done({ ok, json: async () => ok ? { result } : { error: result } }); });
  return (...args) => resolve(...args);
}

test('an old saved-scene response cannot overwrite a reopened dialog', async () => {
  const { panel, $ } = fixture(), resolve = delayedResponse(); $('as-scenes').value = id;
  const pending = panel.loadScene(); panel.epoch += 2;
  resolve({ scene_id: id, request: {} }); await pending;
  assert.equal(panel.appliedScene, undefined);
});

test('saved-result recovery ignores a closed or changed selection', async () => {
  for (const change of ['epoch', 'selection']) {
    const { panel, $ } = fixture(), resolve = delayedResponse(); $('as-handoff-id').value = id;
    const pending = panel.recover();
    if (change === 'epoch') panel.epoch++; else $('as-handoff-id').value = second;
    resolve({ handoff_id: id }); await pending;
    assert.equal(panel.appliedScene, undefined);
  }
});

test('a changed asset selection discards late preview successes and errors', async () => {
  for (const ok of [true, false]) {
    const { panel, $ } = fixture(), resolve = delayedResponse();
    const pending = panel.previewAsset(false); $('as-library').value = second; panel.assetRevision++;
    resolve(ok ? { status: 'completed' } : 'Old asset failed', ok); await pending;
    assert.equal(panel.loadedUrl, undefined);
  }
});

test('character export discovery fills attempt 1 and never adopts another candidate response', async () => {
  for (const changed of [false, true]) {
    const { panel, $ } = fixture(), resolve = delayedResponse();
    const pending = panel.findCharacterExport(); if (changed) $('as-sample').value = '1';
    resolve({ job_id: job, outputs: [{ verified: true, source_kind: 'character', role: 'blender_project', format: 'blend', sha256: id, sample_index: 0, attempt: 1, variant_id: null }] });
    await pending; assert.equal(Number($('as-character-attempt').value), changed ? 0 : 1);
  }
});

test('prepare validates the motion source before saving any attachment scene', async () => {
  const { panel, $ } = fixture(); let writes = 0;
  panel.sceneRequest = () => ({ title: 'Sword', attachments: [{}] }); panel.saveScene = async () => { writes++; return { scene_id: id }; };
  $('as-character-attempt').value = '';
  await assert.rejects(() => panel.prepare(), /Character export attempt needs a number/);
  assert.equal(writes, 0);
});

test('a prepared saved handoff restores its draft and can be built until settings change', async () => {
  const { panel, $ } = fixture(); $('as-handoff-id').value = id;
  const request = { scene_id: second, scene: { title: 'Saved sword', attachments: [] }, attempt: 4,
    motion: { source: { kind: 'kimodo', job_id: job, sample_index: 2, attempt: 1, variant_id: second } } };
  globalThis.fetch = async () => ({ ok: true, json: async () => ({ result: { handoff_id: id, status: 'prepared', request, outputs: [] } }) });
  await panel.recover();
  assert.equal(panel.prepared, id); assert.deepEqual(panel.appliedScene.request, request.scene);
  assert.equal(Number($('as-sample').value), 2); assert.equal(Number($('as-character-attempt').value), 1);
  assert.equal($('as-variant').value, second); assert.equal(Number($('as-attempt').value), 4);
  panel.invalidate(); assert.equal(panel.prepared, null); assert.equal($('as-run').disabled, true);
});
