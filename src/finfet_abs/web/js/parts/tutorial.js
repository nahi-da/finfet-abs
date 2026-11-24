// チュートリアルの表示（4.12.2。v1.7.0）。説明する部品の周りを暗くし、横に吹き出しを表示します。
// 手順の位置はタブごとに sessionStorage に保持し、ページを移っても続きから表示します。
// 部品の有無と、操作が済んだか（done）は、一定の間隔で確かめます（ページの描き直しやダイアログにも追随するため）。

import * as api from '../api.js';
import { button, h } from '../dom.js';
import { app, navigate } from '../main.js';
import { STEPS } from './tutorial-steps.js';
import { toastError } from './toast.js';

const KEY = 'finfet.tutorial';
const SEARCH_KEY = 'finfet.search.state';
const TICK = 200;
/** 部品が見つからないまま、この時間（ミリ秒）が過ぎたら、[次へ] で進めるようにします */
const MISSING_MS = 2500;

let timer = null;
let layer = null;
let shown = { i: -1, missingSince: 0, scrolled: false };

function load() {
  try { return JSON.parse(sessionStorage.getItem(KEY) || 'null'); } catch { return null; }
}
function store(v) {
  try { if (v) sessionStorage.setItem(KEY, JSON.stringify(v)); else sessionStorage.removeItem(KEY); } catch { /* 保存できなくても続けます */ }
}

/** チュートリアルを表示中か。 */
export function isActive() { return !!load(); }

/** 最初の手順から始めます。検索のページの入力は空にします。 */
export function start() {
  try { sessionStorage.removeItem(SEARCH_KEY); } catch { /* なくても続けます */ }
  store({ i: 0 });
  resume();
}

/** 保持している手順から続けます。 */
export function resume() {
  if (!isActive()) return;
  mount();
  if (!timer) timer = setInterval(tick, TICK);
  tick();
}

/** 終了します。 */
export function stop() {
  store(null);
  if (timer) clearInterval(timer);
  timer = null;
  layer?.remove();
  layer = null;
  shown = { i: -1, missingSince: 0, scrolled: false };
  document.removeEventListener('keydown', onKey, true);
  document.removeEventListener('scroll', onScroll, true);
  window.removeEventListener('resize', onScroll);
}

function go(i) {
  if (i >= STEPS.length) { stop(); return; }
  store({ i: Math.max(0, i) });
  shown = { i: -1, missingSince: 0, scrolled: false };
  tick();
}

// スクロール・大きさの変更では、すぐに位置を直します（古い位置の暗い部分で、クリックが止められないように）
function onScroll() { if (layer) tick(); }

function onKey(e) {
  // Esc で終了します（ダイアログを開いている場合は、ダイアログを閉じる操作を優先します）
  if (e.key === 'Escape' && !document.querySelector('dialog[open]')) { e.preventDefault(); stop(); }
}

function mount() {
  if (layer) return;
  layer = h('div', { class: 'tut-layer', role: 'presentation' },
    ...['t', 'l', 'r', 'b'].map((s) => h('div', { class: `tut-shade tut-${s}` })),
    h('div', { class: 'tut-ring' }),
    h('div', { class: 'tut-bubble', role: 'dialog', 'aria-live': 'polite', 'aria-label': 'チュートリアル' }));
  document.addEventListener('keydown', onKey, true);
  document.addEventListener('scroll', onScroll, true);
  window.addEventListener('resize', onScroll);
}

/** 表示する場所（開いているダイアログがあれば、その中。ダイアログの外は操作できないため）。 */
function host() {
  const ds = [...document.querySelectorAll('dialog[open]')];
  return ds.length ? ds[ds.length - 1] : document.body;
}

function visible(el) {
  if (!el || !el.isConnected) return null;
  const r = el.getBoundingClientRect();
  return r.width > 0 && r.height > 0 ? r : null;
}

function tick() {
  const st = load();
  if (!st || !layer) return;
  const i = Math.min(st.i, STEPS.length - 1);
  const step = STEPS[i];
  if (step.done) {
    let ok = false;
    try { ok = step.done(); } catch { ok = false; }
    if (ok) { go(i + 1); return; }
  }
  const hostEl = host();
  if (layer.parentNode !== hostEl) hostEl.append(layer);
  let el = null;
  try { el = step.target ? step.target() : null; } catch { el = null; }
  let rect = visible(el);
  if (rect && !shown.scrolled && (rect.top < 0 || rect.bottom > window.innerHeight)) {
    el.scrollIntoView({ block: 'center' });
    rect = visible(el);
  }
  if (rect) shown.scrolled = true;
  const now = Date.now();
  if (step.target && !rect) {
    if (!shown.missingSince) shown.missingSince = now;
  } else {
    shown.missingSince = 0;
  }
  const missing = !!step.target && !rect && now - shown.missingSince > MISSING_MS;
  const key = `${i}|${missing}`;
  if (shown.key !== key) {
    shown.key = key;
    shown.i = i;
    drawBubble(i, step, missing);
  }
  place(rect, step);
}

function drawBubble(i, step, missing) {
  const bubble = layer.querySelector('.tut-bubble');
  const prev = STEPS[i - 1];
  const canBack = i > 0 && prev && !prev.done;
  const btns = [];
  btns.push(h('button', { type: 'button', class: 'link-btn tut-quit', text: '終了', onclick: () => stop() }));
  btns.push(h('span', { class: 'push' }));
  if (canBack) btns.push(button('戻る', () => go(i - 1)));
  if (step.button && !missing) {
    btns.push(button(step.button.text, async (e) => {
      e.currentTarget.disabled = true;
      try { await step.button.run(); } catch (err) { toastError(err); }
    }, { primary: true }));
  }
  if (step.last) btns.push(button('チュートリアルを終了', () => stop(), { primary: true }));
  else if (!step.done || missing) {
    btns.push(button('次へ', () => {
      if (missing && step.skipTo) navigate(step.skipTo);
      go(i + 1);
    }, { primary: true }));
  }
  bubble.replaceChildren(...[
    h('div', { class: 'tut-count', text: `${i + 1} / ${STEPS.length}` }),
    h('div', { class: 'tut-title', text: step.title }),
    h('p', { class: 'tut-text', text: step.text }),
    step.action && !missing ? h('p', { class: 'tut-action', text: step.action }) : null,
    missing ? h('p', { class: 'tut-missing', text: '説明する部品が画面に見つかりません。[次へ] で先に進めます。' }) : null,
    h('div', { class: 'tut-btns' }, ...btns)].filter(Boolean));
}

/** 暗くする部分・枠・吹き出しの位置。rect がなければ全体を暗くし、吹き出しを中央に置きます。 */
function place(rect, step) {
  const vw = window.innerWidth;
  const vh = window.innerHeight;
  const pad = 6;
  const [t, l, r, b] = ['t', 'l', 'r', 'b'].map((s) => layer.querySelector(`.tut-${s}`));
  const ring = layer.querySelector('.tut-ring');
  const bubble = layer.querySelector('.tut-bubble');
  const set = (el, x, y, w, hh) => Object.assign(el.style, { left: `${x}px`, top: `${y}px`, width: `${Math.max(0, w)}px`, height: `${Math.max(0, hh)}px` });
  if (!rect) {
    set(t, 0, 0, vw, vh);
    for (const el of [l, r, b]) set(el, 0, 0, 0, 0);
    ring.hidden = true;
    const bw = bubble.offsetWidth;
    const bh = bubble.offsetHeight;
    Object.assign(bubble.style, { left: `${Math.max(8, (vw - bw) / 2)}px`, top: `${Math.max(8, (vh - bh) / 2)}px` });
    return;
  }
  const x0 = Math.max(0, rect.left - pad);
  const y0 = Math.max(0, rect.top - pad);
  const x1 = Math.min(vw, rect.right + pad);
  const y1 = Math.min(vh, rect.bottom + pad);
  set(t, 0, 0, vw, y0);
  set(b, 0, y1, vw, vh - y1);
  set(l, 0, y0, x0, y1 - y0);
  set(r, x1, y0, vw - x1, y1 - y0);
  ring.hidden = false;
  set(ring, x0, y0, x1 - x0, y1 - y0);
  // 吹き出し: 下に入れば下、入らなければ上、どちらも入らなければ右か左。画面の中に収めます
  const bw = bubble.offsetWidth;
  const bh = bubble.offsetHeight;
  const gap = 12;
  let left = Math.min(Math.max(8, x0), vw - bw - 8);
  let top;
  if (y1 + gap + bh <= vh - 8) top = y1 + gap;
  else if (y0 - gap - bh >= 8) top = y0 - gap - bh;
  else {
    top = Math.min(Math.max(8, y0), vh - bh - 8);
    left = x1 + gap + bw <= vw - 8 ? x1 + gap : Math.max(8, x0 - gap - bw);
  }
  Object.assign(bubble.style, { left: `${left}px`, top: `${top}px` });
}

/**
 * チュートリアルを始めます（設定・初めての確認から）。デモモードでは同じタブで、通常のモードではデモモードの本ツールを
 * 起動して新しいタブで始めます。新しいタブは、クリックの直後に開きます（ブラウザにポップアップとして止められないように）。
 */
export async function launch() {
  if (app.session?.demo) {
    // 検索のページの入力を空にして始めるため、読み込み直します
    location.href = '/search?tutorial=1';
    return;
  }
  const w = window.open('', '_blank');
  if (w) {
    try {
      w.document.title = 'FinFeT';
      w.document.body.style.cssText = 'font-family: sans-serif; padding: 24px; background: #15171b; color: #e6e6e6;';
      w.document.body.textContent = 'デモモードの FinFeT を起動しています。しばらくお待ちください…';
    } catch { /* 表示できなくても続けます */ }
  }
  try {
    const r = await api.post('/api/v1/tutorial/launch', {});
    if (w) w.location.href = r.url;
    else window.open(r.url, '_blank');
  } catch (e) {
    if (w) w.close();
    toastError(e);
  }
}

/** 初めて起動したときの確認（通常のモードで 1 回だけ）。 */
export async function offerOnce() {
  if (app.session?.demo || app.session?.tutorial_offered) return;
  app.session.tutorial_offered = true;
  api.post('/api/v1/tutorial/offered', {}).catch(() => {});
  const { openDialog } = await import('./dialog.js');
  const d = openDialog({
    title: 'FinFeT へようこそ',
    width: 480,
    body: [
      h('p', { text: 'チュートリアルでは、架空のデータを使って、Blob を検索してダウンロードするまでの流れを体験できます。実際の Azure には接続しません。所要時間は約 5 分です。' }),
      h('p', { class: 'muted', text: 'チュートリアルは、設定の「画面」から、いつでも始められます。' }),
    ],
  });
  d.setFoot(h('span.push'), button('スキップ', () => d.close(true)), button('チュートリアルを始める', () => { d.close(true); launch(); }, { primary: true }));
}
