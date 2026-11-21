// 接続先の追加のダイアログ（4.4.2.2）と、接続先の行の認証方式の選択（4.4.2.1）。

import * as api from '../api.js';
import { button, clear, debounce, h, iconButton, linkButton } from '../dom.js';
import { dateShort } from '../format.js';
import { runTask } from '../task.js';
import { confirmDialog, openDialog } from './dialog.js';
import { checkbox, formGrid, row, setError, textInput } from './form.js';
import { startSignIn } from './signin.js';

const METHODS = [
  ['azure', 'Azure にサインイン（Entra ID）'],
  ['connection_string', '接続文字列'],
  ['sas_url', 'SAS URL'],
  ['account_key', 'アカウント名とキー'],
];
const AZURE = 'core.windows.net';
const RENDER_LIMIT = 500;

function key(a, c) { return `${a}\n${c}`; }

/**
 * 接続先の追加のダイアログを開きます。
 * @param {Object} [opt]
 * @param {{storage_account: string, endpoint_suffix: string, container: string}} [opt.row] 認証方式の選択（その行の認証を変える）
 * @param {string} [opt.initialContainer] 次に接続先を追加するときのコンテナー名の初期値（既知の形式から作成）
 * @returns {Promise<Object[]|null>} 追加した接続先（5.5.2 の targets の形）
 */
export function openAddTargetDialog(opt = {}) {
  return new Promise((resolve) => {
    const choose = Boolean(opt.row);
    let result = null;
    let dirty = false;
    /** 選んだ組: key → {account, suffix, container} */
    const picks = new Map();
    let screenCleanup = () => {};
    const d = openDialog({
      title: '接続先を追加',
      width: 960,
      isDirty: () => dirty && picks.size > 0,
      onClose: () => { screenCleanup(); resolve(result); },
    });

    const titleWith = (name) => h('span', null, linkButton('‹ 接続先を追加', () => first()), h('span', { class: 'muted', text: ' › ' }), name);
    const finish = (targets) => {
      result = targets;
      d.close(true);
    };

    // ---------------------------------------------------------------- 最初の画面
    async function first() {
      screenCleanup();
      screenCleanup = () => {};
      picks.clear();
      d.setTitle(choose ? `認証方式を選択（${opt.row.storage_account} / ${opt.row.container}）` : '接続先を追加');
      const methods = h('section', null, h('h2', { text: '追加の方法' }),
        h('ul', { class: 'list-plain' }, METHODS.map(([id, label]) => h('li', null, linkButton(label, () => screens[id]())))));
      d.setBody(methods);
      d.setFoot();
      try {
        const res = choose
          ? await api.get(`/api/v1/credentials${api.qs({ account: opt.row.storage_account, suffix: opt.row.endpoint_suffix, container: opt.row.container })}`)
          : await api.get('/api/v1/credentials');
        const usable = res.items.filter((c) => c.usable);
        if (usable.length) {
          d.body.append(h('section', null, h('h2', { text: '保存した認証情報' }),
            h('ul', { class: 'list-plain' }, usable.map((c) => h('li', null, linkButton(
              `${c.storage_account}${c.scope_container ? ` / ${c.scope_container}` : ''}${c.endpoint_suffix !== AZURE ? `（${c.endpoint_suffix}）` : ''}　${c.scope_container ? 'コンテナー' : 'ストレージアカウント全体'}　${c.auth_label}${c.expires_at ? `・${dateShort(c.expires_at)} まで` : ''}`,
              () => fromSaved(c)))))));
        }
      } catch { /* 一覧を出せなくても追加はできます */ }
    }

    // ---------------------------------------------------------------- 共通: 選択の行と下のボタン
    const selLine = h('div', { class: 'chips' });
    function drawPicks() {
      clear(selLine);
      selLine.append(h('span', { text: `選択 ${picks.size} 件` }));
      for (const [k, p] of picks) {
        selLine.append(h('span', { class: 'chip' }, `${p.account} / ${p.container}`,
          iconButton('close', '選択から外す', () => { picks.delete(k); drawPicks(); onPicksChanged(); })));
      }
    }
    let onPicksChanged = () => {};

    async function submit(account, suffix, auth, containers, errorEl, replaceExisting = false) {
      setError(errorEl, null);
      if (!containers.length) {
        setError(errorEl, 'コンテナーを1つ以上選んでください。');
        return;
      }
      try {
        const res = await api.post('/api/v1/targets/add', { storage_account: account, endpoint_suffix: suffix, auth, containers, replace: replaceExisting });
        finish(res.targets);
      } catch (e) {
        if (e.code === 'CREDENTIAL_EXISTS') {
          const ex = e.data.existing || {};
          const ok = await confirmDialog({
            title: '保存した認証情報を置き換えますか',
            body: `${ex.storage_account}${ex.scope_container ? ` / ${ex.scope_container}` : ''} の認証情報（${ex.auth_label}・${ex.source_label}）が保存されています。入力した認証情報で置き換えます。`,
            okText: '置き換える',
          });
          if (ok) await submit(account, suffix, auth, containers, errorEl, true);
          return;
        }
        setError(errorEl, e.message);
      }
    }

    // ---------------------------------------------------------------- コンテナーの選択（共通）
    function containerSection(account, opts = {}) {
      const list = h('div', { class: 'folder-list', role: 'group', 'aria-label': 'コンテナー' });
      const kw = textInput('', { width: 'w-mid', placeholder: 'キーワード', 'aria-label': 'コンテナーのキーワード' });
      const nameIn = textInput(opts.initial || '', { width: 'w-mid', 'aria-label': 'コンテナー名' });
      const msg = h('div', { class: 'muted' });
      let names = [];
      const draw = () => {
        clear(list);
        const words = kw.value.toLowerCase().split(/\s+/).filter(Boolean);
        const shown = names.filter((n) => words.every((w) => n.toLowerCase().includes(w)));
        for (const n of shown.slice(0, 2000)) {
          const cb = checkbox(n, picks.has(key(account(), n)), (on2) => {
            const k = key(account(), n);
            if (on2) picks.set(k, { account: account(), container: n });
            else picks.delete(k);
            dirty = true;
            drawPicks();
          });
          cb.classList.add('folder-check');
          list.append(cb);
        }
        if (!names.length) list.append(h('div', { class: 'group', text: opts.emptyText || 'コンテナーはありません。' }));
      };
      kw.addEventListener('input', draw);
      const addByName = () => {
        const n = nameIn.value.trim();
        if (!n) return;
        if (!names.includes(n)) names = [n, ...names];
        picks.set(key(account(), n), { account: account(), container: n });
        nameIn.value = '';
        dirty = true;
        drawPicks();
        draw();
      };
      nameIn.addEventListener('keydown', (e) => { if (e.key === 'Enter') { e.preventDefault(); addByName(); } });
      const el = h('div', null,
        h('div', { class: 'inline' }, opts.title || h('span', { text: 'コンテナー' }), kw),
        msg, list,
        h('div', { class: 'inline', style: { marginTop: '8px' } }, h('span', { text: 'コンテナー名' }), nameIn, linkButton('加える', addByName)));
      return {
        el,
        setNames(n, message) { names = n; msg.textContent = message || ''; draw(); },
        setMessage(m) { msg.textContent = m || ''; },
        redraw: draw,
      };
    }

    // ---------------------------------------------------------------- 保存した認証情報から
    async function fromSaved(c) {
      if (c.scope_container) {
        if (choose) return finish([{ storage_account: c.storage_account, endpoint_suffix: c.endpoint_suffix, container: c.scope_container, auth_kind: c.auth_kind, credential_id: c.id }].map((t) => ({ ...t })));
        const err = h('div');
        await submit(c.storage_account, c.endpoint_suffix, { credential_id: c.id }, [c.scope_container], err);
        if (err.textContent) d.body.append(err);
        return;
      }
      if (choose) {
        const res = await api.post('/api/v1/targets/add', { storage_account: c.storage_account, endpoint_suffix: c.endpoint_suffix, auth: { credential_id: c.id }, containers: [opt.row.container] });
        return finish(res.targets);
      }
      d.setTitle(titleWith(`${c.storage_account}（保存した認証情報）`));
      const cs = containerSection(() => c.storage_account, { initial: opt.initialContainer });
      const err = h('div');
      d.setBody(cs.el, h('section', null, selLine, err));
      picks.clear();
      drawPicks();
      cs.setMessage('読み込み中…');
      try {
        const r = await runTask('POST', '/api/v1/containers/list', { storage_account: c.storage_account, endpoint_suffix: c.endpoint_suffix, auth: { credential_id: c.id } });
        cs.setNames(r.containers, r.list_error ? `コンテナーの一覧を取得できません（${r.list_error}）。コンテナー名を入力してください。` : '');
      } catch (e) {
        cs.setNames([], e.message);
      }
      d.setFoot(h('span.push'), button('キャンセル', () => d.close()), button('追加', () => submit(c.storage_account, c.endpoint_suffix, { credential_id: c.id }, [...picks.values()].map((p) => p.container), err), { primary: true }));
    }

    // ---------------------------------------------------------------- Azure にサインイン
    async function azureScreen() {
      d.setTitle(titleWith('Azure にサインイン'));
      picks.clear();
      const st = await api.get('/api/v1/auth/status');
      if (!st.signed_in) {
        d.setBody(h('div', { class: 'inline' }, h('span', { text: 'サインインしていません。' }),
          button('サインイン', async () => { if (await startSignIn()) azureScreen(); }, { primary: true })));
        d.setFoot(h('span.push'), button('キャンセル', () => d.close()));
        return;
      }
      const settings = (await api.get('/api/v1/settings')).values;
      let mode = settings['auth.default_entra_mode'] === 'access_key' ? 'entra_key' : 'entra_user';
      const a = st.account || {};
      const modeRadios = h('div', { class: 'radios', role: 'radiogroup', 'aria-label': '認証方式' },
        ...[['entra_user', 'Entra ID ユーザーアカウント'], ['entra_key', 'アクセスキー']].map(([v, label]) => h('label.check', null,
          h('input', { type: 'radio', name: 'td-mode', value: v, checked: v === mode, onchange: () => { mode = v; drawAccounts(); } }), label)));
      const top = h('div', { class: 'inline', style: { justifyContent: 'space-between', width: '100%' } },
        h('span', { text: a.tenant_name ? `${a.username}（${a.tenant_name}）` : a.username }),
        h('span', { class: 'inline' }, h('span', { text: '認証方式' }), modeRadios));
      const unavailable = h('div');
      const err = h('div');
      if (choose) {
        d.setBody(top, err);
        d.setFoot(h('span.push'), button('キャンセル', () => d.close()), button('決定', async () => {
          const res = await api.post('/api/v1/targets/add', { storage_account: opt.row.storage_account, endpoint_suffix: opt.row.endpoint_suffix, auth: { kind: mode }, containers: [opt.row.container] });
          finish(res.targets);
        }, { primary: true }));
        return;
      }
      const kw = textInput('', { width: 'w-mid', placeholder: 'キーワード', 'aria-label': 'ストレージアカウントのキーワード' });
      const acctList = h('div', { class: 'vtable td-accounts', role: 'table' });
      const manual = textInput('', { width: 'w-short', 'aria-label': '一覧にないストレージアカウント' });
      let accounts = [];
      let multi = false;
      let current = null;
      const cs = containerSection(() => current, {
        title: h('span', { text: 'コンテナー' }),
        initial: opt.initialContainer,
        emptyText: 'ストレージアカウントを選んでください。',
      });
      const loadContainers = async (name) => {
        current = name;
        cs.setNames([], '読み込み中…');
        try {
          const r = await runTask('POST', '/api/v1/containers/list', { storage_account: name, endpoint_suffix: AZURE, auth: { kind: mode } });
          if (current !== name) return;
          cs.setNames(r.containers, r.list_error ? `コンテナーの一覧を取得できません（${r.list_error}）。コンテナー名を入力してください。` : '');
        } catch (e) {
          if (current === name) cs.setNames([], e.message);
        }
        drawAccounts();
      };
      function drawAccounts() {
        clear(acctList);
        const cols = multi ? '1.2fr 1fr 1fr 0.8fr 0.8fr' : '1.2fr 1fr 1fr 0.8fr';
        const headRow = h('div', { class: 'vrow vhead', role: 'row', style: { gridTemplateColumns: cols, position: 'static' } },
          ...['ストレージアカウント', 'サブスクリプション', 'リソースグループ', 'リージョン', multi ? 'テナント' : null].filter(Boolean).map((t) => h('div', { class: 'vcell', role: 'columnheader', text: t })));
        acctList.append(headRow);
        const words = kw.value.toLowerCase().split(/\s+/).filter(Boolean);
        const shown = accounts.filter((x) => words.every((w) => `${x.name} ${x.subscription} ${x.resource_group} ${x.location}`.toLowerCase().includes(w)));
        for (const x of shown.slice(0, RENDER_LIMIT)) {
          const disabled = mode === 'entra_key' && !x.allow_shared_key;
          const r = h('div', {
            class: `vrow clickable${x.name === current ? ' selected' : ''}`, role: 'row', tabindex: '0',
            style: { gridTemplateColumns: cols, position: 'static' },
            title: disabled ? 'このストレージアカウントでは、アクセスキーによる認証が無効にされています。Entra ID（ユーザーアカウント）を使用してください。' : null,
            'aria-disabled': disabled ? 'true' : null,
            onclick: () => { if (!disabled) loadContainers(x.name); },
            onkeydown: (e) => { if (e.key === 'Enter' && !disabled) loadContainers(x.name); },
          }, ...[x.name, x.subscription, x.resource_group, x.location, multi ? x.tenant_name : null].filter((v) => v !== null).map((t) => h('div', { class: `vcell${disabled ? ' muted' : ''}`, role: 'cell', text: t || '' })));
          acctList.append(r);
        }
        if (shown.length > RENDER_LIMIT) acctList.append(h('div', { class: 'empty', text: `${shown.length.toLocaleString('en-US')} 件のうち ${RENDER_LIMIT} 件を表示しています。キーワードで絞り込んでください。` }));
        if (!accounts.length) acctList.append(h('div', { class: 'empty', text: 'ストレージアカウントはありません。' }));
      }
      kw.addEventListener('input', debounce(drawAccounts, 200));
      const addManual = () => {
        const n = manual.value.trim().toLowerCase();
        if (!/^[a-z0-9]{3,24}$/.test(n)) {
          setError(manual, 'ストレージアカウント名は、3〜24 文字の英小文字と数字です。');
          return;
        }
        setError(manual, null);
        if (!accounts.some((x) => x.name === n)) accounts = [{ name: n, subscription: '', resource_group: '', location: '', allow_shared_key: true }, ...accounts];
        manual.value = '';
        loadContainers(n);
      };
      manual.addEventListener('keydown', (e) => { if (e.key === 'Enter') { e.preventDefault(); addManual(); } });
      const left = h('div', null, kw, acctList, h('div', { class: 'inline', style: { marginTop: '8px' } }, h('span', { text: '一覧にないストレージアカウント' }), manual, linkButton('加える', addManual)));
      const right = h('div', { class: 'td-right' }, cs.el);
      d.setBody(top, unavailable, h('div', { class: 'td-split' }, left, right), h('section', null, selLine, err));
      drawPicks();
      d.setFoot(h('span.push'), button('キャンセル', () => d.close()), button('追加', async () => {
        const groups = new Map();
        for (const p of picks.values()) {
          if (!groups.has(p.account)) groups.set(p.account, []);
          groups.get(p.account).push(p.container);
        }
        if (!groups.size) { setError(err, 'コンテナーを1つ以上選んでください。'); return; }
        const all = [];
        for (const [acct, conts] of groups) {
          try {
            const res = await api.post('/api/v1/targets/add', { storage_account: acct, endpoint_suffix: AZURE, auth: { kind: mode }, containers: conts });
            all.push(...res.targets);
          } catch (e) { setError(err, e.message); return; }
        }
        finish(all);
      }, { primary: true }));
      acctList.append(h('div', { class: 'empty', text: '読み込み中…' }));
      try {
        const res = await runTask('GET', `/api/v1/azure/storage-accounts`);
        accounts = res.accounts;
        multi = res.multi_tenant;
        clear(unavailable);
        for (const u of res.unavailable) {
          unavailable.append(h('div', { class: 'inline warn', style: { margin: '8px 0' } },
            h('span', { text: `テナント「${u.tenant_name}」のストレージアカウントは表示されていません（${u.reason.replace(/。$/, '')}）` }),
            linkButton('このテナントにサインイン', async () => { if (await startSignIn(u.tenant_id)) azureScreen(); })));
        }
      } catch (e) {
        accounts = [];
        unavailable.append(h('div', { class: 'danger', text: e.message }));
      }
      drawAccounts();
    }

    // ---------------------------------------------------------------- 接続文字列・SAS URL・アカウント名とキー
    function secretScreen(method) {
      const label = Object.fromEntries(METHODS)[method];
      d.setTitle(titleWith(label));
      picks.clear();
      dirty = false;
      let parsed = null;
      let fixedContainer = null;
      const err = h('div');
      const parsedLine = h('div', { class: 'muted' });
      const cs = containerSection(() => parsed && parsed.account, { initial: opt.initialContainer || (choose ? opt.row.container : '') });
      const contSec = h('section', null, cs.el);
      const inputs = {};
      let formRows;
      const authBody = () => {
        if (method === 'account_key') {
          return { input: { source: 'account_key', account: inputs.account.value.trim(), key: inputs.key.value.trim(), endpoint_suffix: suffixValue() } };
        }
        return { input: { source: method, value: inputs.value.value.trim() } };
      };
      const suffixValue = () => (inputs.domainOther && inputs.domainOther.checked ? inputs.suffix.value.trim() : AZURE);
      const listContainers = async () => {
        setError(inputs.focus, null);
        if (!parsed) return;
        if (choose && (parsed.account !== opt.row.storage_account || parsed.endpoint_suffix !== opt.row.endpoint_suffix)) {
          setError(inputs.focus, `入力した認証情報は、この行のストレージアカウント（${opt.row.storage_account}）のものではありません。`);
          return;
        }
        if (parsed.level === 'container') {
          fixedContainer = parsed.container;
          if (choose && fixedContainer !== opt.row.container) {
            setError(inputs.focus, `この SAS はコンテナー「${fixedContainer}」のものです。この行のコンテナーには使えません。`);
            return;
          }
          contSec.hidden = true;
          picks.clear();
          picks.set(key(parsed.account, fixedContainer), { account: parsed.account, container: fixedContainer });
          drawPicks();
        } else {
          contSec.hidden = choose;
        }
        cs.setNames([], '読み込み中…');
        try {
          const r = await runTask('POST', '/api/v1/containers/list', { storage_account: parsed.account, endpoint_suffix: parsed.endpoint_suffix, auth: authBody() });
          if (parsed.level !== 'container') cs.setNames(r.containers, r.list_error ? `コンテナーの一覧を取得できません（${r.list_error}）。コンテナー名を入力してください。` : '');
          else cs.setNames([fixedContainer], '');
        } catch (e) {
          cs.setNames([], '');
          setError(inputs.focus, e.message);
        }
      };
      const showParsed = (info) => {
        const parts = [];
        if (info.kind === 'account_key') parts.push('アクセスキー');
        if (info.sas) {
          parts.push({ account: 'アカウント SAS', service: 'サービス SAS', user_delegation: 'ユーザー委任 SAS' }[info.sas.kind] || 'SAS');
          parts.push(info.level === 'container' ? `コンテナー ${info.container}` : info.level === 'blob' ? 'Blob' : 'ストレージアカウント全体');
          parts.push(`権限 ${info.sas.permission_labels.join('・') || (info.sas.policy ? 'アクセスポリシーによる' : 'なし')}`);
          parts.push(info.sas.expiry ? `${dateShort(info.sas.expiry)} まで` : (info.sas.policy ? '期限はアクセスポリシーによる' : '期限なし'));
          if (info.sas.ip_restricted) parts.push('許可された IP アドレスからのみ使えます');
        }
        parsedLine.textContent = `${info.account}${info.endpoint_suffix !== AZURE ? `（${info.endpoint_suffix}）` : ''}　${parts.join('、')}`;
        const warns = [];
        if (info.sas) {
          if (info.sas.can_list_blobs === false) warns.push('一覧（l）の権限がないため、検索できません。');
          if (info.sas.can_read === false) warns.push('読み取り（r）の権限がないため、ダウンロードできません。');
          if (info.sas.can_set_tier === false) warns.push('書き込み（w）の権限がないため、リハイドレートできません。');
        }
        for (const w of info.warnings || []) warns.push(w);
        if (warns.length) parsedLine.append(h('div', { class: 'warn', text: warns.join(' ') }));
      };
      const parse = async () => {
        parsed = null;
        parsedLine.textContent = '';
        const v = inputs.value.value.trim();
        if (!v) return;
        const res = await api.post('/api/v1/secrets/parse', { source: method, value: v });
        if (!res.ok) {
          setError(inputs.value, res.error);
          return;
        }
        setError(inputs.value, null);
        parsed = res.info;
        showParsed(parsed);
        await listContainers();
      };
      if (method === 'account_key') {
        inputs.account = textInput(choose ? opt.row.storage_account : '', { width: 'w-account', 'aria-label': 'アカウント名', maxlength: 24 });
        inputs.key = textInput('', { type: 'password', width: 'w-long', 'aria-label': 'アカウントキー' });
        inputs.suffix = textInput(choose && opt.row.endpoint_suffix !== AZURE ? opt.row.endpoint_suffix : '', { width: 'w-mid', placeholder: 'core.chinacloudapi.cn', 'aria-label': 'その他のドメイン' });
        inputs.domainOther = h('input', { type: 'radio', name: 'td-domain', value: 'other', checked: choose && opt.row.endpoint_suffix !== AZURE });
        const domainAzure = h('input', { type: 'radio', name: 'td-domain', value: 'azure', checked: !(choose && opt.row.endpoint_suffix !== AZURE) });
        inputs.focus = inputs.key;
        const show = button('コンテナーを表示', async () => {
          setError(inputs.account, null);
          setError(inputs.key, null);
          setError(inputs.suffix, null);
          const res = await api.post('/api/v1/secrets/parse', { source: 'account_key', account: inputs.account.value.trim(), key: inputs.key.value.trim(), endpoint_suffix: suffixValue() });
          if (!res.ok) {
            const target = /ドメイン/.test(res.error) ? inputs.suffix : /アカウント名/.test(res.error) ? inputs.account : inputs.key;
            setError(target, res.error);
            return;
          }
          parsed = res.info;
          parsedLine.textContent = '';
          await listContainers();
        });
        formRows = formGrid(
          ...row('アカウント名', inputs.account),
          ...row('アカウントキー', inputs.key),
          ...row('ドメイン', [h('div', { class: 'radios' }, h('label.check', null, domainAzure, 'Azure（core.windows.net）'), h('label.check', null, inputs.domainOther, 'その他'), inputs.suffix), show]),
        );
      } else {
        inputs.value = method === 'connection_string'
          ? h('textarea', { class: 'input mono w-full', rows: '3', 'aria-label': '接続文字列', spellcheck: 'false', autocomplete: 'off', placeholder: 'DefaultEndpointsProtocol=https;AccountName=stexample01;AccountKey=…' })
          : textInput('', { type: 'password', width: 'w-full', 'aria-label': 'SAS URL', placeholder: 'https://stexample01.blob.core.windows.net/logs?sv=…&sig=…' });
        inputs.value.classList.add('secret-input');
        inputs.focus = inputs.value;
        inputs.value.addEventListener('input', debounce(() => { dirty = true; parse(); }, 400));
        formRows = formGrid(...row(method === 'connection_string' ? '接続文字列' : 'SAS URL', [inputs.value, parsedLine]));
      }
      d.setBody(h('section', null, formRows, method === 'account_key' ? parsedLine : null), contSec, h('section', null, selLine, err));
      drawPicks();
      const note = h('span', { class: 'note', text: '入力した認証情報は暗号化して、この PC のこの Windows ユーザーだけが使える形で保存します。' });
      d.setFoot(note, button('キャンセル', () => d.close()), button(choose ? '決定' : '追加', async () => {
        if (!parsed) {
          setError(inputs.focus, '認証情報を入力してください。');
          inputs.focus.focus();
          return;
        }
        const conts = choose ? [opt.row.container] : [...picks.values()].map((p) => p.container);
        await submit(parsed.account, parsed.endpoint_suffix, authBody(), conts, err);
      }, { primary: true }));
      setTimeout(() => inputs.focus.focus(), 0);
    }

    const screens = {
      azure: azureScreen,
      connection_string: () => secretScreen('connection_string'),
      sas_url: () => secretScreen('sas_url'),
      account_key: () => secretScreen('account_key'),
    };
    first();
  });
}
