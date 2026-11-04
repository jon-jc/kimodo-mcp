import { action, getToken } from './api.js';
import { MotionViewer } from './viewer.js';
import { numericField } from './native-data.js';
import { PROP_BONES, matchingCharacterExports, propMotionSource, propSceneRequest } from './asset-data.js';
import './unirig.css';
import './asset-panel.css';

const node = (tag, text = '', cls = '') => { const value = document.createElement(tag); value.textContent = text; value.className = cls; return value; };
const SHA = /^[a-f0-9]{64}$/;

export class AssetPanel {
  constructor() {
    this.epoch = 0; this.assetRevision = 0; this.busy = false; this.assets = []; this.prepared = null;
    this.dialog = document.createElement('dialog'); this.dialog.className = 'unirig-dialog assets-dialog'; this.dialog.setAttribute('aria-labelledby','asset-title');
    this.dialog.innerHTML = `<header class="uni-heading"><div><span class="eyebrow">MODELS · PROPS · ATTACHMENTS</span><h2 id="asset-title">Assets & props</h2><p class="help">Import a weapon or game asset, place it in the world or attach it to your character, then review the animated scene.</p></div><button id="as-close" class="small-button" autofocus>Close</button></header>
    <p id="as-message" class="uni-message" role="status">Imports preserve original files. Preview and scene builds run separate background Blender processes.</p>
    <div class="uni-layout"><section><h3>1. Import & inspect a model</h3><form id="as-import"><fieldset><label>Asset label<input id="as-label" maxlength="160" required placeholder="Steel sword" /></label><label>Library category<select id="as-kind"><option value="prop">Prop · weapon, tool or scenery</option><option value="character">Character · for rigging</option></select></label><label>Choose a model<input id="as-file" type="file" accept=".glb,.fbx,.obj,.vrm" /></label><label>Or enter its local absolute path<input id="as-path" placeholder="D:/Models/sword.glb" /></label><p class="help">GLB, FBX, OBJ or VRM · up to 512 MiB. Prefer self-contained GLB. External texture/material sidecars are not copied. Importing a character does not replace your registered animation character.</p><button class="button primary" type="submit">Import model</button></fieldset></form>
    <div class="uni-actions"><button id="as-refresh" class="small-button">Refresh library</button><button id="as-more" class="small-button">Load more</button></div><label>Shared asset library<select id="as-library"><option value="">Choose an asset</option></select></label><p id="as-asset-info" class="help"></p><div class="uni-actions"><button id="as-view" class="small-button">View saved model</button><button id="as-convert" class="small-button">Build GLB preview</button></div><label>Preview attempt<input id="as-preview-attempt" type="number" min="0" max="99" value="0" /></label>
    <h3 class="as-section">2. Arrange props</h3><form id="as-scene"><fieldset><label>Scene title<input id="as-title" required maxlength="160" value="Character with props" /></label><button id="as-add" class="small-button" type="button">Attach selected prop · max 16</button><div id="as-rows"></div><p class="help">Position uses meters and Blender Z-up. Bone offsets use posed bone-head axes. Rotation uses local X → Y → Z degrees. Use scale 0.01 for a model authored in centimeters. A hand attachment does not create grip/finger animation or collision avoidance.</p><button class="button secondary" type="submit">Save attachment scene</button></fieldset></form>
    <details class="uni-options"><summary>Saved attachment scenes</summary><select id="as-scenes" aria-label="Saved prop scene"></select><button id="as-load-scene" class="small-button">Load scene</button></details></section>
    <section><h3 id="as-view-title">Model preview</h3><div id="as-viewport" class="uni-viewport"><p id="as-view-note">Select a model or completed scene to inspect.</p></div><div class="uni-actions"><button id="as-fit" class="small-button">Fit view</button><button id="as-play" class="small-button" disabled>Play</button><button id="as-bones" class="small-button" aria-pressed="false">Show bones</button><input id="as-time" type="range" min="0" max="0" step="any" value="0" aria-label="Prop scene animation time" disabled /><span id="as-time-label" class="help">0.00 s</span></div><div id="as-downloads" class="download-links"></div>
    <h3 class="as-section">3. Combine with character animation</h3><form id="as-build"><fieldset><label>Motion source<select id="as-source-kind"><option value="kimodo">Generated Kimodo character</option><option value="capture_character">Cleaned FreeMoCap character</option></select></label><div id="as-native-source"><label>Completed Kimodo job ID<input id="as-job" maxlength="32" /></label><div class="uni-fields"><label>Candidate index<input id="as-sample" type="number" min="0" max="3" value="0" /></label><label>Character export attempt<input id="as-character-attempt" type="number" min="0" max="99" value="0" /></label></div><label>Motion version ID · optional<input id="as-variant" maxlength="64" /></label><p class="help">Build the fitted-character preview first. Use the exact candidate, version and character export attempt from its verified output.</p></div><div id="as-capture-source" hidden><label>Completed capture handoff<select id="as-captures"></select></label><label>Capture handoff ID<input id="as-capture" maxlength="64" /></label></div><label>Prop export attempt<input id="as-attempt" type="number" min="0" max="99" value="0" /></label><div class="uni-actions"><button id="as-prepare" type="submit" class="button secondary">Prepare animated scene</button><button id="as-run" type="button" class="button primary" disabled>Build in Blender</button></div></fieldset></form>
    <p class="help">Character keys and skeleton are preserved. Outputs include a combined animated GLB/Blender scene, separate static prop FBXs and an attachment recipe. Unreal socket orientation still needs editor review; this does not import props into Unreal.</p><details class="uni-options" open><summary>Shared scene export history</summary><select id="as-handoffs" aria-label="Saved prop scene export"></select><div class="uni-actions"><input id="as-handoff-id" placeholder="Or paste an export ID" maxlength="64" aria-label="Prop scene export ID" /><button id="as-recover" class="small-button">Read saved result</button></div></details><details class="uni-options"><summary>Verified scene evidence</summary><pre id="as-evidence">No scene selected.</pre></details></section></div>`;
    document.body.append(this.dialog);
    this.$('as-close').onclick = () => this.dialog.close();
    this.dialog.addEventListener('close', () => { this.epoch++; this.prepared = null; this.viewer?.clear(); this.busy = false; });
    this.$('as-import').onsubmit = event => { event.preventDefault(); this.run(() => this.importAsset()); };
    this.$('as-refresh').onclick = () => this.run(() => this.refresh());
    this.$('as-more').onclick = () => this.run(() => this.refresh(true));
    this.$('as-library').onchange = () => this.selectionChanged();
    this.$('as-preview-attempt').addEventListener('input', () => this.selectionChanged());
    this.$('as-view').onclick = () => this.run(() => this.previewAsset(false)); this.$('as-convert').onclick = () => this.run(() => this.previewAsset(true));
    this.$('as-add').onclick = () => { const value = this.selectedAsset(); if (value?.kind !== 'prop') return this.message('Choose a prop from the asset library.', true); this.addRow({asset_id:value.asset_id}); this.invalidate(); };
    this.$('as-scene').addEventListener('input', () => this.invalidate()); this.$('as-scene').addEventListener('change', () => this.invalidate());
    this.$('as-build').addEventListener('input', () => this.invalidate()); this.$('as-build').addEventListener('change', () => this.invalidate());
    this.$('as-scene').onsubmit = event => { event.preventDefault(); this.run(() => this.saveScene()); };
    this.$('as-build').onsubmit = event => { event.preventDefault(); this.run(() => this.prepare()); };
    this.$('as-source-kind').onchange = () => this.sourceVisibility();
    this.$('as-captures').onchange = () => { this.$('as-capture').value = this.$('as-captures').value; this.invalidate(); };
    this.$('as-handoffs').onchange = () => { this.$('as-handoff-id').value = this.$('as-handoffs').value; };
    this.$('as-load-scene').onclick = () => this.run(() => this.loadScene());
    this.$('as-run').onclick = () => this.run(() => this.execute());
    this.$('as-recover').onclick = () => this.run(() => this.recover());
    this.$('as-fit').onclick = () => this.viewer?.fit(); this.$('as-play').onclick = () => this.viewer?.togglePlay();
    this.$('as-time').oninput = () => this.viewer?.seek(Number(this.$('as-time').value));
    this.$('as-bones').onclick = () => { const enabled = this.$('as-bones').getAttribute('aria-pressed') !== 'true'; this.$('as-bones').setAttribute('aria-pressed',String(enabled)); this.viewer?.setSkeleton(enabled); };
    const exports = node('div', '', 'uni-actions');
    const discover = node('button', 'Find verified character export', 'small-button');
    discover.type = 'button'; discover.id = 'as-find-character';
    discover.onclick = () => this.run(() => this.findCharacterExport());
    exports.append(discover); this.$('as-native-source').append(exports);
    const sourceNote = node('p', 'The selected candidate and version must have a completed fitted-character export.', 'help');
    sourceNote.id = 'as-character-note'; this.$('as-native-source').append(sourceNote);
    for (const id of ['as-job', 'as-sample', 'as-variant', 'as-character-attempt']) this.$(id).addEventListener('input', () => {
      this.$('as-character-note').textContent = 'Source changed. Find the matching verified export or enter its exact attempt.';
    });
  }
  $(id) { return this.dialog.querySelector(`#${id}`); }
  message(text,error=false) { this.$('as-message').textContent=text; this.$('as-message').classList.toggle('error',error); }
  async run(fn) { if (this.busy) return; this.busy=true; const epoch=this.epoch; this.lock(true); try { await fn(); } catch(error) { if (epoch===this.epoch && this.dialog.open) this.message(error.message,true); } finally { if(epoch===this.epoch) {this.busy=false;this.lock(false);} } }
  lock(value) {
    this.dialog.querySelectorAll('fieldset').forEach(field => { field.disabled = value; });
    for (const id of ['as-refresh', 'as-view', 'as-convert', 'as-load-scene', 'as-recover']) this.$(id).disabled = value;
    this.$('as-more').disabled = value || (this.libraryOffset ?? 0) >= (this.libraryTotal ?? 0);
    this.$('as-run').disabled = value || !this.prepared; this.dialog.setAttribute('aria-busy', String(value));
  }
  invalidate() { this.prepared=null; this.$('as-run').disabled=true; }
  selectedAsset() { return this.assets.find(asset => asset.asset_id===this.$('as-library').value); }
  active(epoch) { return epoch === this.epoch && this.dialog.open; }
  selectionChanged() {
    this.assetRevision++; this.viewer?.clear(); this.$('as-view-note').hidden = false;
    this.$('as-view-note').textContent = 'Selection changed. View the selected model to inspect it.';
    this.$('as-downloads').replaceChildren(); this.$('as-view-title').textContent = 'Model preview';
    const asset = this.selectedAsset();
    this.$('as-asset-info').textContent = asset ? `${asset.label} · ${asset.format.toUpperCase()} · ${asset.kind} · ${(asset.bytes / 1048576).toFixed(2)} MiB` : '';
  }
  selectOptions(id,rows,label) { const select=this.$(id), previous=select.value; select.replaceChildren(node('option','Choose a saved item')); select.firstChild.value=''; for(const row of rows) {const option=node('option',label(row)); option.value=row.asset_id??row.scene_id??row.handoff_id; select.append(option);} if([...select.options].some(o=>o.value===previous))select.value=previous; }
  async open(context = {}) {
    this.epoch++; this.prepared = null; this.busy = false; this.dialog.showModal(); this.lock(false);
    if (context.job_id) {
      this.$('as-source-kind').value = 'kimodo'; this.sourceVisibility();
      this.$('as-job').value = context.job_id; this.$('as-sample').value = context.sample_index ?? 0;
      this.$('as-variant').value = context.variant_id ?? ''; this.$('as-character-attempt').value = context.attempt ?? 0;
    }
    const epoch = this.epoch;
    await this.run(async () => { await this.refresh(); if (this.active(epoch) && context.job_id) await this.findCharacterExport(); });
  }
  async refresh(more = false) {
    const epoch = this.epoch;
    const [library, scenes, handoffs, captures] = await Promise.all([
      action('list_studio_assets', { limit: 100, offset: more ? this.libraryOffset ?? 0 : 0 }),
      action('list_prop_scenes', { limit: 100 }), action('list_prop_scene_handoffs', { limit: 100 }),
      action('list_freemocap_character_handoffs', { limit: 100 }),
    ]);
    if (!this.active(epoch)) return;
    const selection = this.$('as-library').value;
    this.assets = [...new Map((more ? [...this.assets, ...library.assets] : library.assets).map(asset => [asset.asset_id, asset])).values()];
    this.libraryOffset = (library.offset ?? 0) + library.assets.length;
    this.libraryTotal = library.total;
    this.selectOptions('as-library', this.assets, a => `${a.label} · ${a.kind} · ${a.format}`);
    if (selection !== this.$('as-library').value) this.selectionChanged();
    this.selectOptions('as-scenes', scenes.scenes, a => `${a.title} · ${a.attachments} props`);
    this.selectOptions('as-handoffs', handoffs.handoffs, a => `${a.handoff_id.slice(0, 12)} · ${a.status ?? 'recorded'}`);
    this.selectOptions('as-captures', captures.handoffs, a => `${a.handoff_id.slice(0, 12)} · ${a.recorded_status}`);
    this.$('as-more').disabled = this.libraryOffset >= library.total;
    this.message(`${this.assets.length} library assets. Saved history is verified when opened.`);
  }
  async importAsset() {
    const epoch = this.epoch, file = this.$('as-file').files[0], label = this.$('as-label').value.trim(), kind = this.$('as-kind').value;
    if (!label || label.length > 160) throw new Error('Give the model a label of 1 to 160 characters.');
    let result;
    if (file) {
      if (file.size > 512 * 1024 ** 2) throw new Error('Model exceeds 512 MiB.');
      const extension = file.name.split('.').pop().toLowerCase();
      const query = new URLSearchParams({ extension, label, kind });
      const response = await fetch(`/api/assets/import?${query}`, { method: 'POST', headers: { 'X-Studio-Token': getToken() }, body: file, credentials: 'same-origin' });
      result = await response.json(); if (!response.ok) throw new Error(result.error ?? 'Model import failed.');
    } else {
      const path = this.$('as-path').value.trim();
      if (!path) throw new Error('Choose a model file or enter its absolute local path.');
      result = await action('import_studio_asset', { source_path: path, label, kind });
    }
    if (!this.active(epoch)) return;
    await this.refresh(); if (!this.active(epoch)) return;
    // A large library may place the imported item beyond the first page.
    if (!this.assets.some(asset => asset.asset_id === result.asset_id)) {
      this.assets.push(result); this.selectOptions('as-library', this.assets, a => `${a.label} · ${a.kind} · ${a.format}`);
    }
    this.$('as-library').value = result.asset_id; this.selectionChanged();
    this.message(`Imported ${result.label}. Original files remain unchanged.`);
  }
  async previewAsset(build) {
    const asset = this.selectedAsset(), epoch = this.epoch, revision = this.assetRevision;
    if (!asset) throw new Error('Choose an asset first.');
    const current = () => this.active(epoch) && revision === this.assetRevision && this.selectedAsset()?.asset_id === asset.asset_id;
    const attempt = numericField(this.$('as-preview-attempt').value, 'Preview attempt', 0, 99, true);
    let url;
    if (!build && asset.format === 'glb') url = `/api/assets/${asset.asset_id}/source`;
    else {
      this.message(build ? 'Building a model preview in background Blender…' : 'Checking the saved preview…');
      let result;
      try { result = await action(build ? 'build_studio_asset_preview' : 'get_studio_asset_preview', { asset_id: asset.asset_id, attempt }); }
      catch (error) { if (current()) throw error; return; }
      if (!current()) return;
      if (result.status !== 'completed') throw new Error(`Preview is ${result.status}. Build it explicitly, or inspect an unresolved attempt before retrying.`);
      url = `/api/assets/${asset.asset_id}/preview/${attempt}`;
    }
    if (!current()) return;
    this.$('as-view-title').textContent = asset.label; this.$('as-downloads').replaceChildren(); await this.loadViewer(url);
  }
  async loadViewer(url) { this.viewer??=new MotionViewer(this.$('as-viewport'),{onProgress:text=>{this.$('as-view-note').hidden=false;this.$('as-view-note').textContent=text;},onError:error=>{this.$('as-view-note').hidden=false;this.$('as-view-note').textContent=error.message;},onLoaded:()=>{this.$('as-view-note').hidden=true;},onTime:(time,duration,playing)=>{this.$('as-play').disabled=!duration;this.$('as-play').textContent=playing?'Pause':'Play';this.$('as-time').disabled=!duration;this.$('as-time').max=duration;this.$('as-time').value=time;this.$('as-time-label').textContent=`${time.toFixed(2)} / ${duration.toFixed(2)} s`;}}); await this.viewer.load(url); }
  addRow(value) {
    if (this.$('as-rows').children.length >= 16) return this.message('A scene supports up to 16 props.', true);
    const row = node('fieldset', '', 'as-row'); row.dataset.asset = value.asset_id;
    row.append(node('legend', this.assets.find(a => a.asset_id === value.asset_id)?.label ?? value.asset_id.slice(0, 12)));
    const label = node('label', 'Attachment'), select = node('select'); select.dataset.field = 'bone';
    for (const bone of [null, ...PROP_BONES]) { const option = node('option', bone ?? 'World · stationary'); option.value = bone ?? ''; select.append(option); }
    select.value = value.bone ?? ''; label.append(select); row.append(label);
    for (const [field, caption, defaults, min, max] of [['translation_m', 'Position · meters', [0, 0, 0], -10000, 10000], ['rotation_degrees', 'Rotation · degrees', [0, 0, 0], -360, 360], ['scale', 'Scale', [1, 1, 1], .001, 100]]) {
      const group = node('div', '', 'as-triple'); group.append(node('span', caption));
      for (let index = 0; index < 3; index++) {
        const item = node('label', 'XYZ'[index]), input = node('input');
        input.type = 'number'; input.step = 'any'; input.min = min; input.max = max; input.required = true;
        input.value = (value[field] ?? defaults)[index]; input.dataset.field = field; input.dataset.axis = index;
        input.setAttribute('aria-label', `${caption} ${'XYZ'[index]}`); item.append(input); group.append(item);
      }
      row.append(group);
    }
    const remove = node('button', 'Remove prop', 'small-button'); remove.type = 'button';
    remove.onclick = () => { row.remove(); this.invalidate(); }; row.append(remove); this.$('as-rows').append(row);
  }
  sceneRequest() {
    const rows = [...this.$('as-rows').children].map(row => {
      const value = { asset_id: row.dataset.asset, bone: row.querySelector('select').value || null };
      for (const field of ['translation_m', 'rotation_degrees', 'scale']) value[field] = [...row.querySelectorAll(`input[data-field="${field}"]`)].map(input => input.value);
      return value;
    });
    return propSceneRequest(this.$('as-title').value, rows);
  }
  applyScene(value) { this.$('as-title').value=value.request.title;this.$('as-rows').replaceChildren();for(const item of value.request.attachments)this.addRow(item);this.invalidate();this.message(`Loaded scene ${value.scene_id}. Changes create another saved scene.`); }
  async saveScene(request = this.sceneRequest()) {
    const epoch = this.epoch, result = await action('save_prop_scene', { request }); if (!this.active(epoch)) return;
    const select = this.$('as-scenes');
    if (![...select.options].some(option => option.value === result.scene_id)) {
      const option = node('option', `${request.title} · ${request.attachments.length} props`); option.value = result.scene_id; select.append(option);
    }
    select.value = result.scene_id;
    this.message(`Saved attachment scene ${result.scene_id}. Choose the source animation and prepare it.`);
    this.$('as-evidence').textContent = JSON.stringify(result, null, 2); return result;
  }
  async loadScene() {
    const epoch = this.epoch, id = this.$('as-scenes').value;
    if (!SHA.test(id)) throw new Error('Choose a saved attachment scene.');
    const result = await action('get_prop_scene', { scene_id: id });
    if (this.active(epoch) && id === this.$('as-scenes').value) this.applyScene(result);
  }
  sourceVisibility() { const capture=this.$('as-source-kind').value==='capture_character';this.$('as-native-source').hidden=capture;this.$('as-capture-source').hidden=!capture; }
  motionSource() {
    return propMotionSource({ kind: this.$('as-source-kind').value, capture_handoff_id: this.$('as-capture').value.trim(),
      job_id: this.$('as-job').value.trim(), sample_index: this.$('as-sample').value,
      attempt: this.$('as-character-attempt').value, variant_id: this.$('as-variant').value.trim() });
  }
  async findCharacterExport() {
    const epoch = this.epoch, source = this.motionSource();
    if (source.kind !== 'kimodo') return;
    const identity = JSON.stringify(source);
    this.$('as-character-note').textContent = 'Checking stored character exports…';
    const result = await action('list_job_outputs', { job_id: source.job_id });
    if (!this.active(epoch) || identity !== JSON.stringify(this.motionSource())) return;
    const outputs = matchingCharacterExports(result, source);
    if (!outputs.length) {
      this.$('as-character-note').textContent = 'No verified character export matches this candidate and version. Build its fitted-character preview in Studio first.';
      return;
    }
    this.$('as-character-attempt').value = outputs[0].attempt; this.invalidate();
    this.$('as-character-note').textContent = `Verified character export found · candidate ${source.sample_index} · attempt ${outputs[0].attempt}${source.variant_id ? ' · selected motion version' : ' · original motion'}.`;
  }
  async prepare() {
    const epoch = this.epoch; this.invalidate();
    // Validate every form before writing a scene, and retain one exact draft
    // across the save and prepare calls.
    const request = this.sceneRequest(), source = this.motionSource();
    const attempt = numericField(this.$('as-attempt').value, 'Prop export attempt', 0, 99, true);
    const saved = await this.saveScene(request); if (!this.active(epoch) || !saved) return;
    const result = await action('prepare_prop_scene_handoff', { scene_id: saved.scene_id, source, attempt });
    if (!this.active(epoch)) return;
    this.prepared = result.status === 'prepared' ? result.handoff_id : null;
    this.$('as-handoff-id').value = result.handoff_id; this.$('as-evidence').textContent = JSON.stringify(result, null, 2);
    this.message(`Scene ${result.status}. ${this.prepared ? 'Build in Blender when ready.' : 'Read and review the saved result before another attempt.'}`);
    if (result.status === 'completed') await this.showReport(result);
  }
  async recover() {
    const epoch = this.epoch, id = this.$('as-handoff-id').value.trim();
    if (!SHA.test(id)) throw new Error('Choose a valid saved export ID.');
    const result = await action('get_prop_scene_handoff', { handoff_id: id });
    if (!this.active(epoch) || id !== this.$('as-handoff-id').value.trim()) return;
    const request = result.request, source = request.motion.source;
    this.applyScene({ scene_id: request.scene_id, request: request.scene });
    this.$('as-source-kind').value = source.kind; this.sourceVisibility();
    this.$('as-capture').value = source.capture_handoff_id ?? ''; this.$('as-job').value = source.job_id ?? '';
    this.$('as-sample').value = source.sample_index ?? 0; this.$('as-character-attempt').value = source.attempt ?? 0;
    this.$('as-variant').value = source.variant_id ?? ''; this.$('as-attempt').value = request.attempt;
    this.$('as-character-note').textContent = 'Source restored from this verified scene export.';
    await this.showReport(result);
  }
  async execute() { const id=this.prepared,epoch=this.epoch;if(!id)throw new Error('Prepare the current settings first.');this.invalidate();this.message('Building props and checking every character animation frame…');const result=await action('execute_prop_scene_handoff',{handoff_id:id});if(epoch!==this.epoch||!this.dialog.open)return;await this.showReport(result); }
  async showReport(result) {
    this.invalidate(); this.$('as-handoff-id').value = result.handoff_id;
    this.$('as-evidence').textContent = JSON.stringify(result, null, 2); this.$('as-downloads').replaceChildren(); this.viewer?.clear();
    if (result.status === 'prepared') {
      this.prepared = result.handoff_id; this.message('Prepared scene restored. Review its settings, then build in Blender.'); return;
    }
    if (result.status !== 'completed') { this.message(`Scene ${result.status}. An unresolved dispatch must be inspected before a new attempt.`, true); return; }
    this.message(`Verified scene: ${result.frames} frames at ${result.fps} FPS · character animation preserved.`);
    for (const output of result.outputs) {
      const link = node('a', output.path); link.href = `/api/prop-scenes/${result.handoff_id}/files/${encodeURIComponent(output.path)}`;
      link.download = output.path; this.$('as-downloads').append(link);
    }
    this.$('as-view-title').textContent = 'Verified animated prop scene';
    await this.loadViewer(`/api/prop-scenes/${result.handoff_id}/files/scene-preview.glb`);
  }
}
