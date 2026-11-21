// 検索のページの「URL で指定」のタブ（4.4.6、3.2.9）。

import * as api from '../api.js';
import { button, clear, debounce, h, linkButton } from '../dom.js';
import { dateShort, num } from '../format.js';
import { select } from '../parts/form.js';
import { openAddTargetDialog } from '../parts/target-dialog.js';
import { VirtualTable } from '../parts/table.js';
import { toast } from '../parts/toast.js';

let saved = { text: '', auth: {} };

export async function renderUrlsTab(ctx) {
  const { root } = ctx;
  const ta = h('textarea', { class: 'input mono w-full', rows: '6', spellcheck: 'false', 'aria-label': 'Blob の URL（1行に1つ）', placeholder: 'https://stexample01.blob.core.windows.net/logs/app/2026/11/20/web.log?sv=…&sig=…' });
  ta.value = saved.text;
  const info = h('div', { class: 'muted' });
  const authSec = h('div');
  let parsed = null;
  let rows = [];

  const table = new VirtualTable({
    id: 'urls',
    bottomReserve: 72,
    minHeight: 200,
    columns: [
      { key: 'line', label: '行', width: 56, align: 'right', fixed: true },
      { key: 'storage_account', label: 'ストレージアカウント', width: 170 },
      { key: 'container', label: 'コンテナー', width: 150 },
      { key: 'blob_name', label: 'Blob 名', width: 320, flex: true, mono: true },
      { key: 'sas', label: 'SAS', width: 60, render: (r) => (r.sas ? 'あり' : r.error ? '' : 'なし') },
      { key: 'perm', label: '権限', width: 80, render: (r) => (r.sas ? (r.sas.permission_labels.join('・') || '—') : '') },
      { key: 'exp', label: '期限', width: 110, render: (r) => (r.sas && r.sas.expiry ? dateShort(r.sas.expiry) : '') },
      { key: 'error', label: '誤り', width: 240, render: (r) => (r.error ? h('span', { class: 'danger', text: r.error }) : r.duplicate ? h('span', { class: 'muted', text: '重複（1件にまとめます）' }) : '') },
    ],
    fetch: async (offset, limit) => ({ total: rows.length, items: rows.slice(offset, offset + limit) }),
    empty: 'URL を貼り付けてください。',
  });

  const parse = async () => {
    saved.text = ta.value;
    if (!ta.value.trim()) {
      parsed = null;
      rows = [];
      clear(authSec);
      info.textContent = '';
      await table.reload();
      return;
    }
    try {
      parsed = await api.post('/api/v1/urls/parse', { text: ta.value });
    } catch (e) {
      info.textContent = e.message;
      return;
    }
    rows = parsed.rows;
    info.textContent = `${num(parsed.count)} 件` + (parsed.errors ? `・誤り ${num(parsed.errors)} 件（除いて追加します）` : '') + (parsed.duplicates ? `・重複 ${num(parsed.duplicates)} 件` : '');
    drawAuth();
    await table.reload(false);
  };

  function drawAuth() {
    clear(authSec);
    if (!parsed || !parsed.accounts_without_sas.length) return;
    authSec.append(h('h2', { text: '認証' }));
    for (const a of parsed.accounts_without_sas) {
      const key = `${a.storage_account}|${a.endpoint_suffix}`;
      const opts = [];
      if (parsed.signed_in) opts.push(['entra_user', 'Entra ID（ユーザーアカウント）'], ['entra_key', 'アクセスキー（Entra ID で取得）']);
      for (const c of a.credentials) opts.push([`cred:${c.id}`, `保存した認証情報（${c.scope_container || 'ストレージアカウント全体'}・${c.auth_label}）`]);
      const line = h('div', { class: 'line' }, h('span', { class: 'mono', text: a.storage_account }), a.endpoint_suffix !== 'core.windows.net' ? h('span', { class: 'muted', text: a.endpoint_suffix }) : null);
      if (opts.length) {
        if (!saved.auth[key]) saved.auth[key] = opts.find((o) => o[0].startsWith('cred:'))?.[0] || opts[0][0];
        line.append(select(opts, saved.auth[key], { 'aria-label': `${a.storage_account} の認証`, onchange: (e) => { saved.auth[key] = e.target.value; } }));
      } else {
        line.append(h('span', { class: 'warn', text: '使える認証がありません。' }), linkButton('接続先を追加', async () => { await openAddTargetDialog(); parse(); }));
      }
      authSec.append(line);
    }
  }

  ta.addEventListener('input', debounce(parse, 400));
  const createBtn = button('ジョブを作成', async () => {
    if (!parsed || !parsed.count) {
      toast('URL を貼り付けてください。', { error: true });
      return;
    }
    try {
      const m = await import('../parts/job-dialog.js');
      m.openCreateJobDialog({ source: 'urls', token: parsed.token, auth: saved.auth });
    } catch {
      toast('ジョブの作成は、この版ではまだ使えません。', { error: true });
    }
  }, { primary: true });
  root.append(h('section', { class: 'part' }, ta, info), authSec, h('section', { class: 'part' }, table.el), h('div', { class: 'sticky-bottom' }, h('span.push'), createBtn));
  ctx.onLeave(() => table.destroy());
  await parse();
  table.fit();
}
