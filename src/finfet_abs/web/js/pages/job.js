// ジョブの詳細のページ（4.6）: /jobs/<ID>（Blob）、/condition、/options、/log。

import * as api from '../api.js';
import { button, clear, copyText, debounce, h, iconButton, linkButton } from '../dom.js';
import { on } from '../events.js';
import { dateFull, dateShort, mbps, num, size, speed, timeShort } from '../format.js';
import { confirmDialog, infoDialog, openDialog } from '../parts/dialog.js';
import { pickFolder } from '../parts/folder.js';
import { checkbox, checkInput, formGrid, radios, radioValue, row, select, setError, textInput } from '../parts/form.js';
import { openMenu } from '../parts/menu.js';
import { VirtualTable } from '../parts/table.js';
import { toast, toastError } from '../parts/toast.js';
import { cancelJobs, renameJob } from './jobs.js';

const ITEM_STATES = [['', 'すべて'], ['pending', '待機中'], ['rehydrate_queued', 'リハイドレートの要求待ち'], ['rehydrating', 'リハイドレート中'],
  ['ready', 'ダウンロード待ち'], ['downloading', 'ダウンロード中'], ['completed', '完了'], ['skipped', 'スキップ'], ['error', 'エラー'], ['cancelled', '中止']];
const SUMMARY_STATES = ['completed', 'downloading', 'ready', 'rehydrating', 'rehydrate_queued', 'pending', 'skipped', 'error', 'cancelled'];
const ENDED = new Set(['completed', 'completed_with_errors', 'cancelled']);
const METHOD_LABELS = { rehydrate_download: 'リハイドレートしてダウンロード', rehydrate_only: 'リハイドレートのみ', download_only: 'ダウンロードのみ' };
const STRUCT_LABELS = { preserve: '階層を保つ', flatten: '階層を名前にする', name_only: 'ファイル名だけ', template: '指定する' };
const CONFLICT_LABELS = { number: '番号を付ける', skip: 'スキップ', overwrite: '上書き' };

export async function render(ctx) {
  const { root } = ctx;
  const id = ctx.params[0];
  const tab = ctx.params[1] || 'blobs';
  let job;
  try {
    job = await api.get(`/api/v1/jobs/${encodeURIComponent(id)}`);
  } catch (e) {
    root.append(h('h1', { text: 'ジョブ' }), h('p', null, e.message, ' ', h('a', { href: '/jobs', text: 'ジョブを開く' })));
    return;
  }
  ctx.setTitle(job.name);
  const head = h('div');
  const summary = h('div', { class: 'summary-line' });
  const speedLine = h('div', { class: 'summary-line' });
  root.append(h('div', { class: 'breadcrumb' }, h('a', { href: '/jobs', text: 'ジョブ' }), ' › ', h('span', { text: job.name })), head, summary, speedLine);
  const tabs = h('nav', { class: 'tabs', 'aria-label': 'ジョブの詳細' },
    ...[['blobs', 'Blob', ''], ['condition', '検索条件', '/condition'], ['options', '取得の設定', '/options'], ['log', 'ログ', '/log']]
      .map(([k, label, suffix]) => h('a', { href: `/jobs/${id}${suffix}`, 'aria-current': k === tab ? 'page' : null, text: label })));
  const body = h('div');
  root.append(tabs, body);

  // ---------------------------------------------------------------- 見出しの行
  async function action(name, extra = {}) {
    try {
      if (name === 'cancel') { await cancelJobs([id]); } else { await api.post(`/api/v1/jobs/${id}/actions/${name}`, extra); }
    } catch (e) { toastError(e); }
    refresh();
  }

  function drawHead() {
    clear(head);
    const btns = [];
    const st = job.status;
    if (st === 'held' || st === 'paused') btns.push(button('開始', () => action('start'), { primary: true }), button('中止', () => action('cancel')));
    else if (['queued', 'running', 'waiting_rehydrate'].includes(st)) btns.push(button('一時停止', () => action('pause')), button('中止', () => action('cancel')));
    else if (st === 'waiting_download') btns.push(button('ダウンロードを開始', () => startDownload(), { primary: true }), button('中止', () => action('cancel')));
    else if (ENDED.has(st)) {
      const resume = button(`再開（未完了 ${num(job.unfinished)} 件）`, () => action('resume'));
      resume.disabled = !job.unfinished;
      btns.push(resume, button('すべて再実行', () => rerun(null)));
      if (job.condition) btns.push(h('a', { href: `/search?from-job=${id}`, text: '同じ条件で検索' }));
    }
    const menu = iconButton('more', '操作', (e) => openMenu(e.currentTarget, [
      { text: '名前を変更', onClick: async () => { if (await renameJob(job)) refresh(); } },
      { text: 'CSV を出力', onClick: () => api.downloadGet(`/api/v1/jobs/${id}/items.csv`, 'job.csv') },
      { text: 'ログのフォルダーを開く', onClick: () => api.post('/api/v1/fs/open-folder', { path: job.log_dir }).catch((e) => toastError(e)) },
      ENDED.has(st) ? { text: '削除', onClick: () => remove() } : null,
    ].filter(Boolean)));
    const code = job.last_error ? job.last_error.code : null;
    // 認証が使えないときは、サインインか接続先の追加を求めます（3.1.9・3.3.9）
    const fix = code === 'NOT_SIGNED_IN' || code === 'INTERACTION_REQUIRED'
      ? linkButton('サインイン', () => import('../parts/signin.js').then((m) => m.startSignIn()))
      : code === 'NO_CREDENTIAL' ? h('a', { href: '/connections', text: '接続のページを開く' }) : null;
    const warn = job.last_error ? h('div', { class: 'line status-warn' }, h('span', { text: job.last_error.message }), fix) : null;
    head.append(...[h('div', { class: 'page-head' }, h('h1', { class: 'ellipsis', text: job.name, title: job.name }),
      h('span', { class: ENDED.has(st) || st === 'running' ? '' : 'status-warn', text: job.status_label }),
      h('div', { class: 'actions' }, ...btns, menu)), warn,
    job.source_job ? h('div', { class: 'muted' }, h('a', { href: `/jobs/${job.source_job.id}`, text: `「${job.source_job.name}」（${dateShort(job.source_job.created_at)}）` }), 'から作成') : null].filter(Boolean));
  }

  function drawSummary() {
    clear(summary);
    const c = job.counts || {};
    const by = c.by_status || {};
    summary.append(h('strong', { text: `${num(c.total || 0)} 件・${size(c.bytes || 0)}` }));
    for (const k of SUMMARY_STATES) {
      if (!by[k]) continue;
      const label = ITEM_STATES.find((x) => x[0] === k)[1];
      summary.append(linkButton(`${label} ${num(by[k][0])}`, () => { if (tab !== 'blobs') ctx.navigate(`/jobs/${id}?status=${k}`); else setStatus(k); }));
    }
  }

  const bwIn = textInput('', { width: 'w-num', inputmode: 'decimal', 'aria-label': '帯域の上限（MB/s）' });
  const bwNote = h('span', { class: 'muted' });
  const speedEl = h('span');
  bwIn.addEventListener('change', async () => {
    const v = Number(bwIn.value || 0);
    if (!(v >= 0)) { setError(bwIn, '0 以上の数値を入力してください。'); return; }
    setError(bwIn, null);
    await api.put('/api/v1/jobs/bandwidth', { mbps: v });
    bwNote.textContent = v ? `≒ ${mbps(v)}` : '（無制限）';
  });
  async function drawSpeed() {
    const show = job.status === 'running' || job.status === 'waiting_rehydrate';
    speedLine.hidden = !show;
    if (!show) return;
    const bw = await api.get('/api/v1/jobs/bandwidth');
    if (document.activeElement !== bwIn) bwIn.value = bw.mbps ? String(bw.mbps) : '0';
    bwNote.textContent = bw.mbps ? `≒ ${mbps(bw.mbps)}` : '（無制限）';
    speedEl.textContent = `速度 ${speed(bw.speed)}`;
    clear(speedLine);
    speedLine.append(speedEl, h('span', { text: '帯域の上限' }), bwIn, h('span', { text: 'MB/s' }), bwNote);
    if (job.next_check_at) speedLine.append(h('span.push'), h('span', { class: 'muted', text: `次の確認 ${timeShort(job.next_check_at)}` }), linkButton('今すぐ確認', () => action('check-now')));
  }

  // ---------------------------------------------------------------- タブ
  let setStatus = () => {};
  let reloadTab = async () => {};
  if (tab === 'blobs') blobTab();
  else if (tab === 'condition') conditionTab();
  else if (tab === 'options') optionsTab();
  else logTab();

  function blobTab() {
    const q = ctx.query;
    const f = { q: q.get('q') || '', status: q.get('status') || '', tier: q.get('tier') || '' };
    const selected = new Set();
    const kw = textInput(f.q, { width: 'w-short', placeholder: 'キーワード', 'aria-label': 'キーワード' });
    const stSel = select(ITEM_STATES, f.status, { 'aria-label': '状態' });
    const tierSel = select([['', 'すべて'], ['Hot', 'Hot'], ['Cool', 'Cool'], ['Cold', 'Cold'], ['Archive', 'Archive']], f.tier, { 'aria-label': '層' });
    const apply = () => { f.q = kw.value.trim(); f.status = stSel.value; f.tier = tierSel.value; ctx.updateQuery(f); table.reload(false); };
    kw.addEventListener('input', debounce(apply, 300));
    stSel.addEventListener('change', apply);
    tierSel.addEventListener('change', apply);
    setStatus = (k) => { stSel.value = k; apply(); };
    const bulk = async (act) => {
      const ids = [...selected];
      if (!ids.length) { toast('Blob を選んでください。'); return; }
      if (act === 'raise-priority') {
        const ok = await confirmDialog({ title: '優先度を高にしますか', body: '優先度「高」は料金が高くなります。保留中の要求の優先度は、上げることはできますが下げられません。', okText: '高にする' });
        if (!ok) return;
      }
      if (act === 'rerun-selected') { rerun(ids); return; }
      try {
        const r = await api.post(`/api/v1/jobs/${id}/items/actions/${act}`, { ids });
        if (r.errors && r.errors.length) toast(r.errors[0].message, { error: true });
      } catch (e) { toastError(e); }
      selected.clear();
      refresh();
    };
    body.append(h('div', { class: 'toolbar' }, kw, h('span', { text: '状態' }), stSel, h('span', { text: '層' }), tierSel,
      h('div', { class: 'push' }, linkButton('優先度を高にする', () => bulk('raise-priority')), linkButton('再試行', () => bulk('retry')),
        linkButton('再要求', () => bulk('re-request')), ENDED.has(job.status) ? linkButton('選択した Blob を再実行', () => bulk('rerun-selected')) : null)));
    const table = new VirtualTable({
      id: 'job-items',
      sort: q.get('sort') || 'name',
      onSort: (sort) => ctx.updateQuery({ sort }),
      columns: [
        { key: 'sel', label: '', width: 36, fixed: true, noResize: true, align: 'center', render: (r) => h('input', { type: 'checkbox', checked: selected.has(r.id), 'aria-label': '選択', onchange: (e) => { if (e.target.checked) selected.add(r.id); else selected.delete(r.id); } }) },
        { key: 'name', label: 'Blob 名', width: 280, flex: true, fixed: true, sortable: true, mono: true, render: (r) => h('span', { class: 'ellipsis', text: r.blob_name }), title: (r) => `${r.storage_account}/${r.container}/${r.blob_name}` },
        { key: 'status', label: '状態', width: 140, sortable: true, render: (r) => h('span', { class: r.status === 'error' ? 'status-danger' : '', text: r.status_label, title: r.status_reason || null }) },
        { key: 'tier', label: '層', width: 90, sortable: true, render: (r) => r.tier || '—' },
        { key: 'size', label: 'サイズ', width: 90, align: 'right', sortable: true, render: (r) => size(r.size || 0) },
        { key: 'progress', label: '進捗', width: 100, align: 'right', render: (r) => (r.status === 'downloading' && r.size ? `${Math.floor(((r.progress_bytes ?? r.bytes_downloaded) / r.size) * 100)}%` : '') },
        { key: 'rehydrate_requested', label: 'リハイドレートの要求', width: 130, hidden: true, sortable: true, render: (r) => (r.rehydrate_requested_at ? dateShort(r.rehydrate_requested_at) : (r.rehydrate_requested_known === 0 ? '不明' : '')) },
        { key: 'rehydrate_completed', label: 'リハイドレートの完了', width: 130, hidden: true, sortable: true, render: (r) => dateShort(r.rehydrate_completed_detected_at) },
        { key: 'download_started', label: 'ダウンロードの開始', width: 130, hidden: true, sortable: true, render: (r) => dateShort(r.download_started_at) },
        { key: 'download_completed', label: 'ダウンロードの完了', width: 130, hidden: true, sortable: true, render: (r) => dateShort(r.download_completed_at) },
        { key: 'path', label: '保存先', width: 240, hidden: true, sortable: true, render: (r) => h('span', { class: 'ellipsis mono', text: r.local_path || r.planned_path || '' }) },
        { key: 'error', label: 'エラー', width: 200, sortable: true, render: (r) => h('span', { class: 'ellipsis', text: r.error_message || '', title: r.error_message || null }) },
        { key: 'menu', label: '', width: 40, fixed: true, noResize: true, align: 'center', render: (r) => iconButton('more', '操作', (e) => openMenu(e.currentTarget, [
          r.status === 'completed' && r.local_path ? { text: 'ファイルを開く', onClick: () => itemAction(r, 'open-file') } : null,
          r.local_path ? { text: 'フォルダーを開く', onClick: () => itemAction(r, 'open-folder') } : null,
          { text: 'URL をコピー（SAS なし）', onClick: () => navigator.clipboard.writeText(r.url).then(() => toast('URL をコピーしました。')) },
          { text: '詳細', onClick: () => itemDetail(r) },
        ].filter(Boolean))) },
      ],
      rowId: (r) => r.id,
      fetch: (offset, limit, sort) => api.get(`/api/v1/jobs/${id}/items${api.qs({ offset, limit, sort, q: f.q, status: f.status, tier: f.tier })}`),
      empty: '一致する Blob はありません。',
    });
    body.append(table.el);
    ctx.onLeave(() => table.destroy());
    table.reload();
    reloadTab = () => table.refreshVisible ? table.refreshVisible() : table.reload();
  }

  async function itemAction(r, act) {
    try { await api.post(`/api/v1/jobs/${id}/items/${r.id}/actions/${act}`, {}); } catch (e) { toastError(e); }
  }

  // Blob ごとに記録する情報（3.3.14）。[キー, 表示名, 書式]
  const ITEM_GROUPS = [
    ['Blob の情報', [['storage_account', 'ストレージアカウント'], ['container', 'コンテナー'], ['blob_name', 'Blob 名', 'mono'],
      ['url', 'URL（SAS なし）', 'mono'], ['blob_type', '種類'], ['size', 'サイズ', 'size'], ['etag', 'ETag', 'mono'],
      ['content_md5', 'Content-MD5', 'mono'], ['creation_time', '作成日時', 'date'], ['last_modified', '最終更新日時', 'date'],
      ['tier_at_search', '検索時の層'], ['archive_status_at_search', '検索時のリハイドレートの状態'], ['tier', '現在の層'],
      ['access_tier_change_time', '層の変更日時', 'date']]],
    ['リハイドレート', [['rehydrate_requested_at', '要求日時', 'requested'], ['rehydrate_priority', '優先度', 'prio'],
      ['rehydrate_target_tier', '復元先の層'], ['rehydrate_priority_raised_at', '優先度を上げた日時', 'date'],
      ['next_check_at', '次の確認', 'date'], ['rehydrate_completed_detected_at', '完了を確認した日時', 'date'],
      ['rehydrate_completed_azure_at', '完了時の層の変更日時（Azure）', 'date']]],
    ['ダウンロード', [['download_started_at', '開始日時', 'date'], ['download_resumed_at', '最後に再開した日時', 'date'],
      ['download_completed_at', '完了日時', 'date'], ['planned_path', '保存するパス', 'mono'], ['local_path', '保存したファイル', 'mono'],
      ['bytes_downloaded', '取得したバイト数', 'num'], ['attempts', '試行回数', 'num'], ['md5_result', 'MD5 の照合', 'md5']]],
    ['結果', [['status', '状態', 'status'], ['status_reason', '理由'], ['error_code', 'エラーコード', 'mono'], ['error_message', 'エラーの内容']]],
    ['認証', [['auth_kind', '認証方式', 'auth'], ['credential_id', '保存した認証情報の ID', 'mono']]],
  ];
  const MD5_LABELS = { match: '一致', mismatch: '不一致', none: 'Content-MD5 なし', off: '照合しない' };
  const AUTH_KIND_LABELS = { entra_user: 'Entra ID（ユーザーアカウント）', entra_key: 'Entra ID（アクセスキーを取得）', account_key: 'アクセスキー', sas: 'SAS' };

  function fmtItem(kind, v, d) {
    if (kind === 'date') return dateFull(v);
    if (kind === 'size') return `${size(v)}（${num(v)} バイト）`;
    if (kind === 'num') return num(v);
    if (kind === 'prio') return v === 'High' ? '高' : '標準';
    if (kind === 'md5') return MD5_LABELS[v] || v;
    if (kind === 'auth') return AUTH_KIND_LABELS[v] || v;
    if (kind === 'status') return d.status_label || (ITEM_STATES.find((x) => x[0] === v) || [v, v])[1];
    if (kind === 'requested') return dateFull(v);
    return String(v);
  }

  async function itemDetail(r) {
    const d = { ...await api.get(`/api/v1/jobs/${id}/items/${r.id}`), status_label: r.status_label };
    const body = h('div');
    for (const [title, fields] of ITEM_GROUPS) {
      const kv = h('dl', { class: 'kv' });
      for (const [k, label, kind] of fields) {
        let v = d[k];
        if (k === 'rehydrate_requested_at' && !v && d.rehydrate_requested_known === 0) v = '不明（ジョブの開始前から要求済み）';
        if (v === null || v === undefined || v === '') continue;
        const text = k === 'rehydrate_requested_at' && d.rehydrate_requested_known === 0 && !d.rehydrate_requested_at ? v : fmtItem(kind, v, d);
        kv.append(h('dt', { class: 'muted', text: label }), h('dd', { class: kind === 'mono' ? 'mono' : '', text }));
      }
      if (kv.childNodes.length) body.append(h('h2', { text: title }), kv);
    }
    if (d.error_code) {
      const detail = [`FinFeT ${window.__finfetVersion || ''}`.trim(), `ジョブ: ${job.name}`, `Blob: ${d.storage_account}/${d.container}/${d.blob_name}`,
        `エラーコード: ${d.error_code}`, `内容: ${d.error_message || ''}`, `日時: ${d.updated_at || ''}`].join('\n');
      body.append(h('p', null, linkButton('詳細をコピー', async () => { if (await copyText(detail)) toast('詳細をコピーしました'); })));
    }
    infoDialog({ title: r.blob_name, body, width: 720 });
  }

  function conditionTab() {
    if (job.condition) {
      const c = job.condition;
      const kv = h('dl', { class: 'kv' });
      kv.append(h('dt', { class: 'muted', text: '接続先' }), h('dd', null, ...(c.targets || []).map((t) => h('div', { class: 'mono', text: `${t.storage_account}/${t.container}（${t.auth_kind || ''}）` }))),
        h('dt', { class: 'muted', text: 'パスパターン' }), h('dd', { class: 'mono', text: c.path_pattern }),
        h('dt', { class: 'muted', text: '変数' }), h('dd', { class: 'mono', text: JSON.stringify(c.variables || {}) }));
      body.append(kv, h('p', null, h('a', { href: `/search?from-job=${id}`, text: 'この条件で検索のページを開く' })));
    } else if (job.urls_summary) {
      body.append(h('p', { text: `URL で指定: ${num(job.urls_summary.count)} 件（${(job.urls_summary.accounts || []).join('、')}）` }));
    }
  }

  function optionsTab() {
    const o = job.options;
    const editable = ['held', 'queued', 'paused', 'waiting_download'].includes(job.status);
    const methodR = radios('opt-method', Object.entries(METHOD_LABELS), o.method, () => {});
    const tierSel = select([['Hot', 'Hot'], ['Cool', 'Cool'], ['Cold', 'Cold']], o.target_tier, { 'aria-label': '復元先の層', disabled: !editable });
    const prioSel = select([['Standard', '標準'], ['High', '高']], o.priority, { 'aria-label': '優先度', disabled: !editable });
    const conflictSel = select(Object.entries(CONFLICT_LABELS), o.conflict, { 'aria-label': '同名のファイル', disabled: !editable });
    const nums = {};
    const numIn = (k, label) => { nums[k] = textInput(String(o[k]), { width: 'w-num', inputmode: 'numeric', 'aria-label': label, disabled: !editable }); return nums[k]; };
    if (!editable) methodR.querySelectorAll('input').forEach((x) => { x.disabled = true; });
    const conns = h('span', { class: 'muted', text: `同時に開く接続: 最大 ${num(8)}（ダウンロード ${o.download_concurrency} × ${o.connections_per_blob} = ${o.download_concurrency * o.connections_per_blob}、ほか ${Math.max(0, 8 - o.download_concurrency * o.connections_per_blob)}）` });
    const err = h('div', { class: 'field-error', hidden: true });
    body.append(formGrid(
      ...row('取得の方法', methodR), ...row('復元先の層', h('div', { class: 'line' }, tierSel, h('span', { text: '優先度' }), prioSel)),
      ...row('保存先', h('span', { class: 'mono', text: job.job_folder || o.directory })),
      ...row('パス構造', h('span', { text: STRUCT_LABELS[o.path_structure] + (o.path_structure === 'template' ? `（${o.path_template}）` : '') })),
      ...row('同名のファイル', conflictSel),
      ...row('', h('div', { class: 'line' }, checkbox('最終更新日時をファイルに設定する', o.set_mtime, null, { disabled: !editable }), checkbox('MD5 を照合する', o.verify_md5, null, { disabled: !editable }))),
      ...row('状態確認の並列数', numIn('check_concurrency', '状態確認の並列数')),
      ...row('リハイドレートの要求の並列数', numIn('rehydrate_concurrency', 'リハイドレートの要求の並列数')),
      ...row('ダウンロードの同時実行数', numIn('download_concurrency', 'ダウンロードの同時実行数')),
      ...row('1 Blob あたりの接続数', h('div', { class: 'line' }, numIn('connections_per_blob', '1 Blob あたりの接続数'), conns)),
      ...row('チャンクの大きさ（MiB）', numIn('chunk_size_mib', 'チャンクの大きさ')),
    ), err);
    if (editable) {
      body.append(h('div', { class: 'toolbar' }, button('保存', async () => {
        const changes = { method: radioValue(methodR), target_tier: tierSel.value, priority: prioSel.value, conflict: conflictSel.value };
        for (const [k, el] of Object.entries(nums)) changes[k] = Number(el.value);
        try {
          await api.patch(`/api/v1/jobs/${id}`, { options: changes });
          err.hidden = true;
          toast('取得の設定を保存しました。');
          refresh();
        } catch (e) { err.hidden = false; err.textContent = e.message; }
      }, { primary: true })));
    } else {
      body.append(h('p', { class: 'muted', text: '実行中のジョブでは、帯域の上限だけを変更できます（上の速度の行）。' }));
    }
  }

  function logTab() {
    const blobIn = textInput(ctx.query.get('blob') || '', { width: 'w-mid', placeholder: 'Blob 名', 'aria-label': 'Blob 名' });
    const lvSel = select([['', 'すべて'], ['INFO', 'INFO'], ['WARN', 'WARN'], ['ERROR', 'ERROR']], ctx.query.get('level') || '', { 'aria-label': 'レベル' });
    const pre = h('div', { class: 'pre log-view', tabindex: '0' });
    let follow = true;
    pre.addEventListener('scroll', () => { follow = pre.scrollTop + pre.clientHeight >= pre.scrollHeight - 8; });
    const load = async () => {
      const r = await api.get(`/api/v1/jobs/${id}/log${api.qs({ blob: blobIn.value.trim(), level: lvSel.value })}`);
      pre.textContent = r.lines.map((x) => `${dateFull(x.at)}  ${x.level.padEnd(5)}  ${x.blob ? `${x.blob}  ` : ''}${x.message}`).join('\n');
      if (follow) pre.scrollTop = pre.scrollHeight;
    };
    blobIn.addEventListener('input', debounce(load, 300));
    lvSel.addEventListener('change', load);
    body.append(h('div', { class: 'toolbar' }, blobIn, h('span', { text: 'レベル' }), lvSel,
      h('div', { class: 'push' }, linkButton('ログをコピー', () => navigator.clipboard.writeText(pre.textContent).then(() => toast('ログをコピーしました。'))))), pre);
    load();
    reloadTab = load;
  }

  // ---------------------------------------------------------------- ダイアログ
  async function startDownload() {
    const o = job.options;
    const rem = (job.counts.by_status || {}).rehydrating;
    const dirIn = textInput(o.directory, { width: 'w-long', 'aria-label': '保存先' });
    const conflictSel = select(Object.entries(CONFLICT_LABELS), o.conflict, { 'aria-label': '同名のファイル' });
    const mode = radios('dl-mode', [['all', '完了した Blob からダウンロードし、残りは完了しだいダウンロードする'], ['completed_only', '完了した Blob だけをダウンロードする']], 'all', () => {});
    const d = openDialog({
      title: 'ダウンロードを開始', width: 720,
      body: formGrid(...row('保存先', h('div', { class: 'line' }, dirIn, button('選択', async () => { const r = await pickFolder({ initial: dirIn.value }); if (r) dirIn.value = r.path; }))),
        ...row('同名のファイル', conflictSel), ...(rem ? row('リハイドレート中', h('div', null, h('p', { text: `まだリハイドレート中の Blob が ${num(rem[0])} 件あります。` }), mode)) : [])),
    });
    d.setFoot(h('span.push'), button('キャンセル', () => d.close(true)), button('ダウンロードを開始', async () => {
      try {
        await api.post(`/api/v1/jobs/${id}/actions/start-download`, { mode: rem ? radioValue(mode) : 'completed_only', options: { directory: dirIn.value.trim(), conflict: conflictSel.value } });
        d.close(true);
        refresh();
      } catch (e) { toastError(e); }
    }, { primary: true }));
  }

  async function rerun(itemIds) {
    const m = await import('../parts/job-dialog.js');
    const url = itemIds ? `/api/v1/jobs/${id}/items/actions/rerun-selected` : `/api/v1/jobs/${id}/actions/rerun-all`;
    const newId = await m.openCreateJobDialog({
      source: 'job', jobId: id, itemIds, title: itemIds ? '選択した Blob を再実行' : 'すべて再実行', initial: job.options,
      submitText: '再実行', doneText: '新しいジョブをキューに追加しました。',
      submit: async (b) => (await api.post(url, { ...b, ids: itemIds || undefined })).id,
    });
    if (newId) ctx.navigate(`/jobs/${newId}`);
  }

  async function remove() {
    const parts = checkbox('保存中のファイル（.part）も削除する', false);
    const ok = await confirmDialog({ title: 'ジョブの記録を削除しますか', body: [h('p', { text: 'ダウンロードしたファイルは削除しません。削除した記録は元に戻せません。' }), parts], okText: '削除する', danger: true });
    if (!ok) return;
    await api.post(`/api/v1/jobs/${id}/actions/delete`, { remove_parts: checkInput(parts).checked });
    ctx.navigate('/jobs/ended');
  }

  // ---------------------------------------------------------------- 更新
  async function refresh() {
    try { job = await api.get(`/api/v1/jobs/${id}`); } catch { return; }
    drawHead();
    drawSummary();
    drawSpeed();
    reloadTab();
  }
  const refreshSoon = debounce(refresh, 500);
  ctx.onLeave(on('job.changed', (d) => { if (d.id === id) refreshSoon(); }));
  ctx.onLeave(on('job.progress', (d) => { if (d.id === id) refreshSoon(); }));
  ctx.onLeave(on('rehydrate.done', (d) => { if ((d.job_ids || []).includes(id)) refreshSoon(); }));
  const timer = setInterval(() => { if (job.status === 'running') drawSpeed(); }, 2000);
  ctx.onLeave(() => clearInterval(timer));
  drawHead();
  drawSummary();
  drawSpeed();
}
