// メニュー（行のメニュー「…」、上部のバーのメニュー）。

import { h } from '../dom.js';

let current = null;

/** 開いているメニューを閉じます。 */
export function closeMenu() {
  if (current) {
    current.remove();
    current = null;
    document.removeEventListener('mousedown', onOutside, true);
    document.removeEventListener('keydown', onKey, true);
    window.removeEventListener('resize', closeMenu);
    window.removeEventListener('scroll', onScroll, true);
  }
}

function onOutside(e) {
  if (current && !current.contains(e.target)) closeMenu();
}

function onScroll(e) {
  if (current && e.target instanceof Node && current.contains(e.target)) return;
  closeMenu();
}

function onKey(e) {
  if (e.key === 'Escape') {
    e.stopPropagation();
    closeMenu();
  }
}

/**
 * メニューを開きます。
 * @param {HTMLElement} anchor 位置の基準にする要素
 * @param {(Object|string|Node)[]} items {text, onClick, disabled, href} / 'sep' / Node
 * @param {{align?: 'left'|'right'}} [opt]
 */
export function openMenu(anchor, items, opt = {}) {
  closeMenu();
  const el = h('div', { class: 'menu', role: 'menu' });
  for (const it of items) {
    if (!it) continue;
    if (it === 'sep') el.appendChild(h('div.sep'));
    else if (it instanceof Node) el.appendChild(it);
    else if (it.href) {
      el.appendChild(h('a', { href: it.href, role: 'menuitem', target: it.target, rel: it.target ? 'noopener' : null, onclick: () => closeMenu() }, it.text));
    } else {
      el.appendChild(h('button', {
        type: 'button',
        role: 'menuitem',
        disabled: it.disabled,
        onclick: () => { closeMenu(); it.onClick(); },
      }, it.text));
    }
  }
  document.body.appendChild(el);
  const r = anchor.getBoundingClientRect();
  const w = el.offsetWidth;
  const hgt = el.offsetHeight;
  let left = opt.align === 'left' ? r.left : r.right - w;
  left = Math.max(8, Math.min(left, window.innerWidth - w - 8));
  let top = r.bottom + 2;
  if (top + hgt > window.innerHeight - 8) top = Math.max(8, r.top - hgt - 2);
  el.style.left = `${left}px`;
  el.style.top = `${top}px`;
  current = el;
  setTimeout(() => {
    document.addEventListener('mousedown', onOutside, true);
    document.addEventListener('keydown', onKey, true);
    window.addEventListener('resize', closeMenu);
    window.addEventListener('scroll', onScroll, true);
  }, 0);
  const first = el.querySelector('button:not(:disabled), a');
  if (first) first.focus();
  return el;
}
