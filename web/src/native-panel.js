import './native.css';
import { action } from './api.js';
import { appendConstraint, bindExtractedConstraint, constraintSummary, nativeSettings, nativeWorkspaceUrl, numericField, parseFrameList, parseNativeJson, rootPathConstraint } from './native-data.js';

const copy = value => structuredClone(value);
const node = (tag, text, className) => { const item = document.createElement(tag); if (text !== undefined) item.textContent = text; if (className) item.className = className; return item; };
const button = (text, run) => { const item = node('button', text, 'small-button'); item.type = 'button'; item.addEventListener('click', run); return item; };
function download(value, name) {
  const url = URL.createObjectURL(new Blob([JSON.stringify(value, null, 2)], { type: 'application/json' }));
  const link = node('a'); link.href = url; link.download = name; link.click(); setTimeout(() => URL.revokeObjectURL(url), 1000);
}

export class NativePanel {
  constructor({ container, onChange, onError, onSteps, onImport, getRequest, getSources }) {
    Object.assign(this, { container, onChange, onError, onSteps, onImport, getRequest, getSources });
    this.revision = 0; this.models = []; this.constraints = null; this.baseConstraints = []; this.fields = new Map();
    const details = node('details', undefined, 'native-controls'); details.id = 'native-controls';
    details.append(node('summary', 'Native generation controls'));
    details.append(node('p', 'Choose the exact model and generation settings. This workspace supports up to 4 candidates, 100 steps, 8 actions and 2400 frames per request.', 'help'));
    this.model = this.select(details, 'model', 'Model · skeleton, dataset & version', []);
    this.model.append(new Option('SOMA · Rigplay · v1.2', 'kimodo-soma-rp-v1.2'));
    this.modelNote = node('p', 'Model availability is checked by the worker; selecting a model does not download or load it.', 'help'); details.append(this.modelNote);
    const sampling = this.section(details, 'Sampling & guidance', true), grid = node('div', undefined, 'native-grid'); sampling.append(grid);
    this.number(grid, 'steps', 'Denoising steps', 1, 100, 1);
    this.select(grid, 'cfg_type', 'Guidance mode', [['separated', 'Separate text & constraints'], ['regular', 'Combined guidance'], ['nocfg', 'No classifier-free guidance']]);
    this.number(grid, 'cfg_text', 'Text guidance weight', 0, 20);
    this.number(grid, 'cfg_constraint', 'Constraint guidance weight', 0, 20);
    this.number(grid, 'cfg_regular', 'Combined guidance weight', 0, 20);
    sampling.append(node('p', 'Inactive guidance weights remain saved. Higher weights or more steps do not guarantee a better performance.', 'help'));
    const timing = this.section(details, 'Transitions, heading & correction'), timingGrid = node('div', undefined, 'native-grid'); timing.append(timingGrid);
    this.number(timingGrid, 'transition_frames', 'Transition history · frames', 1, 30, 1);
    this.number(timingGrid, 'first_heading_angle', 'Initial heading · degrees', -360, 360);
    this.number(timingGrid, 'root_margin', 'Root correction margin · meters', 0, 1);
    this.check(timing, 'postprocess', 'Apply native motion postprocessing');
    this.check(timing, 'real_robot_rotations', 'Project G1 rotations to robot joint limits');
    timing.append(node('p', 'Transitions revisit the preceding action’s tail. Postprocessing can reduce skating or constraint error; inspect the result. G1 projection changes the published motion and is only available for G1.', 'help'));
    const outputs = this.section(details, 'Files created with generation'); this.formats = new Map();
    for (const [value, label] of [['npz', 'Kimodo NPZ · source motion'], ['bvh', 'SOMA BVH'], ['amass', 'SMPL-X AMASS'], ['csv', 'G1 MuJoCo CSV']]) this.formats.set(value, this.check(outputs, `format-${value}`, label));
    this.check(outputs, 'bvh_standard_tpose', 'Use standard T-pose for generated BVH');
    outputs.append(node('p', 'Keep NPZ enabled for source inspection and cleanup. Additional formats depend on the selected skeleton. Completed NPZ motions can also be converted later in Export.', 'help'));
    this.constraintsSection = this.section(details, 'Pose constraints & root path');
    this.constraintsSection.append(node('p', 'Constraints guide a new generation. They do not edit the saved source. Frame numbers are zero-based in the new animation; positions use meters, Y up and X/Z ground coordinates.', 'help'));
    this.constraintList = node('div', undefined, 'native-constraint-list'); this.constraintsSection.append(this.constraintList);
    this.constraintsSection.append(button('Clear all staged constraints', () => this.run(() => this.setConstraints([]))));
    this.makePathEditor(); this.makePoseEditor();
    const files = this.section(details, 'Request & constraint JSON');
    files.append(node('p', 'Import a complete request or native constraints JSON from your computer. Files are staged in the editor; Apply validates them before replacing the draft. No generation starts here.', 'help'));
    this.jsonKind = document.createElement('select'); this.jsonKind.id = 'native-json-kind';
    this.jsonKind.append(new Option('Complete generation request', 'request'), new Option('Constraints only', 'constraints'));
    this.label(files, 'File contents', this.jsonKind);
    const file = document.createElement('input'); file.type = 'file'; file.accept = '.json,application/json'; file.id = 'native-json-file'; this.label(files, 'Choose local JSON file', file);
    this.json = document.createElement('textarea'); this.json.id = 'native-json'; this.json.rows = 7; this.json.spellcheck = false; this.json.placeholder = 'Paste JSON or choose a local file'; this.label(files, 'JSON draft · not applied', this.json);
    this.jsonToken = 0;
    this.json.addEventListener('input', () => { this.jsonToken++; });
    this.jsonKind.addEventListener('change', () => { this.jsonToken++; });
    file.addEventListener('change', () => this.run(async () => {
      const selected = file.files[0]; if (!selected) return;
      if (selected.size > 950_000) throw new Error('Choose JSON smaller than 950 KB.');
      const ticket = ++this.jsonToken, text = await selected.text();
      if (ticket !== this.jsonToken) return;
      this.json.value = text; this.message('File loaded into the JSON editor. Apply it to replace the draft.');
    }));
    const fileActions = node('div', undefined, 'native-actions');
    fileActions.append(button('Validate & apply JSON', () => this.run(async () => {
      const kind = this.jsonKind.value, content = this.json.value, revision = this.revision, token = this.jsonToken;
      const parsed = parseNativeJson(content, kind);
      if (kind === 'request') { await this.onImport(parsed, () => revision === this.revision && token === this.jsonToken); return; }
      const request = await this.getRequest(), result = await action('validate_motion', { request: { ...request, constraints: parsed } });
      if (revision !== this.revision || token !== this.jsonToken) throw new Error('The draft changed during validation. Apply the current JSON again.');
      this.setConstraints(result.request.constraints); this.message('Validated constraint sets applied. Preview the complete request before generating.');
    })), button('Export request JSON', () => this.run(async () => { const value = await this.getRequest(); download(value, 'kimodo-request.json'); })), button('Export constraints JSON', () => this.run(async () => { const value = await this.getRequest(); download(value.constraints, 'kimodo-constraints.json'); })));
    files.append(fileActions); this.makeNativeWorkspace(details);
    this.status = node('p', '', 'help'); this.status.setAttribute('role', 'status'); details.append(this.status); container.append(details);
    details.addEventListener('toggle', () => { if (details.open) { this.run(() => this.loadCatalog()); this.syncSources(); } });
    this.load({});
  }
  section(parent, title, open = false) { const details = node('details', undefined, 'native-section'); details.open = open; details.append(node('summary', title)); parent.append(details); return details; }
  label(parent, title, input) { const label = node('label', title); label.htmlFor = input.id; const wrapper = node('div', undefined, 'native-field'); wrapper.append(label, input); parent.append(wrapper); return wrapper; }
  number(parent, key, title, min, max, step = 'any') { const input = document.createElement('input'); Object.assign(input, { id: `native-${key}`, type: 'number', min, max, step, required: true }); this.label(parent, title, input); this.fields.set(key, input); input.addEventListener('input', () => { this.changed(); if (key === 'steps') this.onSteps(Number(input.value)); }); return input; }
  select(parent, key, title, options) { const input = document.createElement('select'); input.id = `native-${key}`; for (const [value, text] of options) input.append(new Option(text, value)); this.label(parent, title, input); this.fields.set(key, input); input.addEventListener('change', () => { if (key === 'model') this.adaptModel(true); this.syncVisibility(); this.changed(); }); return input; }
  check(parent, key, title) { const label = node('label', undefined, 'native-check'), input = document.createElement('input'); input.type = 'checkbox'; input.id = `native-${key}`; label.append(input, document.createTextNode(title)); parent.append(label); this.fields.set(key, input); input.addEventListener('change', () => { this.syncVisibility(); this.changed(); }); return input; }
  message(text) { if (this.status) this.status.textContent = text; }
  async run(task) { try { await task(); } catch (error) { this.message(error.message); this.onError(error); } }
  changed() { this.revision++; this.onChange(); }
  async loadCatalog() {
    if (this.models.length || this.loadingCatalog) return;
    this.loadingCatalog = true;
    try {
      const result = await action('list_models', {}), selected = this.model.value;
      this.models = result.models ?? []; this.model.replaceChildren();
      for (const model of this.models) this.model.append(new Option(`${model.skeleton} · ${model.dataset === 'RP' ? 'Rigplay' : model.dataset} · ${model.name}`, model.id));
      this.model.value = selected || result.default;
      this.adaptModel(false); this.syncVisibility();
    } finally { this.loadingCatalog = false; }
  }
  adaptModel(changed) {
    const info = this.models.find(item => item.id === this.model.value); if (!info) return;
    this.modelNote.textContent = `${info.output_joints} output joints · ${info.formats.join(', ')} · ${info.postprocess_supported ? 'postprocessing supported' : 'no native contact postprocessing'}. Selecting does not load weights. Fitted-character preview is SOMA-specific.`;
    for (const [format, input] of this.formats) { input.disabled = !info.formats.includes(format); if (changed) input.checked = info.formats.includes(format); }
    if (changed) { this.fields.get('postprocess').checked = info.postprocess_supported; this.fields.get('real_robot_rotations').checked = false; }
  }
  syncVisibility() {
    const mode = this.fields.get('cfg_type').value;
    for (const key of ['cfg_text', 'cfg_constraint', 'cfg_regular']) { const input = this.fields.get(key), active = key === 'cfg_regular' ? mode === 'regular' : mode === 'separated'; input.closest('.native-field').hidden = !active; input.disabled = !active; }
    const info = this.models.find(item => item.id === this.model.value), g1 = info?.skeleton === 'G1' || this.model.value.includes('-g1-');
    this.fields.get('postprocess').disabled = g1;
    this.fields.get('real_robot_rotations').disabled = !g1;
    this.fields.get('root_margin').disabled = g1 || !this.fields.get('postprocess').checked;
    this.fields.get('bvh_standard_tpose').disabled = !this.formats.get('bvh').checked;
  }
  load(request) {
    this.base = nativeSettings(request); this.baseConstraints = copy(request.constraints ?? []); this.constraints = null;
    if (![...this.model.options].some(item => item.value === this.base.model)) this.model.append(new Option(this.base.model, this.base.model));
    for (const [key, value] of Object.entries(this.base)) {
      if (key === 'formats') { for (const [format, input] of this.formats) input.checked = value.includes(format); continue; }
      const input = this.fields.get(key);
      if (input.type === 'checkbox') input.checked = value;
      else input.value = key === 'first_heading_angle' ? value * 180 / Math.PI : value;
    }
    this.adaptModel(false); this.syncVisibility();
    this.loadedRaw = this.snapshot(); this.revision++; this.renderConstraints(); this.onSteps(this.base.steps);
  }
  snapshot() { return Object.fromEntries([...this.fields].map(([key, input]) => [key, input.type === 'checkbox' ? input.checked : input.value]).concat([['constraints', copy(this.constraints)]])); }
  dirty() { return JSON.stringify(this.snapshot()) !== JSON.stringify(this.loadedRaw); }
  fieldChanged(key) { return this.snapshot()[key] !== this.loadedRaw[key]; }
  values() {
    const result = nativeSettings(this.base), raw = this.snapshot();
    for (const key of ['model', 'cfg_type']) result[key] = raw[key];
    for (const [key, min, max, integer] of [['steps', 1, 100, true], ['cfg_regular', 0, 20], ['cfg_text', 0, 20], ['cfg_constraint', 0, 20], ['transition_frames', 1, 30, true], ['root_margin', 0, 1]]) result[key] = numericField(raw[key], this.fields.get(key).labels?.[0]?.textContent ?? key, min, max, integer);
    result.first_heading_angle = raw.first_heading_angle === this.loadedRaw.first_heading_angle ? this.base.first_heading_angle : numericField(raw.first_heading_angle, 'Initial heading', -360, 360) * Math.PI / 180;
    for (const key of ['postprocess', 'real_robot_rotations', 'bvh_standard_tpose']) result[key] = raw[key];
    const formats = [...this.formats].filter(([, input]) => input.checked).map(([name]) => name);
    result.formats = this.base.formats.length === formats.length && this.base.formats.every(format => formats.includes(format)) ? copy(this.base.formats) : formats;
    if (!result.formats.length) throw new Error('Choose at least one generation output format.');
    if (!this.models.some(item => item.id === result.model)) throw new Error('Wait for the exact model catalog, then choose a supported model.');
    return result;
  }
  setSteps(steps) { this.fields.get('steps').value = steps; this.changed(); }
  getConstraints() { return copy(this.constraints ?? this.baseConstraints); }
  setConstraints(value) { this.constraints = copy(value); this.renderConstraints(); this.changed(); }
  renderConstraints() {
    this.constraintList.replaceChildren(); const constraints = this.getConstraints();
    if (!constraints.length) this.constraintList.append(node('p', 'No authored constraints. A checked starting pose is added when the request is compiled.', 'help'));
    constraints.forEach((constraint, index) => {
      const row = node('div', undefined, 'native-constraint-row'); row.append(node('span', constraintSummary(constraint)));
      row.append(button('Edit JSON', () => { this.jsonKind.value = 'constraints'; this.json.value = JSON.stringify(constraints, null, 2); this.json.closest('details').open = true; this.json.focus(); }), button('Remove', () => this.setConstraints(this.getConstraints().filter((_, item) => item !== index))));
      this.constraintList.append(row);
    });
  }
  makePathEditor() {
    const box = this.section(this.constraintsSection, 'Author a ground path');
    box.append(node('p', 'Add X/Z targets at destination frames. Optional heading uses degrees (0 = +Z). Dense mode linearly fills only between keys; it is not curve smoothing.', 'help'));
    this.pathRows = node('div', undefined, 'native-path-rows'); box.append(this.pathRows);
    this.addPathRow({ frame: 0, x: 0, z: 0 }); this.addPathRow({ frame: 90, x: 0, z: 1 });
    const denseLabel = node('label', undefined, 'native-check'); this.dense = document.createElement('input'); this.dense.type = 'checkbox'; denseLabel.append(this.dense, document.createTextNode('Fill every frame between path keys · max 512')); box.append(denseLabel);
    const actions = node('div', undefined, 'native-actions'); actions.append(button('Add path key', () => this.run(() => this.addPathRow({}))), button('Stage root path', () => this.run(() => {
      const rows = [...this.pathRows.children].map(row => Object.fromEntries([...row.querySelectorAll('input')].map(input => [input.dataset.key, input.value])));
      this.setConstraints(appendConstraint(this.getConstraints(), rootPathConstraint(rows, this.dense.checked))); this.message('Root path staged. Preview the request to check timing and conflicts.');
    }))); box.append(actions);
  }
  addPathRow(value) {
    if (this.pathRows.children.length >= 128) throw new Error('Keep at most 128 root path keys.');
    const row = node('div', undefined, 'native-path-row');
    for (const [key, title] of [['frame', 'Frame'], ['x', 'X · m'], ['z', 'Z · m'], ['heading', 'Heading · °']]) { const label = node('label', title), input = document.createElement('input'); input.type = 'number'; input.step = key === 'frame' ? '1' : 'any'; input.value = value[key] ?? ''; input.dataset.key = key; input.setAttribute('aria-label', `Path key ${this.pathRows.children.length + 1} ${title}`); label.append(input); row.append(label); }
    row.append(button('Remove key', () => row.remove())); this.pathRows.append(row);
  }
  makePoseEditor() {
    const box = this.section(this.constraintsSection, 'Reuse saved source poses');
    box.append(node('p', 'Choose a completed SOMA source and map its frames to frames in the new animation. Saved corrected versions are available; unsaved drag edits are not used. World root positions are retained.', 'help'));
    this.source = document.createElement('select'); this.source.id = 'native-pose-source'; this.label(box, 'Saved source · candidate & version', this.source);
    this.poseKind = document.createElement('select'); this.poseKind.id = 'native-pose-kind';
    for (const [value, label] of [['fullbody', 'Full body'], ['left-hand', 'Left hand'], ['right-hand', 'Right hand'], ['left-foot', 'Left foot'], ['right-foot', 'Right foot']]) this.poseKind.append(new Option(label, value));
    this.label(box, 'Constrain', this.poseKind);
    this.sourceFrames = document.createElement('input'); this.sourceFrames.id = 'native-source-frames'; this.sourceFrames.value = '0'; this.label(box, 'Source frames · comma separated', this.sourceFrames);
    this.targetFrames = document.createElement('input'); this.targetFrames.id = 'native-target-frames'; this.targetFrames.value = '0'; this.label(box, 'Destination frames · comma separated', this.targetFrames);
    const actions = node('div', undefined, 'native-actions'); actions.append(button('Refresh source choices', () => this.syncSources()), button('Stage pose constraint', () => this.run(async () => {
      const selected = this.sources?.find(item => item.key === this.source.value); if (!selected) throw new Error('Choose a completed saved SOMA source.');
      const snapshot = this.poseSnapshot(), revision = this.revision, model = this.model.value;
      const sourceFrames = parseFrameList(this.sourceFrames.value, { label: 'Source frames' }), targetFrames = parseFrameList(this.targetFrames.value, { label: 'Destination frames' });
      if (sourceFrames.length !== targetFrames.length) throw new Error('Give one destination frame for each source frame.');
      const result = await action('extract_motion_constraints', { ...selected.arguments, source_frames: sourceFrames, target_frames: targetFrames, constraint_type: this.poseKind.value });
      if (revision !== this.revision || snapshot !== this.poseSnapshot()) throw new Error('The source or draft changed while reading poses. Stage the current selection again.');
      this.setConstraints(appendConstraint(this.getConstraints(), bindExtractedConstraint(result, selected.arguments, model)));
      this.message(`Saved source poses staged · checksum ${result.source_sha256.slice(0, 12)}. Preview the request to check conflicts.`);
    }))); box.append(actions); this.syncSources();
  }
  poseSnapshot() { return JSON.stringify([this.source.value, this.poseKind.value, this.sourceFrames.value, this.targetFrames.value]); }
  syncSources() {
    if (!this.source) return; const previous = this.source.value;
    this.sources = this.getSources(); this.source.replaceChildren(new Option('Choose a completed source', ''));
    for (const item of this.sources) this.source.append(new Option(item.label, item.key));
    if (this.sources.some(item => item.key === previous)) this.source.value = previous;
  }
  reportValidity() {
    for (const input of this.fields.values()) if (!input.disabled && !input.checkValidity()) { for (let parent = input.parentElement; parent; parent = parent.parentElement) if (parent.tagName === 'DETAILS') parent.open = true; input.reportValidity(); return false; }
    return true;
  }
  makeNativeWorkspace(parent) {
    const section = this.section(parent, 'Original Kimodo workspace · all native tools');
    section.append(node('p', 'Open the original Kimodo interface for its native constraint timeline, local/world gizmos, SOMA layer, G1 display, motion/example load and save, and screenshot/video renders. This is a separate session; its files do not appear in managed job history automatically.', 'help'));
    section.append(node('p', 'Start loads the selected model and requires its installed dependencies and substantial memory. Stop this workspace before generating managed jobs; it shares the generation lock.', 'help'));
    const status = node('p', 'Not checked. Opening this section does not start a model.', 'help'); status.setAttribute('role', 'status');
    const link = node('a', 'Open original Kimodo UI ↗', 'button secondary compact'); link.target = '_blank'; link.rel = 'noopener'; link.hidden = true;
    const log = node('pre', '', 'native-log'); log.hidden = true;
    const actions = node('div', undefined, 'native-actions'); let busy = false, epoch = 0;
    const controls = [];
    const invoke = async name => {
      if (busy) return; busy = true; const ticket = ++epoch; controls.forEach(item => { item.disabled = true; });
      status.textContent = name === 'start_native_workspace' ? 'Starting the selected model in the original workspace…' : name === 'stop_native_workspace' ? 'Stopping the owned native workspace…' : 'Checking the native workspace…';
      try {
        const result = await action(name, name === 'start_native_workspace' ? { model: this.model.value } : {});
        if (ticket !== epoch) return;
        status.textContent = `${result.status} · ${result.model ?? 'no model selected'}${result.error ? ` · ${result.error}` : ''}`;
        const url = nativeWorkspaceUrl(result); link.hidden = !url; if (url) link.href = url; else link.removeAttribute('href');
        log.textContent = result.log_tail ?? ''; log.hidden = !log.textContent;
        this.nativeWorkspace = result;
      } catch (error) { link.hidden = true; status.textContent = error.message; this.onError(error); }
      finally {
        busy = false; controls.forEach(item => { item.disabled = false; });
        stop.disabled = !this.nativeWorkspace?.owned || ['stopped', 'failed'].includes(this.nativeWorkspace.status);
      }
    };
    const refresh = button('Check native workspace', () => invoke('get_native_workspace'));
    const start = button('Start selected model', () => invoke('start_native_workspace'));
    const stop = button('Stop native workspace', () => invoke('stop_native_workspace')); stop.disabled = true;
    controls.push(refresh, start, stop); actions.append(refresh, start, stop); section.append(status, actions, link, log);
  }
}
