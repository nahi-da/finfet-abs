// 表（仮想スクロール。4.3.1）。データはサーバーから 200 行ずつ取得し、表示中の行だけを描画します。

import { append, clear, h, icon } from '../dom.js';
import { checkbox } from './form.js';
import { openMenu } from './menu.js';

const BLOCK = 200;
const OVERSCAN = 8;
const CARD_QUERY = '(max-width: 639px)';

/** CSS の変数（px）を数で読みます。 */
function cssPx(name, fallback) {
  const v = parseFloat(getComputedStyle(document.documentElement).getPropertyValue(name));
  return Number.isFinite(v) && v > 0 ? v : fallback;
}

/**
 * カードの形での列の置き場所。
 * lead: 左（チェックボックスなど）/ title: 上の 2 行 / meta: 下の 1 行 / side: 右（メニュー）/ none: 表示しない
 */
function cardRole(c) {
  if (c.card) return c.card;
  if (['sel', 'grip', 'pin'].includes(c.key)) return 'lead';
  if (c.key === 'menu') return 'side';
  return c.flex ? 'title' : 'meta';
}

/**
 * @typedef {Object} Column
 * @property {string} key
 * @property {string} label
 * @property {number} [width] 幅（px）
 * @property {boolean} [flex] 残りの幅を使う
 * @property {'right'|'center'} [align]
 * @property {boolean} [mono]
 * @property {boolean} [sortable]
 * @property {string} [sortKey] 並べ替えのキー（既定は key）
 * @property {boolean} [hidden] 既定で隠す
 * @property {boolean} [fixed] 隠せない
 * @property {boolean} [noResize]
 * @property {(row: any, t: VirtualTable) => (Node|string|null)} [render]
 * @property {(row: any) => string} [title] ホバーで表示する文字
 * @property {Node|string} [header] 見出しに置くもの（label の代わり）
 */

export class VirtualTable {
  /**
   * @param {Object} opt
   * @param {string} [opt.id] 列の幅・表示を保存するときの名前
   * @param {Column[]} opt.columns
   * @param {(offset: number, limit: number, sort: string) => Promise<{total: number, items: any[]}>} opt.fetch
   * @param {(row: any) => any} [opt.rowId]
   * @param {(row: any) => string} [opt.rowClass]
   * @param {(row: any, e: MouseEvent) => void} [opt.onRowClick]
   * @param {string} [opt.sort]
   * @param {(sort: string) => void} [opt.onSort]
   * @param {string|Node} [opt.empty]
   * @param {number} [opt.rowHeight]
   * @param {number} [opt.bottomReserve] 表の下に残す高さ（px）
   * @param {number} [opt.minHeight]
   * @param {{widths?: Object, hidden?: string[]}} [opt.saved]
   * @param {(state: {widths: Object, hidden: string[]}) => void} [opt.onLayout]
   * @param {(info: {total: number}) => void} [opt.onLoad]
   */
  constructor(opt) {
    this.opt = opt;
    // 狭い幅・列が収まらない場合は、行をカードの形にします（v1.4.0。opt.cards が false なら表のまま。wantCard）
    this.mq = window.matchMedia(CARD_QUERY);
    this.card = opt.cards !== false && this.mq.matches;
    this.rowH = this.rowHeightNow();
    this.sort = opt.sort || '';
    this.total = null;
    this.blocks = new Map();
    this.pending = new Map();
    this.gen = 0;
    this.widths = { ...(opt.saved?.widths || {}) };
    const savedHidden = opt.saved?.hidden;
    this.hidden = new Set(savedHidden || opt.columns.filter((c) => c.hidden).map((c) => c.key));
    this.el = h('div', { class: 'vtable', role: 'table' });
    this.head = h('div', { class: 'vrow vhead', role: 'row' });
    this.body = h('div', { class: 'vbody', role: 'rowgroup' });
    this.emptyEl = h('div', { class: 'empty', hidden: true });
    this.el.append(this.head, this.body, this.emptyEl);
    this.rendered = new Map();
    this.el.addEventListener('scroll', () => this.schedule());
    this._onResize = () => { this.updateMode(); this.fit(); this.schedule(); };
    window.addEventListener('resize', this._onResize);
    this.el.classList.toggle('cards', this.card);
    this.renderHead();
  }

  /** 行の高さ（px）。文字の大きさの設定と、カードの形かどうかで変わります。 */
  rowHeightNow() {
    const fs = cssPx('--fs-table', 13);
    if (this.card) {
      return this.opt.cardRowHeight || Math.ceil(fs * 1.4 * 2 + cssPx('--fs-small', 12) * 1.5 + 2 + 18);
    }
    if (this.opt.rowHeight) return Math.round(this.opt.rowHeight * (fs / 13));
    return cssPx('--row-h', 36);
  }

  /**
   * カードの形にするか。狭い幅（640px 未満）のほか、列が表の幅に収まらない場合もカードにします
   * （表の中の横方向のスクロールをなくすため。[列] で列を減らして収まれば、表に戻ります）。
   */
  wantCard() {
    if (this.opt.cards === false) return false;
    if (this.mq.matches) return true;
    const avail = this.el.isConnected ? this.el.clientWidth : 0;
    if (!avail) return this.card;
    // 境目で行き来しないよう、表に戻すときは少し余裕を見ます
    return this.card ? avail < this.minWidth() + 16 : avail < this.minWidth();
  }

  /** 画面の幅・文字の大きさ・表示する列が変わったら、行の形と高さを合わせます。 */
  updateMode() {
    const card = this.wantCard();
    const changed = card !== this.card;
    this.card = card;
    const rowH = this.rowHeightNow();
    if (!changed && rowH === this.rowH) return;
    const idx = Math.floor(this.el.scrollTop / this.rowH);
    this.rowH = rowH;
    this.el.classList.toggle('cards', card);
    this.renderHead();
    this.rerender();
    this.el.scrollTop = idx * rowH;
  }

  /** 表示中の列。 */
  get columns() {
    return this.opt.columns.filter((c) => !this.hidden.has(c.key));
  }

  template() {
    return this.columns.map((c) => {
      const w = this.widths[c.key] || c.width || 120;
      return c.flex ? `minmax(${w}px, 1fr)` : `${w}px`;
    }).join(' ');
  }

  minWidth() {
    return this.columns.reduce((a, c) => a + (this.widths[c.key] || c.width || 120), 0);
  }

  /** カードの形の見出し: 左の列の見出し（すべて選択など）と、並べ替えのメニュー。 */
  renderCardHead() {
    this.head.style.gridTemplateColumns = '';
    this.head.style.minWidth = '';
    this.body.style.minWidth = '';
    for (const c of this.columns) {
      if (cardRole(c) === 'lead' && c.header) this.head.appendChild(h('div', { class: 'vcell card-lead' }, c.header));
    }
    const sortable = this.columns.filter((c) => c.sortable && typeof c.label === 'string' && c.label);
    if (sortable.length) {
      const cur = sortable.find((c) => this.sort === (c.sortKey || c.key) || this.sort === `-${c.sortKey || c.key}`);
      const desc = this.sort.startsWith('-');
      const btn = h('button', { type: 'button', class: 'link-btn card-sort', 'aria-haspopup': 'menu' },
        `並べ替え: ${cur ? cur.label : '既定'}`, cur ? icon(desc ? 'sort-desc' : 'sort-asc') : null);
      btn.addEventListener('click', () => openMenu(btn, sortable.flatMap((c) => {
        const sk = c.sortKey || c.key;
        return [
          { text: `${c.label}（昇順）`, onClick: () => this.setSort(sk) },
          { text: `${c.label}（降順）`, onClick: () => this.setSort(`-${sk}`) },
        ];
      }), { align: 'left' }));
      this.head.appendChild(h('div', { class: 'vcell card-sortcell' }, btn));
    }
  }

  renderHead() {
    clear(this.head);
    if (this.card) {
      this.renderCardHead();
      return;
    }
    this.head.style.gridTemplateColumns = this.template();
    this.head.style.minWidth = `${this.minWidth()}px`;
    this.body.style.minWidth = `${this.minWidth()}px`;
    for (const c of this.columns) {
      const cls = ['vcell'];
      if (c.align === 'right') cls.push('right');
      if (c.align === 'center') cls.push('center');
      if (c.sortable) cls.push('sortable');
      const sk = c.sortKey || c.key;
      const cell = h('div', { class: cls.join(' '), role: 'columnheader', title: typeof c.label === 'string' ? c.label : null });
      if (c.header) append(cell, [c.header]);
      else cell.appendChild(h('span', { class: 'ellipsis', text: c.label }));
      if (c.sortable) {
        if (this.sort === sk) cell.appendChild(icon('sort-asc'));
        else if (this.sort === `-${sk}`) cell.appendChild(icon('sort-desc'));
        cell.setAttribute('aria-sort', this.sort === sk ? 'ascending' : this.sort === `-${sk}` ? 'descending' : 'none');
        cell.addEventListener('click', (e) => {
          if (e.target.closest('.resizer')) return;
          this.setSort(this.sort === sk ? `-${sk}` : sk);
        });
      }
      if (!c.noResize) {
        const rz = h('span', { class: 'resizer', 'aria-hidden': 'true' });
        rz.addEventListener('mousedown', (e) => this.startResize(e, c));
        cell.appendChild(rz);
      }
      this.head.appendChild(cell);
    }
  }

  startResize(e, col) {
    e.preventDefault();
    e.stopPropagation();
    const startX = e.clientX;
    const startW = this.widths[col.key] || col.width || 120;
    const rz = e.currentTarget;
    rz.classList.add('active');
    document.body.style.cursor = 'col-resize';
    const move = (ev) => {
      this.widths[col.key] = Math.max(40, Math.round(startW + ev.clientX - startX));
      this.applyTemplate();
    };
    const up = () => {
      rz.classList.remove('active');
      document.body.style.cursor = '';
      document.removeEventListener('mousemove', move);
      document.removeEventListener('mouseup', up);
      this.saveLayout();
    };
    document.addEventListener('mousemove', move);
    document.addEventListener('mouseup', up);
  }

  applyTemplate() {
    const t = this.template();
    const mw = `${this.minWidth()}px`;
    this.head.style.gridTemplateColumns = t;
    this.head.style.minWidth = mw;
    this.body.style.minWidth = mw;
    for (const el of this.rendered.values()) el.style.gridTemplateColumns = t;
  }

  saveLayout() {
    if (this.opt.onLayout) this.opt.onLayout({ widths: this.widths, hidden: [...this.hidden] });
  }

  /** [列] のメニュー。 */
  columnMenu(anchor) {
    const items = this.opt.columns.filter((c) => !c.fixed && typeof c.label === 'string' && c.label).map((c) => checkbox(c.label, !this.hidden.has(c.key), (on) => {
      if (on) this.hidden.delete(c.key);
      else this.hidden.add(c.key);
      this.renderHead();
      this.rerender();
      this.updateMode();
      this.saveLayout();
    }));
    openMenu(anchor, items);
  }

  setSort(sort) {
    this.sort = sort;
    this.renderHead();
    if (this.opt.onSort) this.opt.onSort(sort);
    this.reload(false);
  }

  /** 表の高さを、画面の下端に合わせます。 */
  fit() {
    if (!this.el.isConnected) return;
    this.updateMode();
    const top = this.el.getBoundingClientRect().top;
    let reserve = this.opt.bottomReserve ?? 24;
    // 狭い幅では、画面の下のタブの分も空けます
    const nav = document.getElementById('nav');
    if (nav && !this.el.closest('dialog') && getComputedStyle(nav).position === 'fixed') reserve += nav.offsetHeight;
    const min = this.opt.minHeight || 160;
    const hgt = Math.max(min, window.innerHeight - top - reserve);
    this.el.style.height = `${hgt}px`;
    // ページの下の余白などで、ページ全体にスクロールバーが出る場合は、その分だけ表を低くします
    if (!this.el.closest('dialog')) {
      const over = document.documentElement.scrollHeight - window.innerHeight;
      if (over > 0) this.el.style.height = `${Math.max(min, hgt - over)}px`;
    }
  }

  /** データを取得し直します。 */
  async reload(keepScroll = true) {
    this.gen += 1;
    this.blocks.clear();
    this.pending.clear();
    if (!keepScroll) this.el.scrollTop = 0;
    await this.loadBlock(Math.floor(this.firstVisible() / BLOCK));
    this.fit();
    this.rerender();
  }

  /** 行の内容だけを描き直します（データは取得し直しません）。 */
  rerender() {
    for (const el of this.rendered.values()) el.remove();
    this.rendered.clear();
    this.render();
  }

  firstVisible() {
    return Math.max(0, Math.floor(this.el.scrollTop / this.rowH) - OVERSCAN);
  }

  async loadBlock(b) {
    if (this.blocks.has(b)) return;
    if (this.pending.has(b)) return this.pending.get(b);
    const gen = this.gen;
    const p = (async () => {
      try {
        const res = await this.opt.fetch(b * BLOCK, BLOCK, this.sort);
        if (gen !== this.gen) return;
        this.total = res.total;
        this.blocks.set(b, res.items);
        if (this.opt.onLoad) this.opt.onLoad({ total: res.total });
      } finally {
        this.pending.delete(b);
      }
    })();
    this.pending.set(b, p);
    return p;
  }

  schedule() {
    if (this.raf) return;
    this.raf = requestAnimationFrame(() => {
      this.raf = null;
      this.render();
    });
  }

  rowAt(i) {
    const b = this.blocks.get(Math.floor(i / BLOCK));
    return b ? b[i % BLOCK] : undefined;
  }

  render() {
    const total = this.total ?? 0;
    this.body.style.height = `${total * this.rowH}px`;
    const empty = this.total === 0;
    this.emptyEl.hidden = !empty;
    if (empty) {
      clear(this.emptyEl);
      append(this.emptyEl, [this.opt.empty || 'ありません。']);
    }
    const viewH = this.el.clientHeight || 600;
    const first = this.firstVisible();
    const last = Math.min(total - 1, Math.ceil((this.el.scrollTop + viewH) / this.rowH) + OVERSCAN);
    const need = new Set();
    for (let i = first; i <= last; i += 1) need.add(i);
    for (const [i, el] of this.rendered) {
      if (!need.has(i)) {
        el.remove();
        this.rendered.delete(i);
      }
    }
    const missing = new Set();
    const tpl = this.template();
    for (const i of need) {
      if (this.rendered.has(i)) continue;
      const row = this.rowAt(i);
      let el;
      if (row === undefined) {
        missing.add(Math.floor(i / BLOCK));
        el = h('div', { class: 'vrow loading-row', role: 'row' });
      } else {
        el = this.renderRow(row, i);
      }
      el.style.top = `${i * this.rowH}px`;
      el.style.height = `${this.rowH}px`;
      if (!this.card) el.style.gridTemplateColumns = tpl;
      this.body.appendChild(el);
      this.rendered.set(i, el);
    }
    for (const b of missing) {
      this.loadBlock(b).then(() => {
        for (const [i, el] of this.rendered) {
          if (Math.floor(i / BLOCK) === b && el.classList.contains('loading-row')) {
            el.remove();
            this.rendered.delete(i);
          }
        }
        this.render();
      }).catch(() => {});
    }
  }

  renderRow(row, i) {
    const cls = ['vrow'];
    if (this.opt.onRowClick) cls.push('clickable');
    if (this.opt.rowClass) {
      const extra = this.opt.rowClass(row);
      if (extra) cls.push(extra);
    }
    const el = h('div', { class: cls.join(' '), role: 'row', dataset: { index: String(i) } });
    const parts = this.card ? { lead: h('div.card-lead'), main: h('div.card-main'), meta: h('div.card-meta'), side: h('div.card-side') } : null;
    let titleCell = null;
    for (const c of this.columns) {
      const role = parts ? cardRole(c) : null;
      if (role === 'none') continue;
      const ccls = ['vcell'];
      if (c.align === 'right') ccls.push('right');
      if (c.align === 'center') ccls.push('center');
      if (c.mono) ccls.push('mono');
      if (role === 'title') ccls.push('card-title');
      const content = c.render ? c.render(row, this) : row[c.key];
      const cell = h('div', { class: ccls.join(' '), role: 'cell' });
      if (content !== null && content !== undefined) append(cell, [content]);
      const title = c.title ? c.title(row) : (typeof content === 'string' ? content : null);
      if (title) cell.title = title;
      if (!parts) el.appendChild(cell);
      else if (role === 'title' && !titleCell) titleCell = cell;
      else if (role === 'lead' || role === 'side') parts[role].appendChild(cell);
      else {
        // カードの下の行では、何の値かが分かるように、列の名前をホバーの名前に加えます
        if (typeof c.label === 'string' && c.label) cell.title = `${c.label}: ${cell.title || cell.textContent}`;
        parts.meta.appendChild(cell);
      }
    }
    if (parts) {
      if (titleCell) parts.main.appendChild(titleCell);
      parts.main.appendChild(parts.meta);
      if (parts.lead.firstChild) el.appendChild(parts.lead);
      el.appendChild(parts.main);
      if (parts.side.firstChild) el.appendChild(parts.side);
    }
    if (this.opt.onRowClick) {
      el.addEventListener('click', (e) => {
        if (e.target.closest('button, a, input, select, label, textarea')) return;
        this.opt.onRowClick(row, e);
      });
    }
    return el;
  }

  /**
   * 読み込み済みの行を更新します（表示中の行だけを描き直します。スクロール位置は保ちます）。
   * @param {(row: any) => any|null} fn 更新した行を返す。変えなければ null
   */
  updateRows(fn) {
    for (const items of this.blocks.values()) {
      for (let k = 0; k < items.length; k += 1) {
        const nv = fn(items[k]);
        if (nv) items[k] = nv;
      }
    }
    this.rerender();
  }

  /** 読み込み済みの行（表示の順）。 */
  loadedRows() {
    const out = [];
    for (const [b, items] of [...this.blocks.entries()].sort((x, y) => x[0] - y[0])) {
      items.forEach((r, k) => out.push({ index: b * BLOCK + k, row: r }));
    }
    return out;
  }

  destroy() {
    window.removeEventListener('resize', this._onResize);
    this.gen += 1;
  }
}
