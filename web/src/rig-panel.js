import { action, request } from './api.js';
import { RigViewer } from './rig-viewer.js';
import { adjustmentFromControls, beforeFrame, canEditRig, DraftHistory, freshEdit, placementFromControls, previewDataUrl, sourceKey, upsertAdjustment, validatePreviewData, viewerFilters, visibleJoints } from './rig-data.js';

const $ = id => document.getElementById(id);
const node = (tag, text = '', className = '') => { const item = document.createElement(tag); item.textContent = text; item.className = className; return item; };
const degrees = radians => Math.round(radians * 180 / Math.PI * 1000) / 1000;

export class RigPanel {
  constructor({ onTime, onLoaded, onVariant, onError }) {
    this.callbacks = { onTime, onLoaded, onVariant, onError };
    this.token = 0; this.loadedKey = null; this.selection = null; this.drafts = new Map(); this.busy = false;
    this.viewer = new RigViewer($('rig-viewport'), {
      onTime, onLoaded, onError, onProgress() {}, onSelect: index => this.select(index),
    });
    $('joint-search').addEventListener('input', () => this.filter());
    $('joint-group').addEventListener('change', () => this.filter());
    $('isolate-joint').addEventListener('change', () => this.filter());
    $('show-joint-labels').addEventListener('change', event => this.viewer.setLabels(event.target.checked));
    $('before-ghost').addEventListener('change', event => this.viewer.setGhost(event.target.checked));
    $('rig-camera').addEventListener('change', event => this.viewer.fit(event.target.value));
    for (const axis of ['x', 'y', 'z']) {
      $(`joint-${axis}-range`).addEventListener('input', event => { $(`joint-${axis}`).value = event.target.value; });
      $(`joint-${axis}`).addEventListener('input', event => { $(`joint-${axis}-range`).value = event.target.value; });
    }
    $('joint-form').addEventListener('submit', event => { event.preventDefault(); this.guard(() => this.stageJoint()); });
    $('placement-form').addEventListener('submit', event => { event.preventDefault(); this.guard(() => this.stagePlacement()); });
    $('draft-undo').addEventListener('click', () => { this.history?.undo(); this.renderDraft(); this.fillJoint(); });
    $('draft-redo').addEventListener('click', () => { this.history?.redo(); this.renderDraft(); this.fillJoint(); });
    $('draft-reset').addEventListener('click', () => { if (this.history) { this.history.push(this.baseline); this.renderDraft(); this.fillJoint(); } });
    $('apply-variant').addEventListener('click', () => this.save());
    $('joint-list').addEventListener('keydown', event => {
      if (!['ArrowDown', 'ArrowUp', 'Home', 'End'].includes(event.key)) return;
      const buttons = [...$('joint-list').querySelectorAll('button')];
      if (!buttons.length) return;
      event.preventDefault(); const index = buttons.indexOf(document.activeElement);
      const next = event.key === 'Home' ? 0 : event.key === 'End' ? buttons.length - 1 : (index + (event.key === 'ArrowDown' ? 1 : -1) + buttons.length) % buttons.length;
      buttons[next].focus(); buttons[next].click();
    });
    this.clear();
  }

  guard(task) { try { task(); } catch (error) { this.callbacks.onError(error); } }
  clear() {
    this.token++; this.loadedKey = null; this.loadingKey = null; this.data = null; this.original = null; this.history = null; this.selected = null;
    this.viewer.clear(); $('joint-list').replaceChildren(); $('rig-status').textContent = 'Select a completed candidate to inspect its source joints.';
    $('rig-source-note').textContent = 'Source skeleton and fitted character are separate representations.';
    $('joint-form-fields').disabled = true; $('placement-fields').disabled = true; $('apply-variant').disabled = true;
    $('draft-undo').disabled = true; $('draft-redo').disabled = true; $('draft-reset').disabled = true;
    $('draft-joints').replaceChildren(); $('selected-joint').textContent = 'Choose a joint'; $('joint-position').textContent = '';
  }

  async load(job, sample, variant = null) {
    const id = job.id ?? job.job_id, key = sourceKey(id, sample, variant);
    const sameSource = this.selection && (this.selection.job.id ?? this.selection.job.job_id) === id && this.selection.sample === sample;
    const previousJoint = sameSource && this.data && this.selected !== null ? this.data.joint_names[this.selected] : null;
    const previousFrame = sameSource && this.data ? this.data.source_range.start_frame + Math.round(this.viewer.time * this.data.fps) : null;
    this.selection = { job, sample, variant, key };
    if (key === this.loadedKey || key === this.loadingKey) return;
    this.clear(); this.loadingKey = key; const token = this.token;
    $('rig-status').textContent = 'Loading exact source joint positions…';
    try {
      const variantRecord = variant ? job.variants?.find(item => item.variant_id === variant && item.sample_index === sample) : null;
      const expectedSha = variantRecord?.artifact?.sha256 ?? (variant ? null : job.manifest?.artifacts?.find(item => item.path === `motion_${String(sample).padStart(2, '0')}.npz`)?.sha256);
      const [raw, originalRaw] = await Promise.all([
        request(previewDataUrl(id, sample, variant)), variant ? request(previewDataUrl(id, sample)) : Promise.resolve(null),
      ]);
      if (token !== this.token || this.selection?.key !== key) return;
      const data = validatePreviewData(raw, { job: id, sample, variant, expectedSha });
      const original = originalRaw ? validatePreviewData(originalRaw, { job: id, sample, variant: null,
        expectedSha: data.original_source_sha256 }) : data;
      if (variant) beforeFrame(data, original, 0);
      this.data = data; this.original = original; this.loadedKey = key; this.loadingKey = null;
      this.baseline = data.edit ? structuredClone(data.edit) : freshEdit(original.frames);
      if (!this.drafts.has(key)) { if (this.drafts.size >= 32) this.drafts.delete(this.drafts.keys().next().value); this.drafts.set(key, new DraftHistory(this.baseline)); }
      this.history = this.drafts.get(key);
      this.viewer.loadMotion(data, variant ? original : null);
      const canEdit = canEditRig(data);
      $('placement-fields').disabled = !canEdit;
      $('before-ghost').disabled = !variant;
      const contacts = data.contact_status === 'invalidated_by_joint_adjustment' ? 'Contacts unavailable after joint corrections; inspect feet again.' : data.contact_status === 'preserved_source_predictions' ? 'Contact labels are source predictions.' : 'Contact labels are unavailable for this source.';
      $('rig-source-note').textContent = `${data.skeleton ?? 'Source'} · ${data.joint_names.length} source joints · original frames ${data.source_range.start_frame}–${data.source_range.end_frame_exclusive - 1} · meters, Y up, +Z forward. ${contacts}`;
      $('rig-status').textContent = canEdit ? 'Click a joint or choose it below. Changes are saved as a separate version.' : `Inspect this source rig. ${data.editable?.reason ?? 'This source does not support corrections.'}`;
      this.filter(); this.renderDraft();
      this.select(previousJoint && data.joint_names.includes(previousJoint) ? data.joint_names.indexOf(previousJoint) : data.joint_parents.findIndex(parent => parent === -1));
      if (previousFrame !== null) this.viewer.seek((previousFrame - data.source_range.start_frame) / data.fps);
    } catch (error) {
      if (token !== this.token) return;
      this.loadingKey = null; this.loadedKey = null; this.viewer.clear();
      $('rig-status').textContent = `Source rig unavailable: ${error.message}`;
      this.callbacks.onError(error);
    }
  }

  filter() {
    if (!this.data) return;
    const filters = { query: $('joint-search').value, group: $('joint-group').value, isolate: $('isolate-joint').checked, selected: this.selected };
    this.viewer.setFilters(viewerFilters(filters));
    const visible = visibleJoints(this.data, filters), focused = document.activeElement?.dataset?.joint;
    $('joint-list').replaceChildren();
    this.data.joint_names.forEach((name, index) => {
      if (!visible[index]) return;
      const button = node('button', name, 'joint-item'); button.type = 'button'; button.dataset.joint = String(index); button.setAttribute('aria-pressed', String(index === this.selected));
      button.addEventListener('click', () => this.select(index)); $('joint-list').append(button);
    });
    if (!$('joint-list').children.length) $('joint-list').append(node('p', 'No joints match these filters.', 'help'));
    $('joint-count').textContent = `${visible.filter(Boolean).length} / ${visible.length} joints`;
    if (focused !== undefined) $('joint-list').querySelector(`[data-joint="${focused}"]`)?.focus();
  }

  select(index) {
    this.selected = index; this.viewer.select(index); this.filter(); this.fillJoint(); this.updatePosition();
  }

  fillJoint() {
    if (!this.data || !this.history) return;
    const index = this.selected, name = index === null ? null : this.data.joint_names[index];
    $('selected-joint').textContent = name ?? 'Choose a joint';
    const editable = name && this.data.joint_parents[index] !== -1 && canEditRig(this.data);
    $('joint-form-fields').disabled = !editable;
    $('joint-edit-note').textContent = !name ? 'Select a joint to stage a correction.' : this.data.joint_parents[index] === -1 ? 'Root selected. Use clip placement for the root; local root corrections are excluded.' : 'Local XYZ offsets follow the current pose. Descendants move through forward kinematics; no automatic anatomical or contact repair.';
    const edit = this.history.value;
    const adjustment = edit.joint_adjustments.find(item => item.joint_name === name);
    ['x', 'y', 'z'].forEach((axis, position) => { const angle = degrees(adjustment?.local_xyz_radians[position] ?? 0); $(`joint-${axis}`).value = angle; $(`joint-${axis}-range`).value = angle; });
    $('joint-start').value = adjustment?.start_frame ?? edit.start_frame;
    $('joint-end').value = adjustment?.end_frame_exclusive ?? edit.end_frame_exclusive;
    $('joint-blend').value = adjustment?.blend_frames ?? Math.min(3, Math.floor((edit.end_frame_exclusive - edit.start_frame - 1) / 2));
    $('joint-start').min = edit.start_frame; $('joint-start').max = edit.end_frame_exclusive - 1;
    $('joint-end').min = edit.start_frame + 1; $('joint-end').max = edit.end_frame_exclusive;
  }

  updatePosition() {
    if (!this.data || this.selected === null) { $('joint-position').textContent = ''; return; }
    const frame = Math.min(this.data.frames - 1, Math.round(this.viewer.time * this.data.fps));
    const position = this.data.positions[frame][this.selected];
    $('joint-position').textContent = `Original frame ${this.data.source_range.start_frame + frame} · X ${position[0].toFixed(3)} · Y ${position[1].toFixed(3)} · Z ${position[2].toFixed(3)} m`;
  }

  stageJoint() {
    if (!this.history || this.selected === null) return;
    const adjustment = adjustmentFromControls({ joint_name: this.data.joint_names[this.selected],
      degrees: ['x', 'y', 'z'].map(axis => $(`joint-${axis}`).value), start_frame: $('joint-start').value,
      end_frame_exclusive: $('joint-end').value, blend_frames: $('joint-blend').value }, this.data, this.history.value);
    this.history.push(upsertAdjustment(this.history.value, adjustment)); this.renderDraft();
    $('rig-status').textContent = `Staged ${adjustment.joint_name}. Save a new version to see its validated motion; the current view is unchanged.`;
  }

  stagePlacement() {
    if (!this.history || !canEditRig(this.data)) return;
    this.history.push(placementFromControls({ start_frame: $('trim-start').value, end_frame_exclusive: $('trim-end').value,
      yaw_degrees: $('clip-yaw').value, x: $('clip-x').value, z: $('clip-z').value, root_origin: $('clip-origin').value }, this.history.value, this.original.frames));
    this.renderDraft(); this.fillJoint(); $('rig-status').textContent = 'Clip placement staged. Save a new version to inspect the result.';
  }

  renderDraft() {
    if (!this.history) return;
    const edit = this.history.value;
    $('draft-joints').replaceChildren();
    for (const adjustment of edit.joint_adjustments) {
      const row = node('div', '', 'draft-joint'), select = node('button', `${adjustment.joint_name} · ${adjustment.local_xyz_radians.map(degrees).join(' / ')}°`), remove = node('button', 'Remove');
      select.type = remove.type = 'button'; select.addEventListener('click', () => this.select(this.data.joint_names.indexOf(adjustment.joint_name)));
      remove.setAttribute('aria-label', `Remove ${adjustment.joint_name} from draft`);
      remove.addEventListener('click', () => { const next = this.history.value; next.joint_adjustments = next.joint_adjustments.filter(item => item.joint_name !== adjustment.joint_name); this.history.push(next); this.renderDraft(); this.fillJoint(); });
      row.append(select, remove); $('draft-joints').append(row);
    }
    $('draft-count').textContent = `${edit.joint_adjustments.length} / 16 joints staged`;
    $('draft-undo').disabled = !this.history.canUndo; $('draft-redo').disabled = !this.history.canRedo;
    const changed = JSON.stringify(edit) !== JSON.stringify(this.baseline);
    $('draft-reset').disabled = !changed;
    $('apply-variant').disabled = !changed || this.busy || !canEditRig(this.data);
    $('trim-start').value = edit.start_frame; $('trim-end').value = edit.end_frame_exclusive;
    $('trim-start').max = this.original.frames - 2; $('trim-end').max = this.original.frames;
    $('clip-yaw').value = degrees(edit.yaw_radians); $('clip-x').value = edit.translation_xz[0]; $('clip-z').value = edit.translation_xz[1]; $('clip-origin').value = edit.root_origin;
    $('draft-state').textContent = changed ? 'Unsaved draft · current view still shows the selected saved version.' : 'Saved settings · stage a change to create another version.';
  }

  async save() {
    if (!this.history || this.busy || !this.selection || !canEditRig(this.data)) return;
    const snapshot = { ...this.selection }, edit = this.history.value;
    this.busy = true; this.renderDraft(); $('rig-status').textContent = 'Checking the original source and creating an immutable motion version…';
    try {
      const result = await action('create_motion_variant', { job_id: snapshot.job.id ?? snapshot.job.job_id, sample_index: snapshot.sample, edit });
      if (!/^[0-9a-f]{64}$/.test(result.variant_id)) throw new Error('The server did not return a verified version identifier. Refresh to inspect the result before retrying.');
      await this.callbacks.onVariant(result, snapshot);
    } catch (error) { this.callbacks.onError(error); }
    finally { this.busy = false; this.renderDraft(); }
  }
}
