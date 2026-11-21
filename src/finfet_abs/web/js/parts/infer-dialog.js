// Blob 名からパスパターンを作るダイアログ（依頼者の依頼による機能。decisions.md）。
// 接続先の Blob 名を集め（範囲を選べます）、パスパターンの候補を件数の多い順に示します。選んだ候補を返します。

import * as api from '../api.js';
import { button, h, linkButton } from '../dom.js';
import { on } from '../events.js';
import { dateShort, num, size } from '../format.js';
import { runTask } from '../task.js';
import { openDialog } from './dialog.js';
import { formGrid, radios, radioValue, row, select, textInput } from './form.js';
import { toastError } from './toast.js';

const MODES = [['sample', '階層をたどって見本を集める'], ['first', '先頭から指定した件数'], ['all', 'すべての Blob']];

/**
 * @param {{targets: Array<Object>}} opt 検索のページの接続先（コンテナーが決まっているもの）
 * @returns {Promise<{path_pattern: string, target: Object}|null>}
 */
export function openInferDialog(opt) {
  return new Promise((resolve) => {
    const targets = (opt.targets || []).filter((t) => t.storage_account && t.container);
    let taskId = null;
    let result = null;
    let closed = false;
    const offs = [];
    const d = openDialog({
      title: 'パスパターンを自動生成', width: 960,
      onClose: () => { closed = true; offs.forEach((f) => f()); if (taskId) api.post(`/api/v1/pattern/infer/${taskId}/cancel`, {}).catch(() => {}); resolve(result); },
    });
    if (!targets.length) {
      d.body.append(h('p', { text: '先に、検索のページで接続先（コンテナー）を選んでください。' }));
      d.setFoot(h('span.push'), button('閉じる', () => d.close(true)));
      return;
    }
    const tSel = select(targets.map((t, i) => [String(i), `${t.storage_account} / ${t.container}`]), '0', { 'aria-label': '接続先' });
    const modeR = radios('infer-mode', MODES, 'sample', () => { limitIn.disabled = radioValue(modeR) !== 'first'; });
    const limitIn = textInput('10000', { width: 'w-num', inputmode: 'numeric', 'aria-label': '件数', disabled: true });
    const prefixIn = textInput('', { width: 'w-full', placeholder: '例: logs/2026/', 'aria-label': 'prefix' });
    const status = h('span', { class: 'muted' });
    const cacheInfo = h('span', { class: 'line' });
    const list = h('div');
    d.body.append(formGrid(...row('接続先', tSel), ...row('範囲', h('div', null, modeR, h('div', { class: 'line' }, '件数', limitIn))),
      ...row('prefix', prefixIn, { optional: true })), h('div', { class: 'line infer-status' }, status, cacheInfo), list);

    const runBtn = button('実行', () => run(false), { primary: true });
    const stopBtn = button('中止', () => { if (taskId) api.post(`/api/v1/pattern/infer/${taskId}/cancel`, {}).catch(() => {}); });
    stopBtn.hidden = true;
    d.setFoot(h('span.push'), stopBtn, button('閉じる', () => d.close(true)), runBtn);

    offs.push(on('infer.progress', (p) => {
      if (p.id === taskId) status.textContent = `${num(p.names)} 件の名前を取得しました（要求 ${num(p.requests)} 回）`;
    }));

    async function run(refresh) {
      const t = targets[Number(tSel.value)];
      const mode = radioValue(modeR);
      list.replaceChildren();
      cacheInfo.replaceChildren();
      status.textContent = '取得しています…';
      runBtn.disabled = true;
      stopBtn.hidden = false;
      try {
        // 結果の待ち合わせは runTask（処理がすぐ終わって、待ち受けより先に task.done が届いても取りこぼさない）
        const r = await runTask('POST', '/api/v1/pattern/infer', {
          target: { storage_account: t.storage_account, endpoint_suffix: t.endpoint_suffix, container: t.container, auth_kind: t.auth_kind,
            tenant: t.tenant || null, credential_id: t.credential_id || null },
          mode, prefix: prefixIn.value.trim(), limit: Number(limitIn.value) || 10000, refresh: !!refresh,
        }, (id) => { taskId = id; });
        taskId = null;
        if (closed) return;
        draw(r, t);
      } catch (e) {
        taskId = null;
        status.textContent = '';
        if (e.code !== 'CANCELLED') toastError(e);
        else status.textContent = '中止しました。';
      } finally {
        runBtn.disabled = false;
        stopBtn.hidden = true;
      }
    }

    function draw(r, t) {
      status.textContent = `${num(r.names)} 件の名前から、${num(r.candidates.length)} 個の候補を作りました（要求 ${num(r.requests)} 回）`
        + (r.truncated ? '。上限で打ち切ったため、すべての Blob ではありません' : '')
        + (!r.truncated && !r.complete ? '。見本のため、件数は見本の数です' : '');
      if (r.from_cache) {
        // 一度集めた名前は 60 分の間保持し、取得し直さずに使います（依頼者の依頼）
        cacheInfo.append(h('span', { class: 'muted', text: `${dateShort(r.fetched_at)} に取得した名前を使いました。` }),
          linkButton('取得し直す', () => run(true)));
      }
      if (!r.candidates.length) {
        list.append(h('p', { text: 'Blob がありません。' }));
        return;
      }
      const table = h('table', { class: 'infer-table' }, h('thead', null, h('tr', null, ...['パスパターン', '件数', r.sizes_known ? 'サイズ' : null, '日時の範囲', ''].filter((x) => x !== null).map((x) => h('th', { text: x })))));
      const tb = h('tbody');
      for (const c of r.candidates) {
        const range = c.first ? `${dateShort(c.first)} 〜 ${dateShort(c.last)}` : '';
        // 選択肢の変数の値（先頭の 3 個）を、パスパターンの下に示します
        const choices = Object.entries(c.variables || {}).filter(([, v]) => v.type === 'choice').map(([k, v]) => {
          const o = v.value.options;
          return `${k}: ${o.slice(0, 3).join('、')}${o.length > 3 ? ` ほか ${num(o.length - 3)} 個` : ''}`;
        });
        tb.append(h('tr', { title: c.examples.join('\n') },
          h('td', null, h('div', { class: 'mono', text: c.pattern }), choices.length ? h('div', { class: 'muted', text: `選択肢　${choices.join('　')}` }) : null),
          h('td', { class: 'num', text: num(c.count) }),
          r.sizes_known ? h('td', { class: 'num', text: size(c.bytes) }) : null,
          h('td', { class: 'nowrap', text: range }),
          h('td', null, button('使う', () => {
            result = { path_pattern: c.pattern, target: t, variables: c.variables || {}, complete: !!r.complete,
              found: c.first ? { first: c.first, last: c.last, complete: !!r.complete } : null };
            d.close(true);
          }))));
      }
      table.append(tb);
      list.append(table);
    }
  });
}
