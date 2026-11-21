// ダイアログ（<dialog>。4.3.4）。

import { append, button, clear, h, iconButton } from '../dom.js';

/**
 * ダイアログを開きます。
 * @param {Object} opt
 * @param {string|Node} opt.title 見出し
 * @param {number} [opt.width] 480 / 720 / 960
 * @param {Node|Node[]} [opt.body]
 * @param {Node|Node[]} [opt.foot] 下の行（ボタンなど）
 * @param {() => boolean} [opt.isDirty] 入力の途中なら true（閉じる前に確認します）
 * @param {() => void} [opt.onClose]
 */
export function openDialog(opt) {
  const width = opt.width || 720;
  const titleEl = h('h2', null);
  const head = h('div.dlg-head', null, titleEl, iconButton('close', '閉じる', () => api.close()));
  const body = h('div.dlg-body');
  const foot = h('div.dlg-foot');
  const el = h('dialog', { class: `dlg w${width}` }, h('div.dlg-inner', null, head, body, foot));
  let closed = false;

  const api = {
    el,
    body,
    foot,
    setTitle(t) { clear(titleEl); append(titleEl, [t]); },
    setBody(...nodes) { clear(body); append(body, nodes); },
    setFoot(...nodes) { clear(foot); append(foot, nodes); foot.hidden = nodes.flat().filter(Boolean).length === 0; },
    async close(force = false) {
      if (closed) return;
      if (!force && opt.isDirty && opt.isDirty()) {
        const ok = await confirmDialog({ title: '入力を破棄しますか', body: '入力した内容は保存されません。', okText: '破棄する' });
        if (!ok) return;
      }
      closed = true;
      el.close();
      el.remove();
      if (opt.onClose) opt.onClose();
    },
  };
  el.addEventListener('cancel', (e) => {
    e.preventDefault();
    api.close();
  });
  api.setTitle(opt.title);
  if (opt.body) api.setBody(opt.body);
  api.setFoot(opt.foot || []);
  document.body.appendChild(el);
  el.showModal();
  return api;
}

/**
 * 確認のダイアログ（4.1.2）。取り消せない操作・料金がかかる操作・データを消す操作だけに使います。
 * @param {{title: string, body?: string|Node|Node[], okText: string, danger?: boolean, cancelText?: string, width?: number}} opt
 * @returns {Promise<boolean>}
 */
export function confirmDialog(opt) {
  return new Promise((resolve) => {
    let result = false;
    const d = openDialog({
      title: opt.title,
      width: opt.width || 480,
      body: typeof opt.body === 'string' ? h('p', { text: opt.body }) : opt.body,
      onClose: () => resolve(result),
    });
    const ok = button(opt.okText, () => { result = true; d.close(true); }, { primary: !opt.danger, danger: opt.danger });
    d.setFoot(h('span.push'), button(opt.cancelText || 'キャンセル', () => d.close(true)), ok);
    ok.focus();
  });
}

/**
 * 内容を見せるだけのダイアログ。
 * @param {{title: string, body: string|Node|Node[], width?: number}} opt
 */
export function infoDialog(opt) {
  return new Promise((resolve) => {
    const d = openDialog({
      title: opt.title,
      width: opt.width || 480,
      body: typeof opt.body === 'string' ? h('p', { text: opt.body }) : opt.body,
      onClose: () => resolve(),
    });
    d.setFoot(h('span.push'), button('閉じる', () => d.close(true)));
  });
}
