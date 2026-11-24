// 検索のページ（4.4）。入力の順番は 接続先 → パスパターン → 変数。検索のページに戻ると、入力はそのまま残っています。

import * as api from '../api.js';
import { addButton, button, clear, debounce, h, iconButton, linkButton, menuButton } from '../dom.js';
import { dateShort, num } from '../format.js';
import { confirmDialog } from '../parts/dialog.js';
import { openExport, openImport } from '../parts/exchange.js';
import { checkbox, setError, textInput } from '../parts/form.js';
import { openKnownFormats } from '../parts/known-formats.js';
import { openMenu } from '../parts/menu.js';
import { patternInput } from '../parts/pattern-input.js';
import { openAddTargetDialog } from '../parts/target-dialog.js';
import { toast, toastError } from '../parts/toast.js';
import { periodFromFound, variableRow } from '../parts/variables.js';
import { runTask } from '../task.js';
import { renderUrlsTab } from './search-urls.js';

const KEY = 'finfet.search.state';
const AZURE = 'core.windows.net';

/** 検索のページの入力（ページを移っても残します）。 */
let state = null;

function emptyState(tz) {
  return { name: '', targets: [], path_pattern: '', ignore_case: false, input_timezone: tz || 'local', variables: {}, shift: null };
}

function save() {
  try { sessionStorage.setItem(KEY, JSON.stringify(state)); } catch { /* 保存できなくても使えます */ }
}

function load(tz) {
  if (state) return state;
  try {
    const s = JSON.parse(sessionStorage.getItem(KEY) || 'null');
    if (s && Array.isArray(s.targets)) state = s;
  } catch { state = null; }
  if (!state) state = emptyState(tz);
  return state;
}

/** 検索条件（5.5.2）を、検索のページの入力にします。 */
async function fromCondition(cond, opt = {}) {
  const res = await api.post('/api/v1/targets/resolve', { targets: cond.targets || [] });
  state = {
    name: cond.name || '', targets: res.targets, path_pattern: cond.path_pattern || '', ignore_case: !!cond.ignore_case,
    input_timezone: cond.input_timezone || 'local', variables: cond.variables || {}, shift: opt.shift ? {} : null,
  };
  for (const v of Object.values(state.variables)) v.touched = true;
  save();
}

/** 検索条件（API に送る形）。 */
function toCondition() {
  const vars = {};
  for (const [k, v] of Object.entries(state.variables)) {
    const { touched, ...rest } = v;
    vars[k] = rest;
  }
  return {
    name: state.name || undefined,
    targets: state.targets.map((t) => ({ storage_account: t.storage_account, endpoint_suffix: t.endpoint_suffix, container: t.container, auth_kind: t.auth_kind, tenant: t.tenant || null, credential_id: t.credential_id || null, account: t.account || null })),
    path_pattern: state.path_pattern, ignore_case: state.ignore_case, input_timezone: state.input_timezone, variables: vars,
  };
}

export async function render(ctx) {
  const { root } = ctx;
  const settings = (await api.get('/api/v1/settings')).values;
  load(settings['search.input_timezone']);
  const urlsTab = ctx.path === '/search/urls';

  // 呼び出し（検索履歴・検索結果・ジョブ・保存した認証情報）
  const q = ctx.query;
  try {
    if (q.get('history')) {
      const hist = await api.get(`/api/v1/history/${encodeURIComponent(q.get('history'))}`);
      await fromCondition(hist.condition, { shift: true });
      ctx.navigate('/search', { replace: true });
      return;
    }
    if (q.get('search')) {
      const s = await api.get(`/api/v1/searches/${encodeURIComponent(q.get('search'))}`);
      await fromCondition(s.condition, { shift: false });
      ctx.navigate('/search', { replace: true });
      return;
    }
    if (q.get('from-job')) {
      const j = await api.get(`/api/v1/jobs/${encodeURIComponent(q.get('from-job'))}`);
      if (j.condition) await fromCondition(j.condition, { shift: false });
      ctx.navigate('/search', { replace: true });
      return;
    }
    if (q.get('credential')) {
      const creds = (await api.get('/api/v1/credentials')).items;
      const c = creds.find((x) => x.id === q.get('credential'));
      if (c) {
        const res = await api.post('/api/v1/targets/describe', { targets: [{ storage_account: c.storage_account, endpoint_suffix: c.endpoint_suffix, container: c.scope_container || '', auth_kind: c.auth_kind, credential_id: c.id }] });
        state.targets.push(...res.targets);
        save();
      }
      ctx.navigate('/search', { replace: true });
      return;
    }
  } catch (e) {
    toastError(e);
  }

  const head = h('div', { class: 'page-head' }, h('h1', { text: '検索' }),
    h('div', { class: 'actions' },
      linkButton('新しい条件', async () => {
        if (state.path_pattern || state.targets.length) {
          const ok = await confirmDialog({ title: '入力を消しますか', body: '接続先・パスパターン・変数の入力を消して、新しい条件にします。', okText: '消す' });
          if (!ok) return;
        }
        state = emptyState(settings['search.input_timezone']);
        save();
        ctx.navigate('/search', { replace: true });
      }),
      menuButton('最近の検索', (e) => recentMenu(e.currentTarget)),
      linkButton('ファイルから読み込む', async () => {
        const r = await openImport({ allowLoad: true });
        if (r && r.condition) {
          await fromCondition(r.condition);
          ctx.navigate('/search', { replace: true });
        }
      }),
      linkButton('ファイルに保存', () => openExport({ condition: toCondition() }, 1))));
  const tabs = h('nav', { class: 'tabs', 'aria-label': '検索の方法' },
    h('a', { href: '/search', 'aria-current': urlsTab ? null : 'page', text: 'パスパターンで検索' }),
    h('a', { href: '/search/urls', 'aria-current': urlsTab ? 'page' : null, text: 'URL で指定' }));
  root.append(head, tabs);

  async function recentMenu(anchor) {
    const res = await api.get('/api/v1/searches');
    const items = res.items.slice(0, 10).map((s) => ({
      text: `${dateShort(s.started_at)}　${s.targets_summary}　${s.path_pattern.slice(0, 50)}${s.path_pattern.length > 50 ? '…' : ''}　${num(s.matched_count)} 件`,
      href: `/searches/${s.id}`,
    }));
    openMenu(anchor, items.length ? items : [h('div', { class: 'menu-text', text: '最近の検索はありません。' })], { align: 'right' });
  }

  if (urlsTab) {
    await renderUrlsTab(ctx);
    return;
  }

  // ---------------------------------------------------------------- 接続先
  const targetsSec = h('section', { class: 'part' });
  const patternSec = h('section', { class: 'part' });
  const varsSec = h('section', { class: 'part' });
  const searchBtn = button('検索', () => startSearch(), { primary: true });
  const bottom = h('div', { class: 'sticky-bottom search-actions' }, h('span', { class: 'push' }), searchBtn);
  // 広い画面では 2 列（左: 接続先・パスパターン、右: 変数）にします（v1.5.0。依頼者の意見: 大きい画面で余白が多い）
  root.append(h('div', { class: 'search-cols' }, h('div', { class: 'search-col' }, targetsSec, patternSec), h('div', { class: 'search-col vars-col' }, varsSec)), bottom);
  let analysis = null;
  const containerSuggestions = new Map();

  function drawTargets() {
    clear(targetsSec);
    targetsSec.append(h('h2', { text: '接続先' }));
    if (state.targets.length) {
      const grid = h('div', { class: 'targets', role: 'table' },
        h('div', { class: 'targets-head', role: 'row' }, ...['ストレージアカウント', 'コンテナー', '認証', '', ''].map((t) => h('div', { role: 'columnheader', text: t }))));
      state.targets.forEach((t, i) => grid.append(...targetRow(t, i)));
      targetsSec.append(grid);
    }
    targetsSec.append(h('div', { class: 'line' }, addButton('接続先を追加', async () => {
      const added = await openAddTargetDialog({ initialContainer: state.pendingContainer || '' });
      if (added && added.length) {
        for (const t of added) {
          if (!state.targets.some((x) => x.storage_account === t.storage_account && x.endpoint_suffix === t.endpoint_suffix && x.container === t.container)) state.targets.push(t);
        }
        state.pendingContainer = '';
        save();
        drawTargets();
        analyze();
      }
    })));
  }

  function targetRow(t, i) {
    const accountCell = h('div', { role: 'cell' }, t.storage_account, t.endpoint_suffix !== AZURE ? h('span', { class: 'muted', text: ` ${t.endpoint_suffix}` }) : null);
    let contCell;
    const fixed = t.scope_container || (t.auth_kind === 'sas' && t.credential_id && t.scope_container);
    if (fixed) {
      contCell = h('div', { role: 'cell', class: 'mono', text: t.container });
    } else {
      const listId = `cont-${i}`;
      const inp = textInput(t.container, { width: 'w-mid', mono: true, 'aria-label': `${t.storage_account} のコンテナー`, list: listId });
      inp.dataset.targetIndex = String(i);
      inp.addEventListener('input', () => { t.container = inp.value.trim(); save(); analyzeSoon(); });
      inp.addEventListener('focus', () => loadContainerSuggestions(t, listId));
      contCell = h('div', { role: 'cell' }, inp, h('datalist', { id: listId }));
    }
    const authLink = h('button', {
      type: 'button', class: `link-btn${t.auth_kind && t.resolved !== false ? '' : ' warn'}`, title: t.auth_detail || '認証方式を選択',
      onclick: () => chooseAuth(i),
    }, t.auth_kind && t.resolved !== false ? `${t.auth_label || t.auth_kind}${t.account ? `（${t.account.split('@')[0]}）` : ''}` : [h('span', { class: 'icon icon-warn' }), '認証']);
    authLink.dataset.authIndex = String(i);
    const result = h('span', { class: 'muted ellipsis target-test' });
    const testLink = linkButton('テスト', async () => {
      result.textContent = '確認しています…';
      result.className = 'muted ellipsis target-test';
      try {
        const r = await runTask('POST', '/api/v1/targets/test', { target: { ...t } });
        result.textContent = r.message;
        result.className = `${r.ok ? 'ok' : 'danger'} ellipsis target-test`;
      } catch (e) {
        result.textContent = e.message;
        result.className = 'danger ellipsis target-test';
      }
    });
    const rm = iconButton('close', '接続先を削除', () => { state.targets.splice(i, 1); save(); drawTargets(); analyze(); });
    const cells = [accountCell, contCell, h('div', { role: 'cell' }, authLink), h('div', { role: 'cell', class: 'line' }, testLink, result), h('div', { role: 'cell' }, rm)];
    const out = [h('div', { class: 'targets-row', role: 'row' }, ...cells)];
    const extra = [];
    for (const w of t.warnings || []) extra.push(h('div', { class: 'warn', text: w }));
    const fs = (analysis?.formats || []).find((f) => f.container === t.container);
    if (fs && !state.path_pattern) {
      extra.push(h('div', { class: 'line' }, h('span', { text: `${fs.name}の形式です。` }), linkButton('パスパターンを入れる', () => fromKnown(fs.id, t.container))));
    }
    if (extra.length) out.push(h('div', { class: 'targets-note' }, ...extra));
    return out;
  }

  async function loadContainerSuggestions(t, listId) {
    const key = `${t.storage_account}|${t.endpoint_suffix}|${t.auth_kind}|${t.credential_id || ''}`;
    const dl = document.getElementById(listId);
    if (!dl) return;
    let names = containerSuggestions.get(key);
    if (!names) {
      try {
        const r = await runTask('POST', '/api/v1/suggest/containers', t);
        names = [...new Set([...r.recent, ...r.containers])];
        containerSuggestions.set(key, names);
      } catch { names = []; }
    }
    clear(dl);
    for (const n of names.slice(0, 500)) dl.append(h('option', { value: n }));
  }

  async function chooseAuth(i) {
    const t = state.targets[i];
    const res = await openAddTargetDialog({ row: { storage_account: t.storage_account, endpoint_suffix: t.endpoint_suffix, container: t.container } });
    if (res && res.length) {
      state.targets[i] = { ...res[0], container: res[0].scope_container || t.container };
      save();
      drawTargets();
    }
  }

  async function fromNames() {
    const m = await import('../parts/infer-dialog.js');
    const r = await m.openInferDialog({ targets: state.targets });
    if (!r) return;
    state.path_pattern = r.path_pattern;
    // 自動生成で作った変数（選択肢と、日時の変数の Blob がある期間）を入れます
    for (const [k, v] of Object.entries(r.variables || {})) {
      if (v.type === 'datetime') {
        const def = { type: 'datetime', value: { mode: 'any' }, path_timezone: 'utc', touched: true };
        if (k === 'dt' && r.found) {
          def.found = r.found;
          if (r.found.complete) {
            // すべての Blob から分かった期間は、そのまま期間の初期値にします
            const p = periodFromFound(r.found, 'utc', state.input_timezone);
            def.value = { mode: 'period', start: p.start, end: p.end };
          }
        }
        state.variables[k] = def;
      } else {
        state.variables[k] = { ...v, touched: true, ...(r.complete ? {} : { from_sample: true }) };
      }
    }
    save();
    pi.value = state.path_pattern;
    analyze();
  }

  async function fromKnown(formatId, container) {
    let inputs = {};
    if (formatId && container) {
      try { inputs = (await api.post('/api/v1/known-formats/suggest-inputs', { id: formatId, container })).inputs; } catch { inputs = {}; }
    }
    const b = await openKnownFormats({ formatId, inputs });
    if (!b) return;
    state.path_pattern = b.path_pattern;
    for (const [k, v] of Object.entries(b.variables)) state.variables[k] = { ...v, touched: true };
    if (b.container) {
      const wide = state.targets.find((t) => !t.scope_container && !t.container);
      if (wide) wide.container = b.container;
      else if (container === undefined) state.pendingContainer = b.container;
    }
    save();
    pi.value = state.path_pattern;
    drawTargets();
    analyze();
  }

  // ---------------------------------------------------------------- パスパターン
  const pi = patternInput({
    value: state.path_pattern,
    label: 'パスパターン',
    placeholder: 'y={{dt=YYYY}}/m={{dt=MM}}/d={{dt=DD}}/[^/]+\\.json',
    onInput: (v) => { state.path_pattern = v; save(); analyzeSoon(); },
    suggestions: () => Object.keys(state.variables),
  });
  const patternErr = h('div', { class: 'field-error', hidden: true, role: 'alert' });
  const hint = h('div', { class: 'line', hidden: true });
  const prefixLine = h('div', { class: 'muted' });
  const icWarn = h('span', { class: 'warn', text: '一覧の取得を絞り込めないため、件数が多いと遅くなります', hidden: !state.ignore_case });
  const ic = checkbox('大文字・小文字を区別しない', state.ignore_case, (on) => { state.ignore_case = on; icWarn.hidden = !on; save(); analyze(); });
  patternSec.append(h('h2', { text: 'パスパターン' }), pi.el, patternErr, hint,
    h('div', { class: 'line' }, linkButton('既知の形式から作成', () => fromKnown(null)), linkButton('パスパターンを自動生成', () => fromNames()), menuButton('履歴から', (e) => historyMenu(e.currentTarget)), ic, icWarn),
    prefixLine);

  async function historyMenu(anchor) {
    const res = await api.get('/api/v1/history?menu=1');
    const items = res.items.map((it) => ({
      text: `${it.pinned ? '［ピン留め］' : ''}${it.path_pattern.slice(0, 60)}${it.path_pattern.length > 60 ? '…' : ''}　${it.targets_summary}`,
      onClick: async () => { await fromCondition(it.condition, { shift: true }); ctx.navigate('/search', { replace: true }); },
    }));
    openMenu(anchor, items.length ? items : [h('div', { class: 'menu-text', text: '検索履歴はありません。' })]);
  }

  // ---------------------------------------------------------------- 変数
  const choiceFilters = new Map(); // 選択肢の変数の絞り込みの文字（行を描き直しても保つ）
  // 日時の入力欄（期間の開始・終了）で入力している間は、変数の欄を描き直しません。
  // Chrome は日付・時刻の部品を 1 つ変えるたびに change を出すため、描き直すとフォーカスが外れ、
  // 続けて打った数字が失われていました（「15」と打つと「1」だけ入る）。値はそのつど状態に入れ、
  // 描き直しは日時の入力欄からフォーカスが離れたときにまとめて行います。
  // フォーカスの有無は focusin・focusout で追います（日付の部品を打ち終えたときの change では、
  // document.activeElement が一時的に body を返すことがあるため）。
  let varsPending = false;
  let dtInput = null;
  let pointerInVars = false;
  const dtFocused = () => !!(dtInput && dtInput.isConnected && varsSec.contains(dtInput));
  varsSec.addEventListener('focusin', (e) => {
    dtInput = e.target.classList && e.target.classList.contains('dt-input') ? e.target : null;
  });
  // 描き直しを待っていたら、日時の入力欄から離れたときに描き直します。フォーカスのあった部品は、
  // 描き直した後も同じ位置の部品に戻します
  const flushVars = () => {
    if (!varsPending || dtFocused() || pointerInVars || !ctx.isCurrent()) return;
    const sel = 'button, input, select, textarea, [tabindex]';
    const a = document.activeElement;
    const idx = a && varsSec.contains(a) ? [...varsSec.querySelectorAll(sel)].indexOf(a) : -1;
    drawVars();
    if (idx >= 0) varsSec.querySelectorAll(sel)[idx]?.focus();
  };
  // 変数の欄でマウスのボタンを押している間も、描き直しを待ちます（押している間に描き直すと、
  // ボタンが作り直されて押したことにならないため。打った後の解析は 300 ミリ秒後に描き直します）
  varsSec.addEventListener('pointerdown', () => {
    pointerInVars = true;
    const done = () => {
      window.removeEventListener('pointerup', done, true);
      window.removeEventListener('pointercancel', done, true);
      setTimeout(() => { pointerInVars = false; flushVars(); }, 0);
    };
    window.addEventListener('pointerup', done, true);
    window.addEventListener('pointercancel', done, true);
  }, true);
  varsSec.addEventListener('focusout', (e) => {
    if (e.target === dtInput) dtInput = null;
    setTimeout(flushVars, 0);
  });
  function drawVars() {
    if (dtFocused() || pointerInVars) {
      varsPending = true;
      return;
    }
    varsPending = false;
    clear(varsSec);
    const headRow = h('div', { class: 'line' }, h('h2', { text: '変数' }));
    if (analysis?.merge) {
      headRow.append(h('span', { class: 'merge-hint' }, `${analysis.merge.names.join('・')} を1つの日時の変数にまとめられます。`,
        linkButton('まとめる', () => {
          state.path_pattern = analysis.merge.pattern;
          for (const n of analysis.merge.names) delete state.variables[n];
          pi.value = state.path_pattern;
          save();
          analyze();
        })));
    }
    varsSec.append(headRow);
    const names = Object.keys(state.variables);
    if (!names.length) {
      varsSec.append(h('p', { class: 'muted', text: 'パスパターンに {{名前}} を書くと、変数を指定できます。' }));
      return;
    }
    const inPattern = new Set((analysis?.variables || []).filter((v) => v.in_pattern).map((v) => v.name));
    const formatsOf = (n) => [...state.path_pattern.matchAll(new RegExp(`\\{\\{${n}=([^{}]*)\\}\\}`, 'g'))].map((m) => m[1]);
    for (const n of names) {
      const def = state.variables[n];
      varsSec.append(variableRow({
        name: n, def, inPattern: analysis ? inPattern.has(n) : true, formats: formatsOf(n), inputTimezone: state.input_timezone,
        dtInfo: (analysis?.datetimes || []).find((x) => x.name === n) || null,
        onChange: () => { save(); drawVars(); analyzeSoon(); },
        onQuietChange: () => { save(); analyzeSoon(); },
        choiceFilter: { get: () => choiceFilters.get(n) || '', set: (f) => choiceFilters.set(n, f) },
        onInputTimezone: (tz) => { state.input_timezone = tz; save(); drawVars(); analyze(); },
        onRemove: () => { delete state.variables[n]; save(); drawVars(); },
        fetchCandidates: state.targets.length ? async () => {
          const r = await runTask('POST', '/api/v1/suggest/values', { target: state.targets[0], name: n, path_pattern: state.path_pattern, variables: toCondition().variables, input_timezone: state.input_timezone });
          return r.values;
        } : null,
        shiftHint: state.shift && def.type === 'datetime' && def.value?.mode === 'period' ? () => shiftToToday(def) : null,
      }));
    }
  }

  function shiftToToday(def) {
    const v = def.value;
    const parse = (s) => {
      const m = String(s || '').match(/^(\d{4})[-/]?(\d{1,2})?[-/]?(\d{1,2})?(.*)$/);
      return m ? new Date(Number(m[1]), (Number(m[2]) || 1) - 1, Number(m[3]) || 1) : null;
    };
    const end = parse(v.end);
    const start = parse(v.start);
    if (!end || !start) return;
    const today = new Date();
    today.setHours(0, 0, 0, 0);
    const days = Math.round((today - end) / 86400000);
    const shift = (s) => {
      const m = String(s).match(/^(\d{4})[-/]?(\d{1,2})[-/]?(\d{1,2})(.*)$/);
      if (!m) return s;
      const d = new Date(Number(m[1]), Number(m[2]) - 1, Number(m[3]) + days);
      const p = (x) => String(x).padStart(2, '0');
      return `${d.getFullYear()}-${p(d.getMonth() + 1)}-${p(d.getDate())}${m[4]}`;
    };
    v.start = shift(v.start);
    v.end = shift(v.end);
    // 追加の期間（v1.5.0）も、同じ日数だけ動かします
    for (const p of Array.isArray(v.more) ? v.more : []) { p.start = shift(p.start); p.end = shift(p.end); }
    def.touched = true;
    save();
    drawVars();
    analyze();
  }

  // ---------------------------------------------------------------- 解析（通信なし）
  async function analyze() {
    const containers = state.targets.map((t) => t.container).filter(Boolean);
    let res;
    try {
      res = await api.post('/api/v1/pattern/analyze', { path_pattern: state.path_pattern, containers, variables: toCondition().variables, ignore_case: state.ignore_case, input_timezone: state.input_timezone });
    } catch {
      return;
    }
    if (!ctx.isCurrent()) return;
    analysis = res;
    // 変数の一覧を更新します（値を入力していない、パスパターンにない変数は削除します）
    for (const v of res.variables) {
      if (v.new && !state.variables[v.name]) state.variables[v.name] = { ...v.definition };
      if (!v.in_pattern && state.variables[v.name] && !state.variables[v.name].touched) delete state.variables[v.name];
      if (v.in_pattern && state.variables[v.name] && v.definition.type === 'datetime' && state.variables[v.name].type !== 'datetime') state.variables[v.name] = { ...v.definition };
    }
    save();
    const iss = res.issues[0];
    patternErr.hidden = !iss;
    patternErr.textContent = iss ? (iss.pos ? `${iss.pos} 文字目: ${iss.message}` : iss.message) : '';
    pi.setError(iss && iss.pos ? iss.pos - 1 : null);
    clear(hint);
    hint.hidden = true;
    if (res.literal_hint) {
      hint.hidden = false;
      hint.append(h('span', { text: '記号を含む文字列です。' }), linkButton('記号を文字として扱う', () => { state.path_pattern = res.literal_hint; pi.value = state.path_pattern; save(); analyze(); }));
    }
    if (res.url_hint) {
      hint.hidden = false;
      const u = res.url_hint;
      hint.append(h('span', { text: 'Blob の URL です。' }), linkButton('ストレージアカウント・コンテナー・パスに分けて入れる', async () => {
        if (u.has_sas) {
          toast('URL に SAS が含まれています。接続先の追加の「SAS URL」で追加してください。');
        }
        state.path_pattern = (u.path || '').replace(/[.^$*+?()[\]\\|{}]/g, '\\$&');
        if (!state.targets.some((t) => t.storage_account === u.storage_account && t.container === u.container)) {
          state.targets.push({ storage_account: u.storage_account, endpoint_suffix: u.endpoint_suffix, container: u.container, auth_kind: null, resolved: false, warnings: [] });
        }
        pi.value = state.path_pattern;
        save();
        drawTargets();
        analyze();
      }));
    }
    if (!state.path_pattern) prefixLine.textContent = '';
    else if (iss) prefixLine.textContent = '';
    else if (!res.prefixes.length || (res.prefixes.length === 1 && !res.prefixes[0])) prefixLine.textContent = '一覧の取得の範囲: コンテナー全体';
    else {
      const shown = res.prefixes.length === 1 ? res.prefixes[0] : commonPrefix(res.prefixes);
      prefixLine.textContent = `一覧の取得の範囲: ${shown} で始まる Blob${res.prefixes.length > 1 ? `（${res.prefixes.length} 種類）` : ''}`;
    }
    drawVars();
    drawTargets();
  }
  const analyzeSoon = debounce(analyze, 300);

  function commonPrefix(list) {
    let p = list[0] || '';
    for (const s of list) while (!s.startsWith(p)) p = p.slice(0, -1);
    return p;
  }

  // ---------------------------------------------------------------- 検索
  async function startSearch() {
    if (!state.targets.length) {
      toast('接続先を追加してください。', { error: true });
      return;
    }
    const noAuth = state.targets.findIndex((t) => !t.auth_kind || t.resolved === false);
    if (noAuth >= 0) {
      const el = targetsSec.querySelector(`[data-auth-index="${noAuth}"]`);
      el.scrollIntoView({ block: 'center' });
      el.focus();
      toast('認証方式を選択してください。', { error: true });
      return;
    }
    const noCont = state.targets.findIndex((t) => !t.container);
    if (noCont >= 0) {
      const el = targetsSec.querySelector(`[data-target-index="${noCont}"]`);
      if (el) { setError(el, 'コンテナー名を入力してください。'); el.focus(); }
      return;
    }
    try {
      const r = await api.post('/api/v1/searches', { condition: toCondition() });
      ctx.navigate(`/searches/${r.search_id}`);
    } catch (e) {
      if (e.data && e.data.issues) {
        const iss = e.data.issues[0];
        patternErr.hidden = false;
        patternErr.textContent = iss.pos ? `${iss.pos} 文字目: ${iss.message}` : iss.message;
        pi.focus();
      } else if (e.data && e.data.target_index !== undefined) {
        toastError(e);
      } else {
        toastError(e);
      }
    }
  }
  const onKey = (e) => {
    if (e.key === 'Enter' && (e.ctrlKey || e.metaKey) && !document.querySelector('dialog[open]')) {
      e.preventDefault();
      startSearch();
    }
  };
  document.addEventListener('keydown', onKey);
  ctx.onLeave(() => document.removeEventListener('keydown', onKey));

  drawTargets();
  drawVars();
  analyze();
}
