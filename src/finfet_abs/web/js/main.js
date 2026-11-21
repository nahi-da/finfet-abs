// 起動、ルーティング、ナビゲーション、上部のバー（4.2）。

import * as api from './api.js';
import { button, clear, h } from './dom.js';
import * as events from './events.js';
import { confirmDialog, infoDialog } from './parts/dialog.js';
import { openMenu } from './parts/menu.js';
import { addNotice, markAllRead, notices, setNoticeHandler, toast, unreadCount } from './parts/toast.js';
import { dateShort } from './format.js';

/** URL → ページ（4.2.2）。[正規表現, ページのモジュール, ナビゲーションの項目, ページ名] */
const ROUTES = [
  [/^\/search$/, 'search', 'search', '検索'],
  [/^\/search\/urls$/, 'search', 'search', '検索'],
  [/^\/searches\/([^/]+)$/, 'search-result', 'search', '検索結果'],
  [/^\/jobs$/, 'jobs', 'jobs', 'ジョブ'],
  [/^\/jobs\/ended$/, 'jobs', 'jobs', 'ジョブ'],
  [/^\/jobs\/([^/]+)(?:\/(condition|options|log))?$/, 'job', 'jobs', 'ジョブ'],
  [/^\/history$/, 'history', 'history', '検索履歴'],
  [/^\/connections$/, 'connections', 'connections', '接続'],
  [/^\/settings$/, 'settings', 'settings', '設定'],
];

const pageEl = () => document.getElementById('page');
let cleanups = [];
let guard = null;
let currentUrl = location.pathname + location.search + location.hash;
let renderSeq = 0;
/** 画面全体で使う状態 */
export const app = { session: null, auth: null };

/** ページを離れてよいか（保存していない変更があれば確認します）。 */
async function canLeave() {
  if (!guard) return true;
  const msg = guard();
  if (!msg) return true;
  return confirmDialog({ title: '変更を破棄しますか', body: msg, okText: '破棄する' });
}

/**
 * ページを移ります。
 * @param {string} url
 * @param {{replace?: boolean}} [opt]
 */
export async function navigate(url, opt = {}) {
  if (!(await canLeave())) return false;
  guard = null;
  if (opt.replace) history.replaceState(null, '', url);
  else history.pushState(null, '', url);
  await renderCurrent();
  return true;
}

/** ページを移らずに、URL のクエリだけを変えます（絞り込み・並べ替え・タブを残すため）。 */
export function updateQuery(params) {
  const u = new URL(location.href);
  for (const [k, v] of Object.entries(params)) {
    if (v === null || v === undefined || v === '') u.searchParams.delete(k);
    else u.searchParams.set(k, String(v));
  }
  const s = u.pathname + u.search + u.hash;
  history.replaceState(null, '', s);
  currentUrl = s;
}

function setTitle(name) {
  document.title = `${name} - FinFeT`;
}

async function renderCurrent() {
  const seq = ++renderSeq;
  for (const fn of cleanups) {
    try { fn(); } catch (e) { console.error(e); }
  }
  cleanups = [];
  guard = null;
  const path = location.pathname;
  if (path === '/' || path === '') {
    const start = app.session?.start_page === 'search' ? '/search' : '/jobs';
    history.replaceState(null, '', start);
    return renderCurrent();
  }
  currentUrl = location.pathname + location.search + location.hash;
  const route = ROUTES.find(([re]) => re.test(path));
  for (const a of document.querySelectorAll('.nav a[data-nav]')) {
    if (route && a.dataset.nav === route[2]) a.setAttribute('aria-current', 'page');
    else a.removeAttribute('aria-current');
  }
  const root = pageEl();
  clear(root);
  if (!route) {
    setTitle('見つかりません');
    root.append(h('h1', { text: '見つかりません' }), h('p', null, 'このページはありません。', h('a', { href: '/jobs', text: 'ジョブを開く' })));
    return;
  }
  const [re, mod, , name] = route;
  setTitle(name);
  const params = path.match(re).slice(1);
  const ctx = {
    root,
    params,
    path,
    query: new URLSearchParams(location.search),
    hash: location.hash.slice(1),
    setTitle,
    onLeave: (fn) => cleanups.push(fn),
    setGuard: (fn) => { guard = fn; },
    navigate,
    updateQuery,
    isCurrent: () => seq === renderSeq,
  };
  const loading = setTimeout(() => {
    if (seq === renderSeq && !root.firstChild) root.append(h('p', { class: 'loading', text: '読み込み中…' }));
  }, 300);
  try {
    const page = await import(`./pages/${mod}.js`);
    if (seq !== renderSeq) return;
    await page.render(ctx);
  } catch (e) {
    console.error(e);
    if (seq === renderSeq) {
      clear(root);
      root.append(h('h1', { text: name }), h('p', { class: 'danger', text: e.message || 'ページを表示できませんでした。' }));
    }
  } finally {
    clearTimeout(loading);
  }
}

// ---------------------------------------------------------------- リンクの横取り

document.addEventListener('click', (e) => {
  if (e.defaultPrevented || e.button !== 0 || e.ctrlKey || e.metaKey || e.shiftKey || e.altKey) return;
  const a = e.target.closest('a[href]');
  if (!a || a.target || a.hasAttribute('download')) return;
  const url = new URL(a.href, location.href);
  if (url.origin !== location.origin) return;
  if (url.pathname.startsWith('/docs/') || url.pathname.startsWith('/api/')) return;
  e.preventDefault();
  if (url.pathname + url.search === location.pathname + location.search && url.hash) {
    location.hash = url.hash;
    return;
  }
  navigate(url.pathname + url.search + url.hash);
});

window.addEventListener('popstate', async () => {
  const target = location.pathname + location.search + location.hash;
  if (guard) {
    history.pushState(null, '', currentUrl);
    if (!(await canLeave())) return;
    guard = null;
    history.pushState(null, '', target);
  }
  renderCurrent();
});

window.addEventListener('beforeunload', (e) => {
  if (guard && guard()) {
    e.preventDefault();
    e.returnValue = '';
  }
});

// `/` キーで絞り込みの欄にフォーカスします（4.3.1）
document.addEventListener('keydown', (e) => {
  if (e.key !== '/' || e.ctrlKey || e.altKey || e.metaKey) return;
  const t = e.target;
  if (t instanceof HTMLElement && (t.closest('input, textarea, select, [contenteditable]') || t.closest('dialog'))) return;
  const f = document.querySelector('#page [data-filter]');
  if (f) {
    e.preventDefault();
    f.focus();
  }
});

// ---------------------------------------------------------------- 上部のバー

function setupNav() {
  const toggle = document.getElementById('nav-toggle');
  if (window.innerWidth < 1200) document.body.classList.add('nav-closed');
  toggle.addEventListener('click', () => document.body.classList.toggle('nav-closed'));
}

function updateNoticeButton() {
  const btn = document.getElementById('notices-btn');
  const n = unreadCount();
  btn.textContent = n ? `お知らせ ${n}` : 'お知らせ';
}

function showNotices(anchor) {
  const items = notices.length
    ? notices.map((n) => h('div', { class: 'menu-item-text' },
      h('div', { class: n.level === 'error' ? 'danger' : n.level === 'warn' ? 'warn' : '' }, n.text),
      h('div', { class: 'muted' }, dateShort(n.at.toISOString()), n.url ? ' ' : '', n.url ? h('a', { href: n.url, text: '開く' }) : null)))
    : [h('div', { class: 'menu-text', text: 'お知らせはありません。' })];
  const menu = openMenu(anchor, items);
  menu.style.maxHeight = '60vh';
  menu.style.overflow = 'auto';
  markAllRead();
}

function aboutDialog() {
  const s = app.session;
  infoDialog({
    title: 'このツールについて',
    width: 720,
    body: [
      h('dl', { class: 'kv' },
        h('dt', { text: '名前' }), h('dd', { text: 'FinFeT（Azure Blob Storage用検索・取得ツール）' }),
        h('dt', { text: '版' }), h('dd', { text: s.version }),
        h('dt', { text: 'データ保存先' }), h('dd', { text: `${s.data_dir}（${s.mode_label}）` }),
        h('dt', { text: 'ライセンス' }), h('dd', null, 'MIT License（', h('a', { href: '/docs/licenses.html', target: '_blank', rel: 'noopener', text: '同梱のソフトウェアのライセンス' }), '）')),
      h('p', { class: 'muted', style: { marginTop: '16px' } }, '本ツールは、Azure Blob Storage を利用するための非公式のツールです。Microsoft Corporation およびその関連会社が開発・提供するものではなく、Microsoft による提携、承認、後援を受けていません。Microsoft、Azure は、Microsoft グループの企業の商標です。'),
      h('p', { class: 'muted' }, 'This is an unofficial tool for Azure Blob Storage. It is not developed, provided, endorsed, or sponsored by Microsoft Corporation or its affiliates. Microsoft and Azure are trademarks of the Microsoft group of companies.'),
    ],
  });
}

/** 本ツールを終了します（2.6.4）。 */
export async function shutdownApp() {
  let res = await api.post('/api/v1/app/shutdown', {});
  if (res && res.confirm_required) {
    const ok = await confirmDialog({
      title: '本ツールを終了しますか',
      body: [h('p', { text: '実行中のジョブを一時停止してから終了します。次に起動したときに再開できます。' }),
        h('ul', null, res.running.map((n) => h('li', { text: n })))],
      okText: '終了する',
    });
    if (!ok) return;
    res = await api.post('/api/v1/app/shutdown', { confirm: true });
  }
  guard = null;
  const root = pageEl();
  clear(root);
  root.append(h('h1', { text: '本ツールを終了しました' }), h('p', { text: 'このタブを閉じてください。もう一度使うときは、finfet-abs.bat を実行します。' }));
  document.title = '終了しました - FinFeT';
}

/** アカウントのメニュー（4.8.2）。 */
async function accountMenu(anchor) {
  const auth = app.auth;
  const items = [];
  if (auth && auth.signed_in) {
    items.push(h('div', { class: 'menu-text' }, h('div', { style: { color: 'var(--text)' }, text: auth.account?.name || '' }), h('div', { text: auth.account?.username || '' }), auth.account?.tenant_name ? h('div', { text: auth.account.tenant_name }) : null));
    items.push('sep');
    items.push({ text: '接続のページを開く', onClick: () => navigate('/connections') });
    items.push({ text: 'サインアウト', onClick: () => import('./pages/connections.js').then((m) => m.signOut()) });
  } else if (auth) {
    items.push({ text: 'サインイン', onClick: () => import('./parts/signin.js').then((m) => m.startSignIn()) });
  }
  if (items.length) items.push('sep');
  items.push({ text: 'このツールについて', onClick: aboutDialog });
  items.push({ text: '終了', onClick: () => shutdownApp().catch((e) => toast(e.message, { error: true })) });
  openMenu(anchor, items);
}

/** サインインの状態を表示に反映します。 */
export function setAuth(auth) {
  app.auth = auth;
  const btn = document.getElementById('account-btn');
  btn.textContent = auth && auth.signed_in ? (auth.account?.username || 'アカウント') : 'アカウント';
}

async function refreshAuth() {
  try {
    setAuth(await api.get('/api/v1/auth/status'));
  } catch {
    setAuth(null);
  }
}

// ---------------------------------------------------------------- 起動

async function boot() {
  try {
    app.session = await api.initSession();
  } catch (e) {
    clear(pageEl());
    pageEl().append(h('h1', { text: 'FinFeT' }), h('p', { text: e.status === 401 ? '本ツールの画面を開くには、finfet-abs.bat をもう一度実行してください。' : e.message }));
    return;
  }
  const s = app.session;
  document.getElementById('demo-label').hidden = !s.demo;
  setupNav();
  setNoticeHandler(updateNoticeButton);
  document.getElementById('notices-btn').addEventListener('click', (e) => showNotices(e.currentTarget));
  document.getElementById('account-btn').addEventListener('click', (e) => accountMenu(e.currentTarget));
  events.setConnectionHandler((ok) => { document.getElementById('conn-lost').hidden = ok; });
  events.on('notify', (d) => {
    addNotice(d.level || 'info', d.text, d.url);
    if (app.session?.notifications && 'Notification' in window && Notification.permission === 'granted' && document.hidden) {
      try { new Notification('FinFeT', { body: d.text }); } catch { /* 通知できない環境 */ }
    }
  });
  events.on('auth.changed', (d) => setAuth(d));
  events.on('settings.changed', async (d) => {
    if ((d.keys || []).some((k) => k.startsWith('ui.'))) {
      try { app.settings = (await api.get('/api/v1/settings')).values; } catch { /* 次の機会に */ }
    }
  });
  events.on('nav.counts', (d) => {
    const el = document.getElementById('nav-jobs-count');
    el.textContent = d.jobs_attention ? String(d.jobs_attention) : '';
  });
  document.getElementById('nav-jobs-count').textContent = s.jobs_attention ? String(s.jobs_attention) : '';
  for (const w of s.warnings || []) addNotice('warn', w);
  try { app.settings = (await api.get('/api/v1/settings')).values; } catch { app.settings = {}; }
  refreshAuth();
  events.start(s.events_last);
  await renderCurrent();
}

boot();
