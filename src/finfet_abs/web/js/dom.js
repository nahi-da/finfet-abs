// DOM の補助。値は必ず textContent で表示し、innerHTML に値を入れません（2.5 の S6）。

/**
 * 要素を作ります。
 * @param {string} tag タグ名（"div.cls#id" の形も可）
 * @param {Object|null} attrs 属性。on〜 はイベント、class・text・dataset・style は特別扱い
 * @param {...any} children 子（文字列は文字のノード。null・false は無視）
 * @returns {HTMLElement}
 */
export function h(tag, attrs, ...children) {
  let name = tag;
  let id = null;
  const classes = [];
  const m = tag.match(/^([a-z0-9-]+)((?:[.#][\w-]+)*)$/i);
  if (m) {
    name = m[1];
    for (const part of (m[2] || '').match(/[.#][\w-]+/g) || []) {
      if (part[0] === '.') classes.push(part.slice(1));
      else id = part.slice(1);
    }
  }
  const el = document.createElement(name);
  if (id) el.id = id;
  if (classes.length) el.className = classes.join(' ');
  if (attrs) {
    for (const [k, v] of Object.entries(attrs)) {
      if (v === null || v === undefined || v === false) continue;
      if (k === 'class') el.className = [el.className, v].filter(Boolean).join(' ');
      else if (k === 'text') el.textContent = String(v);
      else if (k === 'dataset') Object.assign(el.dataset, v);
      // CSP によりインラインの style 属性は使えないため、CSSOM で設定します
      else if (k === 'style') Object.assign(el.style, v);
      else if (k.startsWith('on') && typeof v === 'function') el.addEventListener(k.slice(2).toLowerCase(), v);
      else if (k === 'value' && 'value' in el) el.value = v;
      else if (k === 'checked' || k === 'disabled' || k === 'selected' || k === 'hidden' || k === 'indeterminate') el[k] = Boolean(v);
      else if (v === true) el.setAttribute(k, '');
      else el.setAttribute(k, String(v));
    }
  }
  append(el, children);
  return el;
}

/**
 * 子を追加します。
 * @param {HTMLElement} el
 * @param {any[]} children
 */
export function append(el, children) {
  for (const c of children.flat(Infinity)) {
    if (c === null || c === undefined || c === false) continue;
    el.appendChild(c instanceof Node ? c : document.createTextNode(String(c)));
  }
  return el;
}

/** 子をすべて取り除きます。 */
export function clear(el) {
  while (el.firstChild) el.removeChild(el.firstChild);
  return el;
}

/** 子を入れ替えます。 */
export function replace(el, ...children) {
  clear(el);
  return append(el, children);
}

/**
 * アイコン（web/icons の SVG）。
 * @param {string} name
 * @param {{size?: number}} [opt]
 */
export function icon(name, opt = {}) {
  return h('span', { class: `icon icon-${name}${opt.size === 20 ? ' i20' : ''}`, 'aria-hidden': 'true' });
}

/**
 * アイコンだけのボタン（aria-label とホバーの名前を付けます）。
 * @param {string} name アイコンの名前
 * @param {string} label 名前
 * @param {(e: Event) => void} onClick
 */
export function iconButton(name, label, onClick, attrs = {}) {
  return h('button', { type: 'button', class: 'icon-btn', 'aria-label': label, title: label, onclick: onClick, ...attrs }, icon(name));
}

/**
 * 補助の操作の小さいボタン（v1.5.0 で文字のリンクの形をやめ、v1.5.2 で目的ごとに見た目を分けました。依頼者の意見）。
 * attrs.kind: 'add'（追加。+ と破線の枠）、'remove'（削除・除外。赤）、'menu'（メニューを開く。▾）
 */
export function linkButton(text, onClick, attrs = {}) {
  const { kind, ...rest } = attrs;
  const lead = kind === 'add' ? icon('plus') : null;
  const tail = kind === 'menu' ? icon('caret') : null;
  return h('button', { type: 'button', class: kind ? `link-btn lb-${kind}` : 'link-btn', onclick: onClick, ...rest }, lead, text, tail);
}

/** 追加のボタン。 */
export const addButton = (text, onClick, attrs = {}) => linkButton(text, onClick, { ...attrs, kind: 'add' });
/** 削除・除外のボタン。 */
export const removeButton = (text, onClick, attrs = {}) => linkButton(text, onClick, { ...attrs, kind: 'remove' });
/** メニューを開くボタン。 */
export const menuButton = (text, onClick, attrs = {}) => linkButton(text, onClick, { ...attrs, kind: 'menu' });

/**
 * ほかのページへ移るボタン（見た目はボタン、要素はリンク。→ を付けます。v1.5.2）。
 * @param {string} text
 * @param {string} href
 */
export function navButton(text, href, attrs = {}) {
  const { large, ...rest } = attrs;
  return h('a', { href, class: large ? 'btn go' : 'link-btn go', ...rest }, text, icon('arrow'));
}

/** 枠線のボタン（primary: 塗りつぶし）。 */
export function button(text, onClick, opt = {}) {
  const cls = ['btn'];
  if (opt.primary) cls.push('primary');
  if (opt.danger) cls.push('danger');
  if (opt.caution) cls.push('caution'); // 中止など、取り消しにくい操作（赤い枠。v1.5.2）
  if (opt.small) cls.push('small');
  return h('button', { type: 'button', class: cls.join(' '), onclick: onClick, disabled: opt.disabled, title: opt.title }, text);
}

/** 文字列をクリップボードにコピーします。 */
export async function copyText(text) {
  try {
    await navigator.clipboard.writeText(text);
    return true;
  } catch {
    const ta = h('textarea', { style: { position: 'fixed', left: '-9999px' } });
    ta.value = text;
    document.body.appendChild(ta);
    ta.select();
    let ok = false;
    try { ok = document.execCommand('copy'); } catch { ok = false; }
    ta.remove();
    return ok;
  }
}

/** 入力が止まってから呼ぶ関数を作ります。 */
export function debounce(fn, ms) {
  let t = null;
  const f = (...args) => {
    clearTimeout(t);
    t = setTimeout(() => fn(...args), ms);
  };
  f.cancel = () => clearTimeout(t);
  return f;
}
