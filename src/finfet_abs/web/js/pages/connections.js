// 接続のページ（4.8）。

import * as api from '../api.js';
import { button, clear, debounce, h, iconButton, linkButton } from '../dom.js';
import { on } from '../events.js';
import { dateFull, dateShort } from '../format.js';
import { confirmDialog, infoDialog } from '../parts/dialog.js';
import { openMenu } from '../parts/menu.js';
import { startSignIn } from '../parts/signin.js';
import { VirtualTable } from '../parts/table.js';
import { toast, toastError } from '../parts/toast.js';
import { runTask } from '../task.js';

/** サインアウトします（アカウントのメニューからも使います）。 */
export async function signOut() {
  await api.post('/api/v1/auth/sign-out', {});
  toast('サインアウトしました');
}

function signInSection(st) {
  const sec = h('section', { class: 'part' }, h('h2', { text: 'サインイン' }));
  if (!st || !st.signed_in) {
    sec.append(h('div', { class: 'line' }, h('span', { text: 'サインインしていません。' }), button('サインイン', () => startSignIn(), { primary: true })));
    return sec;
  }
  const a = st.account || {};
  const who = a.tenant_name ? `${a.username}（${a.tenant_name}）` : a.username;
  sec.append(h('div', { class: 'line' }, h('span', { text: `${who}でサインインしています。` }), linkButton('サインアウト', () => signOut())));
  const bad = (st.tenants || []).filter((t) => t.status !== 'ok' && t.status !== 'unknown');
  for (const t of bad) {
    sec.append(h('div', { class: 'line' },
      h('span', { class: 'warn', text: `使えないテナント: ${t.name || t.id}（${t.label}）` }),
      linkButton('このテナントにサインイン', () => startSignIn(t.id))));
  }
  return sec;
}

export async function render(ctx) {
  const { root } = ctx;
  const head = h('div', { class: 'page-head' }, h('h1', { text: '接続' }));
  const signHolder = h('div');
  const credSec = h('section', { class: 'part' });
  root.append(head, signHolder, credSec);

  const drawSign = async () => {
    try {
      const st = await api.get('/api/v1/auth/status');
      clear(signHolder);
      signHolder.append(signInSection(st));
    } catch (e) {
      clear(signHolder);
      signHolder.append(h('p', { class: 'danger', text: e.message }));
    }
  };
  ctx.onLeave(on('auth.changed', () => { if (ctx.isCurrent()) drawSign(); }));
  await drawSign();

  // ---------------------------------------------------------------- 保存した認証情報
  const selected = new Set();
  let q = ctx.query.get('q') || '';
  let items = [];
  const filter = h('input', { class: 'input w-mid', type: 'search', placeholder: 'キーワード', value: q, 'aria-label': 'キーワード', dataset: { filter: '1' } });
  const delBtn = button('削除', () => remove([...selected]));
  const delAll = linkButton('すべて削除', () => remove(null));
  const testResults = new Map();

  const removeConfirm = async (ids, all) => {
    const body = { ids: ids || [], all: !!all };
    let res = await api.post('/api/v1/credentials/delete', body);
    if (res && res.confirm_required) {
      const parts = [h('p', { text: '次のジョブ・検索履歴で使っている認証情報が含まれます。削除すると、これらは認証を探し直します。' })];
      if (res.jobs.length) parts.push(h('ul', null, res.jobs.map((j) => h('li', { text: j.name }))));
      if (res.history_count) parts.push(h('p', { text: `検索履歴 ${res.history_count} 件` }));
      const ok = await confirmDialog({ title: '認証情報を削除しますか', body: parts, okText: '削除する', danger: true });
      if (!ok) return;
      res = await api.post('/api/v1/credentials/delete', { ...body, confirm: true });
    }
    toast(`認証情報を削除しました（${res.deleted} 件）`);
    selected.clear();
    await load();
  };

  async function remove(ids) {
    if (ids && !ids.length) {
      toast('削除する行をチェックしてください。');
      return;
    }
    const n = ids ? ids.length : items.length;
    const ok = await confirmDialog({
      title: ids ? `認証情報を削除しますか（${n} 件）` : 'すべての認証情報を削除しますか',
      body: '削除した認証情報は、接続先の追加で入力し直すまで使えません。',
      okText: '削除する',
      danger: true,
    });
    if (!ok) return;
    try { await removeConfirm(ids, !ids); } catch (e) { toastError(e); }
  }

  const test = async (row) => {
    testResults.set(row.id, '確認しています…');
    table.rerender();
    try {
      const r = await runTask('POST', `/api/v1/credentials/${row.id}/test`, {});
      testResults.set(row.id, r.ok ? r.message : r.message);
      toast(r.message, { error: !r.ok });
    } catch (e) {
      testResults.set(row.id, e.message);
    }
    await load();
  };

  const showReason = (row) => infoDialog({
    title: '認証情報を使用できません',
    body: [h('p', { text: row.decrypt_error.message }), h('p', { class: 'muted', text: `Windows のエラーコード: ${row.decrypt_error.winerror ?? '—'}` })],
  });

  const table = new VirtualTable({
    id: 'credentials',
    bottomReserve: 24,
    minHeight: 200,
    columns: [
      { key: 'sel', label: '', width: 36, fixed: true, noResize: true, align: 'center',
        header: h('input', { type: 'checkbox', 'aria-label': 'すべて選択', onchange: (e) => { items.forEach((r) => (e.target.checked ? selected.add(r.id) : selected.delete(r.id))); table.rerender(); } }),
        render: (r) => h('input', { type: 'checkbox', checked: selected.has(r.id), 'aria-label': '選択', onchange: (e) => { if (e.target.checked) selected.add(r.id); else selected.delete(r.id); } }) },
      { key: 'storage_account', label: 'ストレージアカウント', width: 220, fixed: true,
        render: (r) => h('span', null, r.storage_account, r.endpoint_suffix !== 'core.windows.net' ? h('span', { class: 'muted', text: `（${r.endpoint_suffix}）` }) : null) },
      { key: 'scope', label: '範囲', width: 140, render: (r) => r.scope_container || 'アカウント全体' },
      { key: 'auth', label: '認証方式', width: 110,
        render: (r) => (r.usable ? r.auth_label : h('span', { class: 'danger', text: '使用できません' })) },
      { key: 'source', label: '追加の方法', width: 130, render: (r) => r.source_label },
      { key: 'expires', label: '有効期限', width: 110,
        render: (r) => (r.expired ? h('span', { class: 'danger', text: '期限切れ' }) : r.expires_at ? h('span', { title: dateFull(r.expires_at), text: dateShort(r.expires_at) }) : '—') },
      { key: 'tested', label: '最終確認', width: 180, flex: true,
        render: (r) => {
          const t = testResults.get(r.id);
          if (t) return t;
          if (!r.last_test_at) return '—';
          return h('span', { title: r.last_test_message || '' }, dateShort(r.last_test_at), ' ', h('span', { class: r.last_test_ok ? 'ok' : 'danger', text: r.last_test_ok ? '成功' : '失敗' }));
        } },
      { key: 'menu', label: '', width: 40, fixed: true, noResize: true, align: 'center',
        render: (r) => iconButton('more', '操作', (e) => openMenu(e.currentTarget, [
          { text: '接続テスト', onClick: () => test(r) },
          { text: '検索のページで使う', href: `/search?credential=${encodeURIComponent(r.id)}` },
          { text: '削除', onClick: () => remove([r.id]) },
        ])) },
    ],
    fetch: async (offset, limit) => ({ total: items.length, items: items.slice(offset, offset + limit) }),
    onRowClick: (r) => { if (!r.usable) showReason(r); },
    empty: '保存した認証情報はありません。',
  });

  async function load() {
    const res = await api.get(`/api/v1/credentials${api.qs({ q })}`);
    items = res.items;
    for (const id of [...selected]) if (!items.some((r) => r.id === id)) selected.delete(id);
    await table.reload();
  }

  const doFilter = debounce(() => {
    q = filter.value.trim();
    ctx.updateQuery({ q });
    load();
  }, 300);
  filter.addEventListener('input', doFilter);

  credSec.append(h('h2', { text: '保存した認証情報' }),
    h('div', { class: 'toolbar' }, delBtn, delAll, h('div', { class: 'push' }, filter)),
    table.el);
  ctx.onLeave(() => table.destroy());
  await load();
  table.fit();
}
