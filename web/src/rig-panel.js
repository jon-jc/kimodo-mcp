import { action, request } from './api.js';
import { RigViewer } from './rig-viewer.js';
import { adjustmentFromControls, beforeFrame, canEditRig, DraftHistory, freshEdit, placementFromControls, previewDataUrl, sourceKey, upsertAdjustment, validatePreviewData, viewerFilters, visibleJoints } from './rig-data.js';
import { draftPose, editKey, jointWeight, manipulateDraft, validateDraftPreview, windowAroundFrame } from './rig-edit.js';

const $ = id => document.getElementById(id);
const node = (tag, text = '', className = '') => { const item = document.createElement(tag); item.textContent = text; item.className = className; return item; };
const degrees = radians => Math.round(radians * 180 / Math.PI * 1000) / 1000;

export class RigPanel {
  constructor({ onTime, onLoaded, onVariant, onError, getSelectionEpoch = () => 0, onDraftPreviewChange = () => {} }) {
    this.callbacks = { onTime, onLoaded, onVariant, onError, getSelectionEpoch, onDraftPreviewChange };
    this.token = 0; this.loadedKey = null; this.selection = null; this.drafts = new Map(); this.busy = false;
    this.pendingForms = new Map(); this.inputRevision = 0; this.previewToken = 0;
    this.viewer = new RigViewer($('rig-viewport'), {
      onTime, onLoaded, onError, onProgress() {}, onSelect: index => this.select(index),
      onDraftCleared: () => this.previewCleared(), onPoseFrame: () => this.updatePosition(),
      onDragStart: () => this.startDrag(), onDrag: transform => this.drag(transform), onDragEnd: () => this.endDrag(), onDragCancel: () => this.cancelDrag(),
    });
    $('joint-search').addEventListener('input', () => this.filter());
    $('joint-group').addEventListener('change', () => this.filter());
    $('isolate-joint').addEventListener('change', () => this.filter());
    $('show-joint-labels').addEventListener('change', event => this.viewer.setLabels(event.target.checked));
    $('before-ghost').addEventListener('change', event => this.viewer.setGhost(event.target.checked));
    $('rig-camera').addEventListener('change', event => this.viewer.fit(event.target.value));
    for (const axis of ['x', 'y', 'z']) {
      $(`joint-${axis}-range`).addEventListener('input', event => { $(`joint-${axis}`).value = event.target.value; this.captureJoint(); });
      $(`joint-${axis}`).addEventListener('input', event => { $(`joint-${axis}-range`).value = event.target.value; this.captureJoint(); });
    }
    for (const id of ['joint-start', 'joint-end', 'joint-blend']) $(id).addEventListener('input', () => this.captureJoint());
    for (const id of ['trim-start', 'trim-end', 'clip-yaw', 'clip-x', 'clip-z', 'clip-origin']) $(id).addEventListener('input', () => this.capturePlacement());
    $('rig-edit-mode').addEventListener('change', () => { this.fillJoint(); this.preview(); });
    $('preview-draft').addEventListener('click', () => this.preview());
    $('show-saved-pose').addEventListener('click', () => { this.viewer.clearDraftPose(); this.previewCleared(); });
    $('joint-window-current').addEventListener('click', () => this.guard(() => this.windowCurrent()));
    $('joint-form').addEventListener('submit', event => { event.preventDefault(); this.guard(() => this.stageJoint()); });
    $('placement-form').addEventListener('submit', event => { event.preventDefault(); this.guard(() => this.stagePlacement()); });
    $('draft-undo').addEventListener('click', () => { this.history?.undo(); this.refreshHistory(); });
    $('draft-redo').addEventListener('click', () => { this.history?.redo(); this.refreshHistory(); });
    $('draft-reset').addEventListener('click', () => { if (this.history) { this.history.push(this.baseline); this.refreshHistory(); } });
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
  get hasDraftPreview() { return Boolean(this.viewer.draftPose && this.history && (this.dragState || editKey(this.history.value) !== editKey(this.baseline))); }
  forms() { const key = this.selection?.key; if (!this.pendingForms.has(key)) this.pendingForms.set(key, { joints: new Map(), placement: null }); return this.pendingForms.get(key); }
  editedJoint() { if (!this.data || this.selected === null) return null; const parent = this.data.joint_parents[this.selected]; return $('rig-edit-mode').value === 'move' && parent >= 0 ? parent : this.selected; }
  readJoint() { return { degrees: ['x', 'y', 'z'].map(axis => $(`joint-${axis}`).value), start_frame: $('joint-start').value, end_frame_exclusive: $('joint-end').value, blend_frames: $('joint-blend').value }; }
  readPlacement() { return { start_frame: $('trim-start').value, end_frame_exclusive: $('trim-end').value, yaw_degrees: $('clip-yaw').value, x: $('clip-x').value, z: $('clip-z').value, root_origin: $('clip-origin').value }; }
  captureJoint() { if (!this.data || this.editedJoint() === null) return; this.forms().joints.set(this.data.joint_names[this.editedJoint()], this.readJoint()); this.pendingChanged(); }
  capturePlacement() { if (!this.history) return; this.forms().placement = this.readPlacement(); this.pendingChanged(); }
  pendingChanged() { this.inputRevision++; this.previewToken++; this.viewer.setManipulator(null); this.renderDraft(); $('rig-edit-status').textContent = 'Form values are not staged yet. Stage the changes before dragging, previewing or saving.'; }
  hasPending() { const forms = this.forms(); return Boolean(forms.placement || forms.joints.size); }
  refreshHistory() { if (!this.history) return; this.dragState = null; this.viewer.cancelManipulation?.(); this.pendingForms.delete(this.selection.key); this.inputRevision++; this.renderDraft(); this.fillJoint(); this.preview(); }
  clear() {
    this.previewToken++; this.previewBundle = null; this.previewEnabled = false; this.dragState = null;
    this.token++; this.loadedKey = null; this.loadingKey = null; this.data = null; this.original = null; this.history = null; this.selected = null;
    this.viewer.clear(); $('joint-list').replaceChildren(); $('rig-status').textContent = 'Select a completed candidate to inspect its source joints.';
    $('rig-source-note').textContent = 'Source skeleton and fitted character are separate representations.';
    $('joint-form-fields').disabled = true; $('placement-fields').disabled = true; $('apply-variant').disabled = true;
    $('draft-undo').disabled = true; $('draft-redo').disabled = true; $('draft-reset').disabled = true;
    $('draft-joints').replaceChildren(); $('selected-joint').textContent = 'Choose a joint'; $('joint-position').textContent = '';
    $('preview-draft').disabled = true; $('show-saved-pose').disabled = true; $('rig-edit-mode').disabled = true; $('joint-window-current').disabled = true;
    $('rig-edit-status').textContent = 'Choose a completed source skeleton to edit.'; this.callbacks.onDraftPreviewChange(false);
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
      $('before-ghost').disabled = false;
      $('preview-draft').disabled = !canEdit; $('show-saved-pose').disabled = !canEdit; $('rig-edit-mode').disabled = !canEdit;
      const contacts = data.contact_status === 'invalidated_by_joint_adjustment' ? 'Contacts unavailable after joint corrections; inspect feet again.' : data.contact_status === 'preserved_source_predictions' ? 'Contact labels are source predictions.' : 'Contact labels are unavailable for this source.';
      $('rig-source-note').textContent = `${data.skeleton ?? 'Source'} · ${data.joint_names.length} source joints · original frames ${data.source_range.start_frame}–${data.source_range.end_frame_exclusive - 1} · meters, Y up, +Z forward. ${contacts}`;
      $('rig-status').textContent = canEdit ? 'Click a joint or choose it below. Changes are saved as a separate version.' : `Inspect this source rig. ${data.editable?.reason ?? 'This source does not support corrections.'}`;
      this.filter(); this.renderDraft();
      if (previousFrame !== null) this.viewer.seek((previousFrame - data.source_range.start_frame) / data.fps);
      this.select(previousJoint && data.joint_names.includes(previousJoint) ? data.joint_names.indexOf(previousJoint) : data.joint_parents.findIndex(parent => parent === -1));
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
    if (this.dragState) { this.dragState = null; this.viewer.cancelManipulation(); }
    this.selected = index; this.viewer.select(index); this.filter(); this.fillJoint(); this.updatePosition(); this.preview();
  }

  fillJoint() {
    if (!this.data || !this.history) return;
    const index = this.editedJoint(), name = index === null ? null : this.data.joint_names[index];
    const selectedName = this.selected === null ? null : this.data.joint_names[this.selected];
    $('selected-joint').textContent = name && name !== selectedName ? `${name} rotation · moves ${selectedName}` : name ?? 'Choose a joint';
    const editable = name && this.data.joint_parents[index] !== -1 && canEditRig(this.data);
    $('joint-form-fields').disabled = !editable;
    $('joint-window-current').disabled = !editable;
    $('joint-edit-note').textContent = !name ? 'Select a joint to stage a correction.' : this.data.joint_parents[index] === -1 ? (this.selected === index ? 'Drag the root across the ground plane, or use clip placement below.' : 'This bone attaches to the root. Choose Rotate joint to adjust the selected joint.') : 'Move joint aims its parent bone without stretching. Rotate joint edits local XYZ rotation. No multi-joint IK, anatomical limits or automatic foot locking.';
    const edit = this.history.value;
    const adjustment = edit.joint_adjustments.find(item => item.joint_name === name), pending = this.forms().joints.get(name);
    const window = adjustment ?? windowAroundFrame(this.currentFrame(), edit);
    ['x', 'y', 'z'].forEach((axis, position) => { const angle = pending?.degrees[position] ?? degrees(adjustment?.local_xyz_radians[position] ?? 0); $(`joint-${axis}`).value = angle; $(`joint-${axis}-range`).value = angle; });
    $('joint-start').value = pending?.start_frame ?? window.start_frame;
    $('joint-end').value = pending?.end_frame_exclusive ?? window.end_frame_exclusive;
    $('joint-blend').value = pending?.blend_frames ?? window.blend_frames;
    $('joint-start').min = edit.start_frame; $('joint-start').max = edit.end_frame_exclusive - 1;
    $('joint-end').min = edit.start_frame + 1; $('joint-end').max = edit.end_frame_exclusive;
  }

  updatePosition() {
    if (!this.data || this.selected === null) { $('joint-position').textContent = ''; return; }
    const frame = Math.min(this.data.frames - 1, Math.round(this.viewer.time * this.data.fps));
    const position = (this.viewer.draftPose?.positions_m ?? this.data.positions[frame])[this.selected];
    $('joint-position').textContent = `${this.hasDraftPreview ? 'Draft · ' : ''}Original frame ${this.viewer.draftPose?.original_frame ?? this.data.source_range.start_frame + frame} · X ${position[0].toFixed(3)} · Y ${position[1].toFixed(3)} · Z ${position[2].toFixed(3)} m`;
  }

  stageJoint() {
    if (!this.history || this.selected === null) return;
    const adjustment = adjustmentFromControls({ joint_name: this.data.joint_names[this.editedJoint()], ...this.readJoint() }, this.data, this.history.value);
    this.history.push(upsertAdjustment(this.history.value, adjustment)); this.forms().joints.delete(adjustment.joint_name); this.renderDraft(); this.preview();
    $('rig-status').textContent = `Staged ${adjustment.joint_name}. Preview is one frame; Save new version creates the playable motion.`;
  }

  stagePlacement() {
    if (!this.history || !canEditRig(this.data)) return;
    this.history.push(placementFromControls(this.readPlacement(), this.history.value, this.original.frames)); this.forms().placement = null;
    this.renderDraft(); this.fillJoint(); this.preview(); $('rig-status').textContent = 'Clip placement staged. Save a new version for full motion playback.';
  }

  currentFrame() {
    if (!this.history || !this.data) return 0;
    const edit = this.history.value;
    return Math.max(edit.start_frame, Math.min(edit.end_frame_exclusive - 1,
      this.data.source_range.start_frame + Math.round(this.viewer.time * this.data.fps)));
  }

  previewCleared() {
    this.previewEnabled = false; this.previewToken++; this.previewBundle = null; this.dragState = null;
    this.callbacks.onDraftPreviewChange(false);
    $('rig-edit-status').textContent = 'Showing saved motion. Pause and choose Preview draft at this frame to continue editing; unsaved changes remain in the draft.';
  }

  async preview() {
    if (!this.data || !this.history || !canEditRig(this.data) || this.hasPending()) return;
    const edit = this.history.value, frame = this.currentFrame(), key = this.loadedKey, token = ++this.previewToken;
    const context = { job: this.selection.job.id ?? this.selection.job.job_id, sample: this.selection.sample,
      sourceSha: this.original.source_sha256, frame, edit, names: this.original.joint_names, parents: this.original.joint_parents };
    this.previewEnabled = true; this.previewBundle = null; this.viewer.playing = false; this.viewer.setManipulator(null);
    const local = frame - this.data.source_range.start_frame;
    if (local >= 0 && local < this.data.frames) this.viewer.time = local / this.data.fps;
    this.callbacks.onTime(this.viewer.time, this.viewer.duration, false);
    $('rig-edit-status').textContent = `Checking draft at original frame ${frame}…`;
    try {
      const result = await action('preview_motion_variant', { job_id: context.job, sample_index: context.sample, edit, frame_indices: [frame] });
      if (token !== this.previewToken || key !== this.loadedKey || !this.history || editKey(edit) !== editKey(this.history.value)) return;
      this.previewBundle = validateDraftPreview(result, context);
      const pose = result.frames_preview[0];
      this.viewer.showDraftPose(pose, { verified: true, changed: editKey(edit) !== editKey(this.baseline) });
      this.updatePosition(); this.configureManipulator(); this.callbacks.onDraftPreviewChange(this.hasDraftPreview);
    } catch (error) {
      if (token !== this.previewToken || key !== this.loadedKey) return;
      this.viewer.clearDraftPose(false); this.previewBundle = null; this.callbacks.onDraftPreviewChange(false);
      $('rig-edit-status').textContent = `Draft preview unavailable: ${error.message}`;
      this.callbacks.onError(error);
    }
  }

  configureManipulator() {
    const bundle = this.previewBundle, mode = $('rig-edit-mode').value;
    this.viewer.setManipulator(null);
    if (!bundle || this.selected === null || mode === 'view' || this.hasPending()) return;
    const edit = this.history.value, selected = this.selected, target = this.editedJoint();
    const root = this.data.joint_parents[selected] < 0, name = this.data.joint_names[target];
    if (root && mode === 'rotate') { $('rig-edit-status').textContent = 'Choose Move joint to move the root on the ground plane. Use Heading below to turn the clip.'; return; }
    if (!root && (this.data.joint_parents[target] < 0 || !bundle.joint_editing.supported)) {
      $('rig-edit-status').textContent = this.data.joint_parents[target] < 0 ? 'This bone attaches to the root. Choose Rotate joint for this selection.' : bundle.joint_editing.reason; return;
    }
    const adjustment = edit.joint_adjustments.find(item => item.joint_name === name);
    if (!root && adjustment && jointWeight(adjustment, bundle.frames_preview[0].original_frame) < 0.02) {
      $('rig-edit-status').textContent = `${name} has no usable influence at this frame. Choose Edit around this frame, or change its existing window.`; return;
    }
    if (!root && !adjustment && edit.joint_adjustments.length >= 16) { $('rig-edit-status').textContent = 'The draft already edits 16 joints. Remove a correction before editing another joint.'; return; }
    const pose = this.viewer.draftPose;
    this.viewer.setManipulator({ mode, root, position: pose.positions_m[selected], rotation: pose.global_rot_mats[selected] });
    const window = adjustment ?? windowAroundFrame(bundle.frames_preview[0].original_frame, edit);
    $('rig-edit-status').textContent = root ? 'Drag X, Z, or the ground-plane square to move the clip. Release to stage; Undo reverses the drag.'
      : `${mode === 'move' ? `Drag arrows to aim ${this.data.joint_names[selected]} by rotating ${name}; bone length stays fixed.` : `Drag a colored ring to rotate ${name}.`} Frames ${window.start_frame}–${window.end_frame_exclusive - 1}, blend ${window.blend_frames}. Release to stage; Save creates a playable version.`;
  }

  windowCurrent() {
    if (!this.history || this.editedJoint() === null) return;
    const index = this.editedJoint(); if (this.data.joint_parents[index] < 0) return;
    const edit = this.history.value, name = this.data.joint_names[index], previous = edit.joint_adjustments.find(item => item.joint_name === name);
    const adjustment = { joint_name: name, local_xyz_radians: previous?.local_xyz_radians ?? [0, 0, 0], ...windowAroundFrame(this.currentFrame(), edit) };
    this.history.push(upsertAdjustment(edit, adjustment)); this.forms().joints.delete(name); this.renderDraft(); this.fillJoint(); this.preview();
  }

  startDrag() {
    if (!this.previewBundle || !this.history || this.hasPending()) return false;
    try {
      const initial = this.history.value, mode = $('rig-edit-mode').value, target = this.editedJoint();
      const root = this.data.joint_parents[this.selected] < 0, frame = this.previewBundle.frames_preview[0].original_frame;
      let edit = initial;
      if (!root) {
        const name = this.data.joint_names[target];
        let adjustment = edit.joint_adjustments.find(item => item.joint_name === name);
        if (!adjustment) { adjustment = { joint_name: name, local_xyz_radians: [0, 0, 0], ...windowAroundFrame(frame, edit) }; edit = upsertAdjustment(edit, adjustment); }
        if (jointWeight(adjustment, frame) < 0.02) throw new Error('Choose Edit around this frame before dragging this correction.');
        $('joint-start').value = adjustment.start_frame; $('joint-end').value = adjustment.end_frame_exclusive; $('joint-blend').value = adjustment.blend_frames;
      }
      this.dragState = { initial: edit, latest: initial, mode, selected: this.selected, key: this.loadedKey, history: this.history,
        revision: this.history.revision, inputRevision: this.inputRevision, bundle: this.previewBundle };
      this.viewer.playing = false; this.callbacks.onTime(this.viewer.time, this.viewer.duration, false); return true;
    } catch (error) { this.callbacks.onError(error); return false; }
  }

  drag(transform) {
    const drag = this.dragState; if (!drag || drag.key !== this.loadedKey || drag.history !== this.history) return;
    try {
      const edit = manipulateDraft(drag.bundle, drag.initial, { mode: drag.mode, selected: drag.selected, ...transform });
      drag.latest = edit; this.viewer.showDraftPose(draftPose(drag.bundle, edit), { changed: true });
      this.updatePosition(); this.callbacks.onDraftPreviewChange(true);
      $('rig-edit-status').textContent = 'Interactive draft pose. Release to check and stage this drag; no saved animation has changed.';
    } catch (error) { $('rig-edit-status').textContent = error.message; }
  }

  endDrag() {
    const drag = this.dragState; this.dragState = null;
    if (!drag || drag.key !== this.loadedKey || drag.history !== this.history || drag.revision !== this.history.revision || drag.inputRevision !== this.inputRevision) return;
    this.history.push(drag.latest); this.renderDraft(); this.fillJoint(); this.preview();
  }

  cancelDrag() {
    this.dragState = null; this.viewer.cancelManipulation?.(); this.viewer.clearDraftPose(false);
    this.callbacks.onDraftPreviewChange(false); this.preview();
  }

  renderDraft() {
    if (!this.history) return;
    const edit = this.history.value;
    $('draft-joints').replaceChildren();
    for (const adjustment of edit.joint_adjustments) {
      const row = node('div', '', 'draft-joint'), select = node('button', `${adjustment.joint_name} · ${adjustment.local_xyz_radians.map(degrees).join(' / ')}°`), remove = node('button', 'Remove');
      select.type = remove.type = 'button'; select.addEventListener('click', () => { $('rig-edit-mode').value = 'rotate'; this.select(this.data.joint_names.indexOf(adjustment.joint_name)); });
      remove.setAttribute('aria-label', `Remove ${adjustment.joint_name} from draft`);
      remove.addEventListener('click', () => { const next = this.history.value; next.joint_adjustments = next.joint_adjustments.filter(item => item.joint_name !== adjustment.joint_name); this.history.push(next); this.forms().joints.delete(adjustment.joint_name); this.renderDraft(); this.fillJoint(); this.preview(); });
      row.append(select, remove); $('draft-joints').append(row);
    }
    $('draft-count').textContent = `${edit.joint_adjustments.length} / 16 joints staged`;
    $('draft-undo').disabled = !this.history.canUndo; $('draft-redo').disabled = !this.history.canRedo;
    const changed = editKey(edit) !== editKey(this.baseline), pending = this.forms().placement, unstaged = this.hasPending();
    $('draft-reset').disabled = !changed && !unstaged;
    $('apply-variant').disabled = !changed || this.busy || !canEditRig(this.data) || unstaged;
    $('trim-start').value = pending?.start_frame ?? edit.start_frame; $('trim-end').value = pending?.end_frame_exclusive ?? edit.end_frame_exclusive;
    $('trim-start').max = this.original.frames - 2; $('trim-end').max = this.original.frames;
    $('clip-yaw').value = pending?.yaw_degrees ?? degrees(edit.yaw_radians); $('clip-x').value = pending?.x ?? edit.translation_xz[0]; $('clip-z').value = pending?.z ?? edit.translation_xz[1]; $('clip-origin').value = pending?.root_origin ?? edit.root_origin;
    $('draft-state').textContent = unstaged ? 'Unstaged form values · stage each edited form, or Reset draft, before saving or dragging.' : changed ? 'Unsaved draft · preview checks one frame. Playback and exports use the saved version until Save new version.' : 'Saved settings · click a joint and drag its handle, or stage a numeric correction.';
  }

  async save() {
    if (!this.history || this.busy || !this.selection || !canEditRig(this.data) || this.hasPending()) return;
    const history = this.history, revision = history.revision, inputRevision = this.inputRevision;
    const snapshot = { ...this.selection, selection_epoch: this.callbacks.getSelectionEpoch(), draft_revision: revision }, edit = history.value;
    this.busy = true; this.renderDraft(); $('rig-status').textContent = 'Checking the original source and creating an immutable motion version…';
    try {
      const result = await action('create_motion_variant', { job_id: snapshot.job.id ?? snapshot.job.job_id, sample_index: snapshot.sample, edit });
      if (!/^[0-9a-f]{64}$/.test(result.variant_id)) throw new Error('The server did not return a verified version identifier. Refresh to inspect the result before retrying.');
      snapshot.should_select = this.history === history && history.revision === revision && this.inputRevision === inputRevision && this.selection?.key === snapshot.key;
      await this.callbacks.onVariant(result, snapshot);
    } catch (error) { this.callbacks.onError(error); }
    finally { this.busy = false; this.renderDraft(); }
  }
}
