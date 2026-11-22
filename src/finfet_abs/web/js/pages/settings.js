// 設定のページ（4.9）。

import * as api from '../api.js';
import { button, h, linkButton } from '../dom.js';
import { mbps } from '../format.js';
import { confirmDialog } from '../parts/dialog.js';
import { pickFolder } from '../parts/folder.js';
import { checkbox, focusFirstError, formGrid, row, select, setError, showRow, textInput } from '../parts/form.js';
import { toast, toastError } from '../parts/toast.js';

const TZ = [['local', 'ローカル時刻'], ['utc', 'UTC']];

/**
 * 節の定義。項目は [キー, ラベル, 種類, 追加の指定]。
 * 種類: num（数値）、check（チェックボックス）、select、text、dir（フォルダー）、radios、password
 */
function sections(session) {
  return [
    {
      id: 'search', title: '検索', items: [
        ['search.max_results', '上限件数', 'num', { unit: '件' }],
        ['search.max_prefixes', 'prefix の上限', 'num'],
        ['search.merge_trial_pages', 'まとめて取得を試すページ数', 'num'],
        ['search.list_concurrency', '一覧の取得の並列数', 'num'],
        ['search.input_timezone', '期間の入力の既定', 'select', { options: TZ }],
        ['search.exclude_adls_directories', 'Data Lake のディレクトリを除く', 'check'],
        ['search.keep_recent', '検索結果を残す数', 'num', { unit: '件' }],
      ],
    },
    {
      id: 'fetch', title: '取得', items: [
        ['fetch.directory', '保存先', 'dir'],
        ['fetch.job_folder', 'ジョブのフォルダー', 'check', { text: '保存先の下にジョブごとのフォルダーを作る' }],
        ['fetch.path_structure', 'パス構造', 'select', { options: [['preserve', '階層を保つ'], ['flatten', '階層を名前にする'], ['name_only', 'ファイル名だけ'], ['template', '指定する']] }],
        ['fetch.conflict', '同名のファイル', 'select', { options: [['number', '番号を付ける'], ['skip', 'スキップ'], ['overwrite', '上書き']] }],
        ['fetch.set_mtime', '最終更新日時', 'check', { text: 'ファイルの更新日時を Blob の最終更新日時にする' }],
        ['fetch.verify_md5', 'MD5 の照合', 'check', { text: 'Content-MD5 がある Blob を、ダウンロード後に照合する' }],
        ['fetch.reuse_search_info_minutes', '検索結果を確認の代わりに使う時間', 'num', { unit: '分' }],
        ['queue.resume_on_startup', '起動時に実行中だったジョブを再開する', 'check'],
      ],
    },
    {
      id: 'rehydrate', title: 'リハイドレート', items: [
        ['rehydrate.target_tier', '復元先の層', 'select', { options: [['Hot', 'Hot'], ['Cool', 'Cool'], ['Cold', 'Cold']] }],
        ['rehydrate.priority', '優先度', 'select', { options: [['Standard', '標準'], ['High', '高']] }],
        [['rehydrate.poll_first_minutes.high', 'rehydrate.poll_interval_minutes.high'], '完了の確認の間隔（高）', 'poll'],
        [['rehydrate.poll_first_minutes.standard', 'rehydrate.poll_interval_minutes.standard'], '完了の確認の間隔（標準）', 'poll'],
        ['rehydrate.check_on_startup', '起動時にリハイドレート待ちのジョブを確認する', 'check'],
      ],
    },
    {
      id: 'load', title: '負荷', items: [
        ['queue.concurrent_jobs', '同時に実行するジョブの数', 'num'],
        ['transfer.max_connections', '全体の同時接続数', 'num'],
        ['transfer.check_concurrency', '状態確認の並列数', 'num'],
        ['transfer.rehydrate_concurrency', 'リハイドレートの要求の並列数', 'num'],
        ['transfer.download_concurrency', 'ダウンロードの同時実行数', 'num', { unit: 'Blob' }],
        ['transfer.connections_per_blob', '1 Blob あたりの接続数', 'num'],
        ['transfer.chunk_size_mib', 'チャンクの大きさ', 'num', { unit: 'MiB' }],
        ['transfer.bandwidth_mbps', '帯域の上限', 'num', { unit: 'MB/s', mbps: true, step: 'any' }],
        ['transfer.request_rate', '要求の数の上限', 'num', { unit: '件/秒' }],
        ['transfer.retry_total', '再試行の回数', 'num', { unit: '回' }],
        [['transfer.connect_timeout', 'transfer.read_timeout'], 'タイムアウト', 'timeouts'],
        ['load.process_priority', '処理の優先度', 'select', { options: [['normal', '通常'], ['below_normal', 'やや低い'], ['background', '低い（バックグラウンド）']] }],
        [['load.prevent_sleep_downloading', 'load.prevent_sleep_waiting'], 'スリープの抑止', 'sleep'],
      ],
    },
    {
      id: 'auth', title: '認証', items: [
        ['auth.default_entra_mode', '既定の Entra ID の方式', 'radios', { options: [['user_account', 'Entra ID ユーザーアカウント'], ['access_key', 'アクセスキー']] }],
        ['auth.default_tenant', '既定のテナント', 'text', { width: 'w-mid', placeholder: 'organizations' }],
        ['auth.client_id', 'クライアント ID', 'text', { width: 'w-mid', mono: true, resetDefault: true }],
        ['auth.sas_expiry_warn_hours', 'SAS の期限の警告', 'num', { unit: '時間前' }],
      ],
    },
    {
      id: 'proxy', title: 'プロキシ', items: [
        ['network.proxy.mode', 'プロキシ', 'radios', { options: [['auto', '自動'], ['manual', '指定する'], ['none', '使わない']] }],
        ['network.proxy.url', 'プロキシの URL', 'text', { width: 'w-long', mono: true, placeholder: 'http://proxy.example:8080', when: 'manual' }],
        ['network.proxy.bypass', '使わない宛先', 'text', { width: 'w-long', mono: true, placeholder: '*.contoso.com, 10.0.0.0/8', when: 'manual', optional: true }],
        ['network.proxy.username', 'ID', 'text', { width: 'w-mid', when: 'manual', optional: true }],
        ['network.proxy.password', 'パスワード', 'password', { width: 'w-mid', when: 'manual', optional: true }],
        ['network.ca_bundle', '追加の CA 証明書', 'text', { width: 'w-long', mono: true, placeholder: 'C:\\certs\\contoso-ca.pem', optional: true }],
        ['__proxy_test', '', 'proxytest'],
      ],
    },
    {
      id: 'ui', title: '画面', items: [
        ['ui.start_page', '起動時に開くページ', 'select', { options: [['jobs', 'ジョブ'], ['search', '検索']] }],
        ['ui.browser_notifications', 'ブラウザの通知', 'check', { text: 'リハイドレートの完了などを、ブラウザの通知でも知らせる' }],
        ['ui.blob_name_display', 'Blob 名の表示', 'select', { options: [['full', '全体'], ['common', '共通する階層を省略'], ['tail', '末尾のみ']] }],
        ['exit.when_closed', '自動の終了', 'check', { text: '本ツールの画面（ブラウザのタブ）をすべて閉じたら、本ツールを終了する（実行中のジョブ・検索があるときは、終わってから終了します）' }],
        ['exit.wait_minutes', '終了までの時間', 'num', { unit: '分（画面をすべて閉じてから）' }],
      ],
    },
    {
      id: 'data', title: 'データとログ', items: [
        ['__data_dir', 'データ保存先', 'datadir'],
        ['log.level', 'ログのレベル', 'select', { options: [['DEBUG', 'DEBUG'], ['INFO', 'INFO'], ['WARNING', 'WARNING'], ['ERROR', 'ERROR']] }],
        ['log.max_files', 'ログを保存する数', 'num', { unit: '日分' }],
        ['history.max_entries', '検索履歴の上限', 'num', { unit: '件' }],
        ['__reset', '', 'reset'],
      ],
    },
  ];
}

export async function render(ctx) {
  const { root } = ctx;
  const res = await api.get('/api/v1/settings');
  const schema = Object.fromEntries(res.schema.map((s) => [s.key, s]));
  let saved = res.values;
  const session = api.session;
  /** @type {Map<string, {get: () => any, set: (v: any) => void, el: HTMLElement}>} */
  const fields = new Map();
  const whenRows = [];
  const bar = h('div', { class: 'sticky-bottom', hidden: true },
    button('保存', () => save(), { primary: true }),
    linkButton('変更を破棄', () => { load(saved); updateDirty(); }));

  const changes = () => {
    const out = {};
    for (const [key, f] of fields) {
      const v = f.get();
      if (key === 'network.proxy.password') {
        if (v !== '') out[key] = v;
        continue;
      }
      if (JSON.stringify(v) !== JSON.stringify(saved[key])) out[key] = v;
    }
    return out;
  };
  const updateDirty = () => { bar.hidden = Object.keys(changes()).length === 0; };
  const updateWhen = () => {
    const mode = fields.get('network.proxy.mode')?.get();
    for (const [nodes, when] of whenRows) showRow(nodes, mode === when);
  };

  const validateNum = (key, input) => {
    const s = schema[key];
    const v = input.value.trim();
    let msg = null;
    if (v === '' || Number.isNaN(Number(v))) msg = '数値を入力してください。';
    else if (s.kind === 'int' && !/^-?\d+$/.test(v)) msg = '整数を入力してください。';
    else if ((s.min !== undefined && Number(v) < s.min) || (s.max !== undefined && Number(v) > s.max)) msg = `${Number(s.min).toLocaleString('en-US')}〜${Number(s.max).toLocaleString('en-US')} の範囲で入力してください。`;
    setError(input, msg);
    return !msg;
  };

  const numInput = (key, opt = {}) => {
    const input = textInput('', { width: 'w-num', inputmode: 'decimal', 'aria-label': opt.label, onblur: () => validateNum(key, input), oninput: () => { updateDirty(); if (opt.after) opt.after(); } });
    input.classList.add('num');
    fields.set(key, { el: input, get: () => (schema[key].kind === 'float' ? Number(input.value) : (/^-?\d+$/.test(input.value.trim()) ? Number(input.value) : input.value)), set: (v) => { input.value = String(v); } });
    return input;
  };
  const checkField = (key, text) => {
    const lab = checkbox(text, false, () => updateDirty());
    const input = lab.querySelector('input');
    fields.set(key, { el: input, get: () => input.checked, set: (v) => { input.checked = Boolean(v); } });
    return lab;
  };

  const buildItem = (item) => {
    const [key, label, kind, opt = {}] = item;
    switch (kind) {
      case 'num': {
        const input = numInput(key, { label });
        const parts = [input];
        if (opt.unit) parts.push(h('span', { class: 'unit', text: opt.unit }));
        if (opt.mbps) {
          const conv = h('span', { class: 'unit' });
          const upd = () => { conv.textContent = Number(input.value) > 0 ? mbps(input.value) : '0 で無制限'; };
          input.addEventListener('input', upd);
          fields.get(key).after = upd;
          parts.push(conv);
          const f = fields.get(key);
          const origSet = f.set;
          f.set = (v) => { origSet(v); upd(); };
        }
        return row(label, parts, { inline: true });
      }
      case 'check':
        return row(opt.text ? label : '', checkField(key, opt.text || label), { inline: true });
      case 'select': {
        const el = select(opt.options, '', { onchange: () => updateDirty(), 'aria-label': label });
        fields.set(key, { el, get: () => el.value, set: (v) => { el.value = v; } });
        return row(label, el);
      }
      case 'radios': {
        const wrap = h('div', { class: 'radios', role: 'radiogroup', 'aria-label': label });
        const inputs = opt.options.map(([v, text]) => {
          const input = h('input', { type: 'radio', name: key, value: v, onchange: () => { updateDirty(); updateWhen(); } });
          wrap.appendChild(h('label.check', null, input, text));
          return input;
        });
        fields.set(key, { el: inputs[0], get: () => (inputs.find((i) => i.checked) || {}).value, set: (v) => inputs.forEach((i) => { i.checked = i.value === v; }) });
        return row(label, wrap);
      }
      case 'text':
      case 'password': {
        const input = textInput('', { width: opt.width, mono: opt.mono, placeholder: opt.placeholder, type: kind === 'password' ? 'password' : 'text', oninput: () => updateDirty(), 'aria-label': label });
        fields.set(key, { el: input, get: () => input.value.trim(), set: (v) => { input.value = v ?? ''; } });
        if (kind === 'password') {
          fields.get(key).set = () => { input.value = ''; input.placeholder = saved['network.proxy.password_set'] ? '設定済み（変えるときだけ入力）' : ''; };
        }
        const parts = [input];
        if (opt.resetDefault) {
          parts.push(linkButton('既定に戻す', () => { input.value = schema[key].default; updateDirty(); }));
        }
        const r = row(label, parts, { inline: true, optional: opt.optional });
        if (opt.when) whenRows.push([r, opt.when]);
        return r;
      }
      case 'dir': {
        const input = textInput('', { width: 'w-mid', 'aria-label': label, oninput: () => updateDirty() });
        fields.set(key, { el: input, get: () => input.value.trim(), set: (v) => { input.value = v; } });
        const pick = button('選択', async () => {
          const r = await pickFolder({ initial: input.value });
          if (r) { input.value = r.path; updateDirty(); }
        });
        return row(label, [input, pick], { inline: true });
      }
      case 'poll': {
        const [k1, k2] = key;
        return row(label, [h('span', { text: '最初は' }), numInput(k1, { label: `${label} 最初` }), h('span', { text: '分後、以降は' }), numInput(k2, { label: `${label} 以降` }), h('span', { text: '分ごと' })], { inline: true });
      }
      case 'timeouts': {
        const [k1, k2] = key;
        return row(label, [h('span', { text: '接続' }), numInput(k1, { label: '接続のタイムアウト' }), h('span', { text: '秒、読み取り' }), numInput(k2, { label: '読み取りのタイムアウト' }), h('span', { text: '秒' })], { inline: true });
      }
      case 'sleep': {
        const [k1, k2] = key;
        return row(label, [checkField(k1, 'ダウンロード中はスリープしない'), checkField(k2, 'リハイドレート待ちの間もスリープしない')]);
      }
      case 'proxytest': {
        const out = h('div', { class: 'pre' });
        const btn = button('接続を確認', async () => {
          out.textContent = '確認しています…';
          try {
            const r = await api.post('/api/v1/settings/proxy/test', {});
            out.textContent = r.results.map((x) => `${x.ok ? 'OK' : 'NG'}  ${x.target}  ${x.via}${x.message ? `  ${x.message}` : ''}`).join('\n');
          } catch (e) {
            out.textContent = e.message;
          }
        });
        return row('', [btn, out]);
      }
      case 'datadir': {
        const parts = [h('div', null, h('span', { class: 'mono', text: res.data_dir }), h('span', { class: 'muted', text: `　${res.mode_label}` })),
          linkButton('フォルダーを開く', () => api.post('/api/v1/fs/open-folder', { path: res.data_dir }).catch((e) => toastError(e)))];
        if (session.acl_warning) parts.push(h('div', { class: 'warn', text: 'データのフォルダーに、ほかの人から読めないようにする設定（ACL）をできませんでした。ほかの人から読めるおそれがあります。' }));
        return row(label, parts);
      }
      case 'reset':
        return row('', button('設定を既定に戻す', async () => {
          const ok = await confirmDialog({ title: '設定を既定に戻しますか', body: 'すべての設定を既定の値に戻します。プロキシのパスワードも消えます。', okText: '既定に戻す' });
          if (!ok) return;
          const r = await api.post('/api/v1/settings/reset', {});
          saved = r.values;
          load(saved);
          updateDirty();
          toast('設定を既定に戻しました');
        }));
      default:
        return [];
    }
  };

  const secs = sections(session);
  const toc = h('nav', { class: 'settings-toc', 'aria-label': '設定の節' });
  const body = h('div', { class: 'settings-body' });
  for (const sec of secs) {
    toc.appendChild(h('a', { href: `#${sec.id}`, dataset: { sec: sec.id } }, sec.title));
    body.appendChild(h('section', { id: sec.id }, h('h2', { text: sec.title }), formGrid(...sec.items.map(buildItem))));
  }

  const load = (values) => {
    for (const [key, f] of fields) {
      f.set(values[key]);
      setError(f.el, null);
    }
    updateWhen();
  };

  async function save() {
    let ok = true;
    for (const [key, f] of fields) {
      if (schema[key] && (schema[key].kind === 'int' || schema[key].kind === 'float')) ok = validateNum(key, f.el) && ok;
    }
    if (!ok) {
      focusFirstError(body);
      return;
    }
    const ch = changes();
    try {
      const r = await api.put('/api/v1/settings', { changes: ch });
      saved = r.values;
      load(saved);
      updateDirty();
      if (ch['ui.browser_notifications'] && 'Notification' in window && Notification.permission === 'default') {
        Notification.requestPermission().catch(() => {});
      }
      toast('設定を保存しました');
    } catch (e) {
      if (e.fields) {
        for (const [k, msg] of Object.entries(e.fields)) {
          const f = fields.get(k);
          if (f) setError(f.el, msg);
        }
        focusFirstError(body);
      } else {
        toastError(e);
      }
    }
  }

  root.append(h('div', { class: 'page-head' }, h('h1', { text: '設定' })), h('div', { class: 'settings-layout' }, toc, body), bar);
  load(saved);
  updateDirty();
  ctx.setGuard(() => (Object.keys(changes()).length ? '保存していない設定の変更があります。' : null));

  // 現在の節を目次で示します
  const links = new Map([...toc.querySelectorAll('a')].map((a) => [a.dataset.sec, a]));
  const io = new IntersectionObserver((entries) => {
    for (const e of entries) {
      if (e.isIntersecting) {
        links.forEach((a) => a.classList.remove('current'));
        links.get(e.target.id)?.classList.add('current');
      }
    }
  }, { rootMargin: '-60px 0px -70% 0px' });
  body.querySelectorAll('section').forEach((s) => io.observe(s));
  ctx.onLeave(() => io.disconnect());
  if (ctx.hash) document.getElementById(ctx.hash)?.scrollIntoView();
}
