import { action, safeLocalUrl } from './api.js';
import { PlanGate } from './sequence.js';
import { MotionViewer } from './viewer.js';
import { UNIRIG_DEFAULTS, UNIRIG_OPERATIONS, canCancelUniRig, uniRigArtifactUrl, uniRigJobOption, uniRigPlanMatches, uniRigRegistration, uniRigRequest, uniRigRuntimeMessage, verifiedUniRigOutputs } from './unirig-data.js';
import './unirig.css';

const node = (tag, text = '', className = '') => { const result = document.createElement(tag); result.textContent = text; result.className = className; return result; };
const ACTIVE = ['queued', 'running', 'cancelling'];
export class UniRigPanel {
  constructor() {
    this.gate = new PlanGate(); this.assets = []; this.jobs = new Map(); this.epoch = 0; this.readEpoch = 0; this.historyEpoch = 0; this.selectionEpoch = 0; this.busy = false;
    this.dialog = document.createElement('dialog'); this.dialog.className = 'unirig-dialog'; this.dialog.setAttribute('aria-labelledby', 'uni-heading');
    this.dialog.innerHTML = `
      <header class="uni-heading"><div><span class="eyebrow">MESH → SKELETON → SKIN</span><h2 id="uni-heading">Automatic rigging · UniRig</h2><p class="help">Run the released rigging stages on registered local assets. Plan first; start only when the local runtime is configured.</p></div><button id="uni-close" type="button" class="small-button" autofocus>Close</button></header>
      <p id="uni-message" class="uni-message" role="status">Opening this panel does not start inference or download weights.</p>
      <div class="uni-layout"><section><h3>1. Choose assets & stages</h3><form id="uni-form"><label>Operation<select id="uni-operation"></select></label><p id="uni-operation-note" class="help"></p><div id="uni-assets"></div><div class="uni-actions"><button id="uni-add-asset" type="button" class="small-button">Add mesh · max 4</button><button id="uni-refresh-assets" type="button" class="small-button">Refresh asset library</button></div><p class="help">Register a self-contained GLB, FBX, OBJ or VRM in the asset library first, or paste its asset ID. External texture sidecars are not bundled. Up to 512 MiB per asset and 1 GiB total per batch.</p>
        <div class="uni-fields"><label>Seed<input id="uni-seed" type="number" min="0" max="4294967295" step="1" value="12345" required /></label><label>Target face count<input id="uni-faces_target_count" type="number" min="100" max="100000" step="1" value="50000" required /></label></div>
        <details class="uni-options" open><summary>Stage settings</summary><fieldset data-operations="skeleton pipeline"><legend>Skeleton decoding</legend><label class="uni-check"><input id="uni-do_sample" type="checkbox" checked />Sample skeleton predictions</label><label>Maximum generated tokens<input id="uni-max_new_tokens" type="number" min="1" max="2048" step="1" value="2048" required /></label><p class="help">A lower token cap may truncate or fail a skeleton; it does not set a bone count.</p><div class="uni-fields"><label>Search beams<input id="uni-num_beams" type="number" min="1" max="30" step="1" value="15" required /></label><label>Top K<input id="uni-top_k" type="number" min="1" max="100" step="1" value="5" required /></label><label>Top P<input id="uni-top_p" type="number" min="0.000001" max="1" step="any" value="0.95" required /></label><label>Temperature<input id="uni-temperature" type="number" min="0.000001" max="5" step="any" value="1.5" required /></label><label>Repetition penalty<input id="uni-repetition_penalty" type="number" min="0.1" max="10" step="any" value="3" required /></label></div></fieldset><fieldset data-operations="skin pipeline"><legend>Skinning</legend><label>Geodesic calculation<select id="uni-skin_backend"><option value="pyrender">pyrender</option><option value="open3d">Open3D</option></select></label></fieldset><fieldset data-operations="merge pipeline"><legend>Merged output</legend><label>Output format<select id="uni-output_format"><option value="glb">GLB · browser inspection</option><option value="fbx">FBX · editor handoff file</option></select></label><label class="uni-check"><input id="uni-add_root" type="checkbox" />Add a root bone during merge</label></fieldset></details>
        <div class="uni-actions"><button id="uni-plan" type="submit" class="button secondary">Check rigging plan</button><button id="uni-start" type="button" class="button primary" disabled>Start checked rigging</button></div></form>
        <details class="uni-options"><summary>Runtime & released capabilities</summary><button id="uni-capabilities" type="button" class="small-button">Refresh capability checks</button><div id="uni-runtime"></div><div id="uni-boundaries"></div></details>
      </section><section><h3>2. Review plan & progress</h3><div id="uni-plan-result" class="uni-plan-result"><p class="help">A plan checks source identities and local package configuration. It does not prove CUDA, weight access or available memory.</p></div>
        <section class="uni-history"><div class="uni-actions"><h3>Shared rigging jobs</h3><button id="uni-refresh-jobs" type="button" class="small-button">Refresh jobs</button></div><label>Saved job<select id="uni-jobs"><option value="">Choose a job</option></select></label><div class="uni-actions"><input id="uni-job-id" maxlength="32" aria-label="UniRig job ID from another agent" placeholder="Or paste a job ID" /><button id="uni-load-job" type="button" class="small-button">Load job</button></div><p id="uni-job-state" class="help" role="status">No job selected.</p><div class="uni-actions"><button id="uni-read-job" type="button" class="small-button" disabled>Read saved status</button><button id="uni-cancel" type="button" class="small-button" disabled>Cancel selected job</button><button id="uni-log" type="button" class="small-button" disabled>Read log</button><button id="uni-reuse" type="button" class="small-button" disabled>Load request as new draft</button></div><pre id="uni-log-output" hidden></pre></section>
        <section class="uni-results"><h3>3. Inspect verified output</h3><p class="help">UniRig produces a static rig, not animation or an automatic SOMA/Unreal character mapping. Review the rest pose, bone layout, scale and deformation before retargeting.</p><label>Merged GLB preview<select id="uni-preview"><option value="">No verified GLB output</option></select></label><div id="uni-viewport" class="uni-viewport"><p id="uni-viewer-note">Completed GLB outputs can be inspected here.</p></div><div class="uni-actions"><button id="uni-fit" type="button" class="small-button">Fit view</button><button id="uni-bones" type="button" class="small-button" aria-pressed="true">Show bones</button></div><div id="uni-downloads" class="download-links"></div><details class="uni-options"><summary>Verified receipt</summary><pre id="uni-receipt">No job selected.</pre></details></section>
      </section></div>`;
    document.body.append(this.dialog);
    const registration = node('fieldset', '', 'uni-options');
    registration.innerHTML = '<legend>Keep a rigged result in the asset library</legend><label>Verified GLB / FBX result<select id="uni-register-file"></select></label><div class="uni-fields"><label>Library label<input id="uni-register-label" maxlength="160" placeholder="Character rig · reviewed" /></label><label>Asset kind<select id="uni-register-kind"><option value="character">Character</option><option value="prop">Prop</option></select></label></div><button id="uni-register" type="button" class="small-button" disabled>Register selected result</button><p class="help">This copies a verified file into the shared library. It does not replace the current animation character or create a retarget mapping.</p>';
    this.$('uni-downloads').after(registration);
    this.$('uni-register').addEventListener('click', () => this.registerOutput());
    for (const [value, [title]] of Object.entries(UNIRIG_OPERATIONS)) this.$('uni-operation').append(new Option(title, value));
    this.$('uni-close').addEventListener('click', () => this.dialog.close());
    this.dialog.addEventListener('close', () => { this.epoch++; this.readEpoch++; this.historyEpoch++; this.selectionEpoch++; this.gate.invalidate(); clearInterval(this.poll); this.viewer?.clear(); this.busy = false; });
    this.$('uni-form').addEventListener('input', () => this.invalidate());
    this.$('uni-form').addEventListener('change', () => { this.applicability(); this.invalidate(); });
    this.$('uni-form').addEventListener('submit', event => { event.preventDefault(); this.plan(); });
    this.$('uni-start').addEventListener('click', () => this.start());
    this.$('uni-add-asset').addEventListener('click', () => this.safe(() => { this.addAsset(); this.invalidate(); }));
    this.$('uni-refresh-assets').addEventListener('click', () => this.refreshAssets()); this.$('uni-capabilities').addEventListener('click', () => this.capabilities());
    this.$('uni-refresh-jobs').addEventListener('click', () => this.refreshJobs()); this.$('uni-jobs').addEventListener('change', () => this.selectJob(this.$('uni-jobs').value));
    this.$('uni-load-job').addEventListener('click', () => this.selectJob(this.$('uni-job-id').value.trim())); this.$('uni-read-job').addEventListener('click', () => this.loadJob(this.selected));
    this.$('uni-cancel').addEventListener('click', () => this.cancel()); this.$('uni-log').addEventListener('click', () => this.log()); this.$('uni-reuse').addEventListener('click', () => this.reuse());
    this.$('uni-preview').addEventListener('change', () => this.preview()); this.$('uni-fit').addEventListener('click', () => this.viewer?.fit());
    this.$('uni-bones').addEventListener('click', () => { const visible = this.$('uni-bones').getAttribute('aria-pressed') !== 'true'; this.$('uni-bones').setAttribute('aria-pressed', String(visible)); this.viewer?.setSkeleton(visible); });
    this.addAsset(); this.applicability();
  }
  $(id) { return this.dialog.querySelector(`#${id}`); }
  message(text, error = false) { this.$('uni-message').textContent = text; this.$('uni-message').classList.toggle('error', error); }
  async safe(task, epoch = this.epoch) { try { return await task(); } catch (error) { if (this.dialog.open && epoch === this.epoch) this.message(error.message, true); } }
  async open(assetId = null) {
    if (!this.dialog.open) this.dialog.showModal(); const epoch = ++this.epoch;
    if (assetId) { const first = this.$('uni-assets').firstElementChild; first.querySelector('[data-field=asset_id]').value = assetId; this.invalidate(); }
    this.message('Plan and status checks run no rigging. Starting a stage is explicit.');
    await Promise.allSettled([this.capabilities(), this.refreshAssets(), this.refreshJobs()]);
    if (!this.dialog.open || epoch !== this.epoch) return;
    clearInterval(this.poll); this.poll = setInterval(() => { if (this.dialog.open && !document.hidden && !this.busy && ACTIVE.includes(this.currentJob?.status)) this.loadJob(this.selected); }, 4000);
    this.buttons();
  }
  fields() { return Object.fromEntries(['operation', ...Object.keys(UNIRIG_DEFAULTS)].map(name => { const input = this.$(`uni-${name}`); return [name, input.type === 'checkbox' ? input.checked : input.value]; })); }
  rows() { return [...this.$('uni-assets').children].map(row => Object.fromEntries([...row.querySelectorAll('[data-field]')].map(input => [input.dataset.field, input.value]))); }
  input() { return uniRigRequest(this.fields(), this.rows()); }
  invalidate() { this.gate.invalidate(); this.$('uni-plan-result').classList.add('stale'); this.buttons(); }
  applicability() {
    const operation = this.$('uni-operation').value;
    this.$('uni-operation-note').textContent = UNIRIG_OPERATIONS[operation][1];
    for (const fieldset of this.dialog.querySelectorAll('[data-operations]')) { const active = fieldset.dataset.operations.split(' ').includes(operation); fieldset.hidden = !active; fieldset.disabled = !active; }
    for (const row of this.$('uni-assets').children) {
      const rig = row.querySelector('.uni-rig-source'), active = ['skin', 'merge'].includes(operation); rig.hidden = !active; rig.disabled = !active;
      const kind = row.querySelector('[data-field=rig_source]').value;
      for (const group of row.querySelectorAll('[data-rig-kind]')) { group.hidden = !active || kind !== group.dataset.rigKind; group.disabled = !active || kind !== group.dataset.rigKind; }
    }
  }
  addAsset(value = {}) {
    if (this.$('uni-assets').children.length >= 4) throw new Error('A UniRig batch supports at most four meshes.');
    const row = node('div', '', 'uni-asset-row');
    row.innerHTML = `<label>Registered mesh<select data-library><option value="">Choose from the asset library</option></select></label><label>Mesh asset ID<input data-field="asset_id" maxlength="64" required pattern="[a-f0-9]{64}" placeholder="Registered asset ID" /></label><fieldset class="uni-rig-source"><legend>Existing rig source</legend><label>Use<select data-field="rig_source"><option value="self">The mesh’s existing rig · skinning only</option><option value="asset">Another registered rig asset</option><option value="job">A completed UniRig job output</option></select></label><fieldset data-rig-kind="asset"><label>Rig asset ID<input data-field="rig_asset_id" maxlength="64" required pattern="[a-f0-9]{64}" /></label></fieldset><fieldset data-rig-kind="job"><label>Completed job ID<input data-field="source_job_id" maxlength="32" required pattern="[a-f0-9]{32}" /></label><div class="uni-fields"><label>Source item index<input data-field="source_item_index" type="number" min="0" max="3" step="1" value="0" required /></label><label>Completed output<select data-field="rig_output"><option value="skeleton">Skeleton · no skin weights</option><option value="skin">Skin · predicted weights</option></select></label></div></fieldset></fieldset>`;
    for (const input of row.querySelectorAll('[data-field]')) if (value[input.dataset.field] !== undefined && value[input.dataset.field] !== null) input.value = value[input.dataset.field];
    row.querySelector('[data-field=rig_source]').value = value.rig_asset_id ? 'asset' : value.source_job_id ? 'job' : 'self';
    row.querySelector('[data-library]').addEventListener('change', event => { row.querySelector('[data-field=asset_id]').value = event.target.value; this.invalidate(); });
    const remove = node('button', 'Remove mesh', 'small-button'); remove.type = 'button'; remove.addEventListener('click', () => { if (this.$('uni-assets').children.length < 2) { this.message('Keep at least one mesh.', true); return; } row.remove(); this.invalidate(); }); row.append(remove);
    this.$('uni-assets').append(row); this.renderAssetOptions(row); this.applicability();
  }
  renderAssetOptions(row) {
    const select = row.querySelector('[data-library]'), selected = row.querySelector('[data-field=asset_id]').value;
    select.replaceChildren(new Option('Choose from the asset library', ''));
    for (const asset of this.assets.filter(item => ['glb', 'fbx', 'obj', 'vrm'].includes(String(item.format).replace(/^\./, '').toLowerCase()))) select.append(new Option(`${asset.label || asset.source_name || asset.asset_id.slice(0, 8)} · ${asset.format}`, asset.asset_id));
    select.value = selected;
  }
  async refreshAssets() {
    const epoch = this.epoch;
    await this.safe(async () => { const result = await action('list_studio_assets', { limit: 100, offset: 0 }); if (!this.dialog.open || epoch !== this.epoch) return; this.assets = result.assets ?? []; for (const row of this.$('uni-assets').children) this.renderAssetOptions(row); });
  }
  async capabilities() {
    const epoch = this.epoch;
    await this.safe(async () => { const result = await action('get_unirig_capabilities', {}); if (!this.dialog.open || epoch !== this.epoch) return; this.renderRuntime(result.runtime); this.$('uni-boundaries').replaceChildren(); for (const text of result.boundaries ?? []) this.$('uni-boundaries').append(node('p', text, 'help')); });
  }
  renderRuntime(runtime) {
    const box = this.$('uni-runtime'); box.replaceChildren(node('p', uniRigRuntimeMessage(runtime), 'help'));
    for (const check of runtime?.checks ?? []) box.append(node('p', `${check.ready ? 'Available' : 'Missing'} · ${check.name}: ${check.detail}`, check.ready ? 'help' : 'uni-warning'));
    const missing = Object.entries(runtime?.modules ?? {}).filter(([, ready]) => !ready).map(([name]) => name);
    if (missing.length) box.append(node('p', `Missing packages: ${missing.join(', ')}`, 'uni-warning'));
    box.append(node('p', `Downloads ${runtime?.downloads_allowed ? 'allowed by operator settings' : 'disabled by operator settings'}. No installation or download runs from this check.`, 'help'));
  }
  buttons() {
    let plan; try { plan = this.gate.current(this.input()); } catch { /* Incomplete draft. */ }
    this.$('uni-start').disabled = this.busy || plan?.ready !== true; this.$('uni-plan').disabled = this.busy;
    this.$('uni-cancel').disabled = this.busy || !canCancelUniRig(this.currentJob);
    for (const id of ['uni-read-job', 'uni-log', 'uni-reuse']) this.$(id).disabled = this.busy || !this.currentJob;
  }
  validForm() {
    for (const input of this.$('uni-form').querySelectorAll('input,select')) if (!input.matches(':disabled') && !input.checkValidity()) { for (let parent = input.parentElement; parent; parent = parent.parentElement) if (parent.tagName === 'DETAILS') parent.open = true; input.reportValidity(); return false; }
    return this.$('uni-form').reportValidity();
  }
  async plan() {
    if (this.busy || !this.validForm()) return;
    await this.safe(async () => { const input = this.input(), ticket = this.gate.begin(input), epoch = this.epoch; this.busy = true; this.buttons(); this.message('Checking registered sources and the configured UniRig runtime. No rigging stage is running.');
      try { const result = await action('plan_unirig', { request: input }); if (!this.dialog.open || epoch !== this.epoch) return;
        if (!uniRigPlanMatches(result, input)) throw new Error('The plan does not match the exact selected mesh identities and settings.');
        if (!this.gate.accept(ticket, this.input(), result)) throw new Error('The draft changed while checking. Review the current plan again.');
        const box = this.$('uni-plan-result'); box.replaceChildren(node('strong', result.ready ? 'Configuration checks passed · start remains explicit' : 'Setup required before rigging')); box.classList.remove('stale');
        box.append(node('p', `Stages: ${(result.stages ?? []).join(' → ')}`, 'help'));
        for (const item of result.assets) box.append(node('p', `Item ${item.item_index}: mesh ${item.mesh.asset_id.slice(0, 12)} · SHA256 ${item.mesh.sha256.slice(0, 12)}${item.rig ? ' · explicit rig source verified' : ''}`, 'help'));
        for (const note of result.notes ?? []) box.append(node('p', note, 'help')); this.renderRuntime(result.runtime); this.message(result.ready ? 'Plan checked. Starting runs a separate local rigging process; inspect the resulting rig afterward.' : 'The plan identified missing setup. Review Runtime & released capabilities; no stage was started.');
      } finally { if (epoch === this.epoch) { this.busy = false; this.buttons(); } }
    });
  }
  async start() {
    await this.safe(async () => { const plan = this.gate.current(this.input()); if (this.busy || plan?.ready !== true) throw new Error('Check this exact rigging plan and resolve setup requirements before starting.');
      const epoch = this.epoch, selection = this.selectionEpoch; this.busy = true; this.gate.invalidate(); this.buttons(); this.message('Submitting the checked request. Do not retry automatically if the response is uncertain.');
      try { const result = await action('start_unirig', { request: plan.request }); if (!this.dialog.open || epoch !== this.epoch) return;
        uniRigJobOption(result); this.jobs.set(result.id, result); this.renderJobs(); this.message(`Rigging job submitted: ${result.id}. Completion still requires verified outputs.`);
        if (selection === this.selectionEpoch) await this.selectJob(result.id);
      } catch (error) { if (epoch === this.epoch) this.message(`${error.message} Refresh shared jobs before starting a new attempt.`, true); }
      finally { if (epoch === this.epoch) { this.busy = false; this.buttons(); } }
    });
  }
  async refreshJobs() {
    const ticket = ++this.historyEpoch, epoch = this.epoch;
    await this.safe(async () => { const result = await action('list_unirig_jobs', { limit: 40 }); if (!this.dialog.open || epoch !== this.epoch || ticket !== this.historyEpoch) return;
      const kept = this.currentJob; this.jobs = new Map((result.jobs ?? []).map(job => [job.id, job])); if (kept && !this.jobs.has(kept.id)) this.jobs.set(kept.id, kept); this.renderJobs();
    });
  }
  renderJobs() {
    const select = this.$('uni-jobs'); select.replaceChildren(new Option('Choose a job', ''));
    for (const job of this.jobs.values()) { const item = uniRigJobOption(job); select.append(new Option(item.label, item.value)); }
    select.value = this.selected ?? '';
  }
  async selectJob(id) { this.selectionEpoch++; this.selected = id; this.currentJob = null; this.viewer?.clear(); this.$('uni-downloads').replaceChildren(); this.$('uni-preview').replaceChildren(new Option('No verified GLB output', '')); this.$('uni-register-file').replaceChildren(); this.$('uni-register').disabled = true; this.$('uni-log-output').hidden = true; this.$('uni-log-output').textContent = ''; this.$('uni-receipt').textContent = 'Loading selected receipt…'; this.$('uni-job-state').textContent = 'Loading selected job…'; this.buttons(); await this.loadJob(id); }
  async loadJob(id) {
    if (!/^[a-f0-9]{32}$/.test(id ?? '')) { this.message('Choose a job or paste its 32-character ID.', true); return; }
    const ticket = ++this.readEpoch, epoch = this.epoch;
    await this.safe(async () => { const job = await action('get_unirig_job', { job_id: id }); if (!this.dialog.open || epoch !== this.epoch || ticket !== this.readEpoch || this.selected !== id) return;
      if (job.id !== id) throw new Error('Loaded rigging receipt does not match the selected job.'); this.currentJob = job; this.jobs.set(id, job); this.renderJobs(); this.$('uni-job-id').value = id;
      this.$('uni-job-state').textContent = `${job.status} · ${job.stage ?? 'no stage'} · ${id}${job.error ? ` · ${job.error}` : ''}${job.verification_error ? ` · output verification failed: ${job.verification_error}` : ''}`;
      this.$('uni-receipt').textContent = JSON.stringify(job, null, 2); this.renderOutputs(job); this.buttons();
    });
  }
  renderOutputs(job) {
    const files = verifiedUniRigOutputs(job), previous = this.$('uni-preview').value; this.$('uni-downloads').replaceChildren();
    const registration = this.$('uni-register-file'), previousRegistration = registration.value; registration.replaceChildren();
    this.$('uni-preview').replaceChildren(new Option('Choose a verified GLB result', ''));
    for (const item of files) {
      const link = node('a', item.path); link.href = uniRigArtifactUrl(job.id, item.path); link.download = item.path.split('/').at(-1); this.$('uni-downloads').append(link);
      if (item.path.toLowerCase().endsWith('.glb')) this.$('uni-preview').append(new Option(`Item ${item.item_index ?? '?'} · ${item.path}`, item.path));
      if (/\.(glb|fbx)$/i.test(item.path)) registration.append(new Option(item.path, item.path));
    }
    if (files.some(item => item.path === previous && item.path.toLowerCase().endsWith('.glb'))) this.$('uni-preview').value = previous;
    if ([...registration.options].some(option => option.value === previousRegistration)) registration.value = previousRegistration;
    this.$('uni-register').disabled = this.busy || !registration.options.length;
    if (!files.length) { this.viewer?.clear(); this.loadedPreview = null; this.$('uni-viewer-note').hidden = false; this.$('uni-viewer-note').textContent = job.verification_error ? 'Output files failed verification. Downloads and preview are unavailable.' : 'No verified GLB is available yet. Skeleton and skin-only stages may provide FBX for editor inspection.'; }
  }
  preview() {
    const path = this.$('uni-preview').value, job = this.currentJob;
    if (!verifiedUniRigOutputs(job).some(item => item.path === path) || !path.endsWith('.glb')) { this.viewer?.clear(); return; }
    try {
      this.viewer ??= new MotionViewer(this.$('uni-viewport'), { onProgress: text => { this.$('uni-viewer-note').hidden = false; this.$('uni-viewer-note').textContent = text; }, onLoaded: info => { this.$('uni-viewer-note').hidden = true; this.message(`Static rig preview: ${info.meshes} meshes and ${info.bones} bones. This is not a deformation-quality check.`); }, onError: error => { this.$('uni-viewer-note').hidden = false; this.$('uni-viewer-note').textContent = error.message; }, onTime: () => {} });
      this.viewer.setSkeleton(this.$('uni-bones').getAttribute('aria-pressed') === 'true'); this.viewer.load(safeLocalUrl(uniRigArtifactUrl(job.id, path)));
    } catch (error) { this.message(error.message, true); }
  }
  async cancel() {
    const job = this.currentJob, epoch = this.epoch; if (!canCancelUniRig(job) || this.busy) return;
    this.busy = true; this.buttons(); await this.safe(async () => { try { await action('cancel_unirig_job', { job_id: job.id }); if (epoch === this.epoch && this.selected === job.id) await this.loadJob(job.id); } finally { if (epoch === this.epoch) { this.busy = false; this.buttons(); } } });
  }
  async log() {
    const id = this.selected, epoch = this.epoch; await this.safe(async () => { const result = await action('get_unirig_log', { job_id: id, lines: 80 }); if (!this.dialog.open || epoch !== this.epoch || this.selected !== id || result.job_id !== id) return; this.$('uni-log-output').textContent = result.text || 'No log lines yet.'; this.$('uni-log-output').hidden = false; });
  }
  async registerOutput() {
    if (this.busy) return; const epoch = this.epoch, job = this.currentJob;
    await this.safe(async () => { const args = uniRigRegistration(job, this.$('uni-register-file').value, this.$('uni-register-label').value, this.$('uni-register-kind').value); this.busy = true; this.$('uni-register').disabled = true; this.buttons();
      try { const result = await action('register_unirig_output', args); if (!this.dialog.open || epoch !== this.epoch || this.selected !== job.id) return; if (!/^[a-f0-9]{64}$/.test(result.asset_id ?? '') || result.registered_character_changed !== false) throw new Error('The asset registration response could not be confirmed. Refresh the library.'); this.message(`Registered library asset ${result.asset_id}. The current animation character is unchanged.`); await this.refreshAssets(); }
      finally { if (epoch === this.epoch) { this.busy = false; this.buttons(); this.$('uni-register').disabled = !this.$('uni-register-file').options.length; } }
    });
  }
  reuse() {
    const request = this.currentJob?.request; if (!request?.assets) return;
    for (const name of ['operation', ...Object.keys(UNIRIG_DEFAULTS)]) { const input = this.$(`uni-${name}`), value = request[name] ?? UNIRIG_DEFAULTS[name]; if (input.type === 'checkbox') input.checked = value; else input.value = value; }
    this.$('uni-assets').replaceChildren(); for (const asset of request.assets) this.addAsset(asset); this.applicability(); this.invalidate(); this.message('Saved request loaded into a new draft. Check its plan before starting a new job; the earlier result stays saved.');
  }
}
