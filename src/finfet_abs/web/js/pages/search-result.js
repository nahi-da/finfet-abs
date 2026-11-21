// 検索結果のページ（4.4.4、3.2.10）。検索の途中でも、見つかった Blob を表示し、選べます。

import * as api from '../api.js';
import { button, clear, debounce, h, linkButton } from '../dom.js';
import { on } from '../events.js';
import { dateFull, dateShort, duration, num, size, statusClass } from '../format.js';
import { confirmDialog } from '../parts/dialog.js';
import { select, textInput } from '../parts/form.js';
import { openMenu } from '../parts/menu.js';
import { VirtualTable } from '../parts/table.js';
import { toast, toastError } from '../parts/toast.js';

const NOTE_TEXT = { rehydrating: 'リハイドレート中', downloaded: 'ダウンロード済み', writing: '更新中の可能性' };
const NOTE_FULL = { rehydrating: 'すでにリハイドレート中です', downloaded: '過去のジョブでダウンロード済みです', writing: '更新中の可能性があります' };

/** 層の表示（リハイドレート中は「Archive → Hot（標準）」）。 */
export function tierText(r) {
  if (r.archive_status && r.archive_status.startsWith('rehydrate-pending-to-')) {
    const to = r.archive_status.replace('rehydrate-pending-to-', '');
    const p = r.rehydrate_priority === 'High' ? '高' : '標準';
    return `Archive → ${to.charAt(0).toUpperCase()}${to.slice(1)}（${p}）`;
  }
  return r.tier || '—';
}

export async function render(ctx) {
  const { root } = ctx;
  const sid = ctx.params[0];
  let info;
  try {
    info = await api.get(`/api/v1/searches/${encodeURIComponent(sid)}`);
  } catch (e) {
    root.append(h('h1', { text: '検索結果' }), h('p', null, e.message, ' ', h('a', { href: '/search', text: '検索を開く' })));
    return;
  }
  ctx.setTitle(`検索結果（${dateShort(info.started_at)}）`);
  const settings = (await api.get('/api/v1/settings')).values;
  const q = ctx.query;
  const f = {
    q: q.get('q') || '', tier: q.get('tier') || '', type: q.get('type') || '', from: q.get('from') || '', to: q.get('to') || '',
    notes: q.get('notes') || '', sort: q.get('sort') || 'name',
  };
  let display = settings['ui.blob_name_display'] || 'full';
  const iso = (d, t) => (d ? new Date(`${d}T${t}`).toISOString().replace(/\.\d{3}Z$/, 'Z') : '');
  const qs = (extra = {}) => api.qs({ q: f.q, tier: f.tier, type: f.type, from: iso(f.from, '00:00:00'), to: iso(f.to, '23:59:59'), notes: f.notes, ...extra });

  // ---------------------------------------------------------------- 上の部分
  const csvMenu = linkButton('CSV', (e) => openMenu(e.currentTarget, [
    { text: '表示中の行', onClick: () => api.downloadGet(`/api/v1/searches/${sid}/results.csv${qs({ sort: f.sort })}`, 'search.csv') },
    { text: 'チェックした行', onClick: () => api.downloadGet(`/api/v1/searches/${sid}/results.csv${qs({ sort: f.sort, selected: '1' })}`, 'search.csv') },
  ]));
  root.append(
    h('div', { class: 'breadcrumb' }, h('a', { href: '/search', text: '検索' }), ' › ', `検索結果（${dateShort(info.started_at)}）`),
    h('div', { class: 'page-head' }, h('h1', { text: '検索結果' }),
      h('div', { class: 'actions' }, h('a', { href: `/search?search=${sid}`, text: '条件を変えて検索' }), csvMenu)),
    h('div', { class: 'summary-line cond-line' }, h('span', { text: info.targets_summary }), h('span', { class: 'mono ellipsis', title: info.condition.path_pattern, text: info.condition.path_pattern }), h('span', { class: 'muted', text: info.variables_summary })),
  );
  const progress = h('section', { class: 'part progress-sec' });
  root.append(progress);
  let expanded = false;

  function drawProgress(p) {
    clear(progress);
    const running = p.status === 'running';
    const targets = p.targets || [];
    const listed = targets.reduce((a, t) => a + (t.listed || 0), 0);
    const requests = targets.reduce((a, t) => a + (t.requests || 0), 0);
    const hasError = targets.some((t) => t.status === 'error');
    const head = h('div', { class: 'line' });
    if (running) {
      head.append(h('strong', { text: '検索中' }), h('span', { text: `取得 ${num(listed)} 件・一致 ${num(p.matched_count ?? p.matched)} 件` }), h('span.push'),
        button('中止', async () => {
          try { await api.post(`/api/v1/searches/${sid}/cancel`, {}); } catch (e) { toastError(e); }
        }));
    } else {
      const label = { completed: '完了', cancelled: '中止', failed: '検索の失敗' }[p.status] || p.status;
      head.append(h('strong', { class: statusClass(p.status), text: label }),
        h('span', { text: `一致 ${num(p.matched_count)} 件・要求 ${num(requests)} 回${p.elapsed_ms ? `・${duration(p.elapsed_ms)}` : ''}` }));
      if (targets.length > 1 || hasError) {
        head.append(h('button', { type: 'button', class: 'link-btn', 'aria-expanded': String(expanded || hasError), onclick: () => { expanded = !expanded; drawProgress(p); } }, expanded || hasError ? '接続先ごとの結果を隠す' : '接続先ごとの結果'));
      }
    }
    progress.append(head);
    if (p.status === 'cancelled') progress.append(h('div', { class: 'warn', text: '中止したため、結果は不完全です。' }));
    if (p.truncated) progress.append(h('div', { class: 'warn', text: '上限で打ち切りました。条件を絞ってください。' }));
    if (running || expanded || hasError) {
      const grid = h('div', { class: 'progress-grid' });
      for (const t of targets) {
        const st = { waiting: '待機中', listing: '取得中', completed: '完了', error: 'エラー', skipped: 'スキップ', cancelled: '中止' }[t.status] || t.status;
        grid.append(h('div', { class: 'progress-row' },
          h('span', { text: `${t.storage_account} / ${t.container}` }),
          h('span', { class: t.status === 'listing' ? '' : statusClass(t.status), text: st }),
          h('span', { class: 'num', text: `取得 ${num(t.listed)}` }), h('span', { class: 'num', text: `一致 ${num(t.matched)}` }), h('span', { class: 'num', text: `要求 ${num(t.requests)}` })));
        if (t.error && t.error.message) {
          const line = h('div', { class: 'progress-err' }, h('span', { class: t.status === 'error' ? 'danger' : 'muted', text: t.error.message + (t.error.code && /^[A-Za-z]/.test(t.error.code) ? `（${t.error.code}）` : '') }));
          if (t.status === 'error' && !running) {
            line.append(linkButton('再試行', async () => {
              try { await api.post(`/api/v1/searches/${sid}/targets/${t.no}/retry`, {}); poll(); } catch (e) { toastError(e); }
            }), linkButton('この接続先を除く', async () => {
              await api.post(`/api/v1/searches/${sid}/targets/${t.no}/skip`, {});
              poll();
            }));
          }
          grid.append(line);
        }
      }
      progress.append(grid);
    }
  }
  drawProgress(info);

  // ---------------------------------------------------------------- 絞り込み
  const kw = textInput(f.q, { width: 'w-short', placeholder: 'キーワード', 'aria-label': 'キーワード' });
  kw.dataset.filter = '1';
  kw.type = 'search';
  const tierSel = select([['', 'すべて'], ['Hot', 'Hot'], ['Cool', 'Cool'], ['Cold', 'Cold'], ['Archive', 'Archive'], ['rehydrating', 'リハイドレート中']], f.tier, { 'aria-label': '層' });
  const typeSel = select([['', 'すべて'], ['BlockBlob', 'ブロック'], ['AppendBlob', '追加'], ['PageBlob', 'ページ']], f.type, { 'aria-label': '種類' });
  const fromIn = h('input', { type: 'date', class: 'input', value: f.from, 'aria-label': '最終更新の開始' });
  const toIn = h('input', { type: 'date', class: 'input', value: f.to, 'aria-label': '最終更新の終了' });
  const notesSel = select([['', 'すべて'], ['with', 'あり'], ['without', 'なし']], f.notes, { 'aria-label': '注意' });
  const colBtn = linkButton('列', (e) => table.columnMenu(e.currentTarget));
  const dispSel = select([['full', '全体'], ['common', '共通する階層を省略'], ['tail', '末尾のみ']], display, { 'aria-label': 'Blob 名の表示', onchange: (e) => { display = e.target.value; table.reload(); } });
  const filterRow = h('div', { class: 'toolbar' }, kw, h('span', { text: '層' }), tierSel, h('span', { text: '種類' }), typeSel,
    h('span', { text: '最終更新' }), fromIn, h('span', { text: '〜' }), toIn, h('span', { text: '注意' }), notesSel,
    h('div', { class: 'push' }, h('span', { text: 'Blob 名' }), dispSel, colBtn));
  const selLine = h('span');
  const foundLine = h('span', { class: 'muted' });
  const selAll = linkButton('絞り込みに一致するすべてを選択', async () => {
    await api.post(`/api/v1/searches/${sid}/results/select`, { all_matching: true, filter: filterBody(), selected: true });
    refreshRows();
  });
  const clearSel = linkButton('選択を解除', async () => {
    await api.post(`/api/v1/searches/${sid}/results/select`, { clear: true });
    refreshRows();
  });
  const sumRow = h('div', { class: 'toolbar' }, selLine, foundLine, h('div', { class: 'push' }, selAll, clearSel));
  root.append(h('section', { class: 'part' }, filterRow, sumRow));

  const filterBody = () => ({ q: f.q, tier: f.tier ? [f.tier] : [], type: f.type ? [f.type] : [], modified_from: iso(f.from, '00:00:00') || null, modified_to: iso(f.to, '23:59:59') || null, notes: f.notes || null });
  const applyFilter = () => {
    f.q = kw.value.trim();
    f.tier = tierSel.value;
    f.type = typeSel.value;
    f.from = fromIn.value;
    f.to = toIn.value;
    f.notes = notesSel.value;
    ctx.updateQuery({ q: f.q, tier: f.tier, type: f.type, from: f.from, to: f.to, notes: f.notes });
    table.reload(false);
    refreshSummary();
  };
  kw.addEventListener('input', debounce(applyFilter, 300));
  for (const el of [tierSel, typeSel, fromIn, toIn, notesSel]) el.addEventListener('change', applyFilter);

  // ---------------------------------------------------------------- 表
  let lastClicked = null;
  const multi = (info.targets || []).length > 1;
  const savedLayout = (settings['ui.tables'] || {}).results || {};
  const toggle = async (row, checked, index, shift) => {
    if (shift && lastClicked !== null) {
      await api.post(`/api/v1/searches/${sid}/results/select`, { range: { start: lastClicked, end: index }, filter: filterBody(), sort: f.sort, selected: checked });
    } else {
      await api.post(`/api/v1/searches/${sid}/results/select`, { ids: [row.id], selected: checked });
    }
    lastClicked = index;
    refreshRows();
  };
  const headCheck = h('input', {
    type: 'checkbox', 'aria-label': '表示中の行すべて',
    onchange: async (e) => {
      const ids = table.loadedRows().filter(({ index }) => table.rendered.has(index)).map(({ row }) => row.id);
      await api.post(`/api/v1/searches/${sid}/results/select`, { ids, selected: e.target.checked });
      refreshRows();
    },
  });
  const table = new VirtualTable({
    id: 'results',
    sort: f.sort,
    bottomReserve: 64,
    saved: savedLayout,
    onLayout: (layout) => saveLayout('results', layout),
    onSort: (s) => { f.sort = s; ctx.updateQuery({ sort: s }); },
    columns: [
      { key: 'sel', label: '', width: 36, fixed: true, noResize: true, align: 'center', header: headCheck,
        render: (r, t) => h('input', {
          type: 'checkbox', checked: r.selected, 'aria-label': '選択',
          onclick: (e) => { const idx = Number(e.target.closest('.vrow').dataset.index); toggle(r, e.target.checked, idx, e.shiftKey); },
        }) },
      { key: 'target', label: 'ストレージアカウント / コンテナー', width: 230, sortable: true, hidden: !multi, render: (r) => `${r.storage_account} / ${r.container}` },
      { key: 'name', label: 'Blob 名', width: 360, flex: true, mono: true, sortable: true, fixed: true, render: (r) => r.display_name, title: (r) => r.blob_name },
      { key: 'tier', label: '層', width: 150, sortable: true, render: (r) => tierText(r) },
      { key: 'size', label: 'サイズ', width: 90, align: 'right', sortable: true, render: (r) => size(r.size) },
      { key: 'last_modified', label: '最終更新日時', width: 110, sortable: true, render: (r) => dateShort(r.last_modified), title: (r) => dateFull(r.last_modified) },
      { key: 'creation_time', label: '作成日時', width: 110, sortable: true, hidden: true, render: (r) => dateShort(r.creation_time), title: (r) => dateFull(r.creation_time) },
      { key: 'tier_change', label: '層の変更日時', width: 110, sortable: true, hidden: true, render: (r) => dateShort(r.access_tier_change_time), title: (r) => dateFull(r.access_tier_change_time) },
      { key: 'type', label: '種類', width: 80, sortable: true, hidden: true, render: (r) => ({ BlockBlob: 'ブロック', AppendBlob: '追加', PageBlob: 'ページ' }[r.blob_type] || r.blob_type) },
      { key: 'notes', label: '注意', width: 130, sortable: true, render: (r) => r.notes.map((n) => NOTE_TEXT[n.code]).filter(Boolean).join('・'), title: (r) => r.notes.map((n) => NOTE_FULL[n.code] + (n.at ? `（${dateShort(n.at)}）` : '')).join('\n') },
    ],
    rowClass: (r) => (r.selected ? 'selected' : ''),
    fetch: (offset, limit, sort) => api.get(`/api/v1/searches/${sid}/results${qs({ offset, limit, sort, display })}`),
    empty: '一致する Blob はありません。',
  });
  table.el.tabIndex = 0;
  table.el.addEventListener('keydown', async (e) => {
    if (e.key !== ' ' || e.target !== table.el) return;
    e.preventDefault();
    const first = table.loadedRows().find(({ index }) => table.rendered.has(index) && table.el.scrollTop <= index * 32);
    if (first) toggle(first.row, !first.row.selected, first.index, false);
  });
  const jobBtn = button('ジョブを作成', () => createJob(), { primary: true });
  const bottomSel = h('span');
  root.append(table.el, h('div', { class: 'sticky-bottom' }, bottomSel, h('span.push'), jobBtn));
  ctx.onLeave(() => table.destroy());

  async function refreshSummary() {
    try {
      const s = await api.get(`/api/v1/searches/${sid}/summary${qs()}`);
      const sel = s.selected;
      const archive = sel.archive_count ? `（Archive ${num(sel.archive_count)} 件・${size(sel.archive_bytes)}）` : '';
      selLine.textContent = `選択 ${num(sel.count)} 件・${size(sel.bytes)}${archive}`;
      bottomSel.textContent = `選択 ${num(sel.count)} 件・${size(sel.bytes)}`;
      const filtered = s.filtered.count !== s.all.count ? `   絞り込み ${num(s.filtered.count)} 件・${size(s.filtered.bytes)}` : '';
      foundLine.textContent = `見つかった ${num(s.all.count)} 件・${size(s.all.bytes)}${filtered}`;
      selAll.textContent = `絞り込みに一致するすべてを選択（${num(s.filtered.count)} 件）`;
      jobBtn.disabled = sel.count === 0;
      jobBtn.dataset.hidden = String(s.hidden_selected);
    } catch { /* 次の更新で */ }
  }
  async function refreshRows() {
    await table.reload();
    await refreshSummary();
  }

  async function createJob() {
    const hidden = Number(jobBtn.dataset.hidden || 0);
    let onlyVisible = false;
    if (hidden > 0) {
      const ok = await confirmDialog({
        title: '絞り込みで隠れた Blob を含めますか',
        body: `チェックした Blob のうち ${num(hidden)} 件は、絞り込みで表示されていません。含めない場合は、表示中の Blob だけでジョブを作ります。`,
        okText: '含める', cancelText: '含めない',
      });
      onlyVisible = !ok;
    }
    try {
      const m = await import('../parts/job-dialog.js');
      m.openCreateJobDialog({ source: 'search', searchId: sid, filter: onlyVisible ? filterBody() : null });
    } catch (e) {
      toastError(e);
    }
  }

  // ---------------------------------------------------------------- 進捗の更新
  let reloadTimer = null;
  const scheduleReload = () => {
    if (reloadTimer) return;
    reloadTimer = setTimeout(() => { reloadTimer = null; refreshRows(); }, 1500);
  };
  async function poll() {
    try {
      info = await api.get(`/api/v1/searches/${sid}`);
      drawProgress(info);
      await refreshRows();
    } catch { /* 次の更新で */ }
  }
  ctx.onLeave(on('search.progress', (d) => {
    if (d.id !== sid || !ctx.isCurrent()) return;
    drawProgress({ ...info, status: 'running', matched_count: d.matched, targets: d.targets });
    scheduleReload();
  }));
  ctx.onLeave(on('search.done', (d) => { if (d.id === sid && ctx.isCurrent()) poll(); }));
  ctx.onLeave(on('reset', () => { if (ctx.isCurrent()) poll(); }));
  await refreshRows();
  table.fit();
  if (info.status !== 'running') drawProgress(info);
}

/** 表の列の幅・表示を設定に保存します。 */
export const saveLayout = debounce(async (id, layout) => {
  try {
    const cur = (await api.get('/api/v1/settings')).values['ui.tables'] || {};
    cur[id] = layout;
    await api.put('/api/v1/settings', { changes: { 'ui.tables': cur } });
  } catch { /* 保存できなくても使えます */ }
}, 800);
