import './styles.css';
import { action, anchorFrame, buildBrief, description, duration, jobId, request, revisionChanges, safeLocalUrl, sampleCount, sampleName, setToken } from './api.js';
import { MotionViewer } from './viewer.js';

const $ = id => document.getElementById(id);
const state = { jobs: [], selected: null, sample: 0, revision: null, revisionBase: null, connected: false, polling: false, previewUrl: null, previewLoaded: false, loadedInitial: false, jobSignature: '', comparisonSignature: '', selectionSignature: '', previews: new Map(), inspecting: 0, busy: new Set() };
const presets = {
  wave: 'A person stands in place and slowly waves with their right hand.',
  walk: 'A person walks forward at a relaxed, steady pace with natural arm swings.',
  bow: 'A person stands upright, bows politely from the waist, then returns to standing.',
};
const el = (tag, className, text) => { const node = document.createElement(tag); if (className) node.className = className; if (text !== undefined) node.textContent = text; return node; };
const current = () => state.jobs.find(job => jobId(job) === state.selected);
const currentKey = () => `${state.selected}:${state.sample}`;
const completed = () => current()?.status === 'completed';

function notice(message, error = false) {
  $('notice-text').textContent = message; $('notice').classList.toggle('error', error); $('notice').hidden = false;
}
$('dismiss-notice').addEventListener('click', () => { $('notice').hidden = true; });

function viewerMessage(title, detail) {
  $('viewer-message').hidden = false;
  $('viewer-message').querySelector('strong').textContent = title;
  $('viewer-message').querySelector('p').textContent = detail;
}
let viewer;
try {
  viewer = new MotionViewer($('viewport'), {
    onProgress: text => viewerMessage('Loading 3D preview', text),
    onLoaded: info => {
      state.previewLoaded = true;
      $('viewer-message').hidden = true;
      $('asset-label').textContent = `${info.bones} bones · ${info.animations ? 'animated' : 'static'} GLB`;
      $('play').disabled = !info.duration; $('scrubber').disabled = !info.duration;
      $('scrubber').max = String(info.duration || 1);
      enableActions();
    },
    onError: error => { state.previewLoaded = false; viewerMessage('Preview could not load', error.message); $('play').disabled = true; $('scrubber').disabled = true; enableActions(); },
    onTime: (time, total, playing) => {
      $('time-label').textContent = `${time.toFixed(2)} / ${total.toFixed(2)} s`;
      $('scrubber').value = String(time); $('play').textContent = playing ? 'Ⅱ' : '▶';
      $('play').setAttribute('aria-label', playing ? 'Pause animation' : 'Play animation');
      if (!total) { $('play').disabled = true; $('scrubber').disabled = true; }
      if ($('start-pose').checked && current()?.manifest) $('anchor-note').textContent = `Starting from candidate ${state.sample + 1}, frame ${anchorFrame(time, current().manifest.frames, current().manifest.fps)}. Seamless blending is not guaranteed.`;
    },
  });
} catch (error) { viewerMessage('3D graphics are unavailable', 'Enable browser hardware acceleration to use the model viewer. Generation and reviews remain available.'); }

$('play').addEventListener('click', () => viewer?.togglePlay());
$('scrubber').addEventListener('input', event => viewer?.seek(event.target.value));
$('speed').addEventListener('change', event => { if (viewer) viewer.speed = Number(event.target.value); });
$('reset-camera').addEventListener('click', () => viewer?.fit());
for (const [id, method] of [['wireframe', 'setWireframe'], ['skeleton', 'setSkeleton']]) {
  $(id).addEventListener('click', () => { const enabled = $(id).getAttribute('aria-pressed') !== 'true'; $(id).setAttribute('aria-pressed', String(enabled)); viewer?.[method](enabled); });
}

function brief() {
  const beats = $('sequence-mode').checked ? [...$('beats').children].map(row => ({ text: row.querySelector('textarea').value, duration: row.querySelector('input').value })) : undefined;
  const value = buildBrief({ description: $('description').value, duration: $('duration').value, quality: $('quality').value, candidates: $('candidates').value, seed: $('seed').value, beats });
  if (value.seed === undefined) value.seed = crypto.getRandomValues(new Uint32Array(1))[0];
  return value;
}
function startFrom() {
  if (!$('start-pose').checked || $('start-pose').disabled || state.revision) return {};
  const job = current();
  return { start_from: { job_id: jobId(job), sample_index: state.sample, frame: anchorFrame(viewer.time, job.manifest.frames, job.manifest.fps) } };
}
$('start-pose').addEventListener('change', () => { if ($('start-pose').checked) viewer?.seek(viewer.time); $('plan-summary').hidden = true; });
function countCharacters() { $('character-count').textContent = `${$('description').value.length} / 2000`; $('plan-summary').hidden = true; }
$('description').addEventListener('input', countCharacters);
function updateSequence() {
  const enabled = $('sequence-mode').checked; $('sequence-panel').hidden = !enabled; $('duration').disabled = enabled;
  document.querySelector('label[for="description"]').textContent = enabled ? 'Overall scene context' : 'Describe the motion';
  const rows = [...$('beats').children];
  rows.forEach((row, index) => { row.querySelector('span').textContent = `Action ${index + 1}`; row.querySelector('button').disabled = rows.length <= 2; row.querySelector('textarea').disabled = !enabled; row.querySelector('input').disabled = !enabled; });
  $('add-beat').disabled = rows.length >= 8;
  $('sequence-total').textContent = `${rows.length} actions · ${rows.reduce((sum, row) => sum + (Number(row.querySelector('input').value) || 0), 0).toFixed(1)} seconds total`;
  $('plan-summary').hidden = true;
}
function addBeat(text = '', seconds = 2) {
  if ($('beats').children.length >= 8) return;
  const row = el('div', 'beat'), head = el('div', 'beat-head'), remove = el('button', '', 'Remove'); remove.type = 'button';
  head.append(el('span'), remove); row.append(head);
  const input = el('textarea'); input.rows = 2; input.maxLength = 2000; input.required = true; input.value = text; input.setAttribute('aria-label', 'Action description'); input.placeholder = 'A person stands and begins to wave.';
  const timing = el('label', '', 'Duration'), length = el('input'); length.type = 'number'; length.min = '0.2'; length.max = '10'; length.step = '0.1'; length.value = String(seconds); length.required = true;
  timing.append(length, el('span', '', 'seconds')); row.append(input, timing);
  input.addEventListener('input', updateSequence); length.addEventListener('input', updateSequence); remove.addEventListener('click', () => { row.remove(); updateSequence(); });
  $('beats').append(row); updateSequence();
}
$('sequence-mode').addEventListener('change', () => { if ($('sequence-mode').checked && !$('beats').children.length) { addBeat(); addBeat(); } updateSequence(); });
$('add-beat').addEventListener('click', () => addBeat());
for (const id of ['duration', 'quality', 'candidates', 'seed']) $(id).addEventListener('input', () => { $('plan-summary').hidden = true; });
document.querySelectorAll('[data-preset]').forEach(button => button.addEventListener('click', () => { $('description').value = presets[button.dataset.preset]; countCharacters(); $('description').focus(); }));
$('clear-revision').addEventListener('click', () => { state.revision = null; state.revisionBase = null; $('sequence-mode').disabled = false; $('revision-note').hidden = true; $('generate-label').textContent = 'Generate animation'; enableActions(); });

async function withBusy(button, task, message) {
  if (state.busy.has(button.id)) return;
  state.busy.add(button.id); button.disabled = true; button.classList.add('busy');
  if (message) notice(message);
  try { return await task(); } catch (error) { notice(error.message, true); }
  finally { state.busy.delete(button.id); button.classList.remove('busy'); button.disabled = false; enableActions(); }
}

$('plan').addEventListener('click', () => withBusy($('plan'), async () => {
  if (!$('create-form').reportValidity()) return;
  const input = brief(), changes = state.revision ? revisionChanges(input, state.revisionBase) : null;
  if (state.revision && !Object.keys(changes).length) throw new Error('Change a setting or the description to preview a revision.');
  const result = state.revision
    ? await action('revise_animation', { job_id: state.revision, revision: changes, submit: false })
    : await action('plan_animation', { brief: input, ...startFrom() });
  const plan = result.plan ?? {}, generated = result.request ?? {};
  if (generated.seed !== undefined) $('seed').value = generated.seed;
  const box = $('plan-summary'); box.replaceChildren();
  box.append(el('strong', '', `${plan.duration_seconds ?? brief().duration_seconds}s · ${plan.total_frames ?? Math.floor(brief().duration_seconds * 30)} frames · ${generated.samples ?? brief().candidates} candidate(s)`));
  box.append(el('p', '', `Seed ${generated.seed ?? 'assigned at submission'} · ${generated.steps ?? 'planned'} generation steps`));
  for (const note of (result.notes ?? []).slice(0, 4)) box.append(el('p', '', typeof note === 'string' ? note : note.message ?? 'Review the planned request.'));
  box.hidden = false;
}));

$('create-form').addEventListener('submit', event => {
  event.preventDefault();
  withBusy($('generate'), async () => {
    const input = brief();
    const changes = state.revision ? revisionChanges(input, state.revisionBase) : null;
    if (state.revision && !Object.keys(changes).length) throw new Error('Change the description or a setting to create a revision. The original request stays saved.');
    const result = state.revision
      ? await action('revise_animation', { job_id: state.revision, revision: changes, submit: true })
      : await action('generate_animation', { brief: input, ...startFrom() });
    const submitted = result.job ?? result;
    state.selected = jobId(submitted) ?? state.selected; state.sample = 0; state.selectionSignature = '';
    notice('Animation queued. Its progress will appear in your animation history.');
    await refresh();
  });
});

function enableActions() {
  for (const id of ['review', 'compare', 'build-preview', 'send-blender', 'send-unreal']) $(id).disabled = !completed() || !state.connected || state.busy.has(id);
  for (const id of ['generate', 'plan']) $(id).disabled = !state.connected || state.busy.has(id);
  const job = current(), preview = selectedPreview(job, state.sample);
  const canAnchor = completed() && !state.revision && state.previewLoaded && ['somaskel30', 'somaskel77'].includes(job?.manifest?.skeleton) && safeLocalUrl(preview?.url) === state.previewUrl;
  $('start-pose').disabled = !canAnchor;
  if (!canAnchor) $('start-pose').checked = false;
}

function timeAgo(job) {
  const source = job.created_at ?? job.created ?? 0;
  const date = new Date(typeof source === 'number' && source < 1e12 ? source * 1000 : source);
  if (!Number.isFinite(date.getTime()) || !date.getTime()) return jobId(job)?.slice(0, 6) ?? '';
  return date.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' });
}

function revise(job) {
  state.revision = jobId(job);
  $('description').value = description(job);
  $('duration').value = Math.min(duration(job), 10);
  $('seed').value = job.request?.seed ?? '';
  $('candidates').value = job.request?.samples ?? 1;
  $('quality').value = job.request?.steps >= 70 ? 'detailed' : job.request?.steps <= 20 ? 'draft' : 'balanced';
  $('beats').replaceChildren();
  $('sequence-mode').checked = job.request?.segments?.length > 1;
  $('sequence-mode').disabled = $('sequence-mode').checked;
  if ($('sequence-mode').checked) for (const segment of job.request.segments) addBeat(segment.text, segment.duration);
  updateSequence();
  state.revisionBase = brief();
  $('revision-note').hidden = false; $('generate-label').textContent = 'Generate revision';
  countCharacters(); $('description').focus();
  enableActions();
}

function renderJobs() {
  const signature = JSON.stringify([state.jobs, state.selected]);
  if (signature === state.jobSignature) return;
  state.jobSignature = signature; $('job-count').textContent = String(state.jobs.length);
  if (!state.jobs.length) return;
  $('job-list').replaceChildren();
  for (const job of state.jobs) {
    const id = jobId(job), active = ['queued', 'running', 'cancelling'].includes(job.status);
    const card = el('article', `job-card${id === state.selected ? ' selected' : ''}`);
    const select = el('button', 'job-select'); select.setAttribute('aria-label', `${job.status}: ${description(job)}`);
    const top = el('div', 'job-top'); top.append(el('span', `job-status ${['completed', 'failed', 'running', 'queued'].includes(job.status) ? job.status : ''}`, job.status ?? 'unknown'), el('time', 'job-time', timeAgo(job)));
    select.append(top, el('p', 'job-title', description(job)), el('p', 'job-meta', `${duration(job).toFixed(1)}s · ${sampleCount(job)} candidate(s) · seed ${job.request?.seed ?? '—'}`));
    select.addEventListener('click', () => selectJob(id)); card.append(select);
    if (active) {
      const stage = job.progress?.stage ?? job.stage ?? job.status;
      card.append(el('p', 'job-stage', String(stage).replaceAll('_', ' ')));
      const progress = el('progress'); progress.setAttribute('aria-label', 'Generation in progress');
      if (Number.isFinite(job.progress?.current) && job.progress?.total > 0) { progress.max = job.progress.total; progress.value = job.progress.current; }
      card.append(progress);
    }
    if (job.status === 'failed') card.append(el('p', 'job-stage', typeof job.error === 'string' ? job.error : job.error?.message ?? 'Generation failed. Your request is preserved.'));
    const controls = el('div', 'job-controls');
    const log = el('button', '', 'View log'); log.id = `log-${id}`;
    log.addEventListener('click', () => withBusy(log, async () => {
      const result = await action('get_job_log', { job_id: id, lines: 100 });
      $('log-content').textContent = result.log || 'The worker has not written any messages for this job yet.';
      $('log-dialog').showModal();
    })); controls.append(log);
    if (active) {
      const cancel = el('button', '', 'Cancel'); cancel.id = `cancel-${id}`;
      cancel.addEventListener('click', () => withBusy(cancel, async () => { await action('cancel_job', { job_id: id }); await refresh(); })); controls.append(cancel);
    } else {
      const edit = el('button', '', 'Revise'); edit.addEventListener('click', () => revise(job)); controls.append(edit);
      if (['failed', 'cancelled'].includes(job.status)) {
        const retry = el('button', '', 'Retry request'); retry.id = `retry-${id}`;
        retry.addEventListener('click', () => withBusy(retry, async () => { const result = await action('generate_motion', { request: job.request }); state.selected = jobId(result.job ?? result); await refresh(); })); controls.append(retry);
      }
    }
    card.append(controls); $('job-list').append(card);
  }
}

function selectedPreview(job, sample) {
  const saved = Array.isArray(job?.previews) ? job.previews.filter(item => item.sample_index === sample).sort((a, b) => (b.attempt ?? 0) - (a.attempt ?? 0))[0] : null;
  const cached = state.previews.get(`${jobId(job)}:${sample}`);
  if (!cached || (saved?.attempt ?? -1) > (cached.attempt ?? -1)) return saved;
  return { ...cached, exports: cached.exports?.length ? cached.exports : (saved?.attempt === cached.attempt ? saved.exports : []) };
}

function renderDownloads(job) {
  $('downloads').replaceChildren();
  const preview = selectedPreview(job, state.sample);
  const entries = [...(job?.downloads ?? []).filter(item => item.sample_index === state.sample), ...(preview?.exports ?? [])];
  if (preview?.url && !entries.some(item => item.url === preview.url)) entries.push({ label: 'Character GLB', url: preview.url });
  const seen = new Set();
  for (const entry of entries) {
    const url = safeLocalUrl(entry.url); if (!url || seen.has(url)) continue; seen.add(url);
    const link = el('a', '', `↓ ${entry.label ?? entry.format?.toUpperCase() ?? entry.path ?? 'Download'}`);
    link.href = url; link.download = ''; $('downloads').append(link);
  }
}

function renderComparisonOptions() {
  const signature = JSON.stringify(state.jobs.filter(item => item.status === 'completed').map(item => [jobId(item), sampleCount(item), description(item)]));
  if (signature === state.comparisonSignature) return; state.comparisonSignature = signature;
  const previous = $('compare-target').value; $('compare-target').replaceChildren(el('option', '', 'Choose a completed candidate')); $('compare-target').firstChild.value = '';
  for (const job of state.jobs.filter(item => item.status === 'completed')) for (let sample = 0; sample < sampleCount(job); sample++) {
    const option = el('option', '', `${description(job).slice(0, 60)} · candidate ${sample + 1} · ${jobId(job).slice(0, 6)}`);
    option.value = `${jobId(job)}:${sample}`; $('compare-target').append(option);
  }
  if ([...$('compare-target').options].some(item => item.value === previous)) $('compare-target').value = previous;
}

function loadPreview(url) {
  const safe = safeLocalUrl(url);
  if (!safe) return false;
  if (state.previewUrl !== safe) { state.previewLoaded = false; state.previewUrl = safe; viewer?.load(safe); enableActions(); }
  return true;
}

async function selectJob(id, sample = 0) {
  if (id !== state.selected || sample !== state.sample) $('start-pose').checked = false;
  state.selected = id; state.sample = sample; state.selectionSignature = ''; renderJobs(); renderSelection();
}

function metric(label, value, note) {
  const node = el('div', 'metric'); node.append(el('span', '', label), el('strong', '', value));
  if (note) node.append(el('small', '', note));
  return node;
}

async function renderSelection() {
  const job = current(); enableActions();
  if (!job) return;
  renderDownloads(job);
  const signature = JSON.stringify([jobId(job), job.status, job.manifest, job.previews, state.sample]);
  if (signature === state.selectionSignature) return;
  state.selectionSignature = signature;
  const token = ++state.inspecting;
  $('viewer-title').textContent = job.status === 'completed' ? `Candidate ${state.sample + 1}` : 'Animation in progress';
  $('selected-description').textContent = description(job);
  $('candidate-count').textContent = String(job.status === 'completed' ? sampleCount(job) : 0);
  $('candidates-list').replaceChildren(); $('diagnostic-note').textContent = '';
  $('metrics').replaceChildren(metric('Duration', `${duration(job).toFixed(2)} s`), metric('Frames', String(job.manifest?.frames ?? '—')), metric('Seed', String(job.request?.seed ?? '—')));
  if (job.status !== 'completed') {
    $('candidates-list').append(el('div', 'empty-inline', ['failed', 'cancelled'].includes(job.status) ? 'This job has no completed candidates. Revise or retry its saved request.' : 'Candidates appear after generation finishes. You can keep creating while this job runs.'));
    viewer?.clear(); state.previewUrl = null; state.previewLoaded = false;
    viewerMessage(job.status === 'failed' ? 'Generation needs attention' : job.status === 'cancelled' ? 'Generation cancelled' : 'Your animation is on its way', 'The completed character preview will appear after you build the selected candidate.');
    return;
  }
  for (let index = 0; index < sampleCount(job); index++) {
    const button = el('button', 'candidate'); button.setAttribute('aria-pressed', String(index === state.sample));
    button.append(el('strong', '', `Candidate ${index + 1}`), el('small', '', `${job.manifest?.frames ?? '—'} frames · ${job.manifest?.fps ?? 30} fps`), el('small', '', selectedPreview(job, index) ? 'Character preview ready' : 'Motion ready · build preview'));
    if (index === state.sample) button.append(el('span', 'candidate-mark', '✓'));
    button.addEventListener('click', () => selectJob(jobId(job), index)); $('candidates-list').append(button);
  }
  const preview = selectedPreview(job, state.sample);
  if (!preview || !loadPreview(preview.url)) {
    viewer?.clear(); state.previewUrl = null; state.previewLoaded = false; $('asset-label').textContent = 'Motion ready';
    viewerMessage('Bring this motion to your character', 'Build the character preview to rig and retarget this candidate in Blender. The original motion stays saved.');
  }
  try {
    const result = await action('analyze_motion_quality', { job_id: jobId(job), name: sampleName(state.sample) });
    if (token !== state.inspecting) return;
    const labels = { contact_sliding: 'Contact sliding', jerk: 'Peak jerk', bone_length_drift: 'Bone drift' };
    for (const [key, label] of Object.entries(labels)) {
      const item = result.metrics?.[key];
      if (!item?.available) $('metrics').append(metric(label, 'Unavailable', item?.reason ?? 'Required data is unavailable.'));
      else $('metrics').append(metric(label, `${Number(item.max).toPrecision(3)} ${item.unit}`, `Max at frame ${item.worst_frame_index} · ${item.above_threshold ?? 0} values above sensitivity`));
    }
    $('diagnostic-note').textContent = `Key span ${Number(result.last_key_seconds).toFixed(4)} s. Threshold crossings are inspection cues, not a quality score.`;
  } catch (error) { if (token === state.inspecting) $('diagnostic-note').textContent = `Diagnostics unavailable: ${error.message}`; }
}

async function refresh() {
  if (state.polling) return;
  state.polling = true;
  try {
    const result = await request('/api/state'); setToken(result.csrf_token);
    state.connected = true; state.jobs = Array.isArray(result.jobs) ? result.jobs : [];
    $('connection').textContent = 'Connected';
    const health = result.health ?? {}, worker = health.worker_service ?? result.worker ?? {};
    const ready = worker.available ?? worker.running ?? worker.status === 'ready';
    const capacity = health.capacity ?? {}, memoryWarning = capacity.cpu_text_encoder_warning;
    $('runtime-status').textContent = ready ? (memoryWarning ? 'Worker connected · memory is limited' : 'Worker connected') : 'Worker not ready';
    $('runtime-detail').textContent = memoryWarning || (ready ? 'Worker is available. Model access and available memory are checked when generating.' : 'Start the local worker to generate. Saved animation reviews remain available.');
    $('runtime-status').closest('.runtime-card').classList.toggle('warning', Boolean(memoryWarning));
    const target = result.targets?.unreal;
    if (target && !$('ue-project').value) {
      for (const [field, id] of Object.entries({ project_file: 'ue-project', skeleton_path: 'ue-skeleton', skeletal_mesh_path: 'ue-mesh', destination_path: 'ue-destination', asset_name: 'ue-name' })) $(id).value = target[field] ?? '';
    }
    if (!state.selected && !state.loadedInitial) {
      const initial = result.character?.preview_url;
      if (initial && loadPreview(initial)) {
        state.loadedInitial = true; $('viewer-title').textContent = result.character?.name ?? 'Registered character preview';
        $('selected-description').textContent = 'Registered preview. Select a completed animation from your history to work with its candidates.';
      } else if (!state.jobs.length) viewerMessage('Your character stage is ready', 'Generate an animation, then build a character preview. A registered character and Blender connection are required.');
    }
    renderJobs(); renderComparisonOptions(); renderSelection(); enableActions();
  } catch (error) {
    state.connected = false; $('connection').textContent = 'Disconnected'; $('runtime-status').textContent = 'Local server unavailable';
    $('runtime-detail').textContent = error.message;
    if (!state.loadedInitial && !state.selected) viewerMessage('Connect to your local studio', 'Start the Kimodo studio server, then refresh this workspace.');
    enableActions();
  } finally { state.polling = false; }
}
$('refresh').addEventListener('click', refresh);

async function buildPreview(button) {
  const job = current(), index = state.sample, key = currentKey();
  if (job?.status !== 'completed') return;
  await withBusy(button, async () => {
    $('export-status').textContent = 'Preparing the fitted character and executing the checked Blender handoff…';
    const result = await action('build_character_preview', { job_id: jobId(job), sample_index: index });
    if (!safeLocalUrl(result.url)) throw new Error('Blender finished but did not return a usable local preview URL. Refresh the workspace to check the export.');
    const preview = { sample_index: index, url: result.url, attempt: result.attempt, exports: result.exports ?? [] };
    state.previews.set(key, preview);
    $('export-status').textContent = 'Blender finished. The character and animation have been saved.';
    notice('Character preview ready. You can inspect it or import it into Unreal.');
    if (currentKey() === key) { state.selectionSignature = ''; renderSelection(); }
    await refresh();
  }, 'Building the character preview in Blender. This can take a few minutes.');
}
$('build-preview').addEventListener('click', () => buildPreview($('build-preview')));
$('send-blender').addEventListener('click', () => buildPreview($('send-blender')));

$('review').addEventListener('click', () => {
  const job = current(), sample = state.sample;
  if (!job) return;
  // Reserve a tab during the user gesture; async popup creation is often blocked.
  const tab = window.open('about:blank', '_blank'); if (tab) tab.opener = null;
  withBusy($('review'), async () => {
    try {
      const result = await action('create_motion_review', { job_id: jobId(job), name: sampleName(sample) });
      const url = safeLocalUrl(result.url);
      if (!url) throw new Error('The review was created but no local browser URL was returned.');
      if (tab) tab.location.href = url; else { const link = el('a', '', 'Open the completed frame review'); link.href = url; link.target = '_blank'; link.rel = 'noopener'; $('diagnostic-note').replaceChildren(link); }
    } catch (error) { tab?.close(); throw error; }
  });
});

$('compare').addEventListener('click', () => {
  const target = $('compare-target').value;
  if (!target) { notice('Choose a completed candidate to compare.', true); return; }
  const [otherJob, sample] = target.split(':'), offset = Number($('compare-offset').value);
  if (!Number.isInteger(offset) || Math.abs(offset) > 2400) { notice('Frame offset must be a whole number between −2400 and 2400.', true); return; }
  const args = { job_id: state.selected, other_job_id: otherJob, name: sampleName(state.sample), other_name: sampleName(Number(sample)), offset_frames: offset, space: $('compare-space').value };
  const tab = window.open('about:blank', '_blank'); if (tab) tab.opener = null;
  withBusy($('compare'), async () => {
    try {
      const result = await action('compare_motions', args), url = safeLocalUrl(result.url);
      if (!url) throw new Error('Comparison did not return a local review URL.');
      if (tab) tab.location.href = url; else { const link = el('a', '', 'Open the completed comparison'); link.href = url; link.target = '_blank'; link.rel = 'noopener'; $('diagnostic-note').replaceChildren(link); }
    } catch (error) { tab?.close(); throw error; }
  });
});
$('close-log').addEventListener('click', () => $('log-dialog').close());

$('send-unreal').addEventListener('click', () => withBusy($('send-unreal'), async () => {
  const job = current(), sample = state.sample;
  if (!job) return;
  const preview = selectedPreview(job, sample);
  if (!preview) throw new Error('Build this candidate’s character preview first so Unreal imports the matching rig.');
  const target = {};
  for (const [field, id] of Object.entries({ project_file: 'ue-project', skeleton_path: 'ue-skeleton', skeletal_mesh_path: 'ue-mesh', destination_path: 'ue-destination', asset_name: 'ue-name' })) {
    target[field] = $(id).value.trim(); if (!target[field]) { $(id).focus(); throw new Error('Complete every Unreal target field before importing.'); }
  }
  const args = { job_id: jobId(job), source_kind: 'character', sample_index: sample, attempt: preview.attempt ?? 0, target };
  $('export-status').textContent = 'Checking the Unreal target and preparing the import…';
  await action('prepare_unreal_handoff', args);
  $('export-status').textContent = 'Importing into the matching open Unreal project…';
  const result = await action('execute_unreal_handoff', args);
  const report = result.report ?? result;
  $('export-status').textContent = `Unreal import completed. ${report.animation_asset ?? report.asset_path ?? target.destination_path + '/' + target.asset_name}`;
  notice('Animation imported and checked in Unreal.');
}));

function setTab(name) {
  for (const item of ['details', 'export']) { $(`tab-${item}`).setAttribute('aria-selected', String(item === name)); $(`${item}-panel`).hidden = item !== name; }
}
$('tab-details').addEventListener('click', () => setTab('details'));
$('tab-export').addEventListener('click', () => setTab('export'));
document.querySelector('.inspect-tabs').addEventListener('keydown', event => {
  if (['ArrowLeft', 'ArrowRight'].includes(event.key)) { event.preventDefault(); const target = $('tab-details').getAttribute('aria-selected') === 'true' ? 'export' : 'details'; setTab(target); $(`tab-${target}`).focus(); }
});

enableActions(); refresh();
setInterval(() => { if (!document.hidden) refresh(); }, 3500);
document.addEventListener('visibilitychange', () => { if (!document.hidden) refresh(); });
