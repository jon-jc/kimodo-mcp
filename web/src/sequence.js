// Draft transformations stay separate from request compilation and generation.
const copy = value => structuredClone(value);
const key = value => JSON.stringify(value);
export const STARTER_SEQUENCES = {
  greeting: { title: 'Approach, greet, settle', description: 'A relaxed greeting with a clear beginning and finish.', beats: [
    { text: 'A person walks forward slowly with relaxed arms.', duration: 3 },
    { text: 'A person stands upright and waves gently with their right hand.', duration: 3 },
    { text: 'A person lowers their right hand and settles into a relaxed standing pose.', duration: 2 },
  ] },
  jump: { title: 'Crouch, jump, land', description: 'A small two-foot jump with preparation and a balanced landing.', beats: [
    { text: 'A person bends their knees into a shallow crouch with both feet on the ground.', duration: 2 },
    { text: 'A person pushes upward into a small vertical jump using both legs.', duration: 1.5 },
    { text: 'A person lands on both feet, bends their knees to absorb the landing, and stands upright.', duration: 2.5 },
  ] },
  turn: { title: 'Turn, walk, stop', description: 'A deliberate change of direction followed by a controlled stop.', beats: [
    { text: 'A person standing upright turns their body to the left.', duration: 2 },
    { text: 'A person walks forward at a steady relaxed pace with natural arm swings.', duration: 3 },
    { text: 'A person slows down, stops walking, and settles into a balanced standing pose.', duration: 2 },
  ] },
};

export function makeBeats(values, nextId = () => crypto.randomUUID()) {
  if (!Array.isArray(values) || values.length < 2 || values.length > 8) throw new Error('A sequence needs two to eight actions.');
  const result = values.map(item => ({ id: nextId(), text: String(item.text ?? ''), duration: item.duration ?? 2 }));
  if (new Set(result.map(item => item.id)).size !== result.length) throw new Error('Sequence identities must be unique.');
  return result;
}
export function moveBeat(beats, id, direction) {
  const next = copy(beats), index = next.findIndex(item => item.id === id);
  if (![-1, 1].includes(direction) || index < 0) throw new Error('Choose an existing action and move direction.');
  const destination = index + direction;
  if (destination < 0 || destination >= next.length) return next;
  [next[index], next[destination]] = [next[destination], next[index]];
  return next;
}
export function duplicateBeat(beats, id, nextId = () => crypto.randomUUID()) {
  if (beats.length >= 8) throw new Error('A sequence supports at most eight actions.');
  const next = copy(beats), index = next.findIndex(item => item.id === id), identity = nextId();
  if (index < 0 || next.some(item => item.id === identity)) throw new Error('The copied action needs a unique identity.');
  next.splice(index + 1, 0, { ...copy(next[index]), id: identity }); return next;
}
export function removeBeat(beats, id) {
  if (beats.length <= 2) throw new Error('Keep at least two actions, or switch to a single action.');
  if (!beats.some(item => item.id === id)) throw new Error('Choose an existing action.');
  return beats.filter(item => item.id !== id).map(copy);
}
export function requestBeats(beats) {
  return beats.map((item, index) => {
    const text = item.text.trim(), duration = typeof item.duration === 'number' ? item.duration : item.duration.trim() ? Number(item.duration) : NaN;
    if (!text || text.length > 2000 || !Number.isFinite(duration) || duration < 0.2 || duration > 10) throw new Error(`Action ${index + 1} needs a description and a duration from 0.2 to 10 seconds.`);
    return { text, duration };
  });
}

export function sequenceTimeline(beats, plan = null) {
  const fps = plan?.fps ?? 30;
  if (!Number.isFinite(fps) || fps <= 0 || fps > 240) throw new Error('The plan has invalid frame timing.');
  const authoritative = Boolean(plan);
  if (plan && (!Array.isArray(plan.segment_frames) || plan.segment_frames.length !== beats.length || !Array.isArray(plan.segment_timeline) || plan.segment_timeline.length !== beats.length)) throw new Error('The validated timeline does not match these actions.');
  let cursor = 0, invalid = false;
  const segments = beats.map((beat, index) => {
    const duration = Number(beat.duration), valid = String(beat.duration).trim() && Number.isFinite(duration) && duration >= 0.2 && duration <= 10;
    const frames = plan ? plan.segment_frames[index] : valid ? Math.floor(duration * fps) : 0;
    if (!valid) invalid = true;
    if (plan && (!Number.isInteger(frames) || frames < 1)) throw new Error('The validated plan contains invalid frame allocations.');
    const entry = plan?.segment_timeline[index];
    if (entry && (entry.start_frame !== cursor || entry.end_frame_exclusive !== cursor + frames)) throw new Error('The validated timeline is not contiguous.');
    const result = { id: beat.id, index, text: beat.text, frames, start: cursor, end: cursor + frames,
      seconds: frames / fps, requestedSeconds: valid ? duration : null,
      incomingStart: entry?.incoming_transition_start_frame ?? null, incomingEnd: entry?.incoming_transition_end_frame_exclusive ?? null,
      tailFrames: entry?.tail_reprocessed_frames ?? 0 };
    cursor += frames; return result;
  });
  if (plan && plan.total_frames !== cursor) throw new Error('The validated total does not match its actions.');
  return { fps, frames: cursor, seconds: cursor / fps, status: invalid ? 'invalid' : authoritative ? 'validated' : 'estimate', segments };
}

export class PlanGate {
  constructor() { this.revision = 0; this.result = null; this.inputKey = null; }
  invalidate() { this.revision++; this.result = null; this.inputKey = null; }
  begin(input) { this.invalidate(); return { revision: this.revision, inputKey: key(input) }; }
  accept(ticket, currentInput, result) {
    if (ticket.revision !== this.revision || ticket.inputKey !== key(currentInput)) return false;
    this.inputKey = ticket.inputKey; this.result = copy(result); return true;
  }
  current(input) { return this.inputKey === key(input) ? copy(this.result) : null; }
}

export function savedPlanRoute(saved, input, baseline, clearConstraints = false) {
  if (!saved || !/^[0-9a-f]{64}$/.test(saved.plan_id)) throw new Error('Load a verified saved plan first.');
  const model = baseline?.model ?? saved.request?.model;
  if (input.model && model && input.model !== model) throw new Error('A saved-plan revision cannot change its model. Start a new brief instead.');
  if (baseline?.seed !== undefined && input.seed === undefined) throw new Error('Preview the draft to assign a fresh seed, then save the new version.');
  const revision = Object.fromEntries(Object.entries(input).filter(([name, value]) => name !== 'model' && key(value) !== key(baseline?.[name])));
  if (input.beats) { delete revision.description; delete revision.duration_seconds; }
  if (clearConstraints) revision.clear_constraints = true;
  return Object.keys(revision).length ? { kind: 'revision', plan_id: saved.plan_id, revision }
    : { kind: 'exact', plan_id: saved.plan_id };
}

export function needsFreshSeed(input, saved, baseline) {
  return input.seed === undefined && (!saved || baseline?.seed !== undefined);
}

export function savedPlanMetadata(saved, fields, baselineNotes = '') {
  // An empty stored description can have a helpful title fallback in the form.
  // Preserve the stored value unless the user actually edits that notes field.
  const title = saved && fields.title === saved.title ? saved.title : fields.title.trim();
  const notes = fields.notes.trim();
  const description = saved
    ? fields.sequence && notes !== baselineNotes ? notes : saved.description
    : fields.sequence ? notes : title;
  return { title, description, changed: Boolean(saved && (title !== saved.title || description !== saved.description)) };
}

export function savedSequenceNotes(saved) {
  return saved.description.trim() ? saved.description : saved.title.trim() ? saved.title : 'Saved animation sequence';
}

export function friendlyPlanningError(message) {
  return message.includes('Changing timing with existing constraints requires clear_constraints=true')
    ? 'The saved pose or path constraints depend on the original timing. Keep the original action durations, or select “Clear saved pose/path constraints in this revision” and preview again.'
    : message;
}

export const WORKFLOW_STEPS = ['create', 'review', 'export'];

export function workflowState(current, step) {
  if (!WORKFLOW_STEPS.includes(step)) throw new Error('Choose Create, Review & fix, or Export.');
  return { ...current, workflow: step, focusRig: step === 'review' && Boolean(current.focusRig), inspectTab: step === 'export' ? 'export' : 'details' };
}

export function workflowKey(step, keyName) {
  const index = WORKFLOW_STEPS.indexOf(step);
  if (index < 0) return null;
  if (keyName === 'Home') return WORKFLOW_STEPS[0];
  if (keyName === 'End') return WORKFLOW_STEPS.at(-1);
  if (!['ArrowLeft', 'ArrowRight'].includes(keyName)) return null;
  return WORKFLOW_STEPS[(index + (keyName === 'ArrowLeft' ? -1 : 1) + WORKFLOW_STEPS.length) % WORKFLOW_STEPS.length];
}

export function sharedFrameTime(time, timing, targetDuration) {
  if (!Number.isFinite(time) || !Number.isInteger(timing?.frames) || timing.frames < 1 || !Number.isFinite(timing.fps) || timing.fps <= 0 || !Number.isFinite(targetDuration) || targetDuration < 0) return null;
  const frame = Math.max(0, Math.min(timing.frames - 1, Math.round(time * timing.fps)));
  return Math.min(frame / timing.fps, targetDuration);
}
