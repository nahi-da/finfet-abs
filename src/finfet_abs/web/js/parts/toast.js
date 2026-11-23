// お知らせ（4.3.5）。操作の結果は左下に 5 秒間、後から確認したいものは上部のバーの「お知らせ」に残します。

import { copyText, h } from '../dom.js';

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
  if (opt.copy) {
    el.append(h('button', { type: 'button', class: 'link-btn', text: '詳細をコピー', onclick: async () => {
      if (await copyText(opt.copy)) toast('詳細をコピーしました');
    } }));
  }
  box.appendChild(el);
  setTimeout(() => el.remove(), opt.error ? 8000 : 5000);
}

const POPUP_LABEL = { info: '開始', ok: '完了', warn: '注意', error: 'エラー' };
const MAX_POPUPS = 4;

/**
 * アクティビティ（ジョブの開始・完了・エラー、検索の終了など）のポップアップ（v1.5.0。依頼者の依頼）。
 * 画面の右下に表示し、クリックすると詳細のページ（エラーはエラーの内容）を開きます。
 * 表示の時間は 8 秒（エラーは 20 秒）。マウスを重ねている間は消しません。
 * @param {string} level info / ok / warn / error
 * @param {string} text
 * @param {string} [url]
 */
export function activityPopup(level, text, url) {
  let box = document.getElementById('popups');
  if (!box) {
    box = h('div', { id: 'popups', class: 'popups', 'aria-live': 'polite' });
    document.body.appendChild(box);
  }
  while (box.children.length >= MAX_POPUPS) box.firstElementChild.remove();
  const remove = () => el.remove();
  const body = url
    ? h('a', { href: url, class: 'popup-body', title: '詳細を開く' }, h('span', { class: 'popup-text', text }), h('span', { class: 'popup-open', text: level === 'error' ? 'エラーの詳細を開く' : '詳細を開く' }))
    : h('div', { class: 'popup-body' }, h('span', { class: 'popup-text', text }));
  if (url) body.addEventListener('click', () => setTimeout(remove, 0));
  const el = h('div', { class: `popup ${level}`, role: level === 'error' ? 'alert' : 'status' },
    h('span', { class: 'popup-level', text: POPUP_LABEL[level] || 'お知らせ' }),
    body,
    h('button', { type: 'button', class: 'popup-close', 'aria-label': '閉じる', title: '閉じる', text: '×', onclick: remove }));
  const ms = level === 'error' ? 20000 : 8000;
  let timer = setTimeout(remove, ms);
  el.addEventListener('mouseenter', () => clearTimeout(timer));
  el.addEventListener('mouseleave', () => { clearTimeout(timer); timer = setTimeout(remove, 4000); });
  box.appendChild(el);
}

/**
 * 誤りを、文とエラーコードで左下に表示し、[詳細をコピー] で問い合わせ用の詳細をコピーできるようにします（3.7）。
 * 詳細には、本ツールの版、日時、エラーコード、状態コード、要求 ID を含めます（秘密情報は含めません）。
 * @param {any} e ApiError など
 */
export function toastError(e) {
  const code = e && e.code && e.code !== 'ERROR' ? e.code : '';
  const text = (e && e.message) || '要求に失敗しました。';
  const lines = [`FinFeT ${window.__finfetVersion || ''}`.trim(), `日時: ${new Date().toISOString()}`, `内容: ${text}`];
  if (code) lines.push(`エラーコード: ${code}`);
  if (e && e.status) lines.push(`状態コード: ${e.status}`);
  if (e && e.detail) lines.push(`詳細: ${e.detail}`);
  if (e && e.data && e.data.request_id) lines.push(`要求 ID: ${e.data.request_id}`);
  toast(code ? `${text}（${code}）` : text, { error: true, copy: lines.join('\n') });
}

/**
 * 上部のバーの「お知らせ」に残します（最大 50 件。起動し直すと消えます）。
 * @param {string} level info / ok / warn / error
 * @param {string} text
 * @param {string} [url]
 */
export function addNotice(level, text, url, { seq = null, at = null, read = false } = {}) {
  if (seq != null && notices.some((n) => n.seq === seq)) return false;
  notices.unshift({ level, text, url, at: at ? new Date(at * 1000) : new Date(), read, seq });
  notices.sort((a, b) => b.at - a.at);
  notices.length = Math.min(notices.length, MAX_NOTICES);
  onChange();
  return true;
}

/** 既読にした最後のお知らせの番号（サーバーに覚えます。ほかのタブと共有）。 */
export function lastSeq() {
  return notices.reduce((m, n) => (n.seq != null && n.seq > m ? n.seq : m), 0);
}

export function unreadCount() {
  return notices.filter((n) => !n.read).length;
}

export function markAllRead() {
  notices.forEach((n) => { n.read = true; });
  onChange();
}
