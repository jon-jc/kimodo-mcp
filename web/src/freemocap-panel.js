import { action, request } from './api.js';
import { FREEMOCAP_STAGES, FreeMocapPlanGate, canCancelFreeMocapJob, freeMocapJobState, freeMocapRequest, freeMocapStageReadiness, validFreeMocapJobId } from './freemocap-data.js';
import { LandmarkViewer } from './landmark-viewer.js';
import { DEFAULT_WINDOW, LandmarkReviewGate, adjacentLandmarkWindow, canReviewFreeMocap, landmarkArtifactUrl, landmarkFrameEvidence, landmarkPreviewUrl, validateLandmarkPreview } from './freemocap-review-data.js';
import './freemocap.css';

const node = (tag, text = '', className = '') => { const value = document.createElement(tag); value.textContent = text; value.className = className; return value; };

export class FreeMocapPanel {
  constructor() {
    this.operation = 'check_sync'; this.gate = new FreeMocapPlanGate(); this.receipts = new Map();
    this.recordings = []; this.refreshToken = 0; this.jobToken = 0; this.inspectToken = 0;
    this.historyToken = 0; this.reviewGate = new LandmarkReviewGate(); this.reviewData = null; this.quality = null;
    this.dialog = document.createElement('dialog'); this.dialog.className = 'freemocap-dialog';
    this.dialog.setAttribute('aria-labelledby', 'fmc-heading');
    // This template contains static application markup only. All runtime data is textContent.
    this.dialog.innerHTML = `
      <header class="fmc-heading"><div><span class="eyebrow">MULTI-CAMERA CAPTURE</span><h2 id="fmc-heading">FreeMoCap workspace</h2><p class="help">Turn recorded camera views into tracked landmarks, then review and export.</p></div><button id="fmc-close" class="small-button" type="button" autofocus>Close</button></header>
      <div class="fmc-readiness"><span id="fmc-runtime" class="fmc-badge">Checking runtime…</span><span id="fmc-version" class="help">Separate from the Kimodo generation worker</span><button id="fmc-refresh" class="small-button" type="button">Refresh connection &amp; recordings</button></div>
      <p id="fmc-message" class="fmc-message" role="status">Choose a stage. Planning never starts tracking, recording or Blender.</p>
      <nav id="fmc-stages" class="fmc-stages" aria-label="FreeMoCap stages"></nav>
      <div class="fmc-workspace"><section class="fmc-controls" aria-label="Stage settings">
        <h3 id="fmc-stage-title">Check videos</h3><p id="fmc-stage-description" class="help"></p><p id="fmc-stage-readiness" class="fmc-warning" role="status"></p>
        <form id="fmc-form"><fieldset id="fmc-fields">
          <fieldset data-stages="check_sync synchronize import"><label class="field-label" for="fmc-videos">Reference videos <span>one per line</span></label><textarea id="fmc-videos" rows="4" placeholder="camera-left.mp4\ncamera-right.mp4" required></textarea><p class="help">Names are relative to your registered reference folder. Use forward slashes for subfolders. Up to 8 cameras, 10 minutes per video; originals stay intact.</p></fieldset>
          <fieldset data-stages="synchronize"><label class="field-label" for="fmc-sync-method">Alignment signal</label><select id="fmc-sync-method"><option value="audio">Shared audio</option><option value="brightness">Visible flash</option></select><label id="fmc-brightness-row" class="field-label" for="fmc-brightness">Brightness ratio threshold<input id="fmc-brightness" type="number" min="1" max="1000000" step="1" value="1000" /></label><p class="help">Use a clear common sound or flash visible to every camera. Inspect alignment afterward; equal frame counts alone do not prove synchronization.</p></fieldset>
          <fieldset data-stages="import"><label class="field-label" for="fmc-sync-job">Completed synchronization receipt <span>optional</span></label><input id="fmc-sync-job" type="text" maxlength="32" placeholder="Paste the synchronization receipt ID" /><label class="fmc-check"><input id="fmc-sync-confirmed" type="checkbox" />These videos are already aligned in time</label><p class="help">For multiple cameras, use a completed synchronization receipt or confirm the already synchronized input. A new recording copy is created every time.</p></fieldset>
          <fieldset data-stages="calibrate process export"><label class="field-label" for="fmc-recording">Recording to use</label><select id="fmc-recording" required><option value="">Import a take first</option></select><p class="help">Only recordings imported through this workspace can be changed. Import another take to preserve earlier processing results.</p></fieldset>
          <fieldset data-stages="calibrate"><div class="fmc-fields-grid"><label>Board columns<input id="fmc-board-x" type="number" min="2" max="30" step="1" value="5" required /></label><label>Board rows<input id="fmc-board-y" type="number" min="2" max="30" step="1" value="3" required /></label><label>Measured square size · mm<input id="fmc-square-mm" type="number" min="0.01" max="1000" step="0.01" placeholder="Measure the printed board" required /></label><label>Marker / square ratio<input id="fmc-marker-ratio" type="number" min="0.01" max="0.99" step="0.01" value="0.8" required /></label></div><label class="field-label" for="fmc-dictionary">ArUco dictionary ID<input id="fmc-dictionary" type="number" min="0" max="20" step="1" value="2" required /></label><p class="help">ID 2 is DICT_4X4_250. Board geometry must match your printed board; size establishes physical scale. The standard Anipose solver is used.</p><label class="fmc-check"><input id="fmc-ground" type="checkbox" />Align the world to the board plane</label><p class="fmc-warning">Calibration also updates FreeMoCap's shared calibration library and its last-successful calibration.</p></fieldset>
          <fieldset data-stages="process"><div class="fmc-fields-grid"><label>Landmark detector<select id="fmc-detector"><option value="mediapipe">MediaPipe · Blender export available</option><option value="rtmpose">RTMPose · landmark outputs</option></select></label><label>Low-pass cutoff · Hz<input id="fmc-cutoff" type="number" min="0.01" max="100" step="any" value="6" required /></label></div><label class="field-label" for="fmc-calibration-source">Camera calibration</label><select id="fmc-calibration-source"><option value="none">None · single-camera planar capture only</option><option value="recording">From a calibration recording</option><option value="reference">Registered calibration TOML file</option></select><div id="fmc-calibration-recording-row"><label class="field-label" for="fmc-calibration-recording">Calibration recording</label><select id="fmc-calibration-recording"><option value="">Choose a calibration recording</option></select></div><div id="fmc-calibration-file-row"><label class="field-label" for="fmc-calibration-file">Calibration reference</label><input id="fmc-calibration-file" placeholder="calibrations/cameras.toml" /></div><label class="fmc-check"><input id="fmc-planar" type="checkbox" />Allow single-camera planar output</label><p class="fmc-warning">One camera produces a flat pixel-space reconstruction, not depth or metric 3D. Calibrated 3D requires synchronized views of the same movement.</p><details class="advanced"><summary>Advanced processing controls</summary><p class="help">Optional JSON for model variant, detector confidence, filter order and triangulation. Common detector and cutoff values remain controlled above.</p><textarea id="fmc-advanced" rows="6" spellcheck="false" aria-label="Advanced FreeMoCap processing JSON">{}</textarea><p class="help">Keys: rtmpose_model_name, rtmpose_confidence_threshold, mediapipe_detection_confidence, mediapipe_presence_confidence, mediapipe_tracking_confidence, filter_order, use_outlier_rejection, minimum_cameras, maximum_cameras_to_drop, target_reprojection_error. Reprojection error uses normalized image coordinates.</p></details><p class="help">This stage never opens Blender. Tracking may download model files through your separately installed FreeMoCap runtime.</p></fieldset>
          <fieldset data-stages="export"><div class="fmc-check-row"><label class="fmc-check"><input id="fmc-fbx" type="checkbox" checked />FBX</label><label class="fmc-check"><input id="fmc-bvh" type="checkbox" checked />BVH</label></div><label class="field-label" for="fmc-rest">Rig rest pose</label><select id="fmc-rest"><option value="tpose">T-pose</option><option value="apose">A-pose</option></select><label class="fmc-check"><input id="fmc-feet" type="checkbox" />Apply upstream foot-locking cleanup</label><label class="fmc-check"><input id="fmc-hands" type="checkbox" />Limit hand marker movement</label><p class="fmc-warning">This explicit step starts Blender in the background. MediaPipe output only; no editor window opens. Cleanup changes marker motion. Requested rig and cleanup settings need visual verification.</p></fieldset>
          <div class="fmc-actions"><button id="fmc-plan" class="button secondary" type="submit">Review stage plan</button><button id="fmc-run" class="button primary" type="button" disabled>Run this stage</button></div>
        </fieldset></form>
        <details class="advanced fmc-setup"><summary>Runtime setup &amp; limitations</summary><p class="help">FreeMoCap v2.0.0-alpha.25 runs as an external local application. An operator registers its loopback URL, reference folder and recording folder in <code>freemocap/runtime.json</code> under the studio data directory. This panel does not install dependencies, open cameras or record video.</p><p class="help">FreeMoCap is AGPL-3.0 software; its dependencies and model files keep their own terms. Landmark captures do not automatically become Kimodo SOMA motion or match your Unreal skeleton.</p><p class="help"><a href="https://github.com/jon-jc/kimodo-mcp/blob/main/docs/freemocap.md" target="_blank" rel="noopener noreferrer">Operator setup and status-extension guide ↗</a></p><a href="https://github.com/freemocap/freemocap/tree/700c8614d11d886dacfea8e52333d3c0b942ef52" target="_blank" rel="noopener noreferrer">Reviewed upstream version ↗</a></details>
      </section><section class="fmc-results" aria-label="Plans and recorded results">
        <div class="fmc-section-heading"><h3>Stage plan</h3><span id="fmc-plan-state" class="fmc-badge">Not reviewed</span></div><div id="fmc-plan-result" class="fmc-plan-result"><p class="help">Set the inputs on the left, then review the exact stage before running it.</p></div>
        <section class="fmc-recordings"><div class="fmc-section-heading"><h3>Recording inspection</h3><button id="fmc-inspect" class="small-button" type="button">Inspect recording</button></div><label class="field-label" for="fmc-inspect-recording">Recording</label><select id="fmc-inspect-recording"><option value="">Choose a recording</option></select><div id="fmc-inspection" class="fmc-output"><p class="help">Inspect saved output files and readiness independently of processing status.</p></div></section>
        <section class="fmc-jobs"><div class="fmc-section-heading"><h3>Stage receipts</h3><button id="fmc-refresh-job" class="small-button" type="button">Refresh status</button></div><label class="field-label" for="fmc-receipts">Recent receipts in this workspace</label><select id="fmc-receipts"><option value="">No stage submitted yet</option></select><p id="fmc-history-state" class="help" role="status"></p><div class="fmc-receipt-entry"><input id="fmc-receipt-id" type="text" maxlength="32" aria-label="Receipt ID from another agent" placeholder="Or paste a receipt ID from an agent" /><button id="fmc-load-job" class="small-button" type="button">Load receipt</button></div><p id="fmc-job-state" class="help" role="status">A submitted pipeline is not a completed capture.</p><div class="fmc-actions"><button id="fmc-cancel-job" class="small-button" type="button" disabled>Cancel this pipeline</button><button id="fmc-use-recording" class="small-button" type="button" disabled>Use this recording</button><button id="fmc-use-sync" class="small-button" type="button" disabled>Use synchronization receipt</button><button id="fmc-retarget-history" class="small-button" type="button">Character cleanup history</button></div><div id="fmc-job-result" class="fmc-output"></div><details class="advanced"><summary>Full receipt</summary><pre id="fmc-receipt-json">No receipt selected.</pre></details></section>
      </section>
        <section id="fmc-review" class="fmc-review" aria-label="Captured body landmark review" hidden>
          <div class="fmc-section-heading"><h3>Review captured body landmarks</h3><span id="fmc-review-state" class="fmc-badge">Loading</span></div>
          <p id="fmc-review-note" class="help">Read-only processed landmarks. Missing points stay absent; these are not editable character bones.</p>
          <div id="fmc-landmark-viewport" class="fmc-landmark-viewport"></div>
          <div class="fmc-playback"><button id="fmc-review-play" class="small-button" type="button" disabled>Play window</button><input id="fmc-review-scrub" type="range" min="0" max="1" step="1" value="0" aria-label="Captured frame within this window" disabled /><button id="fmc-review-fit" class="small-button" type="button" disabled>Fit view</button></div>
          <p id="fmc-frame-evidence" class="help" role="status"></p>
          <div class="fmc-window-controls"><label>First original frame<input id="fmc-window-start" type="number" min="0" max="143999" step="1" value="0" /></label><label>Frames in window<input id="fmc-window-count" type="number" min="1" max="2400" step="1" value="240" /></label><button id="fmc-window-load" class="small-button" type="button">Load window</button><button id="fmc-window-previous" class="small-button" type="button" disabled>Previous</button><button id="fmc-window-next" class="small-button" type="button" disabled>Next</button></div>
          <label class="field-label" for="fmc-landmark">Identify a landmark</label><select id="fmc-landmark"><option value="">Choose a body landmark</option></select><p id="fmc-landmark-evidence" class="help"></p>
          <details class="advanced" open><summary>Numerical review</summary><div id="fmc-quality" class="fmc-output"></div><div id="fmc-review-frames" class="fmc-review-frames" aria-label="Suggested original frames to inspect"></div></details>
        </section>
      </div>`;
    document.body.append(this.dialog);
    for (const stage of FREEMOCAP_STAGES) {
      const button = node('button', '', 'fmc-stage'); button.type = 'button'; button.dataset.stage = stage.id;
      button.append(node('span', String(FREEMOCAP_STAGES.indexOf(stage) + 1)), node('strong', stage.title));
      button.addEventListener('click', () => this.setStage(stage.id)); this.$('fmc-stages').append(button);
    }
    this.$('fmc-stages').addEventListener('keydown', event => {
      const buttons = [...this.$('fmc-stages').querySelectorAll('button')], index = buttons.indexOf(document.activeElement);
      if (index < 0 || !['ArrowLeft', 'ArrowRight', 'Home', 'End'].includes(event.key)) return;
      event.preventDefault(); const next = event.key === 'Home' ? 0 : event.key === 'End' ? buttons.length - 1 : (index + (event.key === 'ArrowRight' ? 1 : -1) + buttons.length) % buttons.length;
      buttons[next].focus(); buttons[next].click();
    });
    this.$('fmc-close').addEventListener('click', () => this.dialog.close());
    this.dialog.addEventListener('close', () => { this.jobToken++; this.inspectToken++; this.refreshToken++; this.historyToken++; this.reviewGate.close(); this.clearReview(); this.$('fmc-refresh').disabled = false; });
    this.$('fmc-refresh').addEventListener('click', () => this.refresh());
    this.$('fmc-form').addEventListener('input', () => { this.invalidate(); this.dependentFields(); });
    this.$('fmc-form').addEventListener('change', () => { this.invalidate(); this.dependentFields(); });
    this.$('fmc-form').addEventListener('submit', event => { event.preventDefault(); this.plan(); });
    this.$('fmc-run').addEventListener('click', () => this.start());
    this.$('fmc-inspect').addEventListener('click', () => this.inspect());
    this.$('fmc-receipts').addEventListener('change', () => this.loadJob(this.$('fmc-receipts').value));
    this.$('fmc-load-job').addEventListener('click', () => this.loadJob(this.$('fmc-receipt-id').value.trim()));
    this.$('fmc-refresh-job').addEventListener('click', () => this.loadJob(this.selectedJob));
    this.$('fmc-cancel-job').addEventListener('click', () => this.cancel());
    this.$('fmc-retarget-history').addEventListener('click', () => this.openRetarget());
    this.$('fmc-review-play').addEventListener('click', () => this.landmarks?.togglePlay());
    this.$('fmc-review-scrub').addEventListener('input', event => this.landmarks?.seekFrame(Number(event.target.value)));
    this.$('fmc-review-fit').addEventListener('click', () => this.landmarks?.fit());
    this.$('fmc-landmark').addEventListener('change', event => this.landmarks?.select(event.target.value === '' ? null : Number(event.target.value)));
    this.$('fmc-window-load').addEventListener('click', () => this.loadWindowControls());
    for (const [id, direction] of [['fmc-window-previous', -1], ['fmc-window-next', 1]]) this.$(id).addEventListener('click', () => {
      if (this.reviewData) this.loadWindow(adjacentLandmarkWindow(this.reviewData, direction, this.windowCount ?? DEFAULT_WINDOW), this.windowCount ?? DEFAULT_WINDOW);
    });
    this.$('fmc-use-recording').addEventListener('click', () => {
      const receipt = this.receipts.get(this.selectedJob), id = receipt?.recording_id; if (!id || this.busy) return;
      if (receipt.operation === 'calibrate') {
        this.ensureOption(this.$('fmc-calibration-recording'), id); this.$('fmc-calibration-recording').value = id;
        this.$('fmc-calibration-source').value = 'recording';
        if (this.$('fmc-recording').value === id) this.$('fmc-recording').value = '';
        this.setStage('process'); this.message('Camera calibration selected. Choose a separate imported movement recording to process.');
      } else {
        this.ensureOption(this.$('fmc-recording'), id); this.$('fmc-recording').value = id;
        this.setStage(receipt.operation === 'process' && receipt.pipeline_completion_verified ? 'export' : ['calibrate', 'process', 'export'].includes(this.operation) ? this.operation : 'process');
      }
    });
    this.$('fmc-use-sync').addEventListener('click', () => { if (!this.selectedJob || this.busy) return; this.$('fmc-sync-job').value = this.selectedJob; this.setStage('import'); });
    this.renderReceipts(); this.setStage(this.operation);
  }

  $(id) { return this.dialog.querySelector(`#${id}`); }
  message(text, error = false) { this.$('fmc-message').textContent = text; this.$('fmc-message').classList.toggle('error', error); }
  async open() { if (!this.dialog.open) this.dialog.showModal(); await this.refresh(); if (this.selectedJob && this.dialog.open) await this.loadJob(this.selectedJob); }
  async openReceipt(id) {
    if (!validFreeMocapJobId(id)) throw new Error('Choose a valid saved capture receipt.');
    if (!this.dialog.open) this.dialog.showModal();
    if (!this.capabilities) {
      this.$('fmc-runtime').textContent = 'Saved review · runtime not checked';
      this.$('fmc-runtime').classList.remove('ready');
      this.$('fmc-version').textContent = 'Saved files and receipt checks work without a running FreeMoCap service.';
    }
    this.message('Reviewing saved capture evidence locally.');
    await Promise.allSettled([this.refreshHistory(), this.loadJob(id)]);
    if (this.dialog.open) (this.reviewData ? this.$('fmc-review') : this.dialog.querySelector('.fmc-jobs'))?.scrollIntoView({ block: 'start' });
  }
  invalidate() { this.gate.invalidate(); this.$('fmc-plan-state').textContent = 'Needs review'; this.updateRun(); }
  updateRun() {
    const readiness = freeMocapStageReadiness(this.capabilities, this.operation);
    this.$('fmc-stage-readiness').textContent = readiness.note;
    this.$('fmc-stage-readiness').classList.toggle('fmc-warning', !readiness.ready);
    this.$('fmc-stage-readiness').classList.toggle('help', readiness.ready);
    this.$('fmc-plan').disabled = this.busy || !readiness.ready;
    try { this.$('fmc-run').disabled = this.busy || !readiness.ready || !this.gate.ready(this.values()); } catch { this.$('fmc-run').disabled = true; }
  }
  setStage(operation) {
    if (this.busy) return;
    const stage = FREEMOCAP_STAGES.find(item => item.id === operation); if (!stage) return;
    this.operation = operation; this.invalidate();
    for (const item of this.$('fmc-stages').children) item.setAttribute('aria-pressed', String(item.dataset.stage === operation));
    for (const group of this.dialog.querySelectorAll('[data-stages]')) { const active = group.dataset.stages.split(' ').includes(operation); group.hidden = !active; group.disabled = !active; }
    this.$('fmc-stage-title').textContent = stage.title; this.$('fmc-stage-description').textContent = stage.detail;
    this.$('fmc-run').textContent = stage.run; this.dependentFields();
    this.$('fmc-plan-result').replaceChildren(node('p', 'Review this stage and its current settings before running.', 'help'));
  }
  dependentFields() {
    this.$('fmc-brightness-row').hidden = this.$('fmc-sync-method').value !== 'brightness';
    const source = this.$('fmc-calibration-source').value;
    this.$('fmc-calibration-recording-row').hidden = source !== 'recording';
    this.$('fmc-calibration-file-row').hidden = source !== 'reference';
  }
  values() {
    const value = id => this.$(id).value, checked = id => this.$(id).checked;
    return freeMocapRequest({ operation: this.operation, video_files: value('fmc-videos'), recording_id: value('fmc-recording'),
      sync_method: value('fmc-sync-method'), brightness_ratio_threshold: value('fmc-brightness'), sync_job_id: value('fmc-sync-job'), synchronization_confirmed: checked('fmc-sync-confirmed'),
      squares_x: value('fmc-board-x'), squares_y: value('fmc-board-y'), square_length_mm: value('fmc-square-mm'), marker_length_ratio: value('fmc-marker-ratio'), aruco_dictionary_enum: value('fmc-dictionary'), use_groundplane: checked('fmc-ground'),
      detector_type: value('fmc-detector'), cutoff_hz: value('fmc-cutoff'), process_advanced: value('fmc-advanced'), calibration_source: value('fmc-calibration-source'), calibration_file: value('fmc-calibration-file'), calibration_recording_id: value('fmc-calibration-recording'), allow_planar: checked('fmc-planar'),
      fbx: checked('fmc-fbx'), bvh: checked('fmc-bvh'), rest_pose: value('fmc-rest'), apply_foot_locking: checked('fmc-feet'), limit_hand_markers_range_of_motion: checked('fmc-hands') });
  }
  async run(task) {
    if (this.busy) return; this.busy = true; this.$('fmc-fields').disabled = true;
    for (const button of this.$('fmc-stages').children) button.disabled = true;
    this.updateRun();
    try { await task(); } catch (error) { this.message(error.message, true); }
    finally { this.busy = false; this.$('fmc-fields').disabled = false; for (const button of this.$('fmc-stages').children) button.disabled = false; this.updateRun(); }
  }
  ensureOption(select, id) { if (![...select.options].some(option => option.value === id)) { const option = node('option', id); option.value = id; select.append(option); } }
  populateRecordings() {
    for (const id of ['fmc-recording', 'fmc-inspect-recording', 'fmc-calibration-recording']) {
      const select = this.$(id), current = select.value; select.replaceChildren(node('option', 'Choose a recording')); select.firstChild.value = '';
      for (const record of this.recordings) { const option = node('option', `${record.name}${record.video_count ? ` · ${record.video_count} cameras` : ''}`); option.value = record.name; select.append(option); }
      if (current) this.ensureOption(select, current); select.value = current;
    }
  }
  async refresh() {
    const ticket = ++this.refreshToken; this.$('fmc-refresh').disabled = true;
    await this.refreshHistory();
    if (ticket !== this.refreshToken) return;
    try {
      const capabilities = await action('get_freemocap_capabilities', {}); if (ticket !== this.refreshToken) return;
      this.capabilities = capabilities; this.$('fmc-runtime').textContent = capabilities.compatible ? 'Runtime connected' : !capabilities.configured ? 'Setup needed' : capabilities.reachable ? 'Version needs review' : 'Runtime unavailable';
      this.$('fmc-runtime').classList.toggle('ready', capabilities.compatible === true);
      this.$('fmc-version').textContent = `${capabilities.upstream_version || capabilities.reviewed_version || 'FreeMoCap'} · external AGPL-3.0 runtime`;
      if (!capabilities.compatible) { this.gate.invalidate(); this.message(capabilities.error || 'Configure and start the reviewed external FreeMoCap runtime, then refresh.', true); return; }
      const result = await action('list_freemocap_recordings', { limit: 100 }); if (ticket !== this.refreshToken) return;
      this.recordings = result.recordings ?? []; this.populateRecordings();
      this.message('Runtime connected. Choose inputs, review a stage plan, then run that stage explicitly.');
    } catch (error) { if (ticket === this.refreshToken) { this.capabilities = null; this.gate.invalidate(); this.$('fmc-runtime').textContent = 'Connection unavailable'; this.$('fmc-runtime').classList.remove('ready'); this.message(error.message, true); } }
    finally { if (ticket === this.refreshToken) { this.$('fmc-refresh').disabled = false; this.updateRun(); } }
  }
  async plan() {
    if (!this.$('fmc-form').reportValidity()) return;
    await this.run(async () => {
      const request = this.values(), ticket = this.gate.begin(request); this.$('fmc-plan-state').textContent = 'Reviewing…'; this.message('Checking references and preparing the exact stage plan…');
      const plan = await action('plan_freemocap_capture', { request });
      if (!this.gate.accept(ticket, this.values(), plan)) return;
      this.$('fmc-plan-state').textContent = 'Plan reviewed'; const area = this.$('fmc-plan-result'); area.replaceChildren();
      area.append(node('strong', FREEMOCAP_STAGES.find(item => item.id === plan.operation).title));
      const videos = plan.video_metadata?.videos ?? [];
      for (const video of videos) area.append(node('p', `${video.filename} · ${video.frame_count} frames · ${video.fps} fps`, 'help'));
      if (request.recording_id) area.append(node('p', `Recording: ${request.recording_id}`, 'help'));
      area.append(node('p', plan.starts_blender ? 'Running this stage launches background Blender.' : plan.starts_processing ? 'Running this stage starts the external processing pipeline. It does not launch Blender.' : plan.creates_recording ? 'Running this stage creates a new recording copy.' : 'Only this selected stage will run.', 'fmc-warning'));
      const details = node('details', '', 'advanced'), summary = node('summary', 'Inputs, checksums & limitations'); details.append(summary);
      for (const source of plan.sources ?? []) details.append(node('p', `${source.reference || source.path} · SHA256 ${source.sha256}`, 'help'));
      for (const note of plan.notes ?? []) details.append(node('p', String(note), 'help'));
      area.append(details); this.message('Plan ready. Review the inputs and effects, then use the stage button when ready.');
    });
  }
  async start() {
    await this.run(async () => {
      const request = this.values(); if (!freeMocapStageReadiness(this.capabilities, this.operation).ready || !this.gate.ready(request)) throw new Error('Review these exact settings with the required runtime and status extension before running.');
      const selection = this.jobToken, inspected = this.$('fmc-inspect-recording').value;
      this.gate.invalidate(); this.$('fmc-plan-state').textContent = 'Submitted plan'; this.message('Submitting the selected stage. Do not retry while its outcome is unknown…');
      let receipt;
      try { receipt = await action('start_freemocap_capture', { request }); }
      catch (error) {
        const id = error.message.match(/inspect job ([0-9a-f]{32})/i)?.[1];
        if (id) { this.remember({ job_id: id, operation: request.operation, status: 'submission_unknown' }); this.renderReceipts(); if (selection === this.jobToken) await this.loadJob(id); }
        throw error;
      }
      this.remember(receipt); const adopted = selection === this.jobToken;
      if (adopted) { this.selectedJob = receipt.job_id; this.jobToken++; this.renderJob(receipt); }
      this.renderReceipts();
      if (receipt.recording_id && adopted && this.$('fmc-inspect-recording').value === inspected) { this.ensureOption(this.$('fmc-inspect-recording'), receipt.recording_id); this.$('fmc-inspect-recording').value = receipt.recording_id; }
      this.message(freeMocapJobState(receipt));
      if (request.operation === 'import') await this.refresh();
    });
  }
  remember(receipt) {
    if (!validFreeMocapJobId(receipt?.job_id)) throw new Error('The runtime returned no valid managed receipt ID. Inspect server history before retrying.');
    this.receipts.delete(receipt.job_id); this.receipts.set(receipt.job_id, receipt);
    while (this.receipts.size > 100) this.receipts.delete(this.receipts.keys().next().value);
  }
  async refreshHistory() {
    const ticket = ++this.historyToken;
    try {
      const result = await action('list_motion_sources', { source_kind: 'freemocap', limit: 100 });
      if (ticket !== this.historyToken || !this.dialog.open) return;
      const entries = result.sources ?? [], previous = this.receipts; this.receipts = new Map();
      for (const entry of [...entries].reverse()) if (validFreeMocapJobId(entry.source_id)) this.receipts.set(entry.source_id,
        { job_id: entry.source_id, operation: entry.operation, recording_id: entry.recording_id, status: entry.recorded_status, ...previous.get(entry.source_id) });
      if (this.selectedJob && previous.has(this.selectedJob) && !this.receipts.has(this.selectedJob)) this.receipts.set(this.selectedJob, previous.get(this.selectedJob));
      this.renderReceipts(); this.$('fmc-history-state').textContent = `${entries.length} saved stage receipts shown. Use the motion library to browse all sources.`;
    } catch (error) { if (ticket === this.historyToken) this.$('fmc-history-state').textContent = `Saved receipt history unavailable: ${error.message}`; }
  }
  renderReceipts() {
    const select = this.$('fmc-receipts'); select.replaceChildren(node('option', 'Choose a stage receipt')); select.firstChild.value = '';
    for (const receipt of [...this.receipts.values()].reverse()) { const option = node('option', `${receipt.job_id.slice(0, 8)} · ${receipt.operation || 'saved receipt'}${receipt.status ? ` · ${receipt.status}` : ''}`); option.value = receipt.job_id; select.append(option); }
    select.value = this.selectedJob || '';
  }
  async loadJob(id) {
    if (!validFreeMocapJobId(id)) { this.message('Enter a valid 32-character receipt ID from this workspace.', true); return; }
    const token = ++this.jobToken; this.selectedJob = id; this.$('fmc-cancel-job').disabled = true;
    this.reviewGate.select(id); this.quality = null; this.clearReview();
    this.$('fmc-use-recording').disabled = true; this.$('fmc-use-sync').disabled = true;
    this.$('fmc-job-result').replaceChildren(); this.$('fmc-receipt-json').textContent = 'Loading receipt…';
    this.$('fmc-job-state').textContent = 'Loading the saved stage receipt…';
    try { const result = await action('get_freemocap_job', { job_id: id }); if (token !== this.jobToken || id !== this.selectedJob || !this.dialog.open) return; this.remember(result); this.renderReceipts(); await this.renderJob(result); }
    catch (error) { if (token === this.jobToken) { this.$('fmc-job-state').textContent = `Receipt unavailable: ${error.message}`; this.message(error.message, true); } }
  }
  renderJob(job) {
    this.$('fmc-job-state').textContent = `${freeMocapJobState(job)} · ${job.job_id}${job.error ? ` · ${job.error}` : ''}`;
    this.$('fmc-receipt-json').textContent = JSON.stringify(job, null, 2);
    this.$('fmc-cancel-job').disabled = !canCancelFreeMocapJob(job);
    const successful = job.status === 'completed' && job.pipeline_completion_verified;
    this.$('fmc-use-recording').disabled = !job.recording_id || (['calibrate', 'process'].includes(job.operation) && !successful);
    this.$('fmc-use-recording').textContent = job.operation === 'calibrate' ? 'Use as camera calibration' : job.operation === 'process' && successful ? 'Prepare export' : 'Use this recording';
    this.$('fmc-use-sync').disabled = job.operation !== 'synchronize' || job.status !== 'completed';
    const area = this.$('fmc-job-result'); area.replaceChildren();
    if (job.recording_id) area.append(node('p', `Recording: ${job.recording_id}`, 'help'));
    if (!job.pipeline_completion_verified) area.append(node('p', 'Submission and file readiness are separate observations. Do not assume a complete or usable animation from file presence.', 'fmc-warning'));
    if (job.operation === 'check_sync') area.append(node('p', 'Equal frame counts do not verify temporal alignment. Inspect the shared audio/flash and footage before reconstruction.', 'help'));
    const verifiedOutputs = successful && ['process', 'export'].includes(job.operation) && Array.isArray(job.outputs);
    this.renderOutputs(area, verifiedOutputs ? job.outputs : job.recording?.outputs ?? [], verifiedOutputs ? job.job_id : null, verifiedOutputs && job.operation === 'export' ? job : null);
    if (canReviewFreeMocap(job)) {
      if (this.reviewGate.job !== job.job_id) this.reviewGate.select(job.job_id);
      return this.loadWindow(0, DEFAULT_WINDOW);
    } else { this.reviewGate.select(job.job_id); this.clearReview(); }
  }
  renderOutputs(area, outputs, jobId = null, exportJob = null) {
    if (!outputs.length) { area.append(node('p', 'No verified output files reported yet.', 'help')); return; }
    const list = node('ul', '', 'fmc-file-list');
    for (const output of outputs) {
      const item = node('li'); item.append(node('strong', output.path), node('span', `${Number(output.bytes || 0).toLocaleString()} bytes · SHA256 ${String(output.sha256 || '').slice(0, 12)}…`));
      const url = jobId && landmarkArtifactUrl(jobId, output.path);
      if (url) { const link = node('a', 'Download verified file'); link.href = url; link.download = output.path.split('/').pop(); item.append(link); }
      if (exportJob && output.path.toLowerCase().endsWith('.bvh')) { const button = node('button', 'Clean up & retarget', 'small-button'); button.type = 'button'; button.addEventListener('click', () => this.openRetarget(exportJob, output.path)); item.append(button); }
      list.append(item);
    }
    area.append(list);
  }
  async openRetarget(job = null, path = null) {
    try {
      this.retargetPanel ??= new (await import('./capture-retarget-panel.js')).CaptureRetargetPanel();
      if (!this.dialog.open) return;
      if (job) await this.retargetPanel.open(job, path); else await this.retargetPanel.openHistory();
    } catch (error) { this.message(error.message, true); }
  }
  clearReview() {
    this.reviewData = null; this.landmarks?.clear(); this.$('fmc-review').hidden = true;
    this.$('fmc-frame-evidence').textContent = ''; this.$('fmc-landmark-evidence').textContent = '';
    this.$('fmc-quality').replaceChildren(); this.$('fmc-review-frames').replaceChildren();
    for (const id of ['fmc-review-play', 'fmc-review-scrub', 'fmc-review-fit', 'fmc-window-previous', 'fmc-window-next']) this.$(id).disabled = true;
    this.$('fmc-landmark').replaceChildren(node('option', 'Choose a body landmark')); this.$('fmc-landmark').firstChild.value = '';
  }
  loadWindowControls() {
    if (!this.$('fmc-window-start').reportValidity() || !this.$('fmc-window-count').reportValidity()) return;
    const startText = this.$('fmc-window-start').value, countText = this.$('fmc-window-count').value;
    if (!startText.trim() || !countText.trim()) { this.message('Enter the first frame and the number of frames to review.', true); return; }
    this.loadWindow(Number(startText), Number(countText));
  }
  async loadWindow(start, count, focusFrame = null) {
    const job = this.receipts.get(this.selectedJob);
    if (!canReviewFreeMocap(job) || !this.dialog.open) return;
    let url;
    try { url = landmarkPreviewUrl(job.job_id, start, count); } catch (error) { this.message(error.message, true); return; }
    if (this.reviewGate.job !== job.job_id) this.reviewGate.select(job.job_id);
    const ticket = this.reviewGate.begin(), previousName = this.reviewData?.joint_names[this.landmarks?.selected];
    this.clearReview(); this.$('fmc-review').hidden = false; this.$('fmc-review-state').textContent = 'Loading saved landmarks…';
    const qualityRequest = this.quality?.job_id === job.job_id ? Promise.resolve(this.quality) : action('analyze_freemocap_quality', { job_id: job.job_id });
    const [preview, quality] = await Promise.allSettled([request(url), qualityRequest]);
    if (!this.reviewGate.current(ticket) || !this.dialog.open || this.selectedJob !== job.job_id) return;
    try {
      if (preview.status === 'rejected') throw preview.reason;
      const expected = job.outputs?.find(item => item.path === `output_data/${preview.value.detector}_body_3d_xyz.npy`)?.sha256;
      if (!expected) throw new Error('The saved receipt does not contain this body landmark source.');
      const data = validateLandmarkPreview(preview.value, job.job_id, expected);
      this.reviewData = data; this.windowCount = count;
      this.landmarks ??= new LandmarkViewer(this.$('fmc-landmark-viewport'), {
        onError: error => this.message(error.message, true), onFrame: (frame, playing, selected) => this.renderFrame(frame, playing, selected),
        onSelect: selected => { this.$('fmc-landmark').value = selected === null ? '' : String(selected); },
      });
      const selector = this.$('fmc-landmark');
      for (const [index, name] of data.joint_names.entries()) { const option = node('option', `${name}${data.tracked_mask[index] ? '' : ' · virtual center'}`); option.value = String(index); selector.append(option); }
      this.landmarks.loadLandmarks(data);
      if (previousName && data.joint_names.includes(previousName)) this.landmarks.select(data.joint_names.indexOf(previousName));
      this.$('fmc-review-state').textContent = `${data.detector === 'mediapipe' ? 'MediaPipe' : 'RTMPose'} · ${data.joint_names.length} body landmarks`;
      this.$('fmc-review-note').textContent = `${data.coordinates.units === 'pixels' ? 'Planar pixel-space capture; no inferred depth or physical scale.' : data.coordinates.units === 'millimeters' ? 'Calibrated positions in millimeters.' : 'Calibration units are unspecified; no meter scale is assumed.'} Display rotates native Z-up to Y-up without changing scale. Processed positions can include upstream interpolation; visibility is not tracking confidence.${data.fps === null ? ' Frame rate is unavailable for this older receipt; use frame stepping.' : ''}`;
      this.$('fmc-window-start').value = String(data.start_frame); this.$('fmc-window-start').max = String(data.total_frames - 1); this.$('fmc-window-count').value = String(count);
      this.$('fmc-review-scrub').max = String(data.frames - 1); this.$('fmc-review-scrub').disabled = false;
      this.$('fmc-review-play').disabled = data.fps === null || data.frames < 2; this.$('fmc-review-fit').disabled = false;
      this.$('fmc-window-previous').disabled = data.start_frame === 0;
      this.$('fmc-window-next').disabled = data.start_frame + data.frames >= data.total_frames;
      if (focusFrame !== null) this.landmarks.seekFrame(focusFrame - data.start_frame);
      if (quality.status === 'fulfilled' && quality.value.job_id === job.job_id && quality.value.motion_sha256 === data.motion_sha256) {
        this.quality = quality.value; this.renderQuality(quality.value);
      } else this.$('fmc-quality').textContent = quality.status === 'rejected' ? `Numerical review unavailable: ${quality.reason.message}` : 'Numerical review does not match the selected body output.';
    } catch (error) {
      this.clearReview(); this.$('fmc-review').hidden = false; this.$('fmc-review-state').textContent = 'Saved preview unavailable';
      this.$('fmc-review-note').textContent = error.message;
    }
  }
  renderFrame(frame, playing, selected) {
    if (!this.reviewData) return;
    const data = this.reviewData, evidence = landmarkFrameEvidence(data, frame, selected);
    this.$('fmc-review-scrub').value = String(frame); this.$('fmc-review-play').textContent = playing ? 'Pause' : 'Play window';
    this.$('fmc-frame-evidence').textContent = `Original frame ${evidence.frame} of ${data.total_frames - 1}${evidence.time === null ? ' · time unavailable' : ` · ${evidence.time.toFixed(3)} s`} · ${evidence.valid}/${evidence.total} processed landmarks available. Window ${data.start_frame}–${data.start_frame + data.frames - 1}.`;
    const point = evidence.selected;
    this.$('fmc-landmark-evidence').textContent = point ? `${point.name} · ${point.tracked ? 'tracked-landmark position after processing' : 'derived virtual center'} · ${point.valid ? `native X ${point.position[0].toFixed(3)}, Y ${point.position[1].toFixed(3)}, Z ${point.position[2].toFixed(3)} ${evidence.units}` : 'missing in this frame; no replacement is shown'}` : 'Select a point or choose its name to inspect native coordinates.';
  }
  renderQuality(report) {
    const area = this.$('fmc-quality'); area.replaceChildren();
    const coverage = report.coverage, movement = report.movement;
    if (coverage && Number.isFinite(coverage.valid_fraction)) area.append(node('p', `${(100 * coverage.valid_fraction).toFixed(1)}% of processed body points are finite. ${coverage.frames_with_missing_landmarks} frames contain missing points. This is output availability, not detection accuracy.`));
    if (Array.isArray(coverage?.longest_missing_run_frames)) area.append(node('p', `Longest missing run for any body landmark: ${Math.max(0, ...coverage.longest_missing_run_frames)} frames.`));
    if (movement?.available && Number.isFinite(movement.maximum) && Number.isFinite(movement.p95)) area.append(node('p', `Adjacent-frame movement: peak ${movement.maximum.toFixed(3)} ${movement.unit}; 95th percentile ${movement.p95.toFixed(3)} ${movement.unit}. Inspect original frame ${movement.worst_frame_index}. Missing intervals are excluded.`));
    else area.append(node('p', 'No eligible adjacent-frame movement measurements are available.'));
    area.append(node('p', 'These checks do not score animation quality or establish contact, rig or retargeting accuracy.'));
    for (const frame of (report.review_frames ?? []).filter(value => Number.isInteger(value) && value >= 0 && value < this.reviewData.total_frames).slice(0, 24)) {
      const button = node('button', `Inspect frame ${frame}`, 'small-button'); button.type = 'button';
      button.addEventListener('click', () => {
        if (!this.reviewData) return;
        if (frame >= this.reviewData.start_frame && frame < this.reviewData.start_frame + this.reviewData.frames) this.landmarks.seekFrame(frame - this.reviewData.start_frame);
        else this.loadWindow(Math.floor(frame / (this.windowCount ?? DEFAULT_WINDOW)) * (this.windowCount ?? DEFAULT_WINDOW), this.windowCount ?? DEFAULT_WINDOW, frame);
      });
      this.$('fmc-review-frames').append(button);
    }
  }
  async inspect() {
    const id = this.$('fmc-inspect-recording').value; if (!id) { this.message('Choose a recording to inspect.', true); return; }
    const token = ++this.inspectToken; this.$('fmc-inspection').replaceChildren(node('p', 'Checking contained output files and checksums…', 'help'));
    try {
      const report = await action('inspect_freemocap_recording', { recording_id: id });
      if (token !== this.inspectToken || id !== this.$('fmc-inspect-recording').value) return;
      const area = this.$('fmc-inspection'); area.replaceChildren(node('strong', id));
      for (const stage of report.upstream_readiness?.stages ?? []) area.append(node('p', `${stage.complete ? '✓' : '○'} ${stage.name} · ${stage.present_count ?? 0}/${stage.total_count ?? 0} files`, 'help'));
      area.append(node('p', 'Readiness describes files on disk, not capture quality or guaranteed pipeline completion.', 'help')); this.renderOutputs(area, report.outputs ?? []);
    } catch (error) { if (token === this.inspectToken) this.$('fmc-inspection').replaceChildren(node('p', error.message, 'fmc-warning')); }
  }
  async cancel() {
    const id = this.selectedJob, job = this.receipts.get(id); if (!canCancelFreeMocapJob(job)) return;
    const token = ++this.jobToken;
    this.$('fmc-cancel-job').disabled = true;
    try { const result = await action('cancel_freemocap_job', { job_id: id }); this.remember(result); if (id === this.selectedJob && token === this.jobToken) { this.renderJob(result); this.renderReceipts(); } }
    catch (error) { if (id === this.selectedJob && token === this.jobToken) { this.message(error.message, true); this.$('fmc-cancel-job').disabled = false; } }
  }
}
