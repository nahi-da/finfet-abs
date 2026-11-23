// 保存するパスを、ブロックを並べて組み立てる部品（v1.5.2: 依頼者の意見。パス構造の「指定する」で使います）。
// 使える要素（Blob の情報・検索条件の変数・日時・区切り・文字）を、クリックで末尾に加えるか、ドラッグで好きな位置に置きます。
// 組み立てた結果は、保存するパスの入力欄の文字（{{…}} の形）と同じものです。入力欄を直接書き換えた場合も、ブロックに反映します。
// キーボード: ブロックにフォーカスして ← → で移動、Delete・BackSpace で削除します。

import { h, icon } from '../dom.js';

const BUILTINS = [
  ['@account', 'ストレージアカウント'],
  ['@container', 'コンテナー'],
  ['@dir', 'Blob のフォルダー'],
  ['@name', 'ファイル名'],
  ['@stem', 'ファイル名（拡張子なし）'],
  ['@ext', '拡張子'],
  ['@path', 'Blob 名（全体）'],
];
const BUILTIN_LABEL = Object.fromEntries(BUILTINS);

// 日時の書式（単位の番号: 0 年 1 月 2 日 3 時 4 分 5 秒）
const DT_FORMATS = [
  ['YYYYMMDD', '年月日', [0, 1, 2]],
  ['YYYYMM', '年月', [0, 1]],
  ['YYYY', '年', [0]],
  ['MM', '月', [1]],
  ['DD', '日', [2]],
  ['hh', '時', [3]],
  ['YYYYMMDDhh', '年月日時', [0, 1, 2, 3]],
  ['mm', '分', [4]],
];
const DT_LABEL = Object.fromEntries(DT_FORMATS.map(([f, l]) => [f, l]));

/** パスの文字を、ブロックの並びに分けます。 */
export function tokenize(text) {
  const out = [];
  const re = /\{\{([^{}]*)\}\}|\/|[^{/]+|\{/g;
  let m;
  while ((m = re.exec(text || '')) !== null) {
    if (m[0] === '/') out.push({ t: 'sep' });
    else if (m[1] !== undefined) out.push({ t: 'ph', v: m[1] });
    else {
      const last = out[out.length - 1];
      if (last && last.t === 'text') last.v += m[0];
      else out.push({ t: 'text', v: m[0] });
    }
  }
  return out;
}

/** ブロックの並びを、パスの文字に戻します。 */
export function serialize(tokens) {
  return tokens.map((k) => (k.t === 'sep' ? '/' : k.t === 'ph' ? `{{${k.v}}}` : k.v)).join('');
}

function describe(tok) {
  if (tok.t === 'sep') return { cls: 'sep', label: '/', title: 'フォルダーの区切り' };
  if (tok.t === 'text') return { cls: 'text', label: tok.v, title: '文字' };
  const v = tok.v;
  if (v.startsWith('@')) return { cls: 'builtin', label: BUILTIN_LABEL[v] || v, code: `{{${v}}}`, title: `Blob の情報（{{${v}}}）` };
  const [name, fmt] = v.split('=');
  if (fmt !== undefined) return { cls: 'datetime', label: `${name}: ${DT_LABEL[fmt] || fmt}`, code: `{{${v}}}`, title: `日時の変数（{{${v}}}）` };
  return { cls: 'var', label: name, code: `{{${v}}}`, title: `検索条件の変数（{{${v}}}）` };
}

/**
 * @param {Object} opt
 * @param {HTMLInputElement} opt.input 保存するパスの入力欄（この部品は、入力欄の値を書き換え、input のイベントを出します）
 * @param {Object<string, {kind: string, units: number[]}>} [opt.variables] 検索条件の変数
 * @param {boolean} [opt.urlMode] URL で指定したジョブ（検索条件の変数は使えません）
 */
export function pathBlocks(opt) {
  const { input } = opt;
  let tokens = tokenize(input.value);
  let dragFrom = null; // 並びの中のブロックを動かすとき: 位置。パレットから: { tok }
  const canvas = h('div', { class: 'pb-canvas', role: 'list', 'aria-label': '組み立てたパス' });

  function commit(rerender = true) {
    input.value = serialize(tokens);
    input.dispatchEvent(new Event('input', { bubbles: true }));
    if (rerender) render();
  }

  function insertAt(i, tok) {
    tokens.splice(i, 0, tok);
    commit();
  }

  function move(from, to) {
    if (to < 0 || to >= tokens.length || from === to) return;
    const [k] = tokens.splice(from, 1);
    tokens.splice(to, 0, k);
    commit();
    canvas.querySelectorAll('.pb-block')[to]?.focus();
  }

  function remove(i) {
    tokens.splice(i, 1);
    commit();
    const blocks = canvas.querySelectorAll('.pb-block');
    (blocks[Math.min(i, blocks.length - 1)] || canvas).focus();
  }

  /** ドロップの位置（ブロックの中央より左なら前、右なら後ろ）。 */
  function dropIndex(e) {
    const blocks = [...canvas.querySelectorAll('.pb-block')];
    for (let i = 0; i < blocks.length; i++) {
      const r = blocks[i].getBoundingClientRect();
      if (e.clientY < r.top) return i;
      if (e.clientY <= r.bottom && e.clientX < r.left + r.width / 2) return i;
    }
    return blocks.length;
  }

  function clearMarks() {
    canvas.querySelectorAll('.drop-before, .drop-after').forEach((el) => el.classList.remove('drop-before', 'drop-after'));
  }

  canvas.addEventListener('dragover', (e) => {
    if (!dragFrom) return;
    e.preventDefault();
    clearMarks();
    const i = dropIndex(e);
    const blocks = canvas.querySelectorAll('.pb-block');
    if (blocks[i]) blocks[i].classList.add('drop-before');
    else if (blocks.length) blocks[blocks.length - 1].classList.add('drop-after');
  });
  canvas.addEventListener('dragleave', (e) => { if (!canvas.contains(e.relatedTarget)) clearMarks(); });
  canvas.addEventListener('drop', (e) => {
    if (!dragFrom) return;
    e.preventDefault();
    clearMarks();
    let i = dropIndex(e);
    if (typeof dragFrom.index === 'number') {
      if (dragFrom.index < i) i -= 1;
      move(dragFrom.index, i);
    } else insertAt(i, { ...dragFrom.tok });
    dragFrom = null;
  });

  function render() {
    canvas.replaceChildren();
    if (!tokens.length) {
      canvas.appendChild(h('span', { class: 'pb-empty', text: '下の要素をクリックするか、ここにドラッグしてください' }));
      return;
    }
    tokens.forEach((tok, i) => {
      const d = describe(tok);
      const block = h('span', { class: `pb-block ${d.cls}`, role: 'listitem', tabindex: '0', draggable: 'true', title: `${d.title}（ドラッグで移動、← → で移動、Delete で削除）` });
      if (tok.t === 'text') {
        const t = h('input', { class: 'pb-text', value: tok.v, 'aria-label': '文字', size: Math.max(2, tok.v.length) });
        t.addEventListener('input', () => {
          tok.v = t.value.replace(/[{}/]/g, '');
          if (t.value !== tok.v) t.value = tok.v;
          t.size = Math.max(2, tok.v.length);
          commit(false);
        });
        t.addEventListener('blur', () => { if (!tok.v) remove(tokens.indexOf(tok)); });
        t.addEventListener('keydown', (e) => e.stopPropagation());
        block.append(t);
      } else {
        block.append(h('span', { class: 'pb-label', text: d.label }));
      }
      block.append(h('button', { type: 'button', class: 'pb-del', 'aria-label': `${d.title}を削除`, tabindex: '-1', onclick: () => remove(i) }, icon('close')));
      block.addEventListener('dragstart', (e) => {
        dragFrom = { index: i };
        e.dataTransfer.effectAllowed = 'move';
        e.dataTransfer.setData('text/plain', serialize([tok]));
        block.classList.add('dragging');
      });
      block.addEventListener('dragend', () => { block.classList.remove('dragging'); clearMarks(); dragFrom = null; });
      block.addEventListener('keydown', (e) => {
        if (e.key === 'ArrowLeft') { e.preventDefault(); move(i, i - 1); }
        else if (e.key === 'ArrowRight') { e.preventDefault(); move(i, i + 1); }
        else if (e.key === 'Delete' || e.key === 'Backspace') { e.preventDefault(); remove(i); }
      });
      canvas.appendChild(block);
    });
  }

  // ---- 使える要素（パレット）
  function chip(tok, label, cls, title) {
    const el = h('button', { type: 'button', class: `pb-chip ${cls}`, draggable: 'true', title: `${title}（クリックで末尾に追加、ドラッグで好きな位置に追加）` },
      icon('plus'), label);
    el.addEventListener('click', () => {
      insertAt(tokens.length, { ...tok });
      if (tok.t === 'text') canvas.querySelector('.pb-block:last-child .pb-text')?.focus();
    });
    el.addEventListener('dragstart', (e) => {
      dragFrom = { tok };
      e.dataTransfer.effectAllowed = 'copy';
      e.dataTransfer.setData('text/plain', serialize([tok]));
    });
    el.addEventListener('dragend', () => { clearMarks(); dragFrom = null; });
    return el;
  }

  const groups = [];
  groups.push(['Blob の情報', BUILTINS.map(([v, l]) => chip({ t: 'ph', v }, l, 'builtin', `{{${v}}}`))]);
  if (!opt.urlMode) {
    const vars = Object.entries(opt.variables || {});
    const plain = vars.filter(([, v]) => v.kind !== 'datetime');
    if (plain.length) groups.push(['検索条件の変数', plain.map(([n]) => chip({ t: 'ph', v: n }, n, 'var', `{{${n}}}`))]);
    for (const [n, v] of vars.filter(([, x]) => x.kind === 'datetime')) {
      const units = new Set(v.units || []);
      const fmts = DT_FORMATS.filter(([, , need]) => need.every((u) => units.has(u)));
      if (fmts.length) groups.push([`日時（${n}）`, fmts.map(([f, l]) => chip({ t: 'ph', v: `${n}=${f}` }, l, 'datetime', `{{${n}=${f}}}`))]);
    }
  }
  groups.push(['区切り・文字', [
    chip({ t: 'sep' }, '/ フォルダーの区切り', 'sep', '/'),
    chip({ t: 'text', v: '_' }, '_', 'text', '文字 _'),
    chip({ t: 'text', v: '-' }, '-', 'text', '文字 -'),
    chip({ t: 'text', v: '' }, '文字を入力', 'text', '任意の文字'),
  ]]);
  const palette = h('div', { class: 'pb-palette' }, groups.map(([title, chips]) => h('div', { class: 'pb-group' },
    h('div', { class: 'pb-group-title', text: title }), h('div', { class: 'pb-chips' }, chips))));

  // 入力欄を直接書き換えたら、ブロックに反映します（この部品が書き換えたときは、同じ値なので描き直しません）
  input.addEventListener('input', () => {
    if (input.value !== serialize(tokens)) {
      tokens = tokenize(input.value);
      render();
    }
  });

  render();
  const clearBtn = h('button', { type: 'button', class: 'link-btn lb-remove', onclick: () => { tokens = []; commit(); } }, 'すべて削除');
  return h('div', { class: 'path-blocks' },
    h('div', { class: 'pb-head' }, h('span', { class: 'muted', text: 'ブロックで組み立てる' }), h('span.push'), clearBtn),
    canvas, palette);
}
