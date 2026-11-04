import { action } from './api.js';
import { SOURCE_LABELS, SourceReadGate, sourceIdentity, sourcePage } from './source-library-data.js';
import './source-library.css';

const node = (tag, text, className = '') => { const item = document.createElement(tag); item.textContent = text; item.className = className; return item; };
export class SourceLibrary {
  constructor({ onOpen }) {
    this.onOpen = onOpen; this.offset = 0; this.next = null; this.gate = new SourceReadGate();
    this.dialog = document.createElement('dialog'); this.dialog.className = 'source-library';
    this.dialog.setAttribute('aria-labelledby', 'source-library-title');
    this.dialog.innerHTML = `<header><div><span class="eyebrow">SHARED WORKSPACE</span><h2 id="source-library-title">All motion sources</h2><p class="help">Continue generated takes and capture stages from any connected agent or browser.</p></div><button type="button" id="sources-close" class="small-button" autofocus>Close</button></header>
      <div class="sources-toolbar"><label>Workflow<select id="sources-kind"><option value="all">All workflows</option><option value="kimodo">Kimodo · generated motion</option><option value="easymocap">EasyMocap · video fitting</option><option value="freemocap">FreeMoCap · recorded performance</option></select></label><button type="button" id="sources-refresh" class="small-button">Refresh history</button></div>
      <p id="sources-status" class="help" role="status">Reading saved history…</p><p class="sources-note">Statuses come from saved records. Opening a source checks its own result; history does not verify animation quality or start processing.</p>
      <div id="sources-list" class="sources-list"></div><footer><button id="sources-previous" class="small-button" disabled>Previous</button><span id="sources-page" class="help"></span><button id="sources-next" class="small-button" disabled>Next</button></footer>`;
    document.body.append(this.dialog);
    this.$('sources-close').addEventListener('click', () => this.dialog.close());
    this.dialog.addEventListener('close', () => this.gate.invalidate());
    this.$('sources-kind').addEventListener('change', () => { this.offset = 0; this.refresh(); });
    this.$('sources-refresh').addEventListener('click', () => this.refresh());
    this.$('sources-previous').addEventListener('click', () => { this.offset = Math.max(0, this.offset - 30); this.refresh(); });
    this.$('sources-next').addEventListener('click', () => { if (this.next !== null) { this.offset = this.next; this.refresh(); } });
  }
  $(id) { return this.dialog.querySelector(`#${id}`); }
  async open() { if (!this.dialog.open) this.dialog.showModal(); await this.refresh(); }
  async refresh() {
    const ticket = this.gate.begin(), kind = this.$('sources-kind').value, offset = this.offset;
    this.$('sources-status').textContent = 'Reading saved history…'; this.$('sources-list').replaceChildren();
    for (const id of ['sources-refresh', 'sources-previous', 'sources-next']) this.$(id).disabled = true;
    try {
      const data = sourcePage(await action('list_motion_sources', { source_kind: kind, limit: 30, offset }), kind, offset);
      if (!this.gate.current(ticket) || !this.dialog.open) return;
      this.next = data.next_offset;
      this.$('sources-status').textContent = data.sources.length ? `${data.total} saved source records${data.partial ? ' · partial index' : ''}. ${data.warnings.join(' ')}` : `No saved sources in this workflow. ${data.warnings.join(' ')}`;
      this.$('sources-page').textContent = data.sources.length ? `${offset + 1}–${offset + data.sources.length} of ${data.total}` : 'No results';
      for (const item of data.sources) {
        sourceIdentity(item);
        const row = node('article', '', 'source-card'), text = node('div', '', 'source-card-text');
        text.append(node('span', SOURCE_LABELS[item.source_kind], 'eyebrow'), node('h3', item.title), node('p', `${item.recorded_status} · ${item.operation} · ${item.source_id}`, 'help'));
        if (Number.isFinite(item.created_at) && item.created_at > 0) text.append(node('time', new Date(item.created_at * 1000).toLocaleString(), 'help'));
        const button = node('button', item.source_kind === 'freemocap' ? 'Open stage receipt' : 'Open motion', 'button secondary'); button.type = 'button';
        button.addEventListener('click', async () => { button.disabled = true; try { this.dialog.close(); await this.onOpen(item); } catch (error) { if (!this.dialog.open) this.dialog.showModal(); this.$('sources-status').textContent = `Cannot open this source: ${error.message}`; } finally { button.disabled = false; } });
        row.append(text, button); this.$('sources-list').append(row);
      }
      this.$('sources-previous').disabled = offset === 0; this.$('sources-next').disabled = this.next === null;
    } catch (error) { if (this.gate.current(ticket)) this.$('sources-status').textContent = error.message; }
    finally { if (this.gate.current(ticket)) this.$('sources-refresh').disabled = false; }
  }
}
