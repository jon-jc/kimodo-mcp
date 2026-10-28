let csrfToken = '';

export function setToken(value) { csrfToken = typeof value === 'string' ? value : ''; }

export async function request(path, args) {
  const response = await fetch(path, {
    method: args === undefined ? 'GET' : 'POST',
    headers: args === undefined ? {} : { 'Content-Type': 'application/json', 'X-Studio-Token': csrfToken },
    body: args === undefined ? undefined : JSON.stringify(args),
    credentials: 'same-origin',
  });
  const body = await response.json().catch(() => ({ error: 'The local server returned an unreadable response.' }));
  if (!response.ok || body.error) throw new Error(typeof body.error === 'string' ? body.error : `Request failed (${response.status}).`);
  return body;
}

export async function action(name, args) {
  const response = await request(`/api/actions/${encodeURIComponent(name)}`, args);
  return response.result ?? response;
}

export function safeLocalUrl(value, origin = globalThis.location?.origin) {
  if (typeof value !== 'string' || !value.startsWith('/api/')) return null;
  try {
    const parsed = new URL(value, origin);
    return parsed.origin === origin && parsed.pathname.startsWith('/api/') ? parsed.href : null;
  } catch { return null; }
}

export function jobId(job) { return job?.id ?? job?.job_id; }
export function sampleName(index) { return `motion_${String(index).padStart(2, '0')}.npz`; }
export function description(job) { return job?.request?.segments?.map(segment => segment.text).join(' ') || job?.brief?.description || 'Untitled animation'; }
export function duration(job) { return job?.manifest?.frames && job.manifest.fps ? job.manifest.frames / job.manifest.fps : (job?.request?.segments ?? []).reduce((sum, item) => sum + item.duration, 0); }
export function sampleCount(job) { return job?.manifest?.samples ?? job?.request?.samples ?? 0; }

export function buildBrief(values) {
  const brief = { description: values.description.trim(), duration_seconds: Number(values.duration), quality: values.quality, candidates: Number(values.candidates) };
  if (!brief.description) throw new Error('Describe the movement before generating.');
  if (values.beats) {
    if (!Array.isArray(values.beats) || values.beats.length < 2 || values.beats.length > 8) throw new Error('A sequence needs two to eight timed actions.');
    brief.beats = values.beats.map(item => {
      const text = String(item.text ?? '').trim(), seconds = Number(item.duration);
      if (!text || text.length > 2000 || !Number.isFinite(seconds) || seconds < 0.2 || seconds > 10) throw new Error('Every action needs a description and 0.2 to 10 seconds.');
      return { text, duration: seconds };
    });
    delete brief.duration_seconds;
  } else if (!Number.isFinite(brief.duration_seconds) || brief.duration_seconds < 0.2 || brief.duration_seconds > 10) throw new Error('Choose a duration from 0.2 to 10 seconds.');
  if (!['draft', 'balanced', 'detailed'].includes(brief.quality)) throw new Error('Choose a generation detail level.');
  if (!Number.isInteger(brief.candidates) || brief.candidates < 1 || brief.candidates > 4) throw new Error('Choose one to four candidates.');
  if (String(values.seed).trim() !== '') {
    brief.seed = Number(values.seed);
    if (!Number.isInteger(brief.seed) || brief.seed < 0 || brief.seed > 4294967295) throw new Error('Seed must be a whole number from 0 to 4294967295.');
  }
  return brief;
}

export function revisionChanges(input, previous) {
  const changes = Object.fromEntries(Object.entries(input).filter(([key, value]) => JSON.stringify(value) !== JSON.stringify(previous?.[key])));
  if (input.beats) { delete changes.description; delete changes.duration_seconds; }
  return changes;
}

export function anchorFrame(time, frames, fps) {
  if (!Number.isFinite(time) || !Number.isInteger(frames) || frames < 1 || !Number.isFinite(fps) || fps <= 0) throw new Error('The selected motion has no valid frame timing.');
  return Math.max(0, Math.min(frames - 1, Math.round(time * fps)));
}
