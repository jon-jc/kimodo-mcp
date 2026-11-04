import { duplicateBeat, firstBeatIssue, makeBeats, moveBeat, removeBeat, requestBeats, SequenceHistory, sequenceTimeline } from './sequence.js';

const el = (tag, text = '', className = '') => { const node = document.createElement(tag); node.textContent = text; node.className = className; return node; };

export class SequenceComposer {
  constructor({ container, timeline, total, onChange, onError }) {
    Object.assign(this, { container, timeline, total, onChange, onError });
    this.beats = []; this.nodes = new Map(); this.selected = null; this.field = 'text'; this.enabled = false; this.plan = null;
    this.history = new SequenceHistory(); this.editSession = null; this.editSerial = 0;
    this.toolbar = el('div', '', 'sequence-history'); this.toolbar.setAttribute('role', 'group'); this.toolbar.setAttribute('aria-label', 'Sequence edit history');
    this.undoButton = el('button', '↶ Undo', 'small-button'); this.undoButton.type = 'button'; this.undoButton.id = 'sequence-undo';
    this.redoButton = el('button', '↷ Redo', 'small-button'); this.redoButton.type = 'button'; this.redoButton.id = 'sequence-redo';
    this.undoButton.setAttribute('aria-label', 'Undo sequence edit'); this.redoButton.setAttribute('aria-label', 'Redo sequence edit');
    this.undoButton.title = 'Undo the last action edit (Ctrl/Cmd+Z)'; this.redoButton.title = 'Redo the last action edit (Ctrl/Cmd+Shift+Z)';
    this.undoButton.addEventListener('click', () => this.undo()); this.redoButton.addEventListener('click', () => this.redo());
    const help = el('span', 'Typing is grouped until you leave a field.', 'sequence-history-help');
    this.toolbar.append(this.undoButton, this.redoButton, help); this.timeline.before(this.toolbar); this.renderHistory();
    const keyboard = event => {
      if (!this.enabled || event.altKey || event.isComposing || !(event.ctrlKey || event.metaKey)) return;
      const key = event.key.toLowerCase();
      if (key !== 'z' && key !== 'y') return;
      event.preventDefault(); if (key === 'y' || event.shiftKey) this.redo(); else this.undo();
    };
    this.container.addEventListener('keydown', keyboard); this.toolbar.addEventListener('keydown', keyboard);
    this.container.addEventListener('beforeinput', event => {
      if (!this.enabled || !['historyUndo', 'historyRedo'].includes(event.inputType)) return;
      event.preventDefault(); if (event.inputType === 'historyUndo') this.undo(); else this.redo();
    });
  }
  snapshot() { return structuredClone({ beats: this.beats, selected: this.selected, field: this.field }); }
  commitEditing() { this.editSession = null; this.history.commit(); }
  beginEditing(id, field) { this.commitEditing(); this.field = field; this.history.focus(id, field); this.editSession = { id, field, key: ++this.editSerial }; }
  replace(values) {
    // Loading or applying a starter establishes a new editing session, including
    // the surrounding title/notes managed by the parent form.
    this.commitEditing(); this.beats = makeBeats(values); this.selected = this.beats[0].id; this.field = 'text';
    this.history.reset(this.snapshot()); this.plan = null; this.render(); this.onChange();
  }
  getBeats() { return requestBeats(this.beats); }
  reportValidity() {
    if (!this.enabled) return true;
    const issue = firstBeatIssue(this.beats); if (!issue) return true;
    this.focus(issue.id, issue.field);
    const node = this.nodes.get(issue.id), input = issue.field === 'text' ? node.textarea : node.input;
    input.setCustomValidity(issue.message); input.setAttribute('aria-invalid', 'true'); input.reportValidity();
    this.onError(new Error(issue.message)); return false;
  }
  setEnabled(value) { this.commitEditing(); this.enabled = value; this.render(); }
  setPlan(plan) { this.plan = plan; this.renderTimeline(); }
  changed() { this.plan = null; this.renderTimeline(); this.onChange(); }
  renderHistory() { this.undoButton.disabled = !this.enabled || !this.history.canUndo; this.redoButton.disabled = !this.enabled || !this.history.canRedo; }
  restore(snapshot) {
    this.beats = snapshot.beats; this.selected = snapshot.selected; this.field = snapshot.field;
    this.changed(); this.focus(this.selected, this.field);
  }
  undo() { if (!this.enabled) return; this.commitEditing(); const snapshot = this.history.undo(); if (snapshot) this.restore(snapshot); }
  redo() { if (!this.enabled) return; this.commitEditing(); const snapshot = this.history.redo(); if (snapshot) this.restore(snapshot); }
  edit(id, field, value) {
    if (!this.editSession || this.editSession.id !== id || this.editSession.field !== field) this.beginEditing(id, field);
    const next = this.snapshot(), beat = next.beats.find(item => item.id === id); beat[field] = value; next.selected = id; next.field = field;
    this.history.apply(next, this.editSession.key);
    this.beats.find(item => item.id === id)[field] = value;
    const node = this.nodes.get(id), input = field === 'text' ? node.textarea : node.input;
    input.setCustomValidity(''); input.removeAttribute('aria-invalid'); node.summary.textContent = this.beats.find(item => item.id === id).text.trim() || 'Describe this action…';
    this.changed();
  }
  add() {
    if (!this.enabled || this.beats.length >= 8) return;
    this.commitEditing();
    const beat = { id: crypto.randomUUID(), text: '', duration: 2 };
    this.beats.push(beat); this.selected = beat.id; this.field = 'text'; this.history.apply(this.snapshot()); this.changed(); this.focus(beat.id);
  }
  focus(id, field = 'text') {
    this.commitEditing(); this.selected = this.beats.some(item => item.id === id) ? id : this.beats[0]?.id;
    this.field = field; this.history.focus(this.selected, field); this.render();
    const node = this.nodes.get(this.selected); (field === 'duration' ? node?.input : node?.textarea)?.focus();
  }
  operate(id, operation) {
    if (!this.enabled) return;
    try {
      this.commitEditing();
      const index = this.beats.findIndex(item => item.id === id);
      if (operation === 'up' || operation === 'down') this.beats = moveBeat(this.beats, id, operation === 'up' ? -1 : 1);
      else if (operation === 'duplicate') { this.beats = duplicateBeat(this.beats, id); this.selected = this.beats[index + 1].id; }
      else if (operation === 'remove') { this.beats = removeBeat(this.beats, id); if (this.selected === id) this.selected = this.beats[Math.min(index, this.beats.length - 1)].id; }
      else throw new Error('Choose a sequence action operation.');
      this.field = 'text'; this.history.apply(this.snapshot());
      this.changed(); this.render();
      const target = this.nodes.get(operation === 'duplicate' ? this.selected : operation === 'remove' ? this.selected : id);
      if (operation === 'up' || operation === 'down') (target.buttons[operation].disabled ? target.editor.hidden ? target.summary : target.textarea : target.buttons[operation]).focus();
      else target.textarea.focus();
    } catch (error) { this.onError(error); }
  }
  row(beat) {
    const row = el('article', '', 'beat'), head = el('div', '', 'beat-head'), title = el('span'), controls = el('div', '', 'beat-controls');
    const buttons = {};
    for (const [operation, text] of Object.entries({ up: '↑', down: '↓', duplicate: '⧉', remove: '×' })) {
      const button = el('button', text); button.type = 'button'; button.addEventListener('click', () => this.operate(beat.id, operation)); buttons[operation] = button; controls.append(button);
    }
    head.append(title, controls);
    const summary = el('button', '', 'beat-summary'); summary.type = 'button'; summary.setAttribute('aria-controls', `beat-editor-${beat.id}`); summary.addEventListener('click', () => this.focus(beat.id));
    const editor = el('div', '', 'beat-editor'); editor.id = `beat-editor-${beat.id}`;
    const textarea = el('textarea'); textarea.rows = 3; textarea.maxLength = 2000; textarea.required = true; textarea.value = beat.text;
    textarea.placeholder = 'Describe this action and its starting pose.';
    const timing = el('label', 'Requested seconds'), input = el('input'); input.type = 'number'; input.min = '0.2'; input.max = '10'; input.step = 'any'; input.required = true; input.value = beat.duration;
    const exact = el('p', '', 'beat-exact'); timing.append(input); editor.append(textarea, timing, exact); row.append(head, summary, editor);
    for (const [field, control] of [['text', textarea], ['duration', input]]) {
      control.addEventListener('focus', () => this.beginEditing(beat.id, field));
      control.addEventListener('blur', () => this.commitEditing());
      control.addEventListener('input', () => this.edit(beat.id, field, control.value));
    }
    return { row, title, summary, editor, textarea, input, exact, buttons };
  }
  render() {
    const wanted = new Set(this.beats.map(item => item.id));
    for (const [id, node] of this.nodes) if (!wanted.has(id)) { node.row.remove(); this.nodes.delete(id); }
    this.beats.forEach((beat, index) => {
      // Event handlers read the current object; transformations replace data,
      // so retain each identity's object while updating its values.
      let node = this.nodes.get(beat.id);
      if (node && node.beat !== beat) { node.beat.text = beat.text; node.beat.duration = beat.duration; this.beats[index] = node.beat; }
      if (!node) { node = this.row(beat); node.beat = beat; this.nodes.set(beat.id, node); }
      node.title.textContent = `Action ${index + 1}`; node.summary.textContent = beat.text.trim() || 'Describe this action…';
      if (node.textarea.value !== beat.text) node.textarea.value = beat.text;
      if (String(node.input.value) !== String(beat.duration)) node.input.value = beat.duration;
      const selected = beat.id === this.selected; node.editor.hidden = !selected; node.summary.setAttribute('aria-expanded', String(selected)); node.row.classList.toggle('active', selected);
      node.textarea.disabled = node.input.disabled = !this.enabled || !selected;
      for (const control of [node.textarea, node.input]) { control.setCustomValidity(''); control.removeAttribute('aria-invalid'); }
      node.textarea.setAttribute('aria-label', `Action ${index + 1} description`); node.input.setAttribute('aria-label', `Action ${index + 1} requested seconds`);
      for (const [operation, button] of Object.entries(node.buttons)) {
        button.setAttribute('aria-label', `${{ up: 'Move up', down: 'Move down', duplicate: 'Duplicate', remove: 'Remove' }[operation]} action ${index + 1}`);
        button.title = button.getAttribute('aria-label');
        button.disabled = !this.enabled || (operation === 'up' && index === 0) || (operation === 'down' && index === this.beats.length - 1) || (operation === 'duplicate' && this.beats.length >= 8) || (operation === 'remove' && this.beats.length <= 2);
      }
      if (this.container.children[index] !== node.row) this.container.insertBefore(node.row, this.container.children[index] ?? null);
    });
    this.renderTimeline();
  }
  renderTimeline() {
    this.renderHistory();
    if (!this.beats.length) { this.timeline.replaceChildren(); this.total.textContent = ''; return; }
    let timing;
    try { timing = sequenceTimeline(this.beats, this.plan); } catch (error) { this.plan = null; timing = sequenceTimeline(this.beats); this.onError(error); }
    this.timeline.replaceChildren(); this.timeline.setAttribute('aria-label', `${timing.status === 'validated' ? 'Validated' : 'Estimated'} action timeline`);
    for (const segment of timing.segments) {
      const button = el('button', String(segment.index + 1), 'timeline-beat'); button.type = 'button'; button.style.flexGrow = String(Math.max(segment.frames, 1));
      button.setAttribute('aria-label', `Focus action ${segment.index + 1}, frames ${segment.start} to ${Math.max(segment.end - 1, segment.start)}, ${timing.status}`);
      button.setAttribute('aria-pressed', String(segment.id === this.selected)); button.addEventListener('click', () => this.focus(segment.id));
      button.title = `Action ${segment.index + 1} · ${segment.seconds.toFixed(3)} s · ${segment.frames} frames`;
      if (segment.tailFrames) { const tail = el('span', '', 'timeline-tail'); tail.style.width = `${segment.tailFrames / segment.frames * 100}%`; button.append(tail); }
      this.timeline.append(button);
      const info = this.nodes.get(segment.id)?.exact;
      if (info) info.textContent = `${timing.status === 'validated' ? 'Validated' : 'Estimate'}: ${segment.start}–${Math.max(segment.start, segment.end - 1)} · ${segment.frames} frames · ${segment.seconds.toFixed(3)} s${segment.incomingStart !== null ? ` · incoming history ${segment.incomingStart}–${segment.incomingEnd - 1}` : ''}${segment.tailFrames ? ` · last ${segment.tailFrames} frames revisited` : ''}`;
    }
    this.total.textContent = `${this.beats.length} actions · ${timing.status === 'invalid' ? 'Complete each duration' : `${timing.seconds.toFixed(3)} s · ${timing.frames} frames · ${timing.status === 'validated' ? 'validated plan' : 'estimate—preview to validate'}`}`;
    document.getElementById('add-beat').disabled = this.beats.length >= 8;
  }
}
