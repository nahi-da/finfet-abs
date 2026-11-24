// Blob 名からパスパターンを作るダイアログ（依頼者の依頼による機能。decisions.md）。
// 接続先の Blob 名を集め（範囲を選べます）、パスパターンの候補を件数の多い順に示します。選んだ候補を返します。

import * as api from '../api.js';
import { button, h, linkButton } from '../dom.js';
import { on } from '../events.js';
import { dateShort, num, size } from '../format.js';
import { runTask } from '../task.js';
import { openDialog } from './dialog.js';
import { choiceCards, formGrid, radioValue, row, select, textInput } from './form.js';
import { toastError } from './toast.js';


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
      d.body.append(h('p', { text: '先に、検索ページで接続先（コンテナー）を選択してください。' }));
      d.setFoot(h('span.push'), button('閉じる', () => d.close(true)));
      return;
    }
    const tSel = select(targets.map((t, i) => [String(i), `${t.storage_account} / ${t.container}`]), '0', { 'aria-label': '接続先' });
    // 範囲（v1.6.0: フルスキャンとクイックスキャンの 2 つ。「先頭から指定した件数」は画面から外しました）
    const modeR = choiceCards('infer-mode', [
      { value: 'all', label: 'フルスキャン', tag: 'おすすめ',
        desc: 'すべての Blob 名を取得します。パスパターンを正確に生成できます。Blob の件数が多い場合は時間がかかりますが、取得の途中で [ここまでで生成] を押すと、それまでに取得した Blob 名から生成できます。' },
      { value: 'sample', label: 'クイックスキャン',
        desc: '階層ごとに一部の Blob のみを取得します。コンテナー内の Blob が数十万件を超える場合でも、処理時間を短縮できます。ただし、すべての Blob を確認するわけではないため、パスパターンが正確に生成できない場合があります。' },
    ], 'all');
    const prefixIn = textInput('', { width: 'w-full', placeholder: '例: logs/2026/', 'aria-label': 'prefix' });
    const status = h('span', { class: 'muted' });
    const cacheInfo = h('span', { class: 'line' });
    // フルスキャンで 100,000 件を取得して、まだ続きがあるときの確認（v1.6.0）
    const pausePanel = h('div', { class: 'notice infer-pause', hidden: true });
    const list = h('div');
    d.body.append(formGrid(...row('接続先', tSel), ...row('範囲', modeR),
      ...row('prefix', prefixIn, { optional: true })), h('div', { class: 'line infer-status' }, status, cacheInfo), pausePanel, list);

    const runBtn = button('実行', () => run(false), { primary: true });
    const stopBtn = button('中止', () => { if (taskId) api.post(`/api/v1/pattern/infer/${taskId}/cancel`, {}).catch(() => {}); });
    const decide = (action) => {
      pausePanel.hidden = true;
      if (taskId) api.post(`/api/v1/pattern/infer/${taskId}/decide`, { action }).catch(() => {});
      if (action !== 'continue') status.textContent = action === 'stop' ? '取得した Blob 名から生成しています…' : 'クイックスキャンに切り替えています…';
    };
    const hereBtn = button('ここまでで生成', () => decide('stop'));
    stopBtn.hidden = true;
    hereBtn.hidden = true;
    d.setFoot(h('span.push'), stopBtn, hereBtn, button('閉じる', () => d.close(true)), runBtn);

    const minutes = (sec) => (sec < 60 ? `${Math.max(1, Math.round(sec))} 秒` : `${num(Math.round(sec / 60))} 分`);
    offs.push(on('infer.progress', (p) => {
      if (p.id !== taskId) return;
      if (p.paused) {
        status.textContent = '';
        pausePanel.replaceChildren(
          h('p', { text: `${num(p.names)} 件の Blob 名を取得しました（${minutes(p.elapsed_s)}）。まだ取得していない Blob があります。` }),
          p.estimate_1m_s ? h('p', { text: `現在の速さ（1 秒あたり約 ${num(p.rate)} 件）では、Blob が 100 万件ある場合、すべて取得するまでに約 ${minutes(p.estimate_1m_s)} かかります。` }) : null,
          h('p', { text: '取得を続けるか、取得した Blob 名から生成するか、クイックスキャンに切り替えるかを選んでください。' }),
          h('div', { class: 'line' }, button('続行', () => decide('continue'), { primary: true }), button('ここまでで生成', () => decide('stop')),
            button('クイックスキャンに切り替える', () => decide('quick'))));
        pausePanel.hidden = false;
        return;
      }
      status.textContent = `${num(p.names)} 件の Blob 名を取得しました（要求 ${num(p.requests)} 回）`;
    }));

    async function run(refresh) {
      const t = targets[Number(tSel.value)];
      const mode = radioValue(modeR);
      list.replaceChildren();
      cacheInfo.replaceChildren();
      status.textContent = '取得しています…';
      runBtn.disabled = true;
      stopBtn.hidden = false;
      hereBtn.hidden = mode !== 'all';
      pausePanel.hidden = true;
      try {
        // 結果の待ち合わせは runTask（処理がすぐ終わって、待ち受けより先に task.done が届いても取りこぼさない）
        const r = await runTask('POST', '/api/v1/pattern/infer', {
          target: { storage_account: t.storage_account, endpoint_suffix: t.endpoint_suffix, container: t.container, auth_kind: t.auth_kind,
            tenant: t.tenant || null, credential_id: t.credential_id || null, account: t.account || null },
          mode, prefix: prefixIn.value.trim(), refresh: !!refresh,
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
        hereBtn.hidden = true;
        pausePanel.hidden = true;
      }
    }

    function draw(r, t) {
      status.textContent = `${num(r.names)} 件の Blob 名から、${num(r.candidates.length)} 件の候補を生成しました（要求 ${num(r.requests)} 回）`
        + (r.stopped ? '。取得を途中で止めたため、取得した Blob 名のみを対象にしています。件数は取得した Blob の数です'
          : r.switched ? '。クイックスキャンに切り替えたため、一部の Blob のみを対象にしています。件数は取得した Blob の数です'
            : r.truncated ? '。件数の上限に達したため、一部の Blob のみを対象にしています'
              : !r.complete ? '。一部の Blob のみを取得したため、件数は取得した Blob の数です' : '');
      if (r.from_cache) {
        // 一度集めた名前は 60 分の間保持し、取得し直さずに使います（依頼者の依頼）
        cacheInfo.append(h('span', { class: 'muted', text: `${dateShort(r.fetched_at)} に取得した Blob 名を使用しました。` }),
          linkButton('再取得', () => run(true)));
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
