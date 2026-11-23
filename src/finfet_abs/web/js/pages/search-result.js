// 検索結果のページ（4.4.4、3.2.10）。検索の途中でも、見つかった Blob を表示し、選べます。

import * as api from '../api.js';
import { button, clear, debounce, h, linkButton, menuButton, navButton, removeButton } from '../dom.js';
import { on } from '../events.js';
import { dateFull, dateShort, duration, num, size, statusClass } from '../format.js';
import { confirmDialog } from '../parts/dialog.js';
import { select, textInput } from '../parts/form.js';
import { externalLink, hint } from '../parts/hint.js';
import { openMenu } from '../parts/menu.js';
import { VirtualTable } from '../parts/table.js';
import { toast, toastError } from '../parts/toast.js';

const NOTE_TEXT = { rehydrating: 'リハイドレート中', downloaded: 'ダウンロード済み', writing: '更新中の可能性' };
const NOTE_FULL = { rehydrating: 'すでにリハイドレート中です', downloaded: '過去のジョブでダウンロード済みです', writing: '更新中の可能性があります' };
// 注意の説明（v1.5.0。依頼者の意見: 「更新中の可能性」が何を示すかわからない）
const DOC_RESOURCE_LOGS = 'https://learn.microsoft.com/azure/azure-monitor/platform/resource-logs#send-to-azure-storage';
const DOC_REHYDRATE = 'https://learn.microsoft.com/azure/storage/blobs/archive-rehydrate-overview';
const NOTE_HELP = {
  writing: () => [
    h('p', null, h('strong', { text: '更新中の可能性' }), '：最終更新日時が 1 時間以内の Blob です。まだ書き込みが続いている可能性があります。'),
    h('p', { text: '診断設定（リソースログ）をストレージに送る場合、ログは 1 時間ごとの Blob（PT1H.json など）に追記され、その時間が終わるまで書き込みが続きます。今ダウンロードすると、後から追記された分が含まれません。時間をおいてから取得し直してください。' }),
    h('p', null, externalLink(DOC_RESOURCE_LOGS, 'Microsoft のドキュメント: リソースログをストレージに送る')),
  ],
  rehydrating: () => [
    h('p', null, h('strong', { text: 'リハイドレート中' }), '：Archive の層から戻す処理（リハイドレート）が、すでに進んでいる Blob です。ジョブではリハイドレートを要求し直さず、完了を待ってダウンロードします。'),
    h('p', null, externalLink(DOC_REHYDRATE, 'Microsoft のドキュメント: Archive の層からのリハイドレート')),
  ],
  downloaded: () => [h('p', null, h('strong', { text: 'ダウンロード済み' }), '：過去のジョブでダウンロードしたことがある Blob です（同じ接続先・同じ名前）。もう一度取得する必要があるかを確かめてください。')],
};
const notesHelp = (codes) => () => codes.flatMap((c) => (NOTE_HELP[c] ? NOTE_HELP[c]() : []));

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
    root.append(h('h1', { text: '検索結果' }), h('p', null, e.message, ' ', navButton('検索を開く', '/search')));
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
  const csvMenu = menuButton('CSV', (e) => openMenu(e.currentTarget, [
    { text: '表示中の行', onClick: () => api.downloadGet(`/api/v1/searches/${sid}/results.csv${qs({ sort: f.sort })}`, 'search.csv') },
    { text: 'チェックした行', onClick: () => api.downloadGet(`/api/v1/searches/${sid}/results.csv${qs({ sort: f.sort, selected: '1' })}`, 'search.csv') },
  ]));
  root.append(
    h('div', { class: 'breadcrumb' }, h('a', { href: '/search', text: '検索' }), ' › ', `検索結果（${dateShort(info.started_at)}）`),
    h('div', { class: 'page-head' }, h('h1', { text: '検索結果' }),
      h('div', { class: 'actions' }, navButton('条件を変えて検索', `/search?search=${sid}`), csvMenu)),
    h('div', { class: 'summary-line cond-line' }, h('span', { text: info.targets_summary }), h('span', { class: 'mono ellipsis', title: info.condition.path_pattern, text: info.condition.path_pattern }), h('span', { class: 'muted', text: info.variables_summary })),
  );
  const progress = h('section', { class: 'part progress-sec' });
  root.append(progress);
  let expanded = false;

  // 検索中は、表を薄くして操作できなくします（v1.5.0。依頼者の意見: 取得中であることをわかりやすく）
  let searching = info.status === 'running';
  let partsReady = false; // 表・ボタンを作り終えたか（作る前の drawProgress では、状態だけ覚えます）
  const setBusy = (on) => {
    searching = on;
    if (!partsReady) return;
    table.el.classList.toggle('busy', on);
    table.el.setAttribute('aria-busy', String(on));
    table.el.inert = on;
    for (const b of [selAll, clearSel, csvMenu]) b.disabled = on;
    if (on) jobBtn.disabled = true;
  };

  function drawProgress(p) {
    clear(progress);
    const running = p.status === 'running';
    setBusy(running);
    const targets = p.targets || [];
    const listed = targets.reduce((a, t) => a + (t.listed || 0), 0);
    const requests = targets.reduce((a, t) => a + (t.requests || 0), 0);
    const hasError = targets.some((t) => t.status === 'error');
    const head = h('div', { class: 'line' });
    if (running) {
      head.append(h('strong', { text: 'Blob を取得しています…' }), h('span', { text: `取得 ${num(listed)} 件・一致 ${num(p.matched_count ?? p.matched)} 件。終わるまで、一覧は操作できません` }), h('span.push'),
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
    if (running) progress.append(h('div', { class: 'busy-bar', role: 'progressbar', 'aria-label': '検索中' }));
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
            }), removeButton('この接続先を除く', async () => {
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
  const colBtn = menuButton('列', (e) => table.columnMenu(e.currentTarget));
  const dispSel = select([['full', '全体'], ['common', '共通する階層を省略'], ['tail', '末尾のみ']], display, { 'aria-label': 'Blob 名の表示', onchange: (e) => { display = e.target.value; table.reload(); } });
  // 名前と入力欄をまとまりにして、まとまりごとに折り返します。狭い幅では、キーワードのほかは［絞り込み］で開閉します
  const field = (label, cls, ...ctl) => h('div', { class: `field ${cls}` }, h('span', { class: 'field-label', text: label }), ...ctl);
  const filterToggle = h('button', {
    type: 'button', class: 'btn filter-toggle', 'aria-expanded': 'false',
    onclick: () => {
      const open = filterRow.classList.toggle('open');
      filterToggle.setAttribute('aria-expanded', String(open));
    },
  });
  const notesField = field('注意', 'more-f', notesSel);
  notesField.querySelector('.field-label').append(hint(notesHelp(['writing', 'rehydrating', 'downloaded']), '注意の説明'));
  const filterRow = h('div', { class: 'filters' },
    field('キーワード', 'kw', kw), filterToggle,
    field('層', 'more-f', tierSel), field('種類', 'more-f', typeSel),
    field('最終更新', 'more-f', h('div', { class: 'pair' }, fromIn, h('span', { text: '〜' }), toIn)),
    notesField,
    h('div', { class: 'push more-f' }, field('Blob 名の表示', '', dispSel), colBtn));
  const drawFilterToggle = () => {
    const n = [f.tier, f.type, f.from || f.to, f.notes].filter(Boolean).length;
    filterToggle.textContent = n ? `絞り込み（${n}）` : '絞り込み';
  };
  drawFilterToggle();
  const selLine = h('span');
  const foundLine = h('span', { class: 'muted' });
  const selAll = linkButton('すべて選択', async () => {
    await api.post(`/api/v1/searches/${sid}/results/select`, { all_matching: true, filter: filterBody(), selected: true });
    refreshRows();
  });
  const clearSel = linkButton('すべての選択を解除', async () => {
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
    drawFilterToggle();
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
  // 見出しのチェックボックスは、絞り込みに一致するすべての行を選ぶ・外す（v1.5.0。依頼者の意見: 表示中の行だけでなく全体を選ぶ）
  const headCheck = h('input', {
    type: 'checkbox', 'aria-label': '絞り込みに一致するすべての行を選択',
    onchange: async (e) => {
      try {
        await api.post(`/api/v1/searches/${sid}/results/select`, { all_matching: true, filter: filterBody(), selected: e.target.checked });
      } catch (err) { toastError(err); }
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
      { key: 'notes', label: '注意', width: 150, sortable: true,
        render: (r) => {
          const codes = r.notes.map((n) => n.code).filter((c) => NOTE_TEXT[c]);
          if (!codes.length) return '';
          return h('span', { class: 'note-cell' }, h('span', { class: 'ellipsis', text: codes.map((c) => NOTE_TEXT[c]).join('・') }), hint(notesHelp(codes), '注意の説明'));
        },
        title: (r) => r.notes.map((n) => NOTE_FULL[n.code] + (n.at ? `（${dateShort(n.at)}）` : '')).join('\n') },
    ],
    rowClass: (r) => (r.selected ? 'selected' : ''),
    fetch: (offset, limit, sort) => api.get(`/api/v1/searches/${sid}/results${qs({ offset, limit, sort, display })}`),
    empty: '一致する Blob はありません。',
  });
  table.el.tabIndex = 0;
  table.el.addEventListener('keydown', async (e) => {
    if (e.key !== ' ' || e.target !== table.el) return;
    e.preventDefault();
    const first = table.loadedRows().find(({ index }) => table.rendered.has(index) && table.el.scrollTop <= index * table.rowH);
    if (first) toggle(first.row, !first.row.selected, first.index, false);
  });
  const jobBtn = button('ジョブを作成', () => createJob(), { primary: true });
  const bottomSel = h('span', { class: 'summary' });
  root.append(table.el, h('div', { class: 'sticky-bottom' }, bottomSel, h('span.push'), jobBtn));
  ctx.onLeave(() => table.destroy());
  partsReady = true;
  setBusy(searching);

  async function refreshSummary() {
    try {
      const s = await api.get(`/api/v1/searches/${sid}/summary${qs()}`);
      const sel = s.selected;
      const archive = sel.archive_count ? `（Archive ${num(sel.archive_count)} 件・${size(sel.archive_bytes)}）` : '';
      selLine.textContent = `選択 ${num(sel.count)} 件・${size(sel.bytes)}${archive}`;
      bottomSel.textContent = `選択 ${num(sel.count)} 件・${size(sel.bytes)}`;
      const filtered = s.filtered.count !== s.all.count ? `   絞り込み ${num(s.filtered.count)} 件・${size(s.filtered.bytes)}` : '';
      foundLine.textContent = `見つかった ${num(s.all.count)} 件・${size(s.all.bytes)}${filtered}`;
      // 「すべて選択」は表示中（絞り込みに一致する）の行が対象。「すべての選択を解除」は、絞り込みで隠れた行の選択も外す
      const filteredOn = s.filtered.count !== s.all.count;
      selAll.textContent = filteredOn ? `すべて選択（絞り込みに一致する ${num(s.filtered.count)} 件）` : `すべて選択（${num(s.all.count)} 件）`;
      selAll.title = '見えていない行も含めて、絞り込みに一致するすべての行を選びます（見出しのチェックボックスと同じ）';
      clearSel.title = '絞り込みで隠れている行も含めて、すべての選択を外します';
      const inFilter = sel.count - (s.hidden_selected || 0);
      headCheck.checked = s.filtered.count > 0 && inFilter >= s.filtered.count;
      headCheck.indeterminate = inFilter > 0 && inFilter < s.filtered.count;
      headCheck.title = headCheck.checked ? 'すべての選択を外す' : `絞り込みに一致する ${num(s.filtered.count)} 件をすべて選択`;
      clearSel.disabled = searching || sel.count === 0;
      selAll.disabled = searching || s.filtered.count === 0;
      jobBtn.disabled = sel.count === 0 || searching;
      jobBtn.dataset.hidden = String(s.hidden_selected);
      // 選択の行の文が長くなって折り返すと、ページが伸びるため、表の高さを合わせ直します
      table.fit();
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
