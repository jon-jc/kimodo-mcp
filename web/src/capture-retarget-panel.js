import { action, safeLocalUrl } from './api.js';
import { MotionViewer } from './viewer.js';
import { CAPTURE_FILES, CaptureGate, captureArtifactUrl, captureBvhOutputs, captureOptions, capturePreparationMatches, captureTarget, sameCaptureTarget, validateCaptureSource } from './capture-retarget-data.js';
import './capture-retarget.css';

const node = (tag, text = '', className = '') => { const value = document.createElement(tag); value.textContent = text; value.className = className; return value; };
const FIELD_NAMES = ['start_frame', 'end_frame', 'source_scale_to_meters', 'source_rest_pose', 'root_motion', 'rotation_smoothing_radius', 'root_smoothing_radius', 'smoothing_start_frame', 'smoothing_end_frame', 'blend_frames'];
const TARGET_NAMES = ['project_file', 'skeleton_path', 'skeletal_mesh_path', 'destination_path', 'asset_name'];
export class CaptureRetargetPanel {
  constructor() {
    this.gate = new CaptureGate(); this.ueGate = new CaptureGate(); this.epoch = 0; this.historyEpoch = 0; this.busy = false;
    this.dialog = document.createElement('dialog'); this.dialog.className = 'capture-retarget-dialog'; this.dialog.setAttribute('aria-labelledby', 'crt-heading');
    this.dialog.innerHTML = `
      <header class="crt-heading"><div><span class="eyebrow">CAPTURE → CHARACTER → UNREAL</span><h2 id="crt-heading">Clean up & retarget</h2><p class="help">Create a separate character animation from a verified FreeMoCap BVH. Originals remain unchanged.</p></div><button id="crt-close" type="button" class="small-button" autofocus>Close</button></header>
      <p id="crt-message" class="crt-message" role="status">Choose a verified exported BVH or recover a saved handoff.</p>
      <details class="crt-history"><summary>Saved character handoffs · shared with agents</summary><div class="crt-actions"><button id="crt-refresh-history" type="button" class="small-button">Refresh handoffs</button><select id="crt-history" aria-label="Saved capture character handoff"><option value="">Choose a handoff</option></select><button id="crt-load-history" type="button" class="small-button">Load selected</button></div><label for="crt-id">Or paste a handoff ID</label><div class="crt-actions"><input id="crt-id" maxlength="64" placeholder="64-character handoff ID" /><button id="crt-load-id" type="button" class="small-button">Recover handoff</button></div><p id="crt-history-note" class="help">History labels are metadata until the handoff is opened and verified.</p></details>
      <div class="crt-workspace"><section class="crt-controls"><h3>1. Source & cleanup</h3><div id="crt-source" class="crt-source">No source selected.</div>
        <form id="crt-form"><div class="crt-fields"><label>Source scale → meters<input id="crt-source_scale_to_meters" type="number" min="0.000001" max="10" step="any" required placeholder="Choose explicitly" /></label><label>Source rest pose<select id="crt-source_rest_pose" required><option value="">Choose export rest pose</option><option value="tpose">T-pose</option><option value="apose">A-pose</option></select></label></div>
        <div class="crt-actions"><button type="button" class="small-button" data-scale="1">Meters · 1</button><button type="button" class="small-button" data-scale="0.01">Centimeters · 0.01</button><button type="button" class="small-button" data-scale="0.001">Millimeters · 0.001</button></div><p class="help">Confirm the BVH export units and rest pose. A scale value cannot recover missing depth in a single-camera planar capture.</p>
        <div class="crt-fields"><label>Original BVH trim start<input id="crt-start_frame" type="number" min="0" max="143998" step="1" value="0" required /></label><label>Trim end · exclusive<input id="crt-end_frame" type="number" min="2" max="144000" step="1" required /></label></div><p class="help">The exported BVH may start with a reference/rest frame and can differ from landmark frame numbering. Inspect and trim deliberately; no first frame is automatically removed.</p><label class="crt-check"><input id="crt-root_motion" type="checkbox" checked />Retain root movement</label>
        <details open><summary>Bounded smoothing</summary><p class="help">Radius is the number of neighboring frames on each side. Zero leaves that channel unchanged. This does not detect foot contacts or guarantee a natural performance.</p><div class="crt-fields"><label>Rotation radius<input id="crt-rotation_smoothing_radius" type="number" min="0" max="5" step="1" value="0" required /></label><label>Root position radius<input id="crt-root_smoothing_radius" type="number" min="0" max="5" step="1" value="0" required /></label><label>Original window start<input id="crt-smoothing_start_frame" type="number" min="0" max="143999" step="1" placeholder="Trim start" /></label><label>Window end · exclusive<input id="crt-smoothing_end_frame" type="number" min="1" max="144000" step="1" placeholder="Trim end" /></label><label>Blend per edge · frames<input id="crt-blend_frames" type="number" min="0" max="30" step="1" value="3" required /></label></div></details>
        <details><summary>Local character joint corrections · up to 16</summary><p class="help">Offsets use local XYZ degrees on the fitted body, within ±45°. Windows use original BVH frame indices. Fingers, face and cloth are not transferred.</p><div id="crt-corrections"></div><button id="crt-add-correction" type="button" class="small-button">Add joint correction</button></details>
        <label class="crt-attempt">Attempt<input id="crt-attempt" type="number" min="0" max="99" step="1" value="0" required /></label><p class="help">Reuse the same verified preparation when unchanged. After an uncertain execution, read its status and inspect Blender before deliberately choosing another attempt.</p>
        <div class="crt-actions"><button id="crt-prepare" type="submit" class="button secondary">Prepare cleanup</button><button id="crt-run" type="button" class="button primary" disabled>Run in Blender</button><button id="crt-status" type="button" class="small-button" disabled>Read saved status</button></div></form>
      </section><section class="crt-results"><h3>2. Review the fitted character</h3><p id="crt-result-state" class="help">Preparing does not run Blender. Execute only when the source scale, pose and cleanup window are correct.</p><div id="crt-viewport" class="crt-viewport"><p id="crt-viewer-note">A verified completed character preview appears here.</p></div><div class="crt-playback"><button id="crt-play" type="button" class="small-button" disabled>Play</button><input id="crt-scrub" type="range" min="0" max="1" step="0.001" value="0" disabled aria-label="Retargeted animation time" /><span id="crt-time">0.00 s</span><button id="crt-fit" type="button" class="small-button">Fit</button></div><div id="crt-downloads" class="download-links"></div><details><summary>Verified handoff evidence</summary><pre id="crt-evidence">No handoff loaded.</pre></details>
        <section class="crt-unreal"><h3>3. Import the reviewed animation into Unreal</h3><p class="help">Use the existing matching character skeleton and mesh. Choose a new animation asset name. Preparation and import are separate explicit actions.</p><button id="crt-defaults" type="button" class="small-button">Load saved Unreal target</button><form id="crt-ue-form"><div class="crt-fields"><label>Project .uproject<input id="crt-ue-project_file" required /></label><label>Skeleton asset<input id="crt-ue-skeleton_path" required placeholder="/Game/Characters/Character_Skeleton" /></label><label>Skeletal mesh<input id="crt-ue-skeletal_mesh_path" required placeholder="/Game/Characters/Character" /></label><label>Destination folder<input id="crt-ue-destination_path" required placeholder="/Game/Animations" /></label><label>New animation name<input id="crt-ue-asset_name" required pattern="[A-Za-z_][A-Za-z0-9_]{0,63}" placeholder="Capture_Take01" /></label></div><div class="crt-actions"><button id="crt-ue-prepare" type="submit" class="button secondary" disabled>Prepare Unreal import</button><button id="crt-ue-run" type="button" class="button primary" disabled>Import into Unreal</button><button id="crt-ue-status" type="button" class="small-button" disabled>Read import report</button></div></form><p id="crt-ue-result" class="help" role="status">Complete and review the character handoff first.</p></section>
      </section></div>`;
    document.body.append(this.dialog);
    this.$('crt-close').addEventListener('click', () => this.dialog.close());
    this.dialog.addEventListener('close', () => { this.epoch++; this.historyEpoch++; this.gate.invalidate(); this.ueGate.invalidate(); this.viewer?.clear(); this.busy = false; });
    this.$('crt-form').addEventListener('input', () => this.invalidate()); this.$('crt-form').addEventListener('change', () => this.invalidate());
    this.$('crt-form').addEventListener('submit', event => { event.preventDefault(); this.prepare(); });
    this.$('crt-run').addEventListener('click', () => this.execute()); this.$('crt-status').addEventListener('click', () => this.readReport());
    this.$('crt-refresh-history').addEventListener('click', () => this.refreshHistory());
    this.$('crt-load-history').addEventListener('click', () => this.recover(this.$('crt-history').value)); this.$('crt-load-id').addEventListener('click', () => this.recover(this.$('crt-id').value.trim()));
    this.$('crt-add-correction').addEventListener('click', () => this.safe(() => { this.addCorrection(); this.invalidate(); }));
    for (const item of this.dialog.querySelectorAll('[data-scale]')) item.addEventListener('click', () => { this.$('crt-source_scale_to_meters').value = item.dataset.scale; this.invalidate(); });
    this.$('crt-defaults').addEventListener('click', () => this.defaults());
    this.$('crt-ue-form').addEventListener('input', () => { this.ueGate.invalidate(); this.updateButtons(); });
    this.$('crt-ue-form').addEventListener('submit', event => { event.preventDefault(); this.prepareUnreal(); });
    this.$('crt-ue-run').addEventListener('click', () => this.executeUnreal()); this.$('crt-ue-status').addEventListener('click', () => this.readUnreal());
    this.$('crt-play').addEventListener('click', () => this.viewer?.togglePlay()); this.$('crt-scrub').addEventListener('input', event => this.viewer?.seek(event.target.value)); this.$('crt-fit').addEventListener('click', () => this.viewer?.fit());
  }
  $(id) { return this.dialog.querySelector(`#${id}`); }
  message(text, error = false) { this.$('crt-message').textContent = text; this.$('crt-message').classList.toggle('error', error); }
  async safe(task, epoch = this.epoch) { try { return await task(); } catch (error) { if (this.dialog.open && epoch === this.epoch) this.message(error.message, true); } }
  invalidate() { this.gate.invalidate(); this.ueGate.invalidate(); this.updateButtons(); }
  fields() { return Object.fromEntries(FIELD_NAMES.map(name => { const input = this.$(`crt-${name}`); return [name, input.type === 'checkbox' ? input.checked : input.value]; })); }
  corrections() { return [...this.$('crt-corrections').children].map(row => ({ bone: row.querySelector('[data-field=bone]').value, rotation_degrees: ['x', 'y', 'z'].map(name => row.querySelector(`[data-field=${name}]`).value), ...Object.fromEntries(['start_frame', 'end_frame', 'blend_frames'].map(name => [name, row.querySelector(`[data-field=${name}]`).value])) })); }
  input() { const attempt = Number(this.$('crt-attempt').value); if (!Number.isInteger(attempt) || attempt < 0 || attempt > 99 || this.$('crt-attempt').value === '') throw new Error('Choose an attempt from 0 to 99.'); return { job_id: this.source?.job_id, artifact_path: this.source?.artifact_path, options: captureOptions(this.fields(), this.source, this.corrections()), attempt }; }
  ueInput() { if (this.report?.status !== 'completed') throw new Error('A verified completed character is required.'); return { handoff_id: this.report.handoff_id, target: captureTarget(Object.fromEntries(TARGET_NAMES.map(name => [name, this.$(`crt-ue-${name}`).value]))) }; }
  async open(job, artifactPath) {
    if (!this.dialog.open) this.dialog.showModal();
    const epoch = ++this.epoch; this.busy = false; this.source = null; this.report = null; this.invalidate(); this.viewer?.clear(); this.$('crt-downloads').replaceChildren(); this.$('crt-corrections').replaceChildren(); this.$('crt-source').textContent = 'Inspecting the selected exported BVH…';
    this.$('crt-evidence').textContent = 'No handoff loaded.'; this.$('crt-result-state').textContent = 'Source inspection only. No editor has been run.';
    this.refreshHistory();
    await this.safe(async () => {
      const artifact = captureBvhOutputs(job).find(item => item.path === artifactPath); if (!artifact) throw new Error('Choose a BVH listed in a verified completed FreeMoCap export receipt.');
      const selection = { job_id: job.job_id, artifact_path: artifact.path, sha256: artifact.sha256 };
      const result = await action('inspect_freemocap_retarget_source', { job_id: selection.job_id, artifact_path: selection.artifact_path });
      if (epoch !== this.epoch || !this.dialog.open) return;
      this.source = validateCaptureSource(result, selection); this.populate({ start_frame: 0, end_frame: Math.min(2400, result.source_frames), source_scale_to_meters: '', source_rest_pose: '', root_motion: true, rotation_smoothing_radius: 0, root_smoothing_radius: 0, smoothing_start_frame: '', smoothing_end_frame: '', blend_frames: 3, joint_corrections: [] }); this.$('crt-attempt').value = 0;
      this.renderSource(); this.message('Source verified. Confirm its scale and rest pose, choose cleanup, then Prepare.'); this.updateButtons();
    });
  }
  async openHistory() {
    if (!this.dialog.open) this.dialog.showModal(); this.epoch++; this.source = null; this.report = null; this.busy = false; this.invalidate(); this.viewer?.clear();
    this.$('crt-source').textContent = 'Choose a saved handoff to verify and resume.'; this.$('crt-downloads').replaceChildren(); this.$('crt-result-state').textContent = 'No handoff selected.';
    this.dialog.querySelector('.crt-history').open = true; this.message('Open an existing handoff or paste its ID. Reading history runs no editor.'); await this.refreshHistory();
  }
  renderSource() {
    const box = this.$('crt-source'); box.replaceChildren(); if (!this.source) return;
    box.append(node('strong', this.source.artifact_path), node('p', `Receipt ${this.source.job_id} · ${this.source.source_frames} original BVH frames · ${this.source.fps} fps`, 'help'), node('p', `SHA256 ${this.source.source_sha256}`, 'help'));
    for (const note of this.source.notes ?? []) box.append(node('p', note, 'help'));
  }
  populate(options) {
    for (const name of FIELD_NAMES) { const input = this.$(`crt-${name}`); if (input.type === 'checkbox') input.checked = options[name]; else input.value = options[name] ?? ''; }
    this.$('crt-corrections').replaceChildren(); for (const correction of options.joint_corrections ?? []) this.addCorrection(correction);
  }
  addCorrection(value = {}) {
    if (!this.source) throw new Error('Inspect a source first.'); if (this.$('crt-corrections').children.length >= 16) throw new Error('Keep at most 16 joint corrections.');
    const row = node('div', '', 'crt-correction'), bone = document.createElement('select'); bone.dataset.field = 'bone'; bone.setAttribute('aria-label', 'Target character bone');
    for (const name of Object.keys(this.source.mapping)) bone.append(new Option(name, name)); if (value.bone) bone.value = value.bone;
    const label = node('label', 'Character bone'); label.append(bone); row.append(label);
    for (const [name, title, initial] of [['x', 'Local X · °', value.rotation_degrees?.[0] ?? 0], ['y', 'Local Y · °', value.rotation_degrees?.[1] ?? 0], ['z', 'Local Z · °', value.rotation_degrees?.[2] ?? 0], ['start_frame', 'Original start', value.start_frame ?? this.$('crt-start_frame').value], ['end_frame', 'End · exclusive', value.end_frame ?? this.$('crt-end_frame').value], ['blend_frames', 'Blend frames', value.blend_frames ?? 3]]) {
      const wrap = node('label', title), input = document.createElement('input'); input.type = 'number'; input.required = true; input.dataset.field = name; input.value = initial; input.step = ['x', 'y', 'z'].includes(name) ? 'any' : '1'; input.min = ['x', 'y', 'z'].includes(name) ? '-45' : '0'; input.max = ['x', 'y', 'z'].includes(name) ? '45' : name === 'blend_frames' ? '30' : '144000'; wrap.append(input); row.append(wrap);
    }
    const remove = node('button', 'Remove correction', 'small-button'); remove.type = 'button'; remove.addEventListener('click', () => { row.remove(); this.invalidate(); }); row.append(remove); this.$('crt-corrections').append(row);
  }
  updateButtons() {
    for (const form of [this.$('crt-form'), this.$('crt-ue-form')]) for (const control of form.querySelectorAll('input, select, button')) control.disabled = this.busy;
    let prepared = null, unreal = null; try { prepared = this.gate.get(this.input()); } catch { /* Incomplete fields keep execution disabled. */ } try { unreal = this.ueGate.get(this.ueInput()); } catch { /* Incomplete target. */ }
    this.$('crt-prepare').disabled = this.busy || !this.source;
    this.$('crt-run').disabled = this.busy || !prepared || prepared.status !== 'prepared' || this.uncertainId === prepared.handoff_id;
    this.$('crt-status').disabled = this.busy || !this.report;
    this.$('crt-ue-prepare').disabled = this.busy || this.report?.status !== 'completed';
    this.$('crt-ue-run').disabled = this.busy || !unreal || this.uncertainUE === JSON.stringify(this.ueInput());
    this.$('crt-ue-status').disabled = this.busy || this.report?.status !== 'completed';
  }
  async prepare() {
    if (this.busy || !this.validForm(this.$('crt-form'))) return;
    await this.safe(async () => {
      const input = this.input(), source = structuredClone(this.source), epoch = this.epoch, ticket = this.gate.begin(input); this.busy = true; this.updateButtons(); this.message('Preparing a checked, immutable cleanup handoff. Blender is not running yet.');
      try {
        const report = await action('prepare_freemocap_character', input);
        if (!this.dialog.open || epoch !== this.epoch) return;
        if (!capturePreparationMatches(report, source, input)) throw new Error('Prepared handoff does not match the inspected source and exact cleanup settings.');
        if (!this.gate.accept(ticket, this.input(), report)) { this.message('The earlier cleanup snapshot was prepared. Your newer changes remain unsaved; recover the saved handoff from history.'); return; }
        this.adoptReport(report); this.message(report.status === 'completed' ? 'The matching completed handoff was verified and reused. No editor was run.' : 'Cleanup prepared. Run in Blender is a separate action.'); await this.refreshHistory(report.handoff_id);
      } finally { if (epoch === this.epoch) { this.busy = false; this.updateButtons(); } }
    });
  }
  async execute() {
    await this.safe(async () => {
      const input = this.input(), prepared = this.gate.get(input); if (this.busy || prepared?.status !== 'prepared' || this.uncertainId === prepared.handoff_id) throw new Error('Prepare the current settings first, or read the existing dispatch result.');
      const id = prepared.handoff_id, epoch = this.epoch; this.busy = true; this.uncertainId = id; this.updateButtons(); this.message('Running the prepared cleanup and retarget script in connected Blender…');
      try {
        const report = await action('execute_freemocap_character', { handoff_id: id });
        if (!this.dialog.open || epoch !== this.epoch || this.report?.handoff_id !== id) return;
        this.adoptReport(report); this.message(report.status === 'completed' ? 'Character output verified. Review the motion before importing into Unreal.' : `Execution result: ${report.status}. Read status before another attempt.`);
      } catch (error) { if (epoch === this.epoch) this.message(`${error.message} Read saved status before retrying; the editor may still be working.`, true); }
      finally { if (epoch === this.epoch) { this.busy = false; this.updateButtons(); } }
    });
  }
  async readReport() { if (this.report) await this.recover(this.report.handoff_id, false); }
  async recover(id, replaceFields = true) {
    if (!/^[a-f0-9]{64}$/.test(id ?? '') || this.busy) { this.message('Enter a valid handoff ID, and wait for the active operation to finish.', true); return; }
    const epoch = ++this.epoch; this.busy = true; this.updateButtons();
    await this.safe(async () => {
      try {
        const report = await action('get_freemocap_character', { handoff_id: id });
        if (epoch !== this.epoch || !this.dialog.open) return;
        if (report.handoff_id !== id || report.request?.handoff_id !== id) throw new Error('Recovered report identity does not match the selected handoff.');
        const request = report.request;
        if (replaceFields) { this.source = { ...request, source_frames: request.source_frames }; this.populate(request.options); this.$('crt-attempt').value = request.attempt; this.renderSource(); }
        if (report.status === 'prepared') this.uncertainId = null;
        try { if (replaceFields || capturePreparationMatches(report, this.source, this.input())) { const input = this.input(), ticket = this.gate.begin(input); this.gate.accept(ticket, input, report); } } catch { this.gate.invalidate(); }
        this.adoptReport(report); this.message(`Verified saved handoff: ${report.status}. ${report.note ?? report.error ?? ''}`);
      } finally { if (epoch === this.epoch) { this.busy = false; this.updateButtons(); } }
    });
  }
  adoptReport(report) {
    this.report = report; this.$('crt-id').value = report.handoff_id; this.ueGate.invalidate();
    this.$('crt-result-state').textContent = `${report.status} · ${report.handoff_id}${report.frames ? ` · ${report.frames} frames at ${report.fps} fps` : ''}`;
    this.$('crt-evidence').textContent = JSON.stringify(report, null, 2); this.$('crt-downloads').replaceChildren(); this.viewer?.clear();
    this.$('crt-play').disabled = true; this.$('crt-scrub').disabled = true; this.$('crt-viewer-note').hidden = false; this.$('crt-viewer-note').textContent = 'A verified completed character preview appears here.';
    if (report.status !== 'completed') return;
    for (const file of report.outputs ?? []) if (Object.hasOwn(CAPTURE_FILES, file.path)) { const link = node('a', CAPTURE_FILES[file.path]); link.href = captureArtifactUrl(report.handoff_id, file.path); link.download = file.path; this.$('crt-downloads').append(link); }
    const preview = report.outputs?.find(item => item.path === 'character-preview.glb');
    if (preview) {
      try {
        this.viewer ??= new MotionViewer(this.$('crt-viewport'), { onProgress: text => { this.$('crt-viewer-note').textContent = text; }, onLoaded: () => { this.$('crt-viewer-note').hidden = true; }, onError: error => { this.$('crt-viewer-note').hidden = false; this.$('crt-viewer-note').textContent = error.message; }, onTime: (time, total, playing) => { this.$('crt-time').textContent = `${time.toFixed(2)} / ${total.toFixed(2)} s`; this.$('crt-play').textContent = playing ? 'Pause' : 'Play'; this.$('crt-play').disabled = !total; this.$('crt-scrub').disabled = !total; this.$('crt-scrub').max = total || 1; this.$('crt-scrub').value = time; } });
        this.viewer.load(safeLocalUrl(captureArtifactUrl(report.handoff_id, preview.path)));
      } catch (error) { this.$('crt-viewer-note').textContent = error.message; }
    }
  }
  async refreshHistory(preferred = null) {
    const ticket = ++this.historyEpoch, previous = this.$('crt-history').value;
    await this.safe(async () => {
      const result = await action('list_freemocap_character_handoffs', { limit: 100 });
      if (ticket !== this.historyEpoch || !this.dialog.open) return;
      this.$('crt-history').replaceChildren(new Option('Choose a saved handoff', ''));
      for (const item of result.handoffs ?? []) this.$('crt-history').append(new Option(`${item.handoff_id.slice(0, 10)} · ${item.recorded_status} · source ${item.job_id?.slice(0, 8)}`, item.handoff_id));
      this.$('crt-history').value = preferred ?? previous; this.$('crt-history-note').textContent = `${result.handoffs?.length ?? 0} saved handoff(s)${result.partial ? ' · latest results only; older IDs can be pasted below' : ''}. Open one to verify its current files.`;
    });
  }
  async defaults() {
    const epoch = this.epoch, before = JSON.stringify(TARGET_NAMES.map(name => this.$(`crt-ue-${name}`).value));
    await this.safe(async () => { const result = await action('get_unreal_target', {}); if (!this.dialog.open || epoch !== this.epoch || before !== JSON.stringify(TARGET_NAMES.map(name => this.$(`crt-ue-${name}`).value))) return;
      if (!result.configured || !result.target) throw new Error('No saved Unreal target is configured. Enter the intended existing project and character assets.');
      for (const name of TARGET_NAMES) this.$(`crt-ue-${name}`).value = name === 'asset_name' ? '' : result.target[name] ?? '';
      this.ueGate.invalidate(); this.updateButtons(); this.$('crt-ue-result').textContent = 'Saved target loaded. Enter a new animation asset name before preparing.';
    });
  }
  async prepareUnreal() {
    if (this.busy || !this.validForm(this.$('crt-ue-form'))) return;
    await this.safe(async () => { const input = this.ueInput(), ticket = this.ueGate.begin(input), epoch = this.epoch; this.busy = true; this.updateButtons();
      try { const result = await action('prepare_freemocap_unreal', input); if (!this.dialog.open || epoch !== this.epoch) return;
        if (result.handoff_id !== input.handoff_id || !sameCaptureTarget(result.target, input.target)) throw new Error('Unreal preparation does not match this character and target.');
        if (!this.ueGate.accept(ticket, this.ueInput(), result)) throw new Error('The Unreal target changed. Prepare its current values again.'); this.$('crt-ue-result').textContent = 'Import prepared. Unreal has not been changed. Confirm the target and use Import into Unreal when ready.';
      } finally { if (epoch === this.epoch) { this.busy = false; this.updateButtons(); } }
    });
  }
  async executeUnreal() {
    await this.safe(async () => { const input = this.ueInput(); if (this.busy || !this.ueGate.get(input) || this.uncertainUE === JSON.stringify(input)) throw new Error('Prepare this exact target first, or read the existing import report.');
      const epoch = this.epoch; this.busy = true; this.uncertainUE = JSON.stringify(input); this.updateButtons();
      try { const result = await action('execute_freemocap_unreal', input); if (!this.dialog.open || epoch !== this.epoch) return; this.$('crt-ue-result').textContent = result.status === 'completed' ? `Import verified: ${result.animation_asset ?? input.target.asset_name}` : `${result.status}. Read the saved import report before retrying.`;
      } catch (error) { if (epoch === this.epoch) this.$('crt-ue-result').textContent = `${error.message} The outcome may be uncertain; read the import report before retrying.`; }
      finally { if (epoch === this.epoch) { this.busy = false; this.updateButtons(); } }
    });
  }
  async readUnreal() {
    await this.safe(async () => { const input = this.ueInput(), epoch = this.epoch; const result = await action('get_freemocap_unreal', input); if (!this.dialog.open || epoch !== this.epoch || JSON.stringify(input) !== JSON.stringify(this.ueInput())) return;
      this.$('crt-ue-result').textContent = `${result.status} · ${result.animation_asset ?? result.recovery ?? input.target.asset_name}`;
      if (result.status === 'not_executed') this.uncertainUE = null; this.updateButtons();
    });
  }
  validForm(form) {
    for (const input of form.querySelectorAll('input,select')) if (!input.disabled && !input.checkValidity()) { for (let parent = input.parentElement; parent && parent !== form; parent = parent.parentElement) if (parent.tagName === 'DETAILS') parent.open = true; input.reportValidity(); return false; }
    return form.reportValidity();
  }
}
