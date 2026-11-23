// 検索履歴のページ（4.7）。

import * as api from '../api.js';
import { debounce, h, icon, iconButton, linkButton, navButton } from '../dom.js';
import { dateFull, dateShort, num } from '../format.js';
import { confirmDialog } from '../parts/dialog.js';
import { openExport, openImport } from '../parts/exchange.js';
import { openMenu } from '../parts/menu.js';
import { VirtualTable } from '../parts/table.js';
import { toast, toastError } from '../parts/toast.js';

export async function render(ctx) {
  const { root } = ctx;
  let q = ctx.query.get('q') || '';
  let items = [];
  const selected = new Set();
  root.append(h('div', { class: 'page-head' }, h('h1', { text: '検索履歴' }),
    h('div', { class: 'actions' },
      linkButton('インポート', async () => { const r = await openImport({ allowLoad: true }); if (r && r.condition) loadToSearch(r.condition); else load(); }),
      linkButton('エクスポート', () => {
        const ids = selected.size ? [...selected] : items.map((i) => i.id);
        if (!ids.length) { toast('エクスポートする検索履歴がありません。'); return; }
        openExport({ history_ids: ids }, ids.length);
      }))));

  async function loadToSearch(cond) {
    sessionStorage.removeItem('finfet.search.state');
    const res = await api.post('/api/v1/targets/resolve', { targets: cond.targets || [] });
    sessionStorage.setItem('finfet.search.state', JSON.stringify({ ...cond, targets: res.targets, shift: null }));
    ctx.navigate('/search');
  }

  const filter = h('input', { class: 'input w-mid', type: 'search', placeholder: 'キーワード', value: q, 'aria-label': 'キーワード', dataset: { filter: '1' } });
  filter.addEventListener('input', debounce(() => { q = filter.value.trim(); ctx.updateQuery({ q }); load(); }, 300));
  root.append(h('div', { class: 'toolbar' }, filter));

  const pin = async (r) => {
    try {
      await api.patch(`/api/v1/history/${r.id}`, { pinned: !r.pinned });
      await load();
    } catch (e) {
      toastError(e);
    }
  };
  const remove = async (r) => {
    const ok = await confirmDialog({ title: '検索履歴を削除しますか', body: '削除した検索履歴は元に戻せません。', okText: '削除する', danger: true });
    if (!ok) return;
    await api.del(`/api/v1/history/${r.id}`);
    selected.delete(r.id);
    await load();
  };

  const table = new VirtualTable({
    id: 'history',
    rowHeight: 48,
    bottomReserve: 24,
    columns: [
      { key: 'sel', label: '', width: 36, fixed: true, noResize: true, align: 'center',
        render: (r) => h('input', { type: 'checkbox', checked: selected.has(r.id), 'aria-label': '選択', onchange: (e) => { if (e.target.checked) selected.add(r.id); else selected.delete(r.id); } }) },
      { key: 'pin', label: '', width: 36, fixed: true, noResize: true, align: 'center',
        render: (r) => h('button', { type: 'button', class: 'icon-btn', 'aria-pressed': String(r.pinned), 'aria-label': r.pinned ? 'ピン留めを外す' : 'ピン留め', title: r.pinned ? 'ピン留めを外す' : 'ピン留め', onclick: () => pin(r) }, icon(r.pinned ? 'pin-filled' : 'pin')) },
      { key: 'cond', label: '条件', width: 420, flex: true, fixed: true,
        render: (r) => h('a', { class: 'two-line-cell', href: `/search?history=${r.id}` },
          h('span', { class: 'mono ellipsis', text: r.name ? `${r.name}　${r.path_pattern}` : r.path_pattern }),
          h('span', { class: 'muted ellipsis', text: r.variables_summary })),
        title: (r) => r.path_pattern },
      { key: 'targets', label: '接続先', width: 200, render: (r) => r.targets_summary },
      { key: 'used', label: '最終使用', width: 110, render: (r) => dateShort(r.last_used_at), title: (r) => dateFull(r.last_used_at) },
      { key: 'count', label: '件数', width: 80, align: 'right', render: (r) => (r.last_result_count === null || r.last_result_count === undefined ? '' : num(r.last_result_count)) },
      { key: 'menu', label: '', width: 40, fixed: true, noResize: true, align: 'center',
        render: (r) => iconButton('more', '操作', (e) => openMenu(e.currentTarget, [
          { text: '検索のページで開く', href: `/search?history=${r.id}` },
          { text: 'エクスポート', onClick: () => openExport({ history_ids: [r.id] }, 1) },
          { text: '削除', onClick: () => remove(r) },
        ])) },
    ],
    rowClass: (r) => {
      const i = items.indexOf(r);
      const lastPinned = r.pinned && (i === items.length - 1 || !items[i + 1].pinned);
      return `two-line${lastPinned ? ' group-sep' : ''}`;
    },
    fetch: async (offset, limit) => ({ total: items.length, items: items.slice(offset, offset + limit) }),
    empty: h('span', null, '検索履歴はありません。', navButton('検索を開く', '/search')),
  });
  root.append(table.el);
  ctx.onLeave(() => table.destroy());

  async function load() {
    const res = await api.get(`/api/v1/history${api.qs({ q })}`);
    items = res.items;
    await table.reload();
  }
  await load();
  table.fit();
}
