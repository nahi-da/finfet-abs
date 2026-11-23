// 説明の吹き出し（v1.5.0。依頼者の意見: 説明は常に表示せず、ホバーで表示する）。
// [?] のボタンにマウスを重ねる・フォーカスする・クリックすると、説明を表示します。
// 吹き出しの中にリンクを置けるよう、吹き出しにマウスを移しても閉じません。Esc・外のクリックで閉じます。

import { h } from '../dom.js';

let current = null; // { btn, pop, pinned }
let hideTimer = null;

function close() {
  clearTimeout(hideTimer);
  if (!current) return;
  current.pop.remove();
  current.btn.setAttribute('aria-expanded', 'false');
  current = null;
}

function place(btn, pop) {
  const r = btn.getBoundingClientRect();
  const w = pop.offsetWidth;
  const hgt = pop.offsetHeight;
  const vw = document.documentElement.clientWidth;
  const vh = document.documentElement.clientHeight;
  let left = Math.min(Math.max(8, r.left), vw - w - 8);
  let top = r.bottom + 6;
  if (top + hgt > vh - 8 && r.top - hgt - 6 > 8) top = r.top - hgt - 6;
  pop.style.left = `${Math.max(8, left)}px`;
  pop.style.top = `${top}px`;
}

function open(btn, build, pinned) {
  clearTimeout(hideTimer);
  if (current && current.btn === btn) {
    if (pinned) current.pinned = true;
    return;
  }
  close();
  const pop = h('div', { class: 'hint-pop', role: 'tooltip', id: `hint-${Math.random().toString(36).slice(2, 8)}` });
  const content = build();
  pop.append(...(Array.isArray(content) ? content : [content]));
  pop.addEventListener('mouseenter', () => clearTimeout(hideTimer));
  pop.addEventListener('mouseleave', () => { if (current && !current.pinned) scheduleHide(); });
  document.body.appendChild(pop);
  place(btn, pop);
  btn.setAttribute('aria-expanded', 'true');
  btn.setAttribute('aria-describedby', pop.id);
  current = { btn, pop, pinned };
}

function scheduleHide() {
  clearTimeout(hideTimer);
  hideTimer = setTimeout(close, 250);
}

document.addEventListener('keydown', (e) => { if (e.key === 'Escape' && current) close(); });
document.addEventListener('pointerdown', (e) => {
  if (current && !current.pop.contains(e.target) && !current.btn.contains(e.target)) close();
}, true);
window.addEventListener('scroll', () => { if (current) close(); }, true);
window.addEventListener('resize', () => { if (current) close(); });

/**
 * [?] のボタンを作ります。
 * @param {() => (Node|string|Array<Node|string>)} build 吹き出しの中身を作る関数（開くたびに呼びます）
 * @param {string} [label] ボタンの名前（読み上げ用）
 */
export function hint(build, label = '説明') {
  const btn = h('button', { type: 'button', class: 'hint-btn', 'aria-label': label, 'aria-expanded': 'false', text: '?' });
  btn.addEventListener('mouseenter', () => open(btn, build, false));
  btn.addEventListener('mouseleave', () => { if (current && current.btn === btn && !current.pinned) scheduleHide(); });
  btn.addEventListener('focus', () => open(btn, build, false));
  btn.addEventListener('blur', (e) => {
    if (current && current.btn === btn && !current.pinned && !current.pop.contains(e.relatedTarget)) scheduleHide();
  });
  btn.addEventListener('click', (e) => {
    e.stopPropagation();
    if (current && current.btn === btn && current.pinned) close();
    else open(btn, build, true);
  });
  return btn;
}

/** 外部のページへのリンク（新しいタブで開きます）。 */
export function externalLink(href, text) {
  return h('a', { href, target: '_blank', rel: 'noopener noreferrer', text });
}
