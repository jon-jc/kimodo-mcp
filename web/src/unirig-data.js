import { numericField } from './native-data.js';
export const UNIRIG_OPERATIONS = Object.freeze({
  pipeline: ['Complete rigging pipeline', 'Preprocess, predict a skeleton, predict skin weights, then merge the rig into the original mesh.'],
  preprocess: ['Preprocess mesh', 'Prepare normalized mesh data without predicting a skeleton.'],
  skeleton: ['Predict skeleton', 'Preprocess and predict an articulated skeleton; this does not predict skin weights.'],
  skin: ['Predict skin weights', 'Use an existing mesh rig or an explicit registered/completed rig source.'],
  merge: ['Merge existing rig', 'Attach a specified existing rig result to the original mesh; no skeleton inference runs.'],
});
export const UNIRIG_DEFAULTS = Object.freeze({ seed: 12345, faces_target_count: 50000, num_beams: 15, do_sample: true, max_new_tokens: 2048, top_k: 5, top_p: 0.95, temperature: 1.5, repetition_penalty: 3, skin_backend: 'pyrender', output_format: 'glb', add_root: false });
const assetId = value => { if (!/^[a-f0-9]{64}$/.test(value ?? '')) throw new Error('Choose a registered asset or paste its 64-character asset ID.'); return value; };
export function uniRigRequest(fields, rows) {
  if (!Object.hasOwn(UNIRIG_OPERATIONS, fields.operation)) throw new Error('Choose a released UniRig operation.');
  if (!Array.isArray(rows) || rows.length < 1 || rows.length > 4) throw new Error('Choose 1 to 4 registered mesh assets.');
  const assets = rows.map(row => {
    const value = { asset_id: assetId(row.asset_id), source_item_index: 0, rig_output: 'skeleton' };
    if (['skin', 'merge'].includes(fields.operation)) {
      if (row.rig_source === 'asset') value.rig_asset_id = assetId(row.rig_asset_id);
      else if (row.rig_source === 'job') {
        if (!/^[a-f0-9]{32}$/.test(row.source_job_id ?? '')) throw new Error('Choose a completed UniRig source job ID.');
        value.source_job_id = row.source_job_id;
        value.source_item_index = numericField(row.source_item_index, 'Source item index', 0, 3, true);
        if (!['skeleton', 'skin'].includes(row.rig_output)) throw new Error('Choose a completed skeleton or skin output.');
        value.rig_output = row.rig_output;
      } else if (fields.operation === 'merge') throw new Error('Merge requires an explicit registered rig or completed UniRig result.');
    }
    return value;
  });
  if (new Set(assets.map(item => item.asset_id)).size !== assets.length) throw new Error('Each batch item needs a different registered mesh.');
  const result = { operation: fields.operation, assets, ...UNIRIG_DEFAULTS,
    seed: numericField(fields.seed, 'Seed', 0, 4294967295, true), faces_target_count: numericField(fields.faces_target_count, 'Target face count', 100, 100000, true) };
  if (['skeleton', 'pipeline'].includes(fields.operation)) {
    if (typeof fields.do_sample !== 'boolean') throw new Error('Choose whether to sample skeleton predictions.'); result.do_sample = fields.do_sample;
    for (const [name, min, max, integer] of [['num_beams', 1, 30, true], ['max_new_tokens', 1, 2048, true], ['top_k', 1, 100, true], ['top_p', Number.MIN_VALUE, 1], ['temperature', Number.MIN_VALUE, 5], ['repetition_penalty', 0.1, 10]]) result[name] = numericField(fields[name], name.replaceAll('_', ' '), min, max, integer);
  }
  if (['skin', 'pipeline'].includes(fields.operation)) { if (!['pyrender', 'open3d'].includes(fields.skin_backend)) throw new Error('Choose pyrender or open3d skinning.'); result.skin_backend = fields.skin_backend; }
  if (['merge', 'pipeline'].includes(fields.operation)) { if (!['glb', 'fbx'].includes(fields.output_format)) throw new Error('Choose GLB or FBX merged output.'); result.output_format = fields.output_format; result.add_root = Boolean(fields.add_root); }
  return result;
}
export function uniRigArtifactUrl(id, name) {
  if (!/^[a-f0-9]{32}$/.test(id ?? '') || typeof name !== 'string' || !name.startsWith('outputs/') || name.split('/').some(part => !/^[A-Za-z0-9_.-]+$/.test(part) || part === '.' || part === '..')) throw new Error('Choose a listed verified UniRig output.');
  return `/api/unirig/${id}/files/${name.split('/').map(encodeURIComponent).join('/')}`;
}
export function verifiedUniRigOutputs(job) {
  if (job?.status !== 'completed' || job.verification_error || job.output?.job_id !== job.id || !Array.isArray(job.output?.artifacts)) return [];
  return job.output.artifacts.filter(item => {
    try { uniRigArtifactUrl(job.id, item.path); return /^[a-f0-9]{64}$/.test(item.sha256 ?? '') && Number.isInteger(item.bytes) && item.bytes > 0; } catch { return false; }
  });
}
export function canCancelUniRig(job) { return /^[a-f0-9]{32}$/.test(job?.id ?? '') && ['queued', 'running'].includes(job.status); }
export function uniRigRegistration(job, path, label, kind) {
  if (!verifiedUniRigOutputs(job).some(item => item.path === path && /\.(glb|fbx)$/i.test(item.path))) throw new Error('Choose a verified completed GLB or FBX result.');
  if (typeof label !== 'string' || !label.trim() || label.length > 160) throw new Error('Give this result a library label of 1 to 160 characters.');
  if (!['character', 'prop'].includes(kind)) throw new Error('Choose character or prop for the library entry.');
  return { job_id: job.id, name: path, label, kind };
}
export function uniRigJobOption(job) { if (!/^[a-f0-9]{32}$/.test(job?.id ?? '')) throw new Error('The UniRig receipt has an invalid ID.'); return { value: job.id, label: `${job.id.slice(0, 8)} · ${job.request?.operation ?? 'rigging'} · ${job.status}` }; }
export function uniRigRuntimeMessage(runtime) {
  return runtime?.configuration_ready ? 'Runtime configuration checked. GPU operation, weights and available memory are not verified.' : 'UniRig is not configured. Review the missing local runtime checks below.';
}
export function uniRigPlanMatches(plan, request) {
  if (!plan?.request || !Array.isArray(plan.assets) || plan.assets.length !== request.assets.length) return false;
  // The server may materialize optional null fields; compare every requested value.
  for (const [name, value] of Object.entries(request)) {
    if (name === 'assets') continue;
    if (JSON.stringify(plan.request[name]) !== JSON.stringify(value)) return false;
  }
  return request.assets.every((item, index) => Object.entries(item).every(([name, value]) => plan.request.assets?.[index]?.[name] === value)
    && plan.assets[index]?.item_index === index && plan.assets[index]?.mesh?.asset_id === item.asset_id && plan.assets[index]?.mesh?.sha256 === item.asset_id);
}
