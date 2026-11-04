import './toolbox.css';
import { action } from './api.js';
import { controlKind, initialFields, matchingTools, parseArguments, parseControl } from './toolbox-data.js';

const node = (tag, text, className) => { const item = document.createElement(tag); if (text !== undefined) item.textContent = text; if (className) item.className = className; return item; };
export class ToolboxPanel {
  constructor({ onOpen = () => {} } = {}) {
    this.epoch = 0; this.selection = 0; this.pending = false; this.schema = null;
    this.dialog = node('dialog', undefined, 'toolbox-dialog'); this.dialog.setAttribute('aria-labelledby', 'tb-title');
    this.dialog.innerHTML = `<header class="tb-heading"><div><p class="eyebrow">Connected animation workflow</p><h2 id="tb-title">Workflow & tool library</h2><p class="help">Find a workflow, open its visual editor, or use every integrated MCP control here.</p></div><button id="tb-close" type="button" class="small-button">Close</button></header>
      <p id="tb-message" class="tb-message" role="status">Loading the current tool registry…</p>
      <div id="tb-shortcuts" class="tb-actions"></div>
      <details class="tb-section"><summary>Native controls · coverage by project</summary><p class="help">Managed controls and original-application controls are listed separately. Runtime setup is still required.</p><div id="tb-coverage" class="tb-coverage"></div><a href="https://github.com/jon-jc/kimodo-mcp/blob/main/docs/native-controls.md" target="_blank" rel="noreferrer">Read the complete native controls guide</a></details>
      <section class="tb-section"><label>Choose a complete workflow<select id="tb-workflow"><option value="">Choose a recipe</option></select></label><div id="tb-recipe"></div></section>
      <div class="tb-layout"><aside><label>Search tools<input id="tb-search" type="search" placeholder="Search by task or tool name" /></label><label>Tool group<select id="tb-group"><option value="">All tools</option></select></label><p id="tb-count" class="help"></p><div id="tb-tools" class="tb-tools" aria-label="Matching tools"></div></aside>
      <section class="tb-editor"><h3 id="tb-tool-name">Choose a tool</h3><p id="tb-description" class="help"></p><p id="tb-effects" class="tb-effects"></p><form id="tb-form"><fieldset id="tb-fieldset"><label>Editing mode<select id="tb-mode"><option value="fields">Individual controls</option><option value="json">Complete arguments JSON</option></select></label><div id="tb-fields"></div><label id="tb-json-label" hidden>Complete arguments<textarea id="tb-json" rows="14" spellcheck="false">{}</textarea></label><button id="tb-run" type="submit" class="button primary" disabled>Run selected tool</button></fieldset></form>
      <details class="tb-section"><summary>Full input contract · nested controls and bounds</summary><pre id="tb-schema">Choose a tool.</pre></details><section class="tb-section"><h3>Last response</h3><p id="tb-result-context" class="help">Selecting a tool or recipe runs no animation or editor work.</p><pre id="tb-result">No tool has been run here.</pre><button id="tb-download" type="button" class="small-button" disabled>Save request & response</button></section></section></div>`;
    document.body.append(this.dialog);
    for (const [label, target] of [['Create animation', 'workflow-create'], ['Review & fix', 'workflow-review'], ['Export to editors', 'workflow-export'], ['FreeMoCap capture', 'open-freemocap'], ['Video reference', 'open-video-capture'], ['Saved motion', 'open-source-library'], ['Assets & props', 'open-assets'], ['Rig a model', 'open-unirig']]) {
      const button = node('button', label, 'small-button'); button.type = 'button'; button.addEventListener('click', () => { this.dialog.close(); onOpen(target); }); this.$('tb-shortcuts').append(button);
    }
    this.$('tb-close').onclick = () => this.dialog.close();
    this.dialog.addEventListener('close', () => { this.epoch++; this.selection++; });
    this.$('tb-search').addEventListener('input', () => this.renderTools()); this.$('tb-group').addEventListener('change', () => this.renderTools());
    this.$('tb-workflow').addEventListener('change', () => this.recipe());
    this.$('tb-mode').addEventListener('change', () => this.mode());
    this.$('tb-form').addEventListener('submit', event => { event.preventDefault(); this.run(); });
    this.$('tb-form').addEventListener('input', () => { if (this.receipt) this.$('tb-result-context').textContent = `Saved response for ${this.receipt.tool}. The current draft may differ.`; });
    this.$('tb-download').onclick = () => { if (!this.receipt) return; const url = URL.createObjectURL(new Blob([JSON.stringify(this.receipt, null, 2)], { type: 'application/json' })); const a = node('a'); a.href = url; a.download = `${this.receipt.tool}-receipt.json`; a.click(); setTimeout(() => URL.revokeObjectURL(url), 1000); };
  }
  $(id) { return this.dialog.querySelector(`#${id}`); }
  message(text, error = false) { this.$('tb-message').textContent = text; this.$('tb-message').classList.toggle('error', error); }
  async open() {
    if (!this.dialog.open) this.dialog.showModal(); const epoch = ++this.epoch; this.buttons(); this.renderReceipt();
    try { const catalog = await action('get_capabilities', {}); if (!this.dialog.open || epoch !== this.epoch) return;
      this.catalog = catalog;
      this.$('tb-group').replaceChildren(new Option('All tools', ''), ...catalog.tool_groups.map(group => new Option(group.title, group.id)));
      this.$('tb-workflow').replaceChildren(new Option('Choose a recipe', ''), ...catalog.workflows.map(recipe => new Option(recipe.title, recipe.id)));
      this.renderTools(); this.coverage(catalog.native_control_coverage?.sources ?? []);
      this.message(`${catalog.tool_count} tools · ${catalog.workflows.length} workflow recipes · ${catalog.schemas.length} domain schemas. Choose a visual editor above or an exact tool below.`);
      if (this.pending) this.message(`Running ${this.schema?.name ?? 'the selected tool'}. Closing this window does not cancel the operation.`);
      else if (this.receipt?.error) this.message(this.receipt.error, true);
      if (!this.schema) await this.select('get_workspace_state');
    } catch (error) { if (epoch === this.epoch && this.dialog.open) this.message(error.message, true); }
  }
  buttons() { this.$('tb-run').disabled = this.pending || !this.schema; this.$('tb-fieldset').disabled = this.pending; }
  renderReceipt() {
    if (!this.receipt) return;
    const { tool, result, error, received_at } = this.receipt;
    this.$('tb-result').textContent = JSON.stringify(error ? { error } : result, null, 2);
    this.$('tb-result-context').textContent = `${error ? 'Request error' : 'Response'} for ${tool} · ${received_at}. Check its status and evidence before the next stage.`;
    this.$('tb-download').disabled = false;
  }
  renderTools() {
    if (!this.catalog) return; const tools = matchingTools(this.catalog, this.$('tb-search').value, this.$('tb-group').value);
    this.$('tb-count').textContent = `${tools.length} matching tools`; this.$('tb-tools').replaceChildren();
    for (const tool of tools) { const button = node('button', tool.name.replaceAll('_', ' '), 'tb-tool'); button.type = 'button'; button.title = tool.description; button.setAttribute('aria-pressed', String(this.schema?.name === tool.name)); button.onclick = () => this.select(tool.name); this.$('tb-tools').append(button); }
  }
  coverage(projects) {
    this.$('tb-coverage').replaceChildren();
    for (const project of projects) { const card = node('article'); card.append(node('h3', project.title ?? project.id));
      for (const [key, title] of [['available_controls', 'Integrated controls'], ['external_controls', 'Original application / operator workflow'], ['unavailable_controls', 'Unavailable in the reviewed release']]) {
        if (!project[key]?.length) continue; card.append(node('h4', title)); const list = node('ul'); for (const text of project[key]) list.append(node('li', typeof text === 'string' ? text : JSON.stringify(text))); card.append(list);
      } this.$('tb-coverage').append(card);
    }
  }
  async recipe() {
    const workflow = this.$('tb-workflow').value, epoch = this.epoch; this.$('tb-recipe').replaceChildren(); if (!workflow) return;
    try { const recipe = await action('get_workflow', { workflow }); if (!this.dialog.open || epoch !== this.epoch || workflow !== this.$('tb-workflow').value) return;
      const box = this.$('tb-recipe'); box.append(node('p', recipe.requirements.join(' '), 'help'));
      const inputs = node('details'); inputs.append(node('summary', 'Required inputs for this workflow'), node('pre', JSON.stringify(recipe.inputs, null, 2))); box.append(inputs);
      for (const [index, step] of recipe.steps.entries()) { const card = node('article', undefined, 'tb-step'); card.append(node('h4', `${index + 1}. ${step.tool.replaceAll('_', ' ')}`), node('p', step.expected));
        if (step.when) card.append(node('p', `When: ${step.when}`, 'help'));
        const button = node('button', 'Open controls', 'small-button'); button.type = 'button'; button.onclick = async () => { await this.select(step.tool, step.arguments ?? {}); this.$('tb-tool-name').scrollIntoView({ block: 'nearest' }); }; card.append(button);
        const refs = node('details'); refs.append(node('summary', 'Inputs & recovery'), node('pre', JSON.stringify({ arguments_from: step.arguments_from, on_failure: step.on_failure }, null, 2))); card.append(refs); box.append(card);
      }
    } catch (error) { if (epoch === this.epoch && this.dialog.open) this.message(error.message, true); }
  }
  async select(name, supplied = {}) {
    if (this.pending) { this.message('A tool call is still running. Wait for its response before choosing another operation.'); return; }
    const epoch = this.epoch, selection = ++this.selection; this.schema = null; this.buttons();
    try { const tool = await action('get_tool_schema', { tool_name: name }); if (!this.dialog.open || epoch !== this.epoch || selection !== this.selection) return;
      this.schema = tool; this.$('tb-tool-name').textContent = tool.name.replaceAll('_', ' '); this.$('tb-description').textContent = tool.description;
      this.$('tb-effects').textContent = tool.annotations?.readOnlyHint ? 'Read / inspect · this operation is marked read-only.' : 'Action · this may save files, start work, cancel a job or change editor state. Review the operation and arguments before running.';
      this.$('tb-schema').textContent = JSON.stringify(tool.input_schema, null, 2); this.renderFields(supplied); this.$('tb-mode').value = 'fields'; this.$('tb-fields').hidden = false; this.$('tb-json-label').hidden = true;
      this.renderTools(); this.buttons();
    } catch (error) { if (epoch === this.epoch && selection === this.selection && this.dialog.open) this.message(error.message, true); }
  }
  renderFields(supplied) {
    this.$('tb-fields').replaceChildren();
    for (const item of initialFields(this.schema.input_schema, supplied)) {
      const row = node('div', undefined, 'tb-field'); row.dataset.name = item.name; row.dataset.kind = item.kind;
      const label = node('label', `${item.name.replaceAll('_', ' ')}${item.required ? ' · required' : ' · optional'}`); let input;
      if (item.kind === 'boolean' || item.kind === 'enum') {
        input = node('select'); const values = item.kind === 'boolean' ? [false, true] : item.field.enum;
        input.append(new Option('Choose a value', ''), ...values.map(value => new Option(String(value), JSON.stringify(value))));
        input.value = item.kind === 'enum' && Object.hasOwn(supplied, item.name) ? JSON.stringify(supplied[item.name]) : item.kind === 'enum' && Object.hasOwn(item.field, 'default') ? JSON.stringify(item.field.default) : item.text;
      } else { input = node(item.kind === 'json' ? 'textarea' : 'input'); if (item.kind === 'json') { input.rows = 5; input.spellcheck = false; } else { input.type = ['integer', 'number'].includes(item.kind) ? 'number' : 'text'; if (input.type === 'number') { input.step = item.kind === 'integer' ? '1' : 'any'; if (item.field.minimum !== undefined) input.min = item.field.minimum; if (item.field.maximum !== undefined) input.max = item.field.maximum; } } input.value = item.text; }
      input.dataset.value = ''; input.required = item.required; input.disabled = !item.included; label.append(input); row.append(label);
      if (!item.required) { const toggle = node('label', undefined, 'tb-optional'), check = node('input'); check.type = 'checkbox'; check.checked = item.included; check.dataset.include = ''; check.addEventListener('change', () => { input.disabled = !check.checked; input.required = check.checked; }); toggle.append(check, node('span', 'Set this value (otherwise use server default)')); row.prepend(toggle); }
      if (item.field.description) row.append(node('p', item.field.description, 'help')); this.$('tb-fields').append(row);
    }
  }
  fields(omitBlank = false) { return Object.fromEntries([...this.$('tb-fields').children].filter(row => !row.querySelector('[data-include]') || row.querySelector('[data-include]').checked).filter(row => !omitBlank || row.dataset.kind === 'string' || row.querySelector('[data-value]').value.trim()).map(row => [row.dataset.name, parseControl(row.dataset.kind, row.querySelector('[data-value]').value, row.dataset.name)])); }
  mode() {
    try {
      if (this.$('tb-mode').value === 'json') this.$('tb-json').value = JSON.stringify(this.fields(true), null, 2);
      else { const args = parseArguments(this.$('tb-json').value), known = this.schema.input_schema.properties ?? {}; if (Object.keys(args).some(key => !Object.hasOwn(known, key))) throw new Error('Remove unknown argument names before switching to individual controls.'); this.renderFields(args); }
      const json = this.$('tb-mode').value === 'json'; this.$('tb-fields').hidden = json; this.$('tb-json-label').hidden = !json;
      for (const row of this.$('tb-fields').children) {
        const input = row.querySelector('[data-value]'), include = row.querySelector('[data-include]');
        const included = !include || include.checked;
        // Hidden numeric controls still participate in native form validation
        // unless disabled, even after removing their required attribute.
        input.disabled = json || !included; input.required = !json && included;
        if (include) include.disabled = json;
      }
    } catch (error) { this.$('tb-mode').value = this.$('tb-fields').hidden ? 'json' : 'fields'; this.message(error.message, true); }
  }
  async run() {
    if (this.pending || !this.schema) return; const tool = this.schema.name;
    let args; try { args = this.$('tb-mode').value === 'json' ? parseArguments(this.$('tb-json').value) : this.fields(); } catch (error) { this.message(error.message, true); return; }
    this.pending = true; this.buttons(); this.message(`Running ${tool}. Closing this window does not cancel the operation.`);
    try {
      const result = await action(tool, args); this.receipt = { tool, arguments: args, result, received_at: new Date().toISOString() };
      // Only one call may be pending. Its response remains the current receipt
      // even if the dialog closed or reopened while the operation was running.
      this.renderReceipt(); this.message('Response received. Submission alone is not completion; follow the workflow’s verification step.');
    } catch (error) {
      const message = `${error.message} No automatic retry was made. For a submitted action, inspect its saved status before trying again.`;
      this.receipt = { tool, arguments: args, error: message, received_at: new Date().toISOString() };
      this.renderReceipt(); this.message(message, true);
    }
    finally { this.pending = false; this.buttons(); }
  }
}
