import { action, request } from './api.js';
import { RigViewer } from './rig-viewer.js';
import { CAPTURE_ARTIFACTS, CAPTURE_TERMINAL, CaptureGate, CaptureSelection, captureDiagnosticLines, captureEvidence, captureFileUrl, captureOption, capturePreview, checkVideoFile, videoRequest } from './video-data.js';
import './video.css';

const node = (tag, text, className) => { const item = document.createElement(tag); if (text !== undefined) item.textContent = text; if (className) item.className = className; return item; };

export class VideoPanel {
  constructor({ getToken }) {
    this.getToken = getToken; this.gate = new CaptureGate(); this.selection = new CaptureSelection(); this.listEpoch = 0;
    this.dialog = document.createElement('dialog'); this.dialog.className = 'video-dialog';
    this.dialog.setAttribute('aria-labelledby', 'video-heading');
    this.dialog.innerHTML = `
      <div class="video-dialog-heading"><div><span class="eyebrow">REFERENCE CAPTURE</span><h2 id="video-heading">Turn a performance into motion</h2></div><button id="video-close" class="small-button" type="button" autofocus>Close</button></div>
      <p class="help">Estimate one person's body motion from a short video, inspect it beside the reference, then export for retargeting. Clear, continuous full-body footage works best.</p>
      <p id="video-message" class="video-message" role="status">Choose a video to check the capture setup.</p>
      <div class="video-workspace">
        <section class="video-setup" aria-label="Video capture setup">
          <form id="video-form"><fieldset id="video-fields">
            <label class="field-label" for="video-file">Choose a video <span>up to 512 MB</span></label><input id="video-file" type="file" accept=".mp4,.mov,.mkv,.webm,.avi,.m4v" />
            <label class="field-label" for="video-path">Or enter its full local path</label><input id="video-path" type="text" placeholder="D:\\References\\performance.mp4" required />
            <p class="help">File selection saves a local reference copy. A path can point to a larger file. Your original is preserved.</p>
            <div class="video-input-grid"><label>Start · seconds<input id="video-start" type="number" min="0" max="86400" step="any" value="0" required /></label><label>Length · seconds<input id="video-duration" type="number" min="0.01" max="80" step="any" value="4" required /></label><label>Frames per second<input id="video-fps" type="number" min="1" max="60" step="any" value="30" required /></label><label>Body detector<select id="video-detector"><option value="mp-pose">MediaPipe Pose</option><option value="yolo-hrnet">YOLO + HRNet</option></select></label></div>
            <p class="help">Choose 6 to 2,400 sampled frames. Use a static camera. Cuts, missing bodies or multiple people may need a shorter trim; camera-relative depth and scale remain estimates.</p>
            <details class="advanced"><summary>Brief occlusions</summary><label class="field-label" for="video-gap">Bridge brief occlusions · frames</label><input id="video-gap" type="number" min="0" max="15" step="1" value="0" required /><p class="help">Zero requires a usable detection in every frame. A higher value can estimate short internal gaps between detected poses. Bridged frames stay marked for review; cuts, clip edges and ambiguous people are not bridged.</p></details>
            <button id="video-check" class="button secondary full" type="submit">Check video &amp; setup</button>
            <button id="video-start-capture" class="button primary full" type="button" disabled>Capture this motion <span>↗</span></button>
          </fieldset></form>
          <div id="video-checks" class="video-checks" aria-live="polite"></div>
          <details class="advanced"><summary>Setup &amp; capture limits</summary><p class="help">EasyMocap runs in a separate local environment with its required model files. The setup check identifies missing dependencies. No model download or installation runs automatically.</p><p class="help">The result is an estimated SMPL body skeleton. Fingers, face, precise world movement, physical contacts and your character's deformation are not guaranteed. Retarget and inspect the exported motion before using it in Unreal.</p><p class="help">EasyMocap's current license requires registration for organizational projects. Body models and detector weights have separate terms.</p><a href="https://github.com/zju3dv/EasyMocap/blob/e6006fd3814d5f8ad45a7ce965beb9bcc90767f2/LICENSE" target="_blank" rel="noopener noreferrer">Read the source license ↗</a></details>
        </section>
        <section class="video-review" aria-label="Captured motion review">
          <div class="video-job-controls"><label for="video-job">Saved captures<select id="video-job"><option value="">No captures yet</option></select></label><button id="video-refresh" class="small-button" type="button">Refresh</button><button id="video-cancel" class="small-button" type="button" disabled>Cancel capture</button></div>
          <p id="video-job-status" class="help" role="status">Capture jobs are separate from your generated animations.</p>
          <div class="video-compare"><figure><figcaption>Trimmed reference</figcaption><video id="video-reference" controls muted playsinline preload="metadata"></video></figure><figure><figcaption>Estimated body skeleton</figcaption><div id="video-rig" class="video-rig"><p id="video-empty" class="help">The captured skeleton will appear here.</p></div></figure></div>
          <div class="video-playback"><button id="video-play" class="small-button" type="button" disabled>Play</button><input id="video-scrubber" aria-label="Captured motion time" type="range" min="0" max="1" step="0.001" value="0" disabled /><output id="video-time">0.00 / 0.00 s</output><button id="video-fit" class="small-button" type="button" disabled>Fit skeleton</button></div>
          <p id="video-joint" class="help">Click a joint to identify it. Captured SMPL motion is reviewed here and retargeted through Blender.</p>
          <div id="video-diagnostics" class="video-diagnostics"></div>
          <details id="video-confidence" class="advanced" hidden><summary>2D detection confidence at this frame</summary><p id="video-evidence" class="help"></p><p class="help">These are BODY25 image detections, not confidence scores for the fitted 3D joints. Times identify requested samples; source frames may have been resampled.</p><div class="video-table-scroll"><table class="video-confidence-table"><thead><tr><th>Image landmark</th><th>Confidence</th><th>Pixel position</th></tr></thead><tbody id="video-confidence-rows"></tbody></table></div></details>
          <div id="video-downloads" class="video-downloads"></div>
          <details class="advanced"><summary>Capture log</summary><pre id="video-log" class="video-log">No capture selected.</pre></details>
        </section>
      </div>`;
    document.body.append(this.dialog);
    this.$('video-form').noValidate = true;
    this.$('video-close').addEventListener('click', () => this.dialog.close());
    this.dialog.addEventListener('close', () => { this.$('video-reference').pause(); if (this.viewer) this.viewer.playing = false; clearTimeout(this.timer); this.selection.invalidate(); this.listEpoch++; cancelAnimationFrame(this.referenceFrame); });
    this.$('video-form').addEventListener('submit', event => { event.preventDefault(); this.check(); });
    this.$('video-form').addEventListener('input', () => { this.gate.invalidate(); this.$('video-start-capture').disabled = true; this.$('video-checks').replaceChildren(); });
    this.$('video-file').addEventListener('change', () => this.upload());
    this.$('video-start-capture').addEventListener('click', () => this.start());
    this.$('video-refresh').addEventListener('click', () => this.refresh());
    this.$('video-job').addEventListener('change', () => this.select(this.$('video-job').value));
    this.$('video-cancel').addEventListener('click', () => this.cancel());
    this.$('video-play').addEventListener('click', () => this.togglePlay());
    this.$('video-scrubber').addEventListener('input', () => {
      this.seek(Number(this.$('video-scrubber').value));
    });
    this.$('video-fit').addEventListener('click', () => this.viewer?.fit());
    this.$('video-confidence').addEventListener('toggle', () => { this.evidenceFrame = null; this.renderEvidence(this.viewer?.time ?? 0); });
    const reference = this.$('video-reference');
    reference.addEventListener('timeupdate', () => { if (this.viewer?.data) this.viewer.seek(reference.currentTime); this.$('video-play').textContent = reference.paused ? 'Play' : 'Pause'; });
    reference.addEventListener('play', () => this.syncReference());
    reference.addEventListener('error', () => { if (reference.getAttribute('src')) this.message('The analyzed reference could not play. Download it to inspect it; the skeleton is still available.', true); });
    for (const event of ['play', 'pause', 'ended']) reference.addEventListener(event, () => { this.$('video-play').textContent = reference.paused ? 'Play' : 'Pause'; });
  }

  $(id) { return this.dialog.querySelector(`#${id}`); }
  message(text, error = false) { const target = this.$('video-message'); target.textContent = text; target.classList.toggle('error', error); }
  values() { return videoRequest({ video_path: this.$('video-path').value, start_seconds: this.$('video-start').value, duration_seconds: this.$('video-duration').value, fps: this.$('video-fps').value, detector: this.$('video-detector').value, max_gap_frames: this.$('video-gap').value }); }
  async open() { if (!this.dialog.open) this.dialog.showModal(); this.viewer?.resize(); await this.refresh(); }
  async run(fn) {
    if (this.busy) return;
    this.busy = true; this.$('video-fields').disabled = true;
    try { await fn(); } catch (error) { this.message(error.message, true); }
    finally { this.busy = false; this.$('video-fields').disabled = false; this.updateStart(); }
  }
  updateStart() { try { this.$('video-start-capture').disabled = this.busy || !this.gate.ready(this.values()); } catch { this.$('video-start-capture').disabled = true; } }
  togglePlay() {
    const video = this.$('video-reference');
    if (video.getAttribute('src') && !video.error) {
      if (video.paused) { if (video.ended) video.currentTime = 0; video.play().catch(error => this.message(error.message, true)); } else video.pause();
    } else this.viewer?.togglePlay();
  }
  seek(time) { const video = this.$('video-reference'); video.pause(); if (video.getAttribute('src') && !video.error) video.currentTime = time; this.viewer?.seek(time); }
  syncReference() {
    cancelAnimationFrame(this.referenceFrame);
    const video = this.$('video-reference');
    if (!this.dialog.open || video.paused || video.ended) return;
    if (this.viewer?.data) this.viewer.seek(video.currentTime);
    this.referenceFrame = requestAnimationFrame(() => this.syncReference());
  }

  async upload() {
    const file = this.$('video-file').files[0]; if (!file) return;
    this.gate.invalidate();
    await this.run(async () => {
      const extension = checkVideoFile(file); this.message('Copying the reference to your local workspace…');
      const response = await fetch(`/api/video-mocap/reference?extension=${extension}`, { method: 'POST', credentials: 'same-origin', headers: { 'Content-Type': 'application/octet-stream', 'X-Studio-Token': this.getToken() }, body: file });
      const value = await response.json().catch(() => ({ error: 'The local server returned an unreadable upload response.' })); if (!response.ok || value.error) throw new Error(value.error || 'The reference could not be saved.');
      this.$('video-path').value = value.path;
      this.message('Reference copied locally. Check the selected trim and capture setup.');
    });
    this.$('video-file').value = '';
  }

  async check() {
    const invalid = this.$('video-form').querySelector('input:invalid, select:invalid');
    if (invalid) { const details = invalid.closest('details'); if (details) details.open = true; }
    if (!this.$('video-form').reportValidity()) return;
    await this.run(async () => {
      const request = this.values(), ticket = this.gate.begin(request);
      this.message('Checking the video, capture tools and required models…');
      const plan = await action('plan_video_mocap', { request });
      if (!this.gate.accept(ticket, this.values(), plan)) return;
      this.$('video-checks').replaceChildren();
      for (const check of plan.preflight?.checks ?? plan.checks ?? []) {
        const row = node('div', undefined, `video-check ${check.ready ? 'ready' : 'missing'}`);
        const label = { source_video: 'Reference video', runtime_configuration: 'Capture environment', reviewed_source: 'Reviewed EasyMocap version', model_assets: 'Body model and weights', python_dependencies: 'Capture libraries', ffmpeg: 'Reference video encoder', video_metadata: 'Video information', selected_time_range: 'Selected trim' }[check.name] ?? check.name;
        row.append(node('strong', `${check.ready ? '✓' : '○'} ${label}`), node('p', check.detail || ''));
        this.$('video-checks').append(row);
      }
      this.message(plan.ready ? 'Ready to capture this trim. Review the settings, then start when ready.' : 'Capture needs the setup items listed below. Your reference and settings are kept.');
    });
  }

  async start() {
    await this.run(async () => {
      const args = this.gate.consume(this.values()), selection = this.selection.snapshot();
      this.updateStart();
      let job;
      try { job = await action('start_video_mocap', args); }
      catch (error) { throw new Error(`${error.message} Refresh saved captures before checking and trying again; the start response was not confirmed.`); }
      this.message('Capture submitted. Follow its stages below; completion still requires review.');
      if (this.dialog.open && this.selection.current(selection)) await this.select(job.id);
      await this.refresh();
    });
  }

  async refresh(selectId) {
    if (!this.dialog.open) return;
    const epoch = ++this.listEpoch, selection = this.selection.snapshot(); clearTimeout(this.timer);
    let active = false;
    try {
      const result = await action('list_video_mocap_jobs', { limit: 40 });
      if (epoch !== this.listEpoch || !this.dialog.open) return;
      const jobs = Array.isArray(result) ? result : result.jobs ?? [];
      active = jobs.some(job => !CAPTURE_TERMINAL.has(job.status));
      const chosen = (this.selection.current(selection) ? selectId : null) ?? this.selected ?? jobs[0]?.id ?? '';
      if (this.currentJob?.id === chosen && !jobs.some(job => job.id === chosen)) jobs.push(this.currentJob);
      this.$('video-job').replaceChildren(node('option', jobs.length ? 'Choose a capture' : 'No captures yet'));
      this.$('video-job').firstChild.value = '';
      for (const job of jobs) {
        const option = node('option', captureOption(job, job.id).label);
        option.value = job.id; this.$('video-job').append(option);
      }
      this.$('video-job').value = chosen;
      if (chosen && chosen !== this.selected) await this.select(chosen);
      else if (chosen) await this.loadJob(chosen);
    } catch (error) { if (epoch === this.listEpoch) this.message(error.message, true); active = true; }
    finally { if (epoch === this.listEpoch && this.dialog.open && active) this.timer = setTimeout(() => this.refresh(), 2500); }
  }

  async select(id) {
    this.selection.select(id); this.selected = id; this.currentJob = null;
    this.clearReview(); this.$('video-job').value = id;
    this.$('video-job-status').textContent = id ? 'Loading the selected capture…' : 'Choose a saved capture to review.';
    this.$('video-log').textContent = id ? 'Loading capture log…' : 'No capture selected.';
    if (!id) return;
    const selection = this.selection.snapshot();
    try { await this.loadJob(id); } catch (error) { if (this.selection.current(selection)) this.message(error.message, true); }
  }

  clearReview() {
    this.loaded = null; this.preview = null; this.evidenceFrame = null;
    this.viewer?.clear(); const video = this.$('video-reference'); video.pause(); video.removeAttribute('src'); video.load();
    this.$('video-empty').hidden = false; this.$('video-downloads').replaceChildren(); this.$('video-diagnostics').replaceChildren();
    for (const control of ['video-play', 'video-scrubber', 'video-fit', 'video-cancel']) this.$(control).disabled = true;
    this.$('video-time').textContent = '0.00 / 0.00 s'; this.$('video-scrubber').value = '0';
    this.$('video-confidence').hidden = true; this.$('video-confidence-rows').replaceChildren();
    this.$('video-joint').textContent = 'Click a joint to identify it. This SMPL24 capture needs explicit retargeting for your character.';
  }

  async loadJob(id) {
    if (id !== this.selected) return;
    const token = this.selection.beginRead();
    const job = await action('get_video_mocap_job', { job_id: id });
    if (!this.selection.currentRead(token)) return;
    const choice = captureOption(job, id), selector = this.$('video-job');
    let option = [...selector.options].find(item => item.value === choice.value);
    if (!option) { option = node('option'); option.value = choice.value; selector.append(option); }
    option.textContent = choice.label; selector.value = choice.value;
    this.currentJob = job;
    if (job.status !== 'completed' || job.verification_error || !job.output) this.clearReview();
    this.$('video-job-status').textContent = `${job.status} · ${job.stage || 'waiting'}${job.error || job.verification_error ? ` — ${job.error || job.verification_error}` : ''}`;
    this.$('video-cancel').disabled = CAPTURE_TERMINAL.has(job.status) || job.status === 'cancelling';
    this.$('video-diagnostics').replaceChildren(...captureDiagnosticLines(job).map(line => node('p', line)));
    try {
      const log = await action('get_video_mocap_log', { job_id: id, lines: 80 });
      if (!this.selection.currentRead(token)) return;
      this.$('video-log').textContent = log.text || 'No log output yet.';
    } catch (error) { if (this.selection.currentRead(token)) this.$('video-log').textContent = `Log unavailable: ${error.message}`; }
    if (!this.selection.currentRead(token) || job.status !== 'completed' || job.verification_error || !job.output) return;
    const loadedKey = `${id}:${job.output.artifacts?.find(artifact => artifact.name === 'motion.npz')?.sha256}`;
    if (this.loaded === loadedKey) return;
    const data = capturePreview(await request(`/api/video-mocap/${id}/preview`), id, job);
    if (!this.selection.currentRead(token)) return;
    this.clearReview(); this.preview = data;
    if (!this.viewer) this.viewer = new RigViewer(this.$('video-rig'), {
      onLoaded: () => {}, onProgress: () => {}, onError: error => this.message(error.message, true),
      onSelect: index => { this.viewer.select(index); this.$('video-joint').textContent = index === null ? 'Choose a joint to identify it.' : `${this.viewer.data.joint_names[index]} · estimated body joint`; },
      onTime: (time, total, playing) => { this.$('video-scrubber').max = String(total || 1); this.$('video-scrubber').value = String(time); this.$('video-time').textContent = `${time.toFixed(2)} / ${total.toFixed(2)} s`; this.renderEvidence(time); if (!this.$('video-reference').getAttribute('src') || this.$('video-reference').error) this.$('video-play').textContent = playing ? 'Pause' : 'Play'; },
    });
    if (!this.viewer.captureKeys) {
      this.viewer.captureKeys = true;
      this.viewer.renderer.domElement.addEventListener('keydown', event => {
        if (!this.preview || event.altKey || event.ctrlKey || event.metaKey || ![' ', 'ArrowLeft', 'ArrowRight', 'Home', 'End'].includes(event.key)) return;
        event.preventDefault(); event.stopImmediatePropagation();
        if (event.key === ' ') this.togglePlay();
        else this.seek(event.key === 'Home' ? 0 : event.key === 'End' ? this.viewer.duration : Math.max(0, Math.min(this.viewer.duration, this.viewer.time + (event.key === 'ArrowLeft' ? -1 : 1) / this.preview.fps)));
      }, true);
    }
    this.viewer.loadMotion(data); this.$('video-empty').hidden = true;
    this.viewer.resize(); this.$('video-confidence').hidden = false;
    for (const control of ['video-play', 'video-scrubber', 'video-fit']) this.$(control).disabled = false;
    const artifacts = job.output?.artifacts ?? [];
    for (const artifact of artifacts) {
      const url = captureFileUrl(id, artifact.name); if (!url) continue;
      const link = node('a', CAPTURE_ARTIFACTS[artifact.name], 'small-button'); link.href = url; link.download = artifact.name; this.$('video-downloads').append(link);
      if (artifact.name === 'reference.mp4') this.$('video-reference').src = url;
    }
    this.$('video-diagnostics').append(node('strong', `${data.frames} frames · ${data.fps} fps · 24 body joints`), node('p', 'Estimated camera coordinates, not calibrated world or ground motion. Retarget the BVH in Blender and review the result on your character before Unreal import.'), ...captureDiagnosticLines(job).map(line => node('p', line)));
    for (const note of job.diagnostics?.warnings ?? job.output?.warnings ?? []) this.$('video-diagnostics').append(node('p', String(note)));
    this.loaded = loadedKey;
  }

  renderEvidence(time) {
    if (!this.preview) return;
    const evidence = captureEvidence(this.preview, time);
    if (this.evidenceFrame === evidence.frame) return;
    this.evidenceFrame = evidence.frame;
    this.$('video-evidence').textContent = `Frame ${evidence.frame}${evidence.estimated ? ' · BRIDGED 2D ESTIMATE' : ' · detected 2D evidence'} · source sample ${evidence.sourceTime.toFixed(3)} s · ${evidence.usable}/25 landmarks above 5% confidence · mean ${(evidence.mean * 100).toFixed(0)}%.`;
    if (!this.$('video-confidence').open) return;
    this.$('video-confidence-rows').replaceChildren(...evidence.points.map(point => {
      const row = node('tr'); row.append(node('th', point.name), node('td', `${(point.confidence * 100).toFixed(0)}%`), node('td', `${point.x.toFixed(1)}, ${point.y.toFixed(1)}`)); row.firstChild.scope = 'row'; return row;
    }));
  }

  async cancel() {
    const id = this.selected; if (!id) return;
    const selection = this.selection.snapshot();
    this.$('video-cancel').disabled = true;
    try { await action('cancel_video_mocap_job', { job_id: id }); await this.refresh(); }
    catch (error) { if (this.selection.current(selection)) { this.message(error.message, true); await this.loadJob(id).catch(() => {}); } }
  }
}
