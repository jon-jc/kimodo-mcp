import './styles.css';
import './rig.css';
import './sequence.css';
import { action, anchorFrame, buildBrief, description, duration, getToken, jobId, request, revisionChanges, safeLocalUrl, sampleCount, sampleName, setToken } from './api.js';
import { MotionViewer } from './viewer.js';
import { RigPanel } from './rig-panel.js';
import { selectedDownloads, selectedPreview as previewFor, sourceKey, variantArgs } from './rig-data.js';
import { friendlyPlanningError, needsFreshSeed, PlanGate, savedPlanMetadata, savedPlanRoute, savedSequenceNotes, sharedFrameTime, STARTER_SEQUENCES, WORKFLOW_STEPS, workflowKey, workflowState } from './sequence.js';
import { SequenceComposer } from './sequence-composer.js';
import { retainSelectedJob, sourceIdentity } from './source-library-data.js';

const $ = id => document.getElementById(id);
const state = { jobs: [], selected: null, sample: 0, variant: null, viewerMode: 'character', characterInfo: null, revision: null, revisionBase: null, connected: false, polling: false, previewUrl: null, previewLoaded: false, loadedInitial: false, jobSignature: '', comparisonSignature: '', selectionSignature: '', previews: new Map(), verifiedOutputs: new Map(), inspecting: 0, busy: new Set() };
state.planGate = new PlanGate(); state.loadGate = new PlanGate(); state.libraryGate = new PlanGate(); state.savedPlan = null; state.savedBaseline = null; state.libraryOffset = 0; state.workflow = 'create';
state.selectionEpoch = 0;
const presets = {
  wave: 'A person stands in place and slowly waves with their right hand.',
  walk: 'A person walks forward at a relaxed, steady pace with natural arm swings.',
  bow: 'A person stands upright, bows politely from the waist, then returns to standing.',
};
const el = (tag, className, text) => { const node = document.createElement(tag); if (className) node.className = className; if (text !== undefined) node.textContent = text; return node; };
const current = () => state.jobs.find(job => jobId(job) === state.selected);
const currentKey = () => sourceKey(state.selected, state.sample, state.variant);
const completed = () => current()?.status === 'completed';
const selectedVariant = () => current()?.variants?.find(item => item.variant_id === state.variant && item.sample_index === state.sample);
const selectedTiming = () => selectedVariant() ?? (rigPanel?.loadedKey === currentKey() ? rigPanel.data : null) ?? current()?.manifest ?? {};
const activeViewer = () => state.viewerMode === 'source' ? rigPanel?.viewer : viewer;

function notice(message, error = false) {
  $('notice-text').textContent = message; $('notice').classList.toggle('error', error); $('notice').hidden = false;
}
$('dismiss-notice').addEventListener('click', () => { $('notice').hidden = true; });
let videoPanel;
$('open-video-capture').addEventListener('click', async () => {
  try {
    videoPanel ??= new (await import('./video-panel.js')).VideoPanel({ getToken });
    await videoPanel.open();
  } catch (error) { notice(error.message, true); }
});
let freeMocapPanel;
$('open-freemocap').addEventListener('click', async () => {
  try {
    freeMocapPanel ??= new (await import('./freemocap-panel.js')).FreeMocapPanel();
    await freeMocapPanel.open();
  } catch (error) { notice(error.message, true); }
});
let sourceLibrary;
$('open-source-library').addEventListener('click', async () => {
  try {
    sourceLibrary ??= new (await import('./source-library.js')).SourceLibrary({ onOpen: openMotionSource });
    await sourceLibrary.open();
  } catch (error) { notice(error.message, true); }
});
async function openMotionSource(source) {
  sourceIdentity(source);
  if (source.source_kind === 'freemocap') {
    freeMocapPanel ??= new (await import('./freemocap-panel.js')).FreeMocapPanel();
    await freeMocapPanel.openReceipt(source.source_id);
  } else if (source.source_kind === 'easymocap') {
    videoPanel ??= new (await import('./video-panel.js')).VideoPanel({ getToken });
    await videoPanel.open();
    await videoPanel.select(source.source_id);
  } else {
    const epoch = ++state.selectionEpoch;
    const job = await request(`/api/jobs/${source.source_id}`);
    if (epoch !== state.selectionEpoch) return;
    state.jobs = [job, ...state.jobs.filter(item => jobId(item) !== source.source_id)];
    await selectJob(source.source_id);
  }
}

function viewerMessage(title, detail) {
  $('viewer-message').hidden = false;
  $('viewer-message').querySelector('strong').textContent = title;
  $('viewer-message').querySelector('p').textContent = detail;
}
let viewer, rigPanel;
function showTime(kind, time, total, playing) {
  if (state.viewerMode !== kind) return;
  $('time-label').textContent = `${time.toFixed(2)} / ${total.toFixed(2)} s`;
  $('scrubber').max = String(total || 1); $('scrubber').value = String(time); $('play').textContent = playing ? 'Ⅱ' : '▶';
  $('play').setAttribute('aria-label', playing ? 'Pause animation' : 'Play animation');
  $('play').disabled = !total; $('scrubber').disabled = !total;
  if (kind === 'source') rigPanel?.updatePosition();
  const timing = selectedTiming();
  if ($('start-pose').checked && timing.frames) $('anchor-note').textContent = `Starting from candidate ${state.sample + 1}${state.variant ? ', selected version' : ''}, frame ${anchorFrame(time, timing.frames, timing.fps)}. Seamless blending is not guaranteed.`;
  if ($('start-pose').checked && state.planGate.result) { try { if (!state.planGate.current(draftInput())) invalidatePlan(); } catch { invalidatePlan(); } }
}
try {
  viewer = new MotionViewer($('viewport'), {
    onProgress: text => viewerMessage('Loading 3D preview', text),
    onLoaded: info => {
      state.previewLoaded = true; state.characterInfo = info;
      $('viewer-message').hidden = true;
      if (state.viewerMode === 'character') { $('asset-label').textContent = `${info.bones} fitted bones · ${info.animations ? 'animated' : 'static'} GLB`; showTime('character', 0, info.duration, false); }
      enableActions();
    },
    onError: error => { state.previewLoaded = false; viewerMessage('Preview could not load', error.message); if (state.viewerMode === 'character') showTime('character', 0, 0, false); enableActions(); },
    onTime: (time, total, playing) => showTime('character', time, total, playing),
  });
} catch (error) { viewerMessage('3D graphics are unavailable', 'Enable browser hardware acceleration to use the model viewer. Generation and reviews remain available.'); }

try {
  rigPanel = new RigPanel({
    getSelectionEpoch: () => state.selectionEpoch,
    onDraftPreviewChange: () => enableActions(),
    onTime: (time, total, playing) => showTime('source', time, total, playing),
    onLoaded: info => { if (state.viewerMode === 'source') $('asset-label').textContent = `${info.bones} source joints · exact frames`; enableActions(); },
    onError: error => { notice(error.message, true); enableActions(); },
    onVariant: async (result, snapshot) => {
      const job = state.jobs.find(item => jobId(item) === jobId(snapshot.job));
      if (job) { job.variants ??= []; if (!job.variants.some(item => item.variant_id === result.variant_id)) job.variants.push(result); }
      const adopt = snapshot.should_select !== false && snapshot.selection_epoch === state.selectionEpoch && currentKey() === snapshot.key;
      if (adopt) { state.selectionEpoch++; state.variant = result.variant_id; state.selectionSignature = ''; $('start-pose').checked = false; setViewerMode('source'); await renderSelection(); }
      notice(adopt ? 'New motion version saved. Inspect its joints and contact diagnostics before building a fitted-character preview.' : 'The earlier correction snapshot was saved. Your current selection and newer draft remain in place.');
      await refresh();
    },
  });
} catch (error) { $('rig-status').textContent = `Source viewer unavailable: ${error.message}`; }

function setViewerMode(mode) {
  const outgoing = activeViewer(), incoming = mode === 'source' ? rigPanel?.viewer : viewer;
  const preview = selectedPreview(current(), state.sample, state.variant);
  const matchingViews = completed() && rigPanel?.loadedKey === currentKey() && state.previewLoaded && safeLocalUrl(preview?.url) === state.previewUrl;
  const transferredTime = mode !== state.viewerMode && matchingViews ? sharedFrameTime(outgoing?.time, selectedTiming(), incoming?.duration) : null;
  if (outgoing) outgoing.playing = false;
  state.viewerMode = mode;
  $('viewport').hidden = mode !== 'character'; $('rig-viewport').hidden = mode !== 'source'; $('rig-workspace').hidden = mode !== 'source' || state.workflow !== 'review';
  $('mode-character').setAttribute('aria-pressed', String(mode === 'character')); $('mode-source').setAttribute('aria-pressed', String(mode === 'source'));
  $('rig-focus').hidden = mode !== 'source' || state.workflow !== 'review';
  if (mode !== 'source') setRigFocus(false);
  else $('rig-focus').disabled = window.innerWidth <= 1000;
  $('wireframe').disabled = mode !== 'character'; $('skeleton').disabled = mode !== 'character';
  const active = activeViewer(); active?.resize(); active?.fit();
  if (transferredTime !== null) active?.seek(transferredTime);
  $('asset-label').textContent = mode === 'source' ? (rigPanel?.data ? `${rigPanel.data.joint_names.length} source joints · exact frames` : 'Source rig') : (state.previewLoaded ? `${state.characterInfo?.bones ?? '—'} fitted bones · GLB` : 'Fitted character');
  showTime(mode, active?.time ?? 0, active?.duration ?? 0, false); enableActions();
}
function setRigFocus(enabled) {
  state.focusRig = Boolean(enabled && state.workflow === 'review' && state.viewerMode === 'source' && window.innerWidth > 1000);
  document.querySelector('.workspace').classList.toggle('rig-focus', state.focusRig);
  $('rig-focus').setAttribute('aria-pressed', String(state.focusRig));
  $('rig-focus').textContent = state.focusRig ? 'Exit rig focus' : 'Focus rig editor';
  $('rig-focus').disabled = window.innerWidth <= 1000;
  requestAnimationFrame(() => { activeViewer()?.resize(); activeViewer()?.fit(); });
}
function updateWorkflowHint() {
  $('workflow-hint').textContent = state.workflow === 'create'
    ? 'Capture a performance with FreeMoCap, fit a single video, or describe a motion. Each route checks its inputs before processing.'
    : state.workflow === 'review'
      ? completed() ? 'Play the selected version, inspect its source rig, and save small corrections. Originals stay saved.' : 'Choose a completed animation from Animations to review its candidates and make corrections.'
      : completed() ? 'Download this version or prepare its matching Blender and Unreal handoffs.' : 'Choose a completed animation from Animations before exporting.';
}
function setWorkflow(step) {
  const changed = state.workflow !== step;
  Object.assign(state, workflowState(state, step));
  const workspace = document.querySelector('.workspace'); workspace.dataset.workflow = step;
  workspace.classList.toggle('rig-focus', state.focusRig);
  for (const name of WORKFLOW_STEPS) {
    const button = $(`workflow-${name}`), active = name === step;
    button.setAttribute('aria-pressed', String(active));
    if (active) button.setAttribute('aria-current', 'step'); else button.removeAttribute('aria-current');
  }
  $('rig-workspace').hidden = step !== 'review' || state.viewerMode !== 'source';
  $('rig-focus').hidden = step !== 'review' || state.viewerMode !== 'source';
  $('rig-focus').setAttribute('aria-pressed', String(state.focusRig));
  $('rig-focus').textContent = state.focusRig ? 'Exit rig focus' : 'Focus rig editor';
  setTab(state.inspectTab); updateWorkflowHint();
  if (changed) {
    document.querySelector('.studio-main').scrollTop = 0;
    if (window.innerWidth <= 760) window.scrollTo(0, 0);
  }
  requestAnimationFrame(() => { activeViewer()?.resize(); });
}
for (const step of WORKFLOW_STEPS) {
  $(`workflow-${step}`).addEventListener('click', () => setWorkflow(step));
  $(`workflow-${step}`).addEventListener('keydown', event => {
    const next = workflowKey(step, event.key); if (!next) return;
    event.preventDefault(); setWorkflow(next); $(`workflow-${next}`).focus();
  });
}
$('rig-focus').addEventListener('click', () => { setWorkflow('review'); setRigFocus(!state.focusRig); });
window.addEventListener('resize', () => { if (state.focusRig && window.innerWidth <= 1000) setRigFocus(false); $('rig-focus').disabled = window.innerWidth <= 1000; });
$('mode-source').addEventListener('click', () => { setWorkflow('review'); setViewerMode('source'); });
$('edit-skeleton').addEventListener('click', () => { setWorkflow('review'); setViewerMode('source'); setRigFocus(window.innerWidth > 1000); });
$('mode-character').addEventListener('click', () => setViewerMode('character'));
$('motion-version').addEventListener('change', () => { state.selectionEpoch++; state.variant = $('motion-version').value || null; state.selectionSignature = ''; $('start-pose').checked = false; renderSelection(); });
$('play').addEventListener('click', () => activeViewer()?.togglePlay());
$('scrubber').addEventListener('input', event => activeViewer()?.seek(event.target.value));
$('speed').addEventListener('change', event => { for (const item of [viewer, rigPanel?.viewer]) if (item) item.speed = Number(event.target.value); });
$('reset-camera').addEventListener('click', () => activeViewer()?.fit());
for (const [id, method] of [['wireframe', 'setWireframe'], ['skeleton', 'setSkeleton']]) {
  $(id).addEventListener('click', () => { const enabled = $(id).getAttribute('aria-pressed') !== 'true'; $(id).setAttribute('aria-pressed', String(enabled)); viewer?.[method](enabled); });
}

function brief(resolveSeed = false) {
  const beats = $('sequence-mode').checked ? composer.getBeats() : undefined;
  const value = buildBrief({ description: $('description').value, duration: $('duration').value, quality: $('quality').value, candidates: $('candidates').value, seed: $('seed').value, beats, model: state.composerModel });
  if (resolveSeed && needsFreshSeed(value, state.savedPlan, state.savedBaseline)) { value.seed = crypto.getRandomValues(new Uint32Array(1))[0]; $('seed').value = value.seed; }
  return value;
}
function startFrom() {
  if (!$('start-pose').checked || $('start-pose').disabled || state.revision || state.savedPlan) return {};
  const job = current();
  const timing = selectedTiming();
  return { start_from: { job_id: jobId(job), sample_index: state.sample, ...variantArgs(state.variant), frame: anchorFrame(activeViewer().time, timing.frames, timing.fps) } };
}
$('start-pose').addEventListener('change', () => { if ($('start-pose').checked) activeViewer()?.seek(activeViewer().time); invalidatePlan(); });
function invalidatePlan() { state.planGate.invalidate(); composer?.setPlan(null); $('plan-summary').hidden = true; renderSavedState(); }
function countCharacters() { $('character-count').textContent = `${$('description').value.length} / 2000`; invalidatePlan(); }
$('description').addEventListener('input', countCharacters);
const composer = new SequenceComposer({ container: $('beats'), timeline: $('sequence-timeline'), total: $('sequence-total'), onChange: invalidatePlan, onError: error => notice(error.message, true) });
function updateSequence() {
  const enabled = $('sequence-mode').checked; $('sequence-panel').hidden = !enabled; $('duration').disabled = enabled;
  document.querySelector('.workspace').classList.toggle('sequence-authoring', enabled);
  document.querySelector('label[for="description"]').textContent = enabled ? 'Sequence notes · not a motion prompt' : 'Describe the motion';
  composer.setEnabled(enabled); invalidatePlan();
}
$('sequence-mode').addEventListener('change', () => { if ($('sequence-mode').checked && !composer.beats.length) composer.replace([{ text: '', duration: 2 }, { text: '', duration: 2 }]); updateSequence(); });
$('add-beat').addEventListener('click', () => composer.add());
for (const id of ['duration', 'quality', 'candidates', 'seed']) $(id).addEventListener('input', invalidatePlan);
$('apply-starter').addEventListener('click', () => {
  const starter = STARTER_SEQUENCES[$('sequence-starter').value]; if (!starter) return;
  $('sequence-mode').checked = true; $('description').value = starter.description;
  if (!state.savedPlan) $('saved-plan-title').value = starter.title;
  composer.replace(starter.beats); updateSequence(); countCharacters(); composer.focus(composer.beats[0].id);
  notice('Starter actions loaded as editable text and timing. Review each action before previewing the plan.');
});
document.querySelectorAll('[data-preset]').forEach(button => button.addEventListener('click', () => { $('description').value = presets[button.dataset.preset]; countCharacters(); $('description').focus(); }));
$('clear-revision').addEventListener('click', () => { state.revision = null; state.revisionBase = null; state.revisionRequest = null; $('sequence-mode').disabled = false; $('revision-note').hidden = true; $('generate-label').textContent = 'Generate animation'; invalidatePlan(); enableActions(); });

async function withBusy(button, task, message) {
  if (state.busy.has(button.id)) return;
  state.busy.add(button.id); button.disabled = true; button.classList.add('busy'); renderSavedState();
  if (message) notice(message);
  try { return await task(); } catch (error) { notice(friendlyPlanningError(error.message), true); }
  finally { state.busy.delete(button.id); button.classList.remove('busy'); button.disabled = false; enableActions(); }
}

function draftInput() {
  return { brief: brief(), start_from: startFrom().start_from ?? null, saved_plan_id: state.savedPlan?.plan_id ?? null,
    revision_job_id: state.revision ?? null, clear_constraints: $('clear-plan-constraints').checked };
}
function planMetadata() {
  return savedPlanMetadata(state.savedPlan, { title: $('saved-plan-title').value, sequence: $('sequence-mode').checked, notes: $('description').value }, state.savedContext);
}
function loadInput() {
  // This must also work while the visible draft is incomplete and cannot compile.
  return { revision: state.planGate.revision, title: $('saved-plan-title').value, selected: $('saved-plan-list').value, start_from: startFrom().start_from ?? null };
}
function savedDirty() {
  if (!state.savedPlan) return false;
  try {
    return savedPlanRoute(state.savedPlan, brief(), state.savedBaseline, $('clear-plan-constraints').checked).kind !== 'exact'
      || planMetadata().changed;
  } catch { return true; }
}
function renderSavedState() {
  const saved = state.savedPlan, dirty = savedDirty();
  $('loaded-plan-note').hidden = !saved;
  if (saved) $('loaded-plan-label').textContent = `${saved.title} · ${saved.plan_id.slice(0, 8)} · ${saved.request.model}`;
  $('saved-advanced-note').textContent = saved ? `Saved request: ${saved.request.steps} sampling steps · ${(saved.request.constraints ?? []).length} constraint set(s) · ${(saved.request.formats ?? []).join(', ')}. The displayed detail preset changes these sampling steps only if you select a different preset.` : '';
  $('saved-plan-status').textContent = saved ? dirty ? 'Unsaved changes. Preview and save a new version before generating.' : 'Exact saved request ready. Model, constraints and advanced settings are preserved.' : 'Save a validated plan to reuse its exact settings across the studio and connected agents.';
  const constraints = (saved?.request ?? state.revisionRequest)?.constraints ?? [];
  $('clear-plan-constraints-label').hidden = !constraints.length;
  $('clear-plan-constraints').disabled = !constraints.length;
  $('saved-constraint-note').textContent = constraints.length ? `${constraints.length} saved constraint set(s). Changing frame allocations requires explicitly clearing them; text-only edits at the same frame allocations preserve them.` : '';
  for (const id of ['save-plan', 'refresh-plans', 'load-plan', 'previous-plans', 'next-plans']) $(id).disabled = !state.connected || state.busy.has(id) || state.busy.has('load-plan') || state.busy.has('generate') || state.busy.has('refresh-plans')
    || (id === 'load-plan' && !$('saved-plan-list').value) || (id === 'save-plan' && Boolean(saved && !dirty))
    || (id === 'previous-plans' && state.libraryOffset <= 0) || (id === 'next-plans' && !(state.libraryOffset + 20 < state.libraryTotal));
  $('generate').disabled = !state.connected || state.busy.has('generate') || state.busy.has('load-plan') || Boolean(saved && dirty);
  if (saved) $('generate-label').textContent = dirty ? 'Save changes before generating' : 'Generate saved plan';
}
function displayPlan(result) {
  const plan = result.plan ?? {}, generated = result.request ?? {};
  const box = $('plan-summary'); box.replaceChildren();
  box.append(el('strong', '', `${Number(plan.duration_seconds).toFixed(3)} s · ${plan.total_frames} frames · ${generated.samples} candidate(s)`));
  box.append(el('p', '', 'Timing preview only. The 3D viewer still shows the selected saved animation.'));
  const details = el('details', 'plan-details'), advisories = plan.advisories ?? [];
  details.append(el('summary', '', `Request details${advisories.length ? ` · ${advisories.length} transition note(s)` : ' & notes'}`));
  details.append(el('p', '', `Seed ${generated.seed ?? 'assigned at submission'} · ${generated.steps ?? 'planned'} generation steps`));
  for (const note of (result.notes ?? []).slice(0, 4)) details.append(el('p', '', typeof note === 'string' ? note : note.message ?? 'Review the planned request.'));
  for (const advisory of advisories) { const item = el('p', 'plan-advisory', advisory.message ?? 'Review the planned transition.'); if (advisory.action) item.append(el('span', '', ` ${advisory.action}`)); details.append(item); }
  box.append(details);
  if ($('sequence-mode').checked) composer.setPlan(plan);
  box.hidden = false;
}
async function previewCurrentPlan() {
  const input = brief(true), snapshot = draftInput(), ticket = state.planGate.begin(snapshot);
  let result;
  if (state.savedPlan) {
    const route = savedPlanRoute(state.savedPlan, input, state.savedBaseline, snapshot.clear_constraints);
    result = route.kind === 'exact' ? await action('get_animation_plan', { plan_id: route.plan_id })
      : await action('revise_animation_plan', { plan_id: route.plan_id, revision: route.revision, save: false });
  } else if (state.revision) {
    const changes = revisionChanges(input, state.revisionBase);
    if (snapshot.clear_constraints) changes.clear_constraints = true;
    if (!Object.keys(changes).length) throw new Error('Change a setting or the description to preview a revision.');
    result = await action('revise_animation', { job_id: state.revision, revision: changes, submit: false });
  } else result = await action('plan_animation', { brief: input, ...(snapshot.start_from ? { start_from: snapshot.start_from } : {}) });
  let currentInput;
  try { currentInput = draftInput(); } catch { notice('The draft changed while the plan was being checked. Preview the current draft again.'); return null; }
  if (!state.planGate.accept(ticket, currentInput, result)) { notice('The draft changed while the plan was being checked. Preview the current draft again.'); return null; }
  if ($('notice').classList.contains('error')) $('notice').hidden = true;
  displayPlan(result); renderSavedState(); return result;
}
function validCreateForm() {
  if ($('sequence-mode').checked && !composer.reportValidity()) return false;
  return $('create-form').reportValidity();
}
$('plan').addEventListener('click', () => withBusy($('plan'), async () => { if (validCreateForm()) await previewCurrentPlan(); }));

async function refreshPlanLibrary(preferredId = null, requestedOffset = state.libraryOffset) {
  const offset = preferredId ? 0 : requestedOffset;
  const input = { offset, preferredId }, selectedBefore = $('saved-plan-list').value, ticket = state.libraryGate.begin(input);
  const result = await action('list_animation_plans', { limit: 20, offset });
  if (!state.libraryGate.accept(ticket, input, result)) return;
  state.libraryOffset = offset; state.libraryTotal = result.total ?? 0;
  const previous = selectedBefore === $('saved-plan-list').value ? preferredId ?? selectedBefore : $('saved-plan-list').value;
  $('saved-plan-list').replaceChildren(el('option', '', 'Choose a saved plan')); $('saved-plan-list').firstChild.value = '';
  for (const plan of result.plans ?? []) {
    const option = el('option', '', `${plan.title} · ${Number(plan.duration_seconds).toFixed(2)} s · ${plan.segment_count} action(s) · ${plan.plan_id.slice(0, 6)}`);
    option.value = plan.plan_id; $('saved-plan-list').append(option);
  }
  if ([...$('saved-plan-list').options].some(option => option.value === previous)) $('saved-plan-list').value = previous;
  $('saved-library-count').textContent = `${result.plans?.length ? `${offset + 1}–${offset + result.plans.length}` : '0'} of ${result.total ?? 0} saved plans${result.invalid_count ? ` · ${result.invalid_count} unreadable record(s) excluded` : ''}`;
  state.libraryLoaded = true; renderSavedState();
}
function loadSavedPlan(record) {
  if (!record.request || !record.plan || !/^[0-9a-f]{64}$/.test(record.plan_id)) throw new Error('The server did not return a complete saved plan.');
  const request = record.request, segments = request.segments;
  state.savedPlan = record; state.revision = null; state.revisionBase = null; state.revisionRequest = null;
  state.composerModel = request.model; $('revision-note').hidden = true; $('start-pose').checked = false;
  $('saved-plan-title').value = record.title; $('clear-plan-constraints').checked = false;
  $('description').value = segments.length > 1 ? savedSequenceNotes(record) : segments[0].text;
  $('duration').value = segments[0].duration; $('seed').value = request.seed; $('candidates').value = request.samples;
  $('quality').value = request.steps >= 70 ? 'detailed' : request.steps <= 20 ? 'draft' : 'balanced';
  $('sequence-mode').checked = segments.length > 1; $('sequence-mode').disabled = segments.length > 1;
  composer.replace(segments.length > 1 ? segments : [segments[0], { text: '', duration: 2 }]); updateSequence(); countCharacters();
  state.savedBaseline = brief(); state.savedContext = $('description').value.trim();
  const snapshot = draftInput(), ticket = state.planGate.begin(snapshot); state.planGate.accept(ticket, snapshot, record);
  displayPlan(record); enableActions();
}
$('saved-library').addEventListener('toggle', () => { if ($('saved-library').open && !state.libraryLoaded && state.connected) withBusy($('refresh-plans'), () => refreshPlanLibrary()); });
$('refresh-plans').addEventListener('click', () => withBusy($('refresh-plans'), () => refreshPlanLibrary()));
for (const [id, delta] of [['previous-plans', -20], ['next-plans', 20]]) $(id).addEventListener('click', () => withBusy($('refresh-plans'), () => refreshPlanLibrary(null, Math.max(0, state.libraryOffset + delta))));
$('saved-plan-list').addEventListener('change', renderSavedState);
$('saved-plan-title').addEventListener('input', renderSavedState);
$('clear-plan-constraints').addEventListener('change', invalidatePlan);
$('load-plan').addEventListener('click', () => withBusy($('load-plan'), async () => {
  const snapshot = loadInput(), ticket = state.loadGate.begin(snapshot);
  const record = await action('get_animation_plan', { plan_id: snapshot.selected });
  if (!state.loadGate.accept(ticket, loadInput(), record)) { notice('The draft or library selection changed while loading. Your newer edits remain in place; load the selected plan again when ready.'); return; }
  loadSavedPlan(record); notice('Saved plan loaded for editing. Nothing has been generated.');
}));
$('save-plan').addEventListener('click', () => withBusy($('save-plan'), async () => {
  if (state.savedPlan && !savedDirty()) { notice('This exact plan is already saved. Edit it to create a new version.'); return; }
  const metadata = planMetadata(), { title } = metadata;
  if (!title.trim() || title.length > 160) { $('saved-plan-title').focus(); throw new Error('Name the saved plan using 1 to 160 characters.'); }
  if (!validCreateForm()) return;
  const parent = state.savedPlan?.plan_id ?? null;
  const compiled = state.planGate.current(draftInput()) ?? await previewCurrentPlan(); if (!compiled) return;
  const snapshot = draftInput();
  const record = await action('save_animation_plan', { title, request: compiled.request, description: metadata.description, parent_plan_id: parent });
  let unchanged = false; try { unchanged = JSON.stringify(snapshot) === JSON.stringify(draftInput()) && JSON.stringify(metadata) === JSON.stringify(planMetadata()); } catch { /* Retain incomplete newer edits. */ }
  if (unchanged) loadSavedPlan(record);
  await refreshPlanLibrary(record.plan_id);
  notice(unchanged ? 'Exact plan saved. Generate saved plan will use this version’s complete request.' : 'The earlier draft snapshot was saved. Your newer edits remain unsaved.');
}));
$('new-plan').addEventListener('click', () => {
  state.savedPlan = null; state.savedBaseline = null; state.savedContext = null; state.revision = null; state.revisionBase = null; state.revisionRequest = null;
  $('sequence-mode').disabled = false; $('clear-plan-constraints').checked = false; $('revision-note').hidden = true; $('generate-label').textContent = 'Generate animation';
  invalidatePlan(); enableActions(); notice('New brief started with the visible actions. Saved constraints and advanced request settings are no longer attached.');
});

function submissionSelection() { return { key: currentKey(), epoch: state.selectionEpoch }; }
function selectSubmittedJob(result, snapshot) {
  // A late submission must not replace a candidate chosen while it was pending,
  // including an A→B→A selection that happens to return to the same source key.
  if (snapshot.epoch !== state.selectionEpoch || snapshot.key !== currentKey()) return;
  const id = jobId(result.job ?? result);
  if (!id) return;
  state.selected = id; state.sample = 0; state.variant = null; state.selectionSignature = ''; state.selectionEpoch++;
}

$('create-form').addEventListener('submit', event => {
  event.preventDefault();
  if (!validCreateForm()) return;
  withBusy($('generate'), async () => {
    const selection = submissionSelection();
    if (state.busy.has('load-plan')) throw new Error('Wait for the selected saved plan to finish loading.');
    if (state.savedPlan) {
      if (savedDirty()) throw new Error('Save the changed plan as a new version before generating.');
      const result = await action('generate_animation_plan', { plan_id: state.savedPlan.plan_id });
      selectSubmittedJob(result, selection);
      notice('The exact saved plan was queued. Its progress will appear in your animation history.'); await refresh(); return;
    }
    const input = brief(true);
    const changes = state.revision ? revisionChanges(input, state.revisionBase) : null;
    if (changes && $('clear-plan-constraints').checked) changes.clear_constraints = true;
    if (state.revision && !Object.keys(changes).length) throw new Error('Change the description or a setting to create a revision. The original request stays saved.');
    const result = state.revision
      ? await action('revise_animation', { job_id: state.revision, revision: changes, submit: true })
      : await action('generate_animation', { brief: input, ...startFrom() });
    selectSubmittedJob(result, selection);
    notice('Animation queued. Its progress will appear in your animation history.');
    await refresh();
  });
});

function enableActions() {
  $('edit-skeleton').disabled = !completed();
  for (const id of ['review', 'compare', 'build-preview', 'send-blender', 'send-unreal', 'export-motion']) $(id).disabled = !completed() || !state.connected || state.busy.has(id);
  for (const id of ['generate', 'plan', 'open-freemocap', 'open-video-capture', 'open-source-library']) $(id).disabled = !state.connected || state.busy.has(id);
  const job = current(), preview = selectedPreview(job, state.sample, state.variant);
  const matchingView = state.viewerMode === 'source' ? rigPanel?.loadedKey === currentKey() : state.previewLoaded && safeLocalUrl(preview?.url) === state.previewUrl;
  const unsavedPose = state.viewerMode === 'source' && rigPanel?.hasDraftPreview;
  const canAnchor = completed() && !state.revision && !state.savedPlan && matchingView && !unsavedPose && ['somaskel30', 'somaskel77'].includes(job?.manifest?.skeleton);
  $('start-pose').disabled = !canAnchor;
  if (!canAnchor) $('start-pose').checked = false;
  $('saved-export-note').textContent = unsavedPose
    ? 'An unsaved pose is shown. Exports use the selected saved motion. Save and inspect the draft first to export your changes.'
    : 'Exports use the selected saved motion version. Staged corrections must be saved first.';
  if (unsavedPose) $('anchor-note').textContent = 'Save and inspect this correction before using its pose as a starting constraint.';
  else if (!$('start-pose').checked) $('anchor-note').textContent = canAnchor ? 'Use the selected saved motion at the current frame as a starting pose.' : 'Select a completed SOMA character motion to use its saved pose.';
  if (state.planGate.result) { try { if (!state.planGate.current(draftInput())) invalidatePlan(); } catch { invalidatePlan(); } }
  renderSavedState();
  updateWorkflowHint();
}

function timeAgo(job) {
  const source = job.created_at ?? job.created ?? 0;
  const date = new Date(typeof source === 'number' && source < 1e12 ? source * 1000 : source);
  if (!Number.isFinite(date.getTime()) || !date.getTime()) return jobId(job)?.slice(0, 6) ?? '';
  return date.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' });
}

function revise(job) {
  setWorkflow('create');
  state.savedPlan = null; state.savedBaseline = null; state.composerModel = job.request?.model; state.revisionRequest = job.request;
  $('clear-plan-constraints').checked = false;
  state.revision = jobId(job);
  $('description').value = description(job);
  $('duration').value = Math.min(duration(job), 10);
  $('seed').value = job.request?.seed ?? '';
  $('candidates').value = job.request?.samples ?? 1;
  $('quality').value = job.request?.steps >= 70 ? 'detailed' : job.request?.steps <= 20 ? 'draft' : 'balanced';
  $('sequence-mode').checked = job.request?.segments?.length > 1;
  $('sequence-mode').disabled = $('sequence-mode').checked;
  composer.replace($('sequence-mode').checked ? job.request.segments : [job.request.segments[0], { text: '', duration: 2 }]);
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
        retry.addEventListener('click', () => withBusy(retry, async () => { const selection = submissionSelection(); const result = await action('generate_motion', { request: job.request }); selectSubmittedJob(result, selection); await refresh(); })); controls.append(retry);
      }
    }
    card.append(controls); $('job-list').append(card);
  }
}

function selectedPreview(job, sample, variant = null) {
  const cached = state.previews.get(sourceKey(jobId(job), sample, variant));
  return previewFor(job, sample, variant, cached);
}

function renderDownloads(job) {
  $('downloads').replaceChildren();
  const preview = selectedPreview(job, state.sample, state.variant);
  const source = { ...job, downloads: [...(job?.downloads ?? []), ...(state.verifiedOutputs.get(jobId(job)) ?? [])] };
  const entries = selectedDownloads(source, state.sample, state.variant, preview);
  const seen = new Set();
  for (const entry of entries) {
    const url = safeLocalUrl(entry.url); if (!url || seen.has(url)) continue; seen.add(url);
    const link = el('a', '', `↓ ${entry.label ?? entry.format?.toUpperCase() ?? entry.path ?? 'Download'}`);
    link.href = url; link.download = ''; $('downloads').append(link);
  }
}

async function refreshVerifiedOutputs(id) {
  const result = await action('list_job_outputs', { job_id: id });
  if (result.job_id !== id || !Array.isArray(result.outputs)) throw new Error('The verified output list does not match this animation.');
  if (state.verifiedOutputs.size >= 64 && !state.verifiedOutputs.has(id)) state.verifiedOutputs.delete(state.verifiedOutputs.keys().next().value);
  state.verifiedOutputs.set(id, result.outputs);
  if (state.selected === id) renderDownloads(current());
}

function renderComparisonOptions() {
  const signature = JSON.stringify(state.jobs.filter(item => item.status === 'completed').map(item => [jobId(item), sampleCount(item), description(item), item.variants?.map(version => [version.variant_id, version.sample_index])]));
  if (signature === state.comparisonSignature) return; state.comparisonSignature = signature;
  const previous = $('compare-target').value; $('compare-target').replaceChildren(el('option', '', 'Choose a completed candidate')); $('compare-target').firstChild.value = '';
  for (const job of state.jobs.filter(item => item.status === 'completed')) for (let sample = 0; sample < sampleCount(job); sample++) {
    const option = el('option', '', `${description(job).slice(0, 60)} · candidate ${sample + 1} · ${jobId(job).slice(0, 6)}`);
    option.value = `${jobId(job)}:${sample}`; $('compare-target').append(option);
    for (const version of (job.variants ?? []).filter(item => item.sample_index === sample)) {
      const variant = el('option', '', `${description(job).slice(0, 40)} · candidate ${sample + 1} · version ${version.variant_id.slice(0, 8)}`);
      variant.value = `${jobId(job)}:${sample}:${version.variant_id}`; $('compare-target').append(variant);
    }
  }
  if ([...$('compare-target').options].some(item => item.value === previous)) $('compare-target').value = previous;
}

function loadPreview(url) {
  const safe = safeLocalUrl(url);
  if (!safe) return false;
  if (state.previewUrl !== safe) { state.previewLoaded = false; state.previewUrl = safe; viewer?.load(safe); enableActions(); }
  return true;
}

async function selectJob(id, sample = 0, openReview = true) {
  state.selectionEpoch++;
  if (id === state.selected && sample === state.sample) { if (openReview) setWorkflow('review'); return; }
  if (id !== state.selected || sample !== state.sample) $('start-pose').checked = false;
  state.selected = id; state.sample = sample; state.variant = null; state.selectionSignature = ''; if (openReview) setWorkflow('review'); setViewerMode('source'); renderJobs(); renderSelection();
}

function renderVersions(job) {
  const signature = JSON.stringify([jobId(job), state.sample, state.variant, job.status, job.variants]);
  if (signature === state.versionSignature) return;
  state.versionSignature = signature;
  $('motion-version').replaceChildren(el('option', '', 'Original motion')); $('motion-version').firstChild.value = '';
  for (const item of (job.variants ?? []).filter(version => version.sample_index === state.sample)) {
    const option = el('option', '', `Version ${item.variant_id.slice(0, 8)} · ${item.frames} frames · ${item.edit?.joint_adjustments?.length ?? 0} joint edits`);
    option.value = item.variant_id; $('motion-version').append(option);
  }
  if (state.variant && ![...$('motion-version').options].some(option => option.value === state.variant)) {
    const option = el('option', '', `Selected version ${state.variant.slice(0, 8)} · verifying`); option.value = state.variant; $('motion-version').append(option);
  }
  $('motion-version').value = state.variant ?? ''; $('motion-version').disabled = job.status !== 'completed';
}

function metric(label, value, note) {
  const node = el('div', 'metric'); node.append(el('span', '', label), el('strong', '', value));
  if (note) node.append(el('small', '', note));
  return node;
}

async function renderSelection() {
  const job = current(); enableActions();
  if (!job) return;
  renderDownloads(job); renderVersions(job);
  const signature = JSON.stringify([jobId(job), job.status, job.manifest, job.previews, job.variant_previews, job.variants, state.sample, state.variant]);
  if (signature === state.selectionSignature) return;
  state.selectionSignature = signature;
  const token = ++state.inspecting;
  $('viewer-title').textContent = job.status === 'completed' ? `Candidate ${state.sample + 1}${state.variant ? ' · edited version' : ' · original'}` : 'Animation in progress';
  $('selected-description').textContent = description(job);
  $('candidate-count').textContent = String(job.status === 'completed' ? sampleCount(job) : 0);
  $('candidates-list').replaceChildren(); $('diagnostic-note').textContent = '';
  const timing = selectedTiming();
  $('metrics').replaceChildren(metric('Duration', timing.frames && timing.fps ? `${(timing.frames / timing.fps).toFixed(2)} s` : `${duration(job).toFixed(2)} s`), metric('Frames', String(timing.frames ?? '—')), metric('Seed', String(job.request?.seed ?? '—')));
  const format = job.manifest?.skeleton?.startsWith('soma') ? 'bvh' : job.manifest?.skeleton?.startsWith('smpl') ? 'amass' : 'csv';
  $('motion-export-format').value = format;
  [...$('motion-export-format').options].forEach(option => { option.disabled = option.value !== format; });
  if (job.status !== 'completed') {
    $('candidates-list').append(el('div', 'empty-inline', ['failed', 'cancelled'].includes(job.status) ? 'This job has no completed candidates. Revise or retry its saved request.' : 'Candidates appear after generation finishes. You can keep creating while this job runs.'));
    viewer?.clear(); rigPanel?.clear(); state.previewUrl = null; state.previewLoaded = false;
    viewerMessage(job.status === 'failed' ? 'Generation needs attention' : job.status === 'cancelled' ? 'Generation cancelled' : 'Your animation is on its way', 'The completed character preview will appear after you build the selected candidate.');
    return;
  }
  rigPanel?.load(job, state.sample, state.variant);
  refreshVerifiedOutputs(jobId(job)).catch(error => { if (token === state.inspecting) notice(`Downloads could not refresh: ${error.message}`, true); });
  for (let index = 0; index < sampleCount(job); index++) {
    const button = el('button', 'candidate'); button.setAttribute('aria-pressed', String(index === state.sample));
    const version = index === state.sample ? state.variant : null;
    const available = selectedPreview(job, index, version);
    const clip = version ? job.variants?.find(item => item.variant_id === version && item.sample_index === index) : job.manifest;
    button.append(el('strong', '', `Candidate ${index + 1}`), el('small', '', `${clip?.frames ?? '—'} frames · ${clip?.fps ?? 30} fps`), el('small', '', available ? `${version ? 'Edited' : 'Original'} preview ready` : `${version ? 'Edited motion' : 'Original motion'} ready · build preview`));
    if (index === state.sample) button.append(el('span', 'candidate-mark', '✓'));
    button.addEventListener('click', () => selectJob(jobId(job), index, false)); $('candidates-list').append(button);
  }
  const preview = selectedPreview(job, state.sample, state.variant);
  if (!preview || !loadPreview(preview.url)) {
    viewer?.clear(); state.previewUrl = null; state.previewLoaded = false; if (state.viewerMode === 'character') $('asset-label').textContent = 'Motion ready';
    viewerMessage('Bring this motion to your character', 'Build the character preview to rig and retarget this candidate in Blender. The original motion stays saved.');
  }
  try {
    const result = await action('analyze_motion_quality', { job_id: jobId(job), name: sampleName(state.sample), ...variantArgs(state.variant) });
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
    state.connected = true;
    const recent = Array.isArray(result.jobs) ? result.jobs : [];
    state.jobs = retainSelectedJob(recent, state.selected, state.jobs);
    const selected = state.selected;
    if (selected && !recent.some(job => jobId(job) === selected)) {
      try {
        const older = await request(`/api/jobs/${selected}`);
        if (state.selected === selected) state.jobs = [older, ...state.jobs.filter(job => jobId(job) !== selected)];
      } catch { /* Keep the selected take visible; its review reports missing or changed artifacts. */ }
    }
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
$('refresh').addEventListener('click', () => { state.selectionSignature = ''; refresh(); });

async function buildPreview(button) {
  const job = current(), index = state.sample, variant = state.variant, key = currentKey();
  if (job?.status !== 'completed') return;
  await withBusy(button, async () => {
    $('export-status').textContent = 'Preparing the fitted character and executing the checked Blender handoff…';
    const result = await action('build_character_preview', { job_id: jobId(job), sample_index: index, ...variantArgs(variant) });
    if (!safeLocalUrl(result.url)) throw new Error('Blender finished but did not return a usable local preview URL. Refresh the workspace to check the export.');
    const preview = { sample_index: index, variant_id: variant, url: result.url, attempt: result.attempt, exports: result.exports ?? [] };
    state.previews.set(key, preview);
    $('export-status').textContent = 'Blender finished. The character and animation have been saved.';
    notice('Character preview ready. You can inspect it or import it into Unreal.');
    if (currentKey() === key) { state.selectionSignature = ''; setWorkflow('review'); setViewerMode('character'); renderSelection(); }
    await refresh();
  }, 'Building the character preview in Blender. This can take a few minutes.');
}
$('build-preview').addEventListener('click', () => buildPreview($('build-preview')));
$('send-blender').addEventListener('click', () => buildPreview($('send-blender')));

$('review').addEventListener('click', () => {
  const job = current(), sample = state.sample, variant = state.variant;
  if (!job) return;
  // Reserve a tab during the user gesture; async popup creation is often blocked.
  const tab = window.open('about:blank', '_blank'); if (tab) tab.opener = null;
  withBusy($('review'), async () => {
    try {
      const result = await action('create_motion_review', { job_id: jobId(job), name: sampleName(sample), ...variantArgs(variant) });
      const url = safeLocalUrl(result.url);
      if (!url) throw new Error('The review was created but no local browser URL was returned.');
      if (tab) tab.location.href = url; else { const link = el('a', '', 'Open the completed frame review'); link.href = url; link.target = '_blank'; link.rel = 'noopener'; $('diagnostic-note').replaceChildren(link); }
    } catch (error) { tab?.close(); throw error; }
  });
});

$('compare').addEventListener('click', () => {
  const target = $('compare-target').value;
  if (!target) { notice('Choose a completed candidate to compare.', true); return; }
  const [otherJob, sample, otherVariant] = target.split(':'), offset = Number($('compare-offset').value);
  if (!Number.isInteger(offset) || Math.abs(offset) > 2400) { notice('Frame offset must be a whole number between −2400 and 2400.', true); return; }
  const args = { job_id: state.selected, other_job_id: otherJob, name: sampleName(state.sample), other_name: sampleName(Number(sample)), offset_frames: offset, space: $('compare-space').value, ...variantArgs(state.variant), ...(otherVariant ? { other_variant_id: otherVariant } : {}) };
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
  const job = current(), sample = state.sample, variant = state.variant;
  if (!job) return;
  const preview = selectedPreview(job, sample, variant);
  if (!preview) throw new Error('Build this candidate’s character preview first so Unreal imports the matching rig.');
  const target = {};
  for (const [field, id] of Object.entries({ project_file: 'ue-project', skeleton_path: 'ue-skeleton', skeletal_mesh_path: 'ue-mesh', destination_path: 'ue-destination', asset_name: 'ue-name' })) {
    target[field] = $(id).value.trim(); if (!target[field]) { $(id).focus(); throw new Error('Complete every Unreal target field before importing.'); }
  }
  const args = { job_id: jobId(job), source_kind: 'character', sample_index: sample, attempt: preview.attempt ?? 0, target, ...variantArgs(variant) };
  $('export-status').textContent = 'Checking the Unreal target and preparing the import…';
  await action('prepare_unreal_handoff', args);
  $('export-status').textContent = 'Importing into the matching open Unreal project…';
  const result = await action('execute_unreal_handoff', args);
  const report = result.report ?? result;
  $('export-status').textContent = `Unreal import completed. ${report.animation_asset ?? report.asset_path ?? target.destination_path + '/' + target.asset_name}`;
  notice('Animation imported and checked in Unreal.');
}));

$('export-motion').addEventListener('click', () => withBusy($('export-motion'), async () => {
  const id = state.selected;
  const result = await action('export_motion', { job_id: id, sample_index: state.sample, format: $('motion-export-format').value, ...variantArgs(state.variant) });
  const url = safeLocalUrl(result.artifact?.url);
  if (!url) throw new Error('Export completed without a browser download URL. Refresh the verified output list.');
  await refreshVerifiedOutputs(id);
  $('export-status').textContent = 'Motion file exported. The selected version’s frame count and frame rate are preserved.';
  notice('Motion export completed. Its verified file is available under Motion details.'); await refresh();
}));

function setTab(name) {
  for (const item of ['details', 'export']) { $(`tab-${item}`).setAttribute('aria-selected', String(item === name)); $(`${item}-panel`).hidden = item !== name; }
}
$('tab-details').addEventListener('click', () => setWorkflow('review'));
$('tab-export').addEventListener('click', () => setWorkflow('export'));
document.querySelector('.inspect-tabs').addEventListener('keydown', event => {
  if (['ArrowLeft', 'ArrowRight'].includes(event.key)) { event.preventDefault(); const target = $('tab-details').getAttribute('aria-selected') === 'true' ? 'export' : 'details'; setWorkflow(target === 'details' ? 'review' : 'export'); $(`tab-${target}`).focus(); }
});

setWorkflow('create'); enableActions(); refresh();
setInterval(() => { if (!document.hidden) refresh(); }, 3500);
document.addEventListener('visibilitychange', () => { if (!document.hidden) refresh(); });
