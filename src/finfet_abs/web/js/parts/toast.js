// お知らせ（4.3.5）。操作の結果は左下に 5 秒間、後から確認したいものは上部のバーの「お知らせ」に残します。

import { h } from '../dom.js';

const MAX_NOTICES = 50;
/** @type {{level: string, text: string, url?: string, at: Date, read: boolean}[]} */
export const notices = [];
let onChange = () => {};

/** お知らせの件数の表示を更新する関数を設定します。 */
export function setNoticeHandler(fn) { onChange = fn; }

/**
 * 左下に 5 秒間表示します。
 * @param {string} text
 * @param {{href?: string, linkText?: string, error?: boolean}} [opt]
 */
export function toast(text, opt = {}) {
  const box = document.getElementById('toasts');
  if (!box) return;
  const el = h('div', { class: `toast${opt.error ? ' error' : ''}`, role: 'status' },
    h('span', { text }),
    opt.href ? h('a', { href: opt.href, text: opt.linkText || '開く' }) : null);
  box.appendChild(el);
  setTimeout(() => el.remove(), opt.error ? 8000 : 5000);
}

/**
 * 上部のバーの「お知らせ」に残します（最大 50 件。起動し直すと消えます）。
 * @param {string} level info / ok / warn / error
 * @param {string} text
 * @param {string} [url]
 */
export function addNotice(level, text, url) {
  notices.unshift({ level, text, url, at: new Date(), read: false });
  notices.length = Math.min(notices.length, MAX_NOTICES);
  onChange();
}

export function unreadCount() {
  return notices.filter((n) => !n.read).length;
}

export function markAllRead() {
  notices.forEach((n) => { n.read = true; });
  onChange();
}
