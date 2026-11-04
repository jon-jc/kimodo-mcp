import { duplicateBeat, makeBeats, moveBeat, removeBeat, requestBeats, sequenceTimeline } from './sequence.js';

const el = (tag, text = '', className = '') => { const node = document.createElement(tag); node.textContent = text; node.className = className; return node; };

export class SequenceComposer {
  constructor({ container, timeline, total, onChange, onError }) {
    Object.assign(this, { container, timeline, total, onChange, onError });
    this.beats = []; this.nodes = new Map(); this.selected = null; this.enabled = false; this.plan = null;
  }
  replace(values) { this.beats = makeBeats(values); this.selected = this.beats[0].id; this.plan = null; this.render(); this.onChange(); }
  getBeats() { return requestBeats(this.beats); }
  setEnabled(value) { this.enabled = value; this.render(); }
  setPlan(plan) { this.plan = plan; this.renderTimeline(); }
  changed() { this.plan = null; this.renderTimeline(); this.onChange(); }
  add() {
    if (this.beats.length >= 8) return;
    const beat = { id: crypto.randomUUID(), text: '', duration: 2 };
    this.beats.push(beat); this.selected = beat.id; this.changed(); this.render(); this.focus(beat.id);
  }
  focus(id) { this.selected = id; this.render(); this.nodes.get(id)?.textarea.focus(); }
  operate(id, operation) {
    try {
      const index = this.beats.findIndex(item => item.id === id);
      if (operation === 'up' || operation === 'down') this.beats = moveBeat(this.beats, id, operation === 'up' ? -1 : 1);
      else if (operation === 'duplicate') { this.beats = duplicateBeat(this.beats, id); this.selected = this.beats[index + 1].id; }
      else { this.beats = removeBeat(this.beats, id); if (this.selected === id) this.selected = this.beats[Math.min(index, this.beats.length - 1)].id; }
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
    textarea.addEventListener('input', () => { beat.text = textarea.value; summary.textContent = beat.text.trim() || 'Describe this action…'; this.changed(); });
    input.addEventListener('input', () => { beat.duration = input.value; this.changed(); });
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
