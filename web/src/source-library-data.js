export const SOURCE_LABELS = { kimodo: 'Kimodo · generated motion', easymocap: 'EasyMocap · video fitting', freemocap: 'FreeMoCap · recorded performance' };
export function sourceIdentity(value) {
  if (!value || !Object.hasOwn(SOURCE_LABELS, value.source_kind) || !/^[0-9a-f]{32}$/.test(value.source_id)) throw new Error('This source does not have a recognized workflow and receipt ID.');
  return `${value.source_kind}:${value.source_id}`;
}
export function sourcePage(value, kind, offset) {
  if (!value || value.source_kind !== kind || value.offset !== offset || value.metadata_only !== true || !Array.isArray(value.sources) || value.sources.length > 100) throw new Error('The history response does not match this page.');
  const seen = new Set();
  for (const item of value.sources) {
    const key = sourceIdentity(item);
    if (seen.has(key) || (kind !== 'all' && item.source_kind !== kind) || typeof item.title !== 'string' || typeof item.recorded_status !== 'string' || item.verification !== 'not_checked') throw new Error('History contains an inconsistent source.');
    seen.add(key);
  }
  if (!Number.isInteger(value.total) || value.total < value.sources.length || (value.next_offset !== null && (!Number.isInteger(value.next_offset) || value.next_offset <= offset || value.next_offset > 10000))) throw new Error('History has invalid pagination.');
  return value;
}
export class SourceReadGate {
  constructor() { this.revision = 0; }
  begin() { return ++this.revision; }
  invalidate() { this.revision++; }
  current(ticket) { return this.revision === ticket; }
}

// A take opened from older history remains selected when periodic recent-job
// refreshes return only newer takes. Its full record is refreshed separately.
export function retainSelectedJob(recent, selected, existing) {
  const values = [...recent];
  if (selected && !values.some(job => (job.id ?? job.job_id) === selected)) {
    const retained = existing.find(job => (job.id ?? job.job_id) === selected);
    if (retained) values.push(retained);
  }
  return values;
}
