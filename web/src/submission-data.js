import { jobId } from './api.js';
import { sourceKey } from './rig-data.js';

export function submissionSelection(state) {
  return { key: sourceKey(state.selected, state.sample, state.variant), epoch: state.selectionEpoch };
}

// A queued job belongs in history regardless of what the artist is reviewing.
// Only an unchanged selection may automatically switch to that queued result.
export function selectSubmittedJob(state, result, snapshot) {
  const current = submissionSelection(state), id = jobId(result?.job ?? result);
  if (snapshot?.epoch !== current.epoch || snapshot?.key !== current.key || !/^[a-f0-9]{32}$/.test(id ?? '')) return false;
  state.selected = id; state.sample = 0; state.variant = null;
  state.selectionSignature = ''; state.selectionEpoch++;
  return true;
}
