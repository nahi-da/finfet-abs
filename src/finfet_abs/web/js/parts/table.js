// 表（仮想スクロール。4.3.1）。データはサーバーから 200 行ずつ取得し、表示中の行だけを描画します。

import { append, clear, h, icon } from '../dom.js';
import { checkbox } from './form.js';
import { openMenu } from './menu.js';

const BLOCK = 200;
const OVERSCAN = 8;

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
    this.rowH = opt.rowHeight || 32;
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
    this._onResize = () => { this.fit(); this.schedule(); };
    window.addEventListener('resize', this._onResize);
    this.renderHead();
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

  renderHead() {
    clear(this.head);
    this.head.style.gridTemplateColumns = this.template();
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
    const move = (ev) => {
      this.widths[col.key] = Math.max(40, Math.round(startW + ev.clientX - startX));
      this.applyTemplate();
    };
    const up = () => {
      document.removeEventListener('mousemove', move);
      document.removeEventListener('mouseup', up);
      this.saveLayout();
    };
    document.addEventListener('mousemove', move);
    document.addEventListener('mouseup', up);
  }

  applyTemplate() {
    const t = this.template();
    this.head.style.gridTemplateColumns = t;
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
    const top = this.el.getBoundingClientRect().top;
    const reserve = this.opt.bottomReserve ?? 24;
    const hgt = Math.max(this.opt.minHeight || 160, window.innerHeight - top - reserve);
    this.el.style.height = `${hgt}px`;
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
      el.style.gridTemplateColumns = tpl;
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
    for (const c of this.columns) {
      const ccls = ['vcell'];
      if (c.align === 'right') ccls.push('right');
      if (c.align === 'center') ccls.push('center');
      if (c.mono) ccls.push('mono');
      const content = c.render ? c.render(row, this) : row[c.key];
      const cell = h('div', { class: ccls.join(' '), role: 'cell' });
      if (content !== null && content !== undefined) append(cell, [content]);
      const title = c.title ? c.title(row) : (typeof content === 'string' ? content : null);
      if (title) cell.title = title;
      el.appendChild(cell);
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
