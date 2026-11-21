// ジョブのページ（4.5）: キュー（/jobs）と終了（/jobs/ended）。

import * as api from '../api.js';
import { button, debounce, h, icon, iconButton, linkButton } from '../dom.js';
import { on } from '../events.js';
import { dateFull, dateShort, num, size } from '../format.js';
import { confirmDialog, openDialog } from '../parts/dialog.js';
import { openMenu } from '../parts/menu.js';
import { formGrid, row, select, setError, textInput } from '../parts/form.js';
import { VirtualTable } from '../parts/table.js';
import { toast } from '../parts/toast.js';

const WARN_STATES = new Set(['waiting_download']);
const RESULT_LABELS = { completed: '完了', completed_with_errors: '一部失敗', cancelled: '中止' };

/** 進捗の文（4.5.1）。 */
export function progressText(j) {
  const c = j.counts || {};
  const by = c.by_status || {};
  const n = (k) => (by[k] ? by[k][0] : 0);
  const total = c.total || 0;
  if (j.status === 'waiting_download') {
    const done = n('completed') + n('ready');
    return `${num(done)} / ${num(total)} 件をリハイドレート済み`;
  }
  if (j.status === 'held' || j.status === 'queued') return total ? `${num(total)} 件・${size(c.bytes)}` : '';
  const done = n('completed') + n('skipped');
  return `${num(done)} / ${num(total)} 件・${size(c.bytes_downloaded || 0)} / ${size(c.bytes || 0)}`;
}

/** 中止（リハイドレートを要求済みの Blob があれば確認します。3.3.12）。 */
export async function cancelJobs(ids) {
  try {
    await api.post('/api/v1/jobs/actions/cancel', { ids });
    return true;
  } catch (e) {
    if (e.code !== 'CANCEL_CONFIRM') { toast(e.message, { error: true }); return false; }
    const ok = await confirmDialog({
      title: 'ジョブを中止しますか',
      body: `リハイドレートを要求済みの Blob が ${num(e.data.rehydrating)} 件あります。要求は取り消せず、料金はすでに発生しています。完了した Blob は、復元先の層の料金がかかります。`,
      okText: '中止する', danger: true,
    });
    if (!ok) return false;
    await api.post('/api/v1/jobs/actions/cancel', { ids, confirm: true });
    return true;
  }
}

/** 名前を変更するダイアログ。変更したら true。 */
export function renameJob(job) {
  return new Promise((resolve) => {
    let changed = false;
    const input = textInput(job.name, { width: 'w-full', 'aria-label': 'ジョブ名', maxlength: 200 });
    const d = openDialog({ title: '名前を変更', width: 480, body: formGrid(...row('ジョブ名', input)), onClose: () => resolve(changed) });
    const save = async () => {
      const name = input.value.trim();
      if (!name) { setError(input, 'ジョブ名を入力してください。'); return; }
      try {
        await api.patch(`/api/v1/jobs/${job.id}`, { name });
        changed = true;
        d.close(true);
      } catch (e) { setError(input, e.message); }
    };
    input.addEventListener('keydown', (e) => { if (e.key === 'Enter') save(); });
    d.setFoot(h('span.push'), button('キャンセル', () => d.close(true)), button('変更', save, { primary: true }));
    input.focus();
    input.select();
  });
}

export async function render(ctx) {
  const { root } = ctx;
  const ended = ctx.path === '/jobs/ended';
  const selected = new Set();
  let items = [];
  let counts = { queue: 0, ended: 0 };
  const tabQueue = h('a', { href: '/jobs', 'aria-current': ended ? null : 'page' });
  const tabEnded = h('a', { href: '/jobs/ended', 'aria-current': ended ? 'page' : null });
  root.append(h('div', { class: 'page-head' }, h('h1', { text: 'ジョブ' })), h('nav', { class: 'tabs', 'aria-label': 'ジョブの一覧' }, tabQueue, tabEnded));
  const drawTabs = () => {
    tabQueue.textContent = `キュー ${num(counts.queue)}`;
    tabEnded.textContent = `終了 ${num(counts.ended)}`;
  };

  const act = async (action) => {
    const ids = [...selected];
    if (!ids.length) { toast('ジョブを選んでください。'); return; }
    if (action === 'cancel') await cancelJobs(ids);
    else if (action === 'delete') {
      const ok = await confirmDialog({ title: 'ジョブの記録を削除しますか', body: 'ダウンロードしたファイルは削除しません。削除した記録は元に戻せません。', okText: '削除する', danger: true });
      if (!ok) return;
      await api.post('/api/v1/jobs/actions/delete', { ids });
    } else await api.post(`/api/v1/jobs/actions/${action}`, { ids });
    selected.clear();
    load();
  };

  let table;
  if (!ended) {
    const settings = (await api.get('/api/v1/settings')).values;
    const conc = select([1, 2, 3, 4].map((n) => [n, String(n)]), settings['queue.concurrent_jobs'], {
      'aria-label': '同時に実行するジョブの数',
      onchange: async (e) => { await api.put('/api/v1/settings', { changes: { 'queue.concurrent_jobs': Number(e.target.value) } }); load(); },
    });
    root.append(h('div', { class: 'toolbar' }, button('開始', () => act('start')), button('一時停止', () => act('pause')), button('中止', () => act('cancel')),
      linkButton('すべて開始', async () => { await api.post('/api/v1/jobs/actions/start-all', {}); load(); }),
      h('div', { class: 'push' }, h('span', { text: '同時に実行するジョブの数' }), conc)));
  } else {
    const kw = h('input', { class: 'input w-mid', type: 'search', placeholder: 'キーワード', 'aria-label': 'キーワード', value: ctx.query.get('q') || '' });
    const res = select([['', 'すべて'], ['completed', '完了'], ['completed_with_errors', '一部失敗'], ['cancelled', '中止']], ctx.query.get('result') || '', { 'aria-label': '結果', onchange: () => { ctx.updateQuery({ result: res.value }); load(); } });
    kw.addEventListener('input', debounce(() => { ctx.updateQuery({ q: kw.value.trim() }); load(); }, 300));
    root.append(h('div', { class: 'toolbar' }, button('削除', () => act('delete')), kw, h('span', { text: '結果' }), res));
  }

  // ---------------------------------------------------------------- 順番の変更（ドラッグ・Alt+↑↓・メニュー）
  async function moveTo(id, index) {
    const others = items.filter((x) => x.id !== id);
    const before = others[index]?.id || null;
    const after = before ? null : (others[others.length - 1]?.id || null);
    try {
      await api.put('/api/v1/jobs/queue-order', { id, before, after });
    } catch (e) { toast(e.message, { error: true }); }
    load();
  }
  let dragId = null;

  const nameCell = (r) => h('a', { href: `/jobs/${r.id}`, class: 'ellipsis', text: r.name, title: r.name });
  const menuCell = (r) => iconButton('more', '操作', (e) => openMenu(e.currentTarget, ended ? [
    { text: '開く', href: `/jobs/${r.id}` },
    { text: '削除', onClick: async () => { selected.clear(); selected.add(r.id); await act('delete'); } },
  ] : [
    { text: '先頭に移動して開始', onClick: async () => { await api.post(`/api/v1/jobs/${r.id}/actions/move-top-and-start`, {}); load(); } },
    { text: '先頭へ', onClick: () => moveTo(r.id, 0) },
    { text: '末尾へ', onClick: () => moveTo(r.id, items.length) },
    { text: '名前を変更', onClick: () => rename(r) },
    { text: '中止', onClick: async () => { if (await cancelJobs([r.id])) load(); } },
  ]));
  const check = (r) => h('input', { type: 'checkbox', checked: selected.has(r.id), 'aria-label': '選択', onchange: (e) => { if (e.target.checked) selected.add(r.id); else selected.delete(r.id); } });

  const queueCols = [
    { key: 'grip', label: '', width: 32, fixed: true, noResize: true, align: 'center',
      render: (r) => h('span', { class: 'grip', draggable: 'true', title: 'ドラッグして順番を変える（Alt+↑ / Alt+↓）', 'aria-hidden': 'true',
        ondragstart: (e) => { dragId = r.id; e.dataTransfer.effectAllowed = 'move'; e.dataTransfer.setData('text/plain', r.id); } }, icon('grip')) },
    { key: 'sel', label: '', width: 36, fixed: true, noResize: true, align: 'center', render: check },
    { key: 'name', label: '名前', width: 260, flex: true, fixed: true, render: nameCell },
    { key: 'status', label: '状態', width: 150, render: (r) => h('span', { class: WARN_STATES.has(r.status) || r.last_error ? 'status-warn' : '', text: r.status_label, title: r.last_error ? r.last_error.message : null }) },
    { key: 'progress', label: '進捗', width: 240, render: (r) => progressText(r) },
    { key: 'targets', label: '接続先', width: 180, render: (r) => r.targets_summary },
    { key: 'created', label: '追加', width: 110, render: (r) => dateShort(r.created_at), title: (r) => dateFull(r.created_at) },
    { key: 'menu', label: '', width: 40, fixed: true, noResize: true, align: 'center', render: menuCell },
  ];
  const endedCols = [
    { key: 'sel', label: '', width: 36, fixed: true, noResize: true, align: 'center', render: check },
    { key: 'name', label: '名前', width: 260, flex: true, fixed: true, render: nameCell },
    { key: 'result', label: '結果', width: 100, render: (r) => h('span', { class: r.status === 'completed' ? '' : 'status-warn', text: RESULT_LABELS[r.status] }) },
    { key: 'count', label: '件数', width: 140, align: 'right', render: (r) => { const by = r.counts.by_status || {}; return `${num((by.completed || [0])[0])} / ${num(r.counts.total || 0)}`; } },
    { key: 'size', label: 'サイズ', width: 100, align: 'right', render: (r) => size(r.counts.bytes || 0) },
    { key: 'targets', label: '接続先', width: 180, render: (r) => r.targets_summary },
    { key: 'ended', label: '終了日時', width: 120, render: (r) => dateShort(r.ended_at), title: (r) => dateFull(r.ended_at) },
    { key: 'menu', label: '', width: 40, fixed: true, noResize: true, align: 'center', render: menuCell },
  ];
  table = new VirtualTable({
    id: ended ? 'jobs-ended' : 'jobs-queue',
    columns: ended ? endedCols : queueCols,
    rowId: (r) => r.id,
    fetch: async (offset, limit) => ({ total: items.length, items: items.slice(offset, offset + limit) }),
    empty: ended ? '終了したジョブはありません。' : h('span', null, 'キューにジョブはありません。', h('a', { href: '/search', text: '検索を開く' })),
  });
  root.append(table.el);
  ctx.onLeave(() => table.destroy());

  if (!ended) {
    table.el.addEventListener('dragover', (e) => { if (dragId) { e.preventDefault(); e.dataTransfer.dropEffect = 'move'; } });
    table.el.addEventListener('drop', (e) => {
      if (!dragId) return;
      e.preventDefault();
      const body = table.body.getBoundingClientRect();
      const idx = Math.max(0, Math.min(items.length, Math.round((e.clientY - body.top) / table.rowH)));
      const from = items.findIndex((x) => x.id === dragId);
      const id = dragId;
      dragId = null;
      moveTo(id, idx > from ? idx - 1 : idx);
    });
    table.el.addEventListener('keydown', (e) => {
      if (!e.altKey || (e.key !== 'ArrowUp' && e.key !== 'ArrowDown')) return;
      const id = selected.size === 1 ? [...selected][0] : null;
      if (!id) return;
      e.preventDefault();
      const i = items.findIndex((x) => x.id === id);
      moveTo(id, Math.max(0, e.key === 'ArrowUp' ? i - 1 : i + 1));
    });
    table.el.tabIndex = 0;
  }

  async function rename(r) {
    if (await renameJob(r)) load();
  }

  async function load() {
    const res = await api.get(`/api/v1/jobs${api.qs({ view: ended ? 'ended' : 'queue', q: ctx.query.get('q') || '', result: ctx.query.get('result') || '' })}`);
    items = res.items;
    counts = res.counts;
    drawTabs();
    for (const id of [...selected]) if (!items.some((x) => x.id === id)) selected.delete(id);
    await table.reload(false);
  }
  const reload = debounce(load, 300);
  for (const t of ['job.changed', 'jobs.order', 'jobs.deleted', 'job.progress']) ctx.onLeave(on(t, reload));
  await load();
  table.fit();
}
