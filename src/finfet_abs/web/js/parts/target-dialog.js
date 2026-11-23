// 接続先の追加のダイアログ（4.4.2.2）と、接続先の行の認証方式の選択（4.4.2.1）。

import * as api from '../api.js';
import { addButton, button, clear, debounce, h, icon, iconButton, linkButton } from '../dom.js';
import { on } from '../events.js';
import { dateShort } from '../format.js';
import { runTask } from '../task.js';
import { confirmDialog, openDialog } from './dialog.js';
import { formGrid, row, select, setError, textInput } from './form.js';
import { startSignIn } from './signin.js';

const METHODS = [
  ['azure', 'サインイン中のアカウントから選ぶ'],
  ['connection_string', '接続文字列'],
  ['sas_url', 'SAS URL'],
  ['account_key', 'アカウント名とキー'],
];
// 追加の方法の説明（v1.5.3: 依頼者の意見。最初の画面が簡素で、違いが分かりにくかったため）
const METHOD_DESC = {
  connection_string: 'Azure Portal の［アクセスキー］などで確認できる接続文字列を貼り付けます。',
  sas_url: 'ストレージアカウントまたはコンテナーの SAS URL を貼り付けます。操作できる範囲と期限は、SAS のとおりです。',
  account_key: 'ストレージアカウント名と、アクセスキーを入力します。',
};

/**
 * 区画（v1.5.4: 依頼者の意見。一覧の見た目を見直したときに加えた、枠と見出しの付いたまとまり）。
 * @param {string|null} step 手順の番号（なければ付けません）
 * @param {string} title 見出し
 * @param {Array<Node|null>} extra 見出しの右に並べるもの
 */
function pane(step, title, extra, ...body) {
  return h('section', { class: 'td-pane' },
    h('div', { class: 'td-pane-head' }, step ? h('span', { class: 'td-step', 'aria-hidden': 'true', text: step }) : null,
      h('h3', { class: 'td-pane-title', text: title }), ...extra),
    ...body);
}

/** 虫眼鏡の付いた絞り込みの欄。 */
function searchBox(placeholder, label) {
  const input = textInput('', { placeholder, 'aria-label': label });
  return { input, el: h('div', { class: 'td-search' }, icon('search'), input) };
}

const num = (n) => n.toLocaleString('en-US');
/** 件数の表示（絞り込んでいるときは「表示 / 全体」）。 */
const countText = (shown, all) => (all ? (shown === all ? `${num(all)} 件` : `${num(shown)} / ${num(all)} 件`) : '');

/** 待っている間の表示（回る印と文）。 */
function loadingPanel(text, sub) {
  const t = h('div', { class: 'loading-text', text });
  const s = h('div', { class: 'loading-sub muted', text: sub || '' });
  const el = h('div', { class: 'loading-panel', role: 'status', 'aria-live': 'polite' },
    h('span', { class: 'spinner', 'aria-hidden': 'true' }), h('div', { class: 'loading-body' }, t, s));
  return { el, set: (text2, sub2) => { t.textContent = text2; if (sub2 !== undefined) s.textContent = sub2; } };
}
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
      onPicksChanged = () => {};
      activeCs = null;
      picks.clear();
      d.setTitle(choose ? `認証方式を選択（${opt.row.storage_account} / ${opt.row.container}）` : '接続先を追加');
      d.setBody(loadingPanel('読み込んでいます…').el);
      d.setFoot(h('span.push'), button('キャンセル', () => d.close()));
      let st = null;
      try { st = await api.get('/api/v1/auth/status'); } catch { /* サインインの状態が分からなくても選べます */ }
      const signedIn = !!(st && st.signed_in);
      const accts = st && st.accounts && st.accounts.length ? st.accounts : (st && st.account ? [st.account] : []);
      const who = accts.map((x) => x.username).filter(Boolean);
      const card = (id, title, desc, extra = {}) => h('button', { type: 'button', class: `method-card${extra.main ? ' main' : ''}`, onclick: () => screens[id]() },
        h('span', { class: 'method-title' }, title, extra.tag ? h('span', { class: 'choice-tag', text: extra.tag }) : null),
        h('span', { class: 'method-desc', text: desc }),
        extra.sub ? h('span', { class: 'method-sub', text: extra.sub }) : null,
        h('span', { class: 'method-go' }, icon('arrow')));
      const azureCard = signedIn
        ? card('azure', 'サインイン中のアカウントから選ぶ', 'サインインしているアカウントで使えるストレージアカウントの一覧から、コンテナーを選びます。キーや SAS を入力する必要はありません。',
          { main: true, tag: 'おすすめ', sub: `サインイン中: ${who.join('、') || '（アカウント）'}` })
        : card('azure', 'Microsoft Entra ID でサインインして選ぶ', 'サインインすると、そのアカウントで使えるストレージアカウントの一覧から、コンテナーを選べます。',
          { main: true, tag: 'おすすめ', sub: 'まだサインインしていません' });
      const methods = h('section', { class: 'method-section' }, h('h2', { text: '追加の方法' }),
        h('div', { class: 'method-grid' }, azureCard),
        h('h3', { class: 'method-subhead', text: 'キー・SAS・接続文字列を使う' }),
        h('div', { class: 'method-grid three' }, METHODS.filter(([id]) => id !== 'azure').map(([id, label]) => card(id, label, METHOD_DESC[id]))));
      d.setBody(methods);
      try {
        const res = choose
          ? await api.get(`/api/v1/credentials${api.qs({ account: opt.row.storage_account, suffix: opt.row.endpoint_suffix, container: opt.row.container })}`)
          : await api.get('/api/v1/credentials');
        const usable = res.items.filter((c) => c.usable);
        if (usable.length) {
          d.body.append(h('section', { class: 'method-section' }, h('h2', { text: '保存した認証情報から選ぶ' }),
            h('div', { class: 'saved-list' }, usable.map((c) => h('button', { type: 'button', class: 'saved-item', onclick: () => fromSaved(c) },
              h('span', { class: 'saved-name mono', text: `${c.storage_account}${c.scope_container ? ` / ${c.scope_container}` : ''}${c.endpoint_suffix !== AZURE ? `（${c.endpoint_suffix}）` : ''}` }),
              h('span', { class: 'saved-meta muted', text: `${c.scope_container ? 'コンテナー' : 'ストレージアカウント全体'}・${c.auth_label}${c.expires_at ? `・${dateShort(c.expires_at)} まで` : ''}` }),
              icon('arrow'))))));
        }
      } catch { /* 一覧を出せなくても追加はできます */ }
    }

    // ---------------------------------------------------------------- 共通: 選択の行と下のボタン
    const selLine = h('div', { class: 'td-selection', 'aria-live': 'polite' });
    /** 今の画面のコンテナーの区画（選択を外したときに、チェックを描き直します）。 */
    let activeCs = null;
    function drawPicks() {
      clear(selLine);
      selLine.append(h('span', { class: 'td-sel-label' }, '選択 ', h('strong', { text: String(picks.size) }), ' 件'));
      if (!picks.size) selLine.append(h('span', { class: 'muted small', text: '追加するコンテナーを選んでください。' }));
      for (const [k, p] of picks) {
        selLine.append(h('span', { class: 'chip' }, h('span', { class: 'chip-acct', text: `${p.account} /` }), p.container,
          iconButton('close', '選択から外す', () => {
            picks.delete(k);
            drawPicks();
            onPicksChanged();
            if (activeCs) activeCs.redraw();
          })));
      }
    }
    /** 選択が変わったときに呼びます（ストレージアカウントの一覧の「n 件選択」の表示）。 */
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
      const list = h('div', { class: 'pick-list', role: 'group', 'aria-label': 'コンテナー' });
      const kwBox = searchBox('コンテナーを絞り込む', 'コンテナーのキーワード');
      const kw = kwBox.input;
      const nameIn = textInput(opts.initial || '', { placeholder: 'コンテナー名', 'aria-label': 'コンテナー名' });
      const msgEl = h('div', { class: 'td-pane-msg' });
      const forAcct = h('span', { class: 'td-for mono' });
      const count = h('span', { class: 'td-count' });
      let names = [];
      let message = '';
      let loading = null;
      const visible = () => {
        const words = kw.value.toLowerCase().split(/\s+/).filter(Boolean);
        return names.filter((n) => words.every((w) => n.toLowerCase().includes(w)));
      };
      const changed = () => {
        dirty = true;
        drawPicks();
        onPicksChanged();
      };
      const allBtn = linkButton('表示中をすべて選ぶ', () => {
        for (const n of visible()) picks.set(key(account(), n), { account: account(), container: n });
        changed();
        draw();
      });
      const noneBtn = linkButton('選択を外す', () => {
        for (const n of names) picks.delete(key(account(), n));
        changed();
        draw();
      });
      const draw = () => {
        clear(list);
        const acct = account();
        forAcct.textContent = acct || '';
        msgEl.textContent = names.length ? message : '';
        if (loading) {
          count.textContent = '';
          allBtn.disabled = true;
          noneBtn.disabled = true;
          list.append(loading.el);
          return;
        }
        const shown = visible();
        count.textContent = countText(shown.length, names.length);
        allBtn.disabled = !shown.length;
        noneBtn.disabled = !names.some((n) => picks.has(key(acct, n)));
        for (const n of shown.slice(0, 2000)) {
          const on0 = picks.has(key(acct, n));
          const item = h('label', { class: `pick-item cont-item${on0 ? ' selected' : ''}` });
          item.append(h('input', {
            type: 'checkbox', checked: on0,
            onchange: (e) => {
              const k = key(account(), n);
              if (e.target.checked) picks.set(k, { account: account(), container: n });
              else picks.delete(k);
              item.classList.toggle('selected', e.target.checked);
              noneBtn.disabled = !names.some((x) => picks.has(key(account(), x)));
              changed();
            },
          }), h('span', { class: 'cont-name mono', text: n }));
          list.append(item);
        }
        if (shown.length > 2000) list.append(h('div', { class: 'pick-empty', text: `${num(shown.length)} 件のうち 2,000 件を表示しています。キーワードで絞り込んでください。` }));
        if (!names.length) list.append(h('div', { class: 'pick-empty', text: message || opts.emptyText || 'コンテナーはありません。' }));
        else if (!shown.length) list.append(h('div', { class: 'pick-empty', text: 'キーワードに合うコンテナーはありません。' }));
      };
      kw.addEventListener('input', draw);
      const addByName = () => {
        const n = nameIn.value.trim();
        if (!n || !account()) return;
        if (!names.includes(n)) names = [n, ...names];
        picks.set(key(account(), n), { account: account(), container: n });
        nameIn.value = '';
        changed();
        draw();
      };
      nameIn.addEventListener('keydown', (e) => { if (e.key === 'Enter') { e.preventDefault(); addByName(); } });
      const el = pane(opts.step || null, 'コンテナー', [forAcct, count],
        h('div', { class: 'td-pane-tools' }, kwBox.el, h('span', { class: 'td-bulk' }, allBtn, noneBtn)),
        msgEl, list,
        h('div', { class: 'td-pane-foot' }, h('span', { class: 'td-foot-label', text: '一覧にないコンテナー' }), nameIn, addButton('加える', addByName)));
      const cs = {
        el,
        setNames(n, m) { loading = null; names = n; message = m || ''; draw(); },
        setMessage(m) { message = m || ''; draw(); },
        /** 取得中の表示。 */
        setLoading(text) { loading = loadingPanel(text || 'コンテナーの一覧を取得しています…'); draw(); },
        redraw: draw,
      };
      activeCs = cs;
      draw();
      return cs;
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
      onPicksChanged = () => {};
      const cs = containerSection(() => c.storage_account, { initial: opt.initialContainer });
      const err = h('div');
      d.setBody(cs.el, selLine, err);
      picks.clear();
      drawPicks();
      cs.setLoading();
      try {
        const r = await runTask('POST', '/api/v1/containers/list', { storage_account: c.storage_account, endpoint_suffix: c.endpoint_suffix, auth: { credential_id: c.id } });
        cs.setNames(r.containers, r.list_error ? `コンテナーの一覧を取得できません（${r.list_error}）。コンテナー名を入力してください。` : '');
      } catch (e) {
        cs.setNames([], e.message);
      }
      d.setFoot(h('span.push'), button('キャンセル', () => d.close()), button('追加', () => submit(c.storage_account, c.endpoint_suffix, { credential_id: c.id }, [...picks.values()].map((p) => p.container), err), { primary: true }));
    }

    // ---------------------------------------------------------------- Azure にサインイン
    async function azureScreen(user) {
      d.setTitle(titleWith('サインイン中のアカウントから選ぶ'));
      picks.clear();
      // 待っている間も、何をしているかを表示します（v1.5.3: 依頼者の意見）
      d.setBody(loadingPanel('サインインの状態を確認しています…').el);
      d.setFoot(h('span.push'), button('キャンセル', () => d.close()));
      const st = await api.get('/api/v1/auth/status');
      if (!st.signed_in) {
        d.setBody(h('div', { class: 'inline' }, h('span', { text: 'サインインしていません。' }),
          button('サインイン', async () => { if (await startSignIn()) azureScreen(); }, { primary: true })));
        d.setFoot(h('span.push'), button('キャンセル', () => d.close()));
        return;
      }
      const settings = (await api.get('/api/v1/settings')).values;
      let mode = settings['auth.default_entra_mode'] === 'access_key' ? 'entra_key' : 'entra_user';
      // 使うアカウント（複数のアカウントでサインインしているときは選べます。1 つだけなら記録せず、既定のアカウントを使います）
      const accts = st.accounts && st.accounts.length ? st.accounts : [st.account || {}];
      const a = accts.find((x) => x.id === user) || st.account || accts[0] || {};
      const acc = accts.length > 1 ? a.id : null;
      const who = accts.length > 1
        ? select(accts.map((x) => [x.id, x.tenant_name ? `${x.username}（${x.tenant_name}）` : x.username]), a.id, {
          'aria-label': '使うアカウント', onchange: (e) => azureScreen(e.target.value),
        })
        : h('span', { class: 'td-who', text: a.tenant_name ? `${a.username}（${a.tenant_name}）` : a.username });
      const addAcct = addButton('アカウントを追加', async () => { if (await startSignIn(null, { add: true })) azureScreen(a.id); });
      const modeRadios = h('div', { class: 'segmented', role: 'radiogroup', 'aria-label': '認証方式' },
        ...[['entra_user', 'Entra ID ユーザーアカウント'], ['entra_key', 'アクセスキー']].map(([v, label]) => h('label', { class: 'seg' },
          h('input', { type: 'radio', name: 'td-mode', value: v, checked: v === mode, onchange: () => { mode = v; drawAccounts(); } }), h('span', { text: label }))));
      const top = h('div', { class: 'td-toolbar' },
        h('div', { class: 'td-tool' }, h('span', { class: 'td-tool-label', text: 'アカウント' }), who, addAcct),
        h('div', { class: 'td-tool' }, h('span', { class: 'td-tool-label', text: '認証方式' }), modeRadios));
      const unavailable = h('div', { class: 'td-notices' });
      const err = h('div');
      if (choose) {
        d.setBody(top, err);
        d.setFoot(h('span.push'), button('キャンセル', () => d.close()), button('決定', async () => {
          const res = await api.post('/api/v1/targets/add', { storage_account: opt.row.storage_account, endpoint_suffix: opt.row.endpoint_suffix, auth: { kind: mode, account: acc }, containers: [opt.row.container] });
          finish(res.targets);
        }, { primary: true }));
        return;
      }
      const kwBox = searchBox('名前・サブスクリプション・リソースグループ・リージョン', 'ストレージアカウントのキーワード');
      const kw = kwBox.input;
      const acctCount = h('span', { class: 'td-count' });
      const acctList = h('div', { class: 'pick-list td-accounts', role: 'listbox', 'aria-label': 'ストレージアカウント' });
      const manual = textInput('', { placeholder: 'ストレージアカウント名', 'aria-label': '一覧にないストレージアカウント' });
      let accounts = [];
      let multi = false;
      let current = null;
      let listing = true; // 一覧を取得している間は、取得中の表示のままにします
      const cs = containerSection(() => current, {
        step: '2',
        initial: opt.initialContainer,
        emptyText: 'ストレージアカウントを選ぶと、ここにコンテナーが表示されます。',
      });
      const loadContainers = async (name) => {
        current = name;
        drawAccounts();
        cs.setLoading(`${name} のコンテナーを取得しています…`);
        try {
          const r = await runTask('POST', '/api/v1/containers/list', { storage_account: name, endpoint_suffix: AZURE, auth: { kind: mode, account: acc } });
          if (current !== name) return;
          cs.setNames(r.containers, r.list_error ? `コンテナーの一覧を取得できません（${r.list_error}）。コンテナー名を入力してください。` : '');
        } catch (e) {
          if (current === name) cs.setNames([], e.message);
        }
      };
      function drawAccounts() {
        if (listing) return;
        const refocus = acctList.contains(document.activeElement);
        const top0 = acctList.scrollTop;
        clear(acctList);
        const words = kw.value.toLowerCase().split(/\s+/).filter(Boolean);
        const shown = accounts.filter((x) => words.every((w) => `${x.name} ${x.subscription} ${x.resource_group} ${x.location} ${x.tenant_name || ''}`.toLowerCase().includes(w)));
        acctCount.textContent = countText(shown.length, accounts.length);
        const selCount = new Map();
        for (const p of picks.values()) selCount.set(p.account, (selCount.get(p.account) || 0) + 1);
        for (const x of shown.slice(0, RENDER_LIMIT)) {
          const disabled = mode === 'entra_key' && !x.allow_shared_key;
          const isCur = x.name === current;
          const n = selCount.get(x.name) || 0;
          const meta = [x.subscription, x.resource_group, x.location, multi ? x.tenant_name : null].filter(Boolean).join(' · ');
          acctList.append(h('div', {
            class: `pick-item acct-item${isCur ? ' selected' : ''}${disabled ? ' disabled' : ''}`, role: 'option', tabindex: disabled ? '-1' : '0',
            'aria-selected': isCur ? 'true' : 'false', 'aria-disabled': disabled ? 'true' : null,
            title: disabled ? 'このストレージアカウントでは、アクセスキーによる認証が無効にされています。Entra ID（ユーザーアカウント）を使用してください。' : null,
            onclick: () => { if (!disabled) loadContainers(x.name); },
            onkeydown: (e) => { if ((e.key === 'Enter' || e.key === ' ') && !disabled) { e.preventDefault(); loadContainers(x.name); } },
          },
          h('span', { class: 'acct-main' }, h('span', { class: 'acct-name mono', text: x.name }), h('span', { class: 'acct-meta', text: meta || '名前で指定したストレージアカウント' })),
          disabled ? h('span', { class: 'td-badge', text: 'アクセスキーは無効' }) : null,
          n ? h('span', { class: 'td-badge on', text: `${n} 件選択` }) : null,
          icon('chevron')));
        }
        if (shown.length > RENDER_LIMIT) acctList.append(h('div', { class: 'pick-empty', text: `${num(shown.length)} 件のうち ${RENDER_LIMIT} 件を表示しています。キーワードで絞り込んでください。` }));
        if (!accounts.length) acctList.append(h('div', { class: 'pick-empty', text: 'ストレージアカウントはありません。一覧にない場合は、下の欄に名前を入力してください。' }));
        else if (!shown.length) acctList.append(h('div', { class: 'pick-empty', text: 'キーワードに合うストレージアカウントはありません。' }));
        acctList.scrollTop = top0;
        if (refocus) acctList.querySelector('.acct-item.selected')?.focus();
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
      const left = pane('1', 'ストレージアカウント', [acctCount],
        h('div', { class: 'td-pane-tools' }, kwBox.el), acctList,
        h('div', { class: 'td-pane-foot' }, h('span', { class: 'td-foot-label', text: '一覧にないストレージアカウント' }), manual, addButton('加える', addManual)));
      onPicksChanged = () => drawAccounts();
      d.setBody(top, unavailable, h('div', { class: 'td-split' }, left, cs.el), selLine, err);
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
            const res = await api.post('/api/v1/targets/add', { storage_account: acct, endpoint_suffix: AZURE, auth: { kind: mode, account: acc }, containers: conts });
            all.push(...res.targets);
          } catch (e) { setError(err, e.message); return; }
        }
        finish(all);
      }, { primary: true }));
      // 取得中の表示: 段階（テナントごと）・見つかった数・経過した秒数（v1.5.3: 依頼者の意見）
      const loading = loadingPanel('ストレージアカウントの一覧を取得しています…', 'サインイン中のアカウントで使えるテナント・サブスクリプションを調べています。');
      const elapsed = h('div', { class: 'loading-elapsed muted' });
      loading.el.querySelector('.loading-body').append(elapsed);
      acctList.append(loading.el);
      kw.disabled = true;
      const t0 = Date.now();
      const tick = setInterval(() => { elapsed.textContent = `経過 ${Math.floor((Date.now() - t0) / 1000)} 秒`; }, 1000);
      let taskId = null;
      const offProgress = on('accounts.progress', (p) => {
        if (!p || (taskId && p.id && p.id !== taskId)) return;
        if (p.step === 'tenants') loading.set('テナントの一覧を取得しています…');
        else if (p.step === 'tenant') {
          loading.set(`テナント「${p.tenant}」のストレージアカウントを取得しています（${p.index} / ${p.total}）…`,
            p.found ? `これまでに ${p.found.toLocaleString('en-US')} 件見つかりました。` : 'サブスクリプションが多い場合は、数十秒かかることがあります。');
        }
      });
      const prevCleanup = screenCleanup;
      screenCleanup = () => { clearInterval(tick); offProgress(); prevCleanup(); };
      try {
        const res = await runTask('GET', `/api/v1/azure/storage-accounts${acc ? `?account=${encodeURIComponent(acc)}` : ''}`, undefined, (id) => { taskId = id; });
        accounts = res.accounts;
        multi = res.multi_tenant;
        clear(unavailable);
        for (const u of res.unavailable) {
          unavailable.append(h('div', { class: 'td-notice' }, icon('warn'),
            h('span', { text: `テナント「${u.tenant_name}」のストレージアカウントは表示されていません（${u.reason.replace(/。$/, '')}）` }),
            linkButton('このテナントにサインイン', async () => { if (await startSignIn(u.tenant_id, { account: a.id })) azureScreen(a.id); })));
        }
      } catch (e) {
        accounts = [];
        unavailable.append(h('div', { class: 'danger', text: e.message }));
      } finally {
        clearInterval(tick);
        offProgress();
        kw.disabled = false;
        listing = false;
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
      const parsedLine = h('div', { class: 'td-parsed' });
      onPicksChanged = () => {};
      const cs = containerSection(() => parsed && parsed.account, { step: '2', initial: opt.initialContainer || (choose ? opt.row.container : ''), emptyText: '認証情報を入力すると、ここにコンテナーが表示されます。' });
      const contSec = h('div', { class: 'td-cont-wrap' }, cs.el);
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
        cs.setLoading(`${parsed.account} のコンテナーを取得しています…`);
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
      d.setBody(pane('1', '認証情報', [], formRows, method === 'account_key' ? parsedLine : null), contSec, selLine, err);
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
