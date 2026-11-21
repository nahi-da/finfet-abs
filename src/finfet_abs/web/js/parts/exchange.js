// エクスポートのダイアログ（4.7.1）とインポート（3.5.4.2）。

import * as api from '../api.js';
import { button, h } from '../dom.js';
import { confirmDialog, infoDialog, openDialog } from './dialog.js';
import { checkbox, radios } from './form.js';
import { toast } from './toast.js';

const WARN = '認証情報は暗号化されずに、そのままファイルに書き込まれます。このファイルを持っている人は、ストレージアカウントにアクセスできます。ファイルの置き場所と渡す相手に注意し、不要になったら削除してください。';

/**
 * エクスポートのダイアログ。
 * @param {{history_ids?: string[], condition?: Object}} target 対象
 * @param {number} count 件数
 */
export async function openExport(target, count) {
  let includes = 'targets';
  const counts = h('div', { class: 'muted', hidden: true });
  const warn = h('p', { class: 'danger', text: WARN, hidden: true });
  const saveBtn = button('保存', () => save(), { primary: true });
  const r = radios('exp-includes', [['condition', '検索条件だけ'], ['targets', '検索条件と接続先'], ['credentials', '検索条件、接続先、認証情報']], includes, async (v) => {
    includes = v;
    const cred = v === 'credentials';
    warn.hidden = !cred;
    counts.hidden = !cred;
    saveBtn.textContent = cred ? '認証情報を含めて保存' : '保存';
    if (cred) {
      try {
        const c = await api.post('/api/v1/history/export/counts', target);
        counts.textContent = `含める認証情報 ${c.included} 件` + (c.not_included || c.entra ? `、含められないもの ${c.not_included + c.entra} 件（Entra ID ${c.entra} 件、復号できないもの ${c.not_included} 件）` : '');
      } catch { counts.textContent = ''; }
    }
  });
  r.style.flexDirection = 'column';
  r.style.alignItems = 'flex-start';
  const d = openDialog({
    title: 'エクスポート', width: 480,
    body: h('div', { class: 'form' }, h('div', { class: 'label', text: '対象' }), h('div', { class: 'control', text: `検索条件 ${count} 件` }),
      h('div', { class: 'label', text: '含める情報' }), h('div', { class: 'control' }, r, warn, counts)),
  });
  async function save() {
    try {
      const name = await api.downloadPost('/api/v1/history/export', { ...target, includes }, '検索条件.finfet-abs.json');
      d.close(true);
      toast(`${name} を保存しました`);
    } catch (e) {
      toast(e.message, { error: true });
    }
  }
  d.setFoot(h('span.push'), button('キャンセル', () => d.close(true)), saveBtn);
}

/**
 * インポート（ファイルを選んで、内容を確認します）。
 * @param {{allowLoad?: boolean}} [opt] 検索のページに読み込めるか
 * @returns {Promise<{condition?: Object, added?: number}|null>}
 */
export function openImport(opt = {}) {
  return new Promise((resolve) => {
    const input = h('input', { type: 'file', accept: '.json,application/json', style: { display: 'none' } });
    document.body.append(input);
    input.addEventListener('change', async () => {
      const file = input.files[0];
      input.remove();
      if (!file) return resolve(null);
      if (file.size > 1024 * 1024) {
        toast('ファイルが大きすぎます（1 MB まで）。', { error: true });
        return resolve(null);
      }
      const buf = new Uint8Array(await file.arrayBuffer());
      let bin = '';
      for (let i = 0; i < buf.length; i += 0x8000) bin += String.fromCharCode(...buf.subarray(i, i + 0x8000));
      let pv;
      try {
        pv = await api.post('/api/v1/history/import/preview', { content_base64: btoa(bin) });
      } catch (e) {
        toast(e.message, { error: true });
        return resolve(null);
      }
      resolve(await previewDialog(pv, opt));
    });
    input.click();
  });
}

function previewDialog(pv, opt) {
  return new Promise((resolve) => {
    let result = null;
    let saveCreds = true;
    const items = h('ul', { class: 'list-plain' }, pv.items.map((it) => h('li', null,
      it.error && !it.path_pattern ? h('span', { class: 'danger', text: `${it.index + 1}: ${it.error}` })
        : h('div', null, h('div', { class: 'mono ellipsis', text: it.path_pattern }),
          h('div', { class: 'muted', text: [it.name, it.targets_summary, it.variables_summary].filter(Boolean).join('   ') }),
          it.error ? h('div', { class: 'danger', text: it.error }) : null))));
    const body = [h('p', { text: `検索条件 ${pv.items.length} 件` }), items];
    for (const w of pv.warnings) body.push(h('p', { class: 'warn', text: w }));
    if (pv.credentials) {
      body.push(h('p', { class: 'warn', text: `このファイルには認証情報が含まれています（${pv.credentials} 件）。` }),
        checkbox('認証情報を保存する', true, (on) => { saveCreds = on; }));
    }
    const d = openDialog({ title: '検索条件を読み込む', width: 720, body, onClose: () => resolve(result) });
    const commit = async (mode, decisions = {}) => {
      try {
        let res = await api.post('/api/v1/history/import/commit', { token: pv.token, mode, save_credentials: saveCreds, decisions });
        if (res && res.conflicts) {
          for (const c of res.conflicts) {
            const ok = await confirmDialog({
              title: '保存した認証情報を置き換えますか',
              body: `${c.storage_account}${c.scope_container ? ` / ${c.scope_container}` : ''} の認証情報が保存されています。ファイルの認証情報で置き換えます。`,
              okText: '置き換える', cancelText: '置き換えない',
            });
            decisions[c.key] = ok;
          }
          res = await api.post('/api/v1/history/import/commit', { token: pv.token, mode, save_credentials: saveCreds, decisions });
        }
        result = res;
        d.close(true);
        if (res.credential_errors && res.credential_errors.length) {
          await infoDialog({ title: '保存できなかった認証情報', body: h('ul', null, res.credential_errors.map((x) => h('li', { text: x }))) });
        }
        if (res.had_credentials) {
          await infoDialog({ title: '検索条件を読み込みました', body: '取り込んだファイルには認証情報が平文で含まれています。不要であれば削除してください。' });
        } else if (mode === 'history') {
          toast(`検索履歴に追加しました（${res.added} 件）`, { href: '/history', linkText: '検索履歴を開く' });
        }
      } catch (e) {
        toast(e.message, { error: true });
      }
    };
    const foot = [h('span.push'), button('キャンセル', () => d.close(true)), button('検索履歴に追加', () => commit('history'), { primary: !opt.allowLoad || pv.items.length !== 1 })];
    if (opt.allowLoad && pv.items.length === 1) foot.push(button('検索のページに読み込む', () => commit('load'), { primary: true }));
    d.setFoot(...foot);
  });
}
