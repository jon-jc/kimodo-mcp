import test from 'node:test';
import assert from 'node:assert/strict';
import { register } from 'node:module';
import { initialFields } from '../src/toolbox-data.js';
register('./helpers/css-loader.mjs', import.meta.url);
const { ToolboxPanel } = await import('../src/toolbox-panel.js');

function fixture() {
  const elements = new Map();
  const $ = id => {
    if (!elements.has(id)) elements.set(id, { value:'', disabled:false, hidden:false, textContent:'', children:[], replaceChildren() {} });
    return elements.get(id);
  };
  const panel = Object.assign(Object.create(ToolboxPanel.prototype), {
    $, epoch:1, selection:1, pending:false, receipt:null,
    dialog:{ open:true, showModal() { this.open = true; } },
    schema:{ name:'get_job', input_schema:{ properties:{ job_id:{ type:'string' } }, required:['job_id'] } },
    message(value, error) { this.lastMessage = value; this.error = error; },
    renderTools() {}, coverage() {},
  });
  panel.renderFields = function(args) {
    $('tb-fields').children = initialFields(this.schema.input_schema, args).map(item => {
      const input = { value:item.text, disabled:!item.included, required:item.required };
      const include = item.required ? null : { checked:item.included, disabled:false };
      return { dataset:{ name:item.name, kind:item.kind }, querySelector(selector) { return selector === '[data-value]' ? input : include; } };
    });
  };
  $('tb-mode').value = 'json'; $('tb-json').value = '{"job_id":"job-one"}';
  return { panel, $ };
}
function delayedResponse() {
  let resolve;
  globalThis.fetch = () => new Promise(done => { resolve = (result, ok = true) => done({ ok, json:async () => ok ? { result } : { error:result } }); });
  return (...args) => resolve(...args);
}

test('JSON mode disables hidden invalid numeric fields and restores corrected controls', () => {
  const { panel, $ } = fixture();
  panel.schema.input_schema = { properties:{ attempt:{ type:'integer', minimum:0, maximum:99 }, limit:{ type:'integer', default:20 } }, required:['attempt'] };
  panel.renderFields({ attempt:100 });
  const old = $('tb-fields').children[0].querySelector('[data-value]');
  panel.mode();
  assert.equal(old.disabled, true); assert.equal(old.required, false);
  assert.equal($('tb-fields').children[1].querySelector('[data-include]').disabled, true);
  assert.equal($('tb-fields').hidden, true);
  $('tb-json').value = '{"attempt":0}'; $('tb-mode').value = 'fields'; panel.mode();
  const corrected = $('tb-fields').children[0].querySelector('[data-value]');
  assert.equal(corrected.value, '0'); assert.equal(corrected.disabled, false); assert.equal(corrected.required, true);
  assert.equal($('tb-fields').children[1].querySelector('[data-value]').disabled, true);
  assert.equal($('tb-fields').hidden, false);
  assert.deepEqual(panel.fields(), { attempt:0 });
});

test('a response completing after dialog close or reopen remains the visible downloadable receipt', async () => {
  for (const reopen of [false, true]) {
    const { panel, $ } = fixture(), resolve = delayedResponse();
    const pending = panel.run();
    assert.equal(panel.pending, true); assert.equal($('tb-fieldset').disabled, true);
    panel.dialog.open = false; panel.epoch++;
    if (reopen) { panel.dialog.open = true; panel.epoch++; }
    resolve({ id:'job-one', status:'completed' }); await pending;
    assert.equal(panel.pending, false); assert.equal($('tb-fieldset').disabled, false);
    assert.deepEqual(JSON.parse($('tb-result').textContent), panel.receipt.result);
    assert.match($('tb-result-context').textContent, /Response for get_job/);
    assert.equal($('tb-download').disabled, false);
    assert.equal(panel.receipt.arguments.job_id, 'job-one');
  }
});

test('a failed request after close preserves its error and never downloads an older result as the current response', async () => {
  const { panel, $ } = fixture(), resolve = delayedResponse();
  panel.receipt = { tool:'old_tool', result:{ old:true }, received_at:'yesterday' };
  panel.renderReceipt();
  const pending = panel.run(); panel.dialog.open = false; panel.epoch++;
  resolve('Connection lost', false); await pending;
  assert.equal(panel.receipt.tool, 'get_job'); assert.equal(panel.receipt.result, undefined);
  assert.match(panel.receipt.error, /No automatic retry/);
  assert.deepEqual(JSON.parse($('tb-result').textContent), { error:panel.receipt.error });
  assert.match($('tb-result-context').textContent, /Request error for get_job/);
  assert.equal(panel.pending, false); assert.equal(panel.error, true);
});

test('reopening restores a completed receipt without running the selected operation', async () => {
  const { panel, $ } = fixture();
  globalThis.Option = class { constructor(text, value) { this.text = text; this.value = value; } };
  const calls = [];
  globalThis.fetch = async path => { calls.push(path); return { ok:true, json:async () => ({ result:{ tool_count:103, tool_groups:[], workflows:[], schemas:[], tools:[] } }) }; };
  panel.receipt = { tool:'get_job', arguments:{ job_id:'saved' }, result:{ id:'saved', status:'completed' }, received_at:'now' };
  panel.dialog.open = false;
  await panel.open();
  assert.equal(panel.dialog.open, true);
  assert.deepEqual(JSON.parse($('tb-result').textContent), panel.receipt.result);
  assert.deepEqual(calls, ['/api/actions/get_capabilities']);
});
