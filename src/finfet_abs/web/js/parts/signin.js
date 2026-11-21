// サインインのダイアログ（4.8.3）。

import * as api from '../api.js';
import { button, copyText, h } from '../dom.js';
import { on } from '../events.js';
import { openDialog } from './dialog.js';
import { toast, toastError } from './toast.js';

/**
 * サインインを始めます（tenantId を指定すると、そのテナントにサインイン）。
 * サインインが終わると、resolve(true)。取り消すと resolve(false)。
 * @param {string} [tenantId]
 * @returns {Promise<boolean>}
 */
export function startSignIn(tenantId) {
  return new Promise(async (resolve) => {
    let res;
    try {
      res = tenantId ? await api.post(`/api/v1/auth/tenants/${encodeURIComponent(tenantId)}/sign-in`, {})
        : await api.post('/api/v1/auth/user/sign-in', {});
    } catch (e) {
      toastError(e);
      resolve(false);
      return;
    }
    let finished = false;
    const urlEl = h('div', { class: 'mono ellipsis', title: res.auth_uri, style: { flex: '1', minWidth: '0' } }, res.auth_uri);
    const copyBtn = button('コピー', async () => {
      if (await copyText(res.auth_uri)) toast('URL をコピーしました');
    });
    const status = await api.get('/api/v1/auth/status');
    const body = [
      h('p', { text: 'ブラウザでサインインのページを開きました。開かない場合や、別のブラウザでサインインする場合は、次の URL を開いてください。' }),
      h('div', { class: 'inline', style: { flexWrap: 'nowrap', width: '100%' } }, urlEl, copyBtn),
      status.default_client_id ? h('p', { class: 'muted', style: { marginTop: '12px' } },
        'サインインのページには「Microsoft Azure CLI」と表示されます。本ツールは Azure の開発ツールと同じ方法でサインインします。') : null,
    ];
    const off = on('auth.changed', (d) => {
      if (finished) return;
      if (d.signed_in && !d.signin_pending) {
        finished = true;
        off();
        d2.close(true);
        toast(`${d.account?.username || ''} でサインインしました`);
        resolve(true);
      }
    });
    const d2 = openDialog({
      title: tenantId ? 'このテナントにサインイン' : 'サインイン',
      width: 720,
      body,
      onClose: () => {
        off();
        if (!finished) {
          finished = true;
          api.post('/api/v1/auth/sign-in/cancel', { flow_id: res.flow_id }).catch(() => {});
          resolve(false);
        }
      },
    });
    d2.setFoot(h('span.push'), button('キャンセル', () => d2.close(true)));
  });
}
