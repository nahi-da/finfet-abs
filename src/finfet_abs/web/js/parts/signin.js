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
 * @param {{add?: boolean, account?: string}} [opt] add: 別のアカウントを追加、account: そのアカウントで
 * @returns {Promise<boolean>}
 */
export function startSignIn(tenantId, opt = {}) {
  return new Promise(async (resolve) => {
    let res;
    let before;
    try {
      before = new Set(((await api.get('/api/v1/auth/status')).accounts || []).map((a) => a.id));
      res = tenantId ? await api.post(`/api/v1/auth/tenants/${encodeURIComponent(tenantId)}/sign-in`, { account: opt.account || null })
        : await api.post('/api/v1/auth/user/sign-in', { add: !!opt.add, account: opt.account || null });
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
      h('p', { text: 'ブラウザでサインインページを開きました。ページが開かない場合や、別のブラウザでサインインする場合は、次の URL を開いてください。' }),
      h('div', { class: 'inline', style: { flexWrap: 'nowrap', width: '100%' } }, urlEl, copyBtn),
      status.default_client_id ? h('p', { class: 'muted', style: { marginTop: '12px' } },
        'サインインページには「Microsoft Azure CLI」と表示されます。本ツールは、Azure の開発ツールと同じ方法でサインインします。') : null,
    ];
    const off = on('auth.changed', (d) => {
      if (finished || d.signin_pending || !d.signed_in) return;
      // アカウントの追加では、新しいアカウントが増えたとき、アカウントの指定があれば、そのアカウントがあるときに終わります
      const accounts = d.accounts || [];
      const added = accounts.find((a) => !before.has(a.id));
      const target = opt.account ? accounts.find((a) => a.id === opt.account.toLowerCase()) : null;
      if (opt.add && !added) return;
      if (opt.account && !target) return;
      finished = true;
      off();
      d2.close(true);
      toast(`${(added || target || d.account)?.username || ''} でサインインしました`);
      resolve(true);
    });
    const d2 = openDialog({
      title: tenantId ? 'このテナントにサインイン' : opt.add ? 'アカウントを追加' : 'サインイン',
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
