// 変数の行（4.4.3）。種類によって、値の指定の欄の形が変わります。

import { addButton, button, clear, h, iconButton, linkButton, removeButton } from '../dom.js';
import { openDialog } from './dialog.js';
import { checkbox, select, setError, textInput } from './form.js';
import { openMenu } from './menu.js';

const CHAR_CLASSES = [['digit', '数字'], ['lower', '英小文字'], ['upper', '英大文字'], ['symbol', '記号'], ['space', '空白'],
  ['hiragana', 'ひらがな'], ['katakana', 'カタカナ'], ['kanji', '漢字']];
const UNIT_NAMES = [['year', '年', 'YYYY|YY|year'], ['month', '月', 'MM|M|month'], ['day', '日', 'DD|D|day'],
  ['hour', '時', 'hh|h|hour'], ['minute', '分', 'mm|m|minute'], ['second', '秒', 'ss|s|second']];

/**
 * 値の「*」の書き方（v1.6.0）: 文字の「*」を「\*」にします。Azure から取得した候補と、v1.5 までの値に使います。
 * この書き方で特別な意味を持つのは「\*」の組だけです（「\」の後が「*」でなければ、「\」は文字のまま）。
 * そのため「\」自体を書き換える必要はありません（例: 文字の「\*」は「\\*」になり、「\」と文字の「*」として読まれます）。
 */
export function escapeWildcard(x) { return String(x).split('*').join('\\*'); }

/** v1.5 までの一致方法（exact・prefix・partial）の値を、「*」の書き方に直します（読み込んだときに 1 回だけ）。 */
export function toWildcardValues(v) {
  if (!v || v.mode !== 'one_of' || v.syntax === 'wildcard') return v;
  const m = v.match || 'exact';
  v.values = (v.values || []).map((it) => {
    const e = escapeWildcard(it.value);
    return { ...it, value: m === 'prefix' ? `${e}*` : m === 'partial' ? `*${e}*` : e };
  });
  v.syntax = 'wildcard';
  delete v.match;
  return v;
}

const WEEKDAYS = ['月', '火', '水', '木', '金', '土', '日'];

/** 書式の中の単位（日時の変数の書式から）。 */
function unitsInFormats(formats) {
  const out = new Set();
  for (const f of formats) {
    const alias = { year: 'YYYY', month: 'MM', day: 'DD', hour: 'hh', minute: 'mm', second: 'ss' }[f] || f;
    let s = alias;
    while (s.length) {
      const m = s.match(/^(YYYY|YY|MM|DD|hh|mm|ss|M|D|h|m|s)/);
      if (m) {
        const sym = m[1];
        const unit = { YYYY: 'year', YY: 'year', MM: 'month', M: 'month', DD: 'day', D: 'day', hh: 'hour', h: 'hour', mm: 'minute', m: 'minute', ss: 'second', s: 'second' }[sym];
        out.add(unit);
        s = s.slice(sym.length);
      } else {
        s = s.slice(1);
      }
    }
  }
  return UNIT_NAMES.filter(([u]) => out.has(u));
}

/**
 * 自動生成で分かった期間（パス上の日時。`2026-11-01T00:00:00`）を、期間の入力の基準の文字（`2026-11-01 00:00`）にします。
 * @param {{first: string, last: string}} found
 * @param {string} pathTz 'utc' / 'local'
 * @param {string} inputTz 'utc' / 'local'
 * @returns {{start: string, end: string}}
 */
export function periodFromFound(found, pathTz, inputTz) {
  const p = (x) => String(x).padStart(2, '0');
  const conv = (iso) => {
    const s = String(iso).slice(0, 19);
    if (pathTz === inputTz) return s.slice(0, 16).replace('T', ' ');
    const dt = pathTz === 'utc' ? new Date(`${s}Z`) : new Date(s);
    if (inputTz === 'utc') return `${dt.getUTCFullYear()}-${p(dt.getUTCMonth() + 1)}-${p(dt.getUTCDate())} ${p(dt.getUTCHours())}:${p(dt.getUTCMinutes())}`;
    return `${dt.getFullYear()}-${p(dt.getMonth() + 1)}-${p(dt.getDate())} ${p(dt.getHours())}:${p(dt.getMinutes())}`;
  };
  return { start: conv(found.first), end: conv(found.last) };
}

const pad2 = (x) => String(x).padStart(2, '0');
const range = (a, b) => { const out = []; for (let i = a; i <= b; i++) out.push(i); return out; };

/** 入力の基準（'local' / 'utc'）の今の壁時計の時刻。 */
function wallNow(tz) {
  const t = new Date();
  return tz === 'utc'
    ? { y: t.getUTCFullYear(), mo: t.getUTCMonth() + 1, d: t.getUTCDate(), h: t.getUTCHours() }
    : { y: t.getFullYear(), mo: t.getMonth() + 1, d: t.getDate(), h: t.getHours() };
}

/** 期間の入力の文字（`2026-11-20`、`2026/11/20 09:00`、`20261120` など）を、上位からの数の並びにします。 */
function wallParts(text) {
  const s = String(text || '').trim();
  let m = s.match(/^(\d{4})(?:[-/](\d{1,2})(?:[-/](\d{1,2})(?:[ T](\d{1,2})(?::(\d{1,2})(?::(\d{1,2}))?)?)?)?)?$/);
  if (m) return m.slice(1).filter((x) => x !== undefined).map(Number);
  m = s.match(/^\d{4}(?:\d{2}){0,5}$/);
  if (m) {
    const out = [Number(s.slice(0, 4))];
    for (let i = 4; i < s.length; i += 2) out.push(Number(s.slice(i, i + 2)));
    return out;
  }
  return null;
}

/** 期間の端を、比べられる形（`2026-11-20 09:00`）にします。終了は、その単位の最後に寄せます。 */
function normPoint(text, isEnd) {
  const p = wallParts(text) || [];
  const y = p[0] ?? 0;
  const mo = p[1] ?? (isEnd ? 12 : 1);
  const last = new Date(Date.UTC(y, mo, 0)).getUTCDate();
  const d = p[2] ?? (isEnd ? last : 1);
  const hh = p[3] ?? (isEnd ? 23 : 0);
  const mi = p[4] ?? (isEnd ? 59 : 0);
  return `${String(y).padStart(4, '0')}-${pad2(mo)}-${pad2(d)} ${pad2(hh)}:${pad2(mi)}`;
}

/** パスの書式の最も細かい単位から、期間の入力の部品の種類を決めます。 */
function inputKindOf(finest) {
  if (finest === 'year') return { type: 'number', unit: 'year' };
  if (finest === 'month') return { type: 'month', unit: 'month' };
  if (finest === 'day') return { type: 'date', unit: 'day' };
  if (finest === 'hour') return { type: 'datetime-local', unit: 'hour', step: '3600' };
  if (finest === 'minute') return { type: 'datetime-local', unit: 'minute', step: '60' };
  return { type: 'datetime-local', unit: 'second', step: '1' };
}

/** 保存している期間の文字を、入力の部品の値にします（足りない単位は、開始は最初・終了は最後で補います）。 */
function toInputValue(text, kind, isEnd) {
  const p = wallParts(text);
  if (!p) return '';
  const y = String(p[0]).padStart(4, '0');
  if (kind.unit === 'year') return y;
  const mo = p[1] ?? (isEnd ? 12 : 1);
  if (kind.unit === 'month') return `${y}-${pad2(mo)}`;
  const last = new Date(Date.UTC(p[0], mo, 0)).getUTCDate();
  const d = p[2] ?? (isEnd ? last : 1);
  if (kind.unit === 'day') return `${y}-${pad2(mo)}-${pad2(d)}`;
  const hh = p[3] ?? (isEnd ? 23 : 0);
  const mi = kind.unit === 'hour' ? 0 : (p[4] ?? (isEnd ? 59 : 0));
  const base = `${y}-${pad2(mo)}-${pad2(d)}T${pad2(hh)}:${pad2(mi)}`;
  return kind.unit === 'second' ? `${base}:${pad2(p[5] ?? (isEnd ? 59 : 0))}` : base;
}

/** 入力の部品の値を、保存する期間の文字（`2026-11-20 09:00` など）にします。 */
function fromInputValue(value, kind) {
  const s = String(value || '').trim();
  if (!s) return '';
  if (kind.type === 'datetime-local') {
    const t = s.replace('T', ' ');
    return kind.unit === 'second' && t.length === 16 ? `${t}:00` : (kind.unit === 'second' ? t : t.slice(0, 16));
  }
  return s;
}

/**
 * ボタンを並べた切り替え（どれか 1 つを選ぶ）。
 * @param {[string, string][]} options [値, 表示]
 */
function segmented(options, value, label, onPick) {
  const wrap = h('div', { class: 'seg', role: 'group', 'aria-label': label });
  for (const [k, t] of options) {
    wrap.append(h('button', { type: 'button', text: t, 'aria-pressed': String(k === value), onclick: () => onPick(k) }));
  }
  return wrap;
}

/**
 * 変数の行を描きます。
 * @param {Object} ctx
 * @param {string} ctx.name
 * @param {Object} ctx.def 変数の定義（5.5.2。この関数の中で書き換えます）
 * @param {boolean} ctx.inPattern
 * @param {string[]} ctx.formats 日時の変数の書式の一覧
 * @param {string} ctx.inputTimezone
 * @param {Object|null} ctx.dtInfo パターンの解析の結果（変換後の期間）
 * @param {() => void} ctx.onChange
 * @param {(tz: string) => void} ctx.onInputTimezone
 * @param {() => void} ctx.onRemove
 * @param {() => Promise<string[]>} [ctx.fetchCandidates] [Azure から候補を取得]
 * @param {Object|null} ctx.shiftHint [今日に合わせて移動] の提案
 */
export function variableRow(ctx) {
  const d = ctx.def;
  d.value = d.value || { mode: 'any' };
  const changed = () => { ctx.def.touched = true; ctx.onChange(); };
  const shown = {}; // 入力中に差し替える表示（検索する期間・期間の帯）
  const nameCell = h('div', { class: 'var-name' }, h('span', { class: 'mono', text: ctx.name }), d.label ? h('span', { class: 'muted', text: ` ${d.label}` }) : null);
  const body = h('div', { class: 'var-body' });
  const side = h('div', { class: 'var-side' });
  const row = h('div', { class: `var-row${ctx.inPattern ? '' : ' var-unused'}` }, nameCell, body, side);

  const menuItems = [{ text: '表示名を付ける', onClick: () => editLabel() }];
  if (d.type === 'datetime') {
    menuItems.push({ text: d.path_timezone === 'local' ? 'パス上の日時の基準を UTC にする' : 'パス上の日時の基準をローカル時刻にする', onClick: () => { d.path_timezone = d.path_timezone === 'local' ? 'utc' : 'local'; changed(); } });
  }
  side.append(iconButton('more', '変数の操作', (e) => openMenu(e.currentTarget, menuItems)));
  if (!ctx.inPattern) {
    side.prepend(h('span', { class: 'muted', text: 'パスパターンに含まれていません' }), iconButton('close', '変数を削除', ctx.onRemove));
  }

  function editLabel() {
    const inp = textInput(d.label || '', { width: 'w-mid', 'aria-label': '表示名' });
    const dlg = openDialog({ title: `表示名（${ctx.name}）`, width: 480, body: inp });
    const ok = () => { d.label = inp.value.trim() || undefined; dlg.close(true); changed(); };
    inp.addEventListener('keydown', (e) => { if (e.key === 'Enter') ok(); });
    dlg.setFoot(h('span.push'), button('キャンセル', () => dlg.close(true)), button('決定', ok, { primary: true }));
    inp.focus();
  }

  const typeSel = d.type === 'datetime' ? h('span', { class: 'var-type', text: '日時' }) : select([['string', '文字列'], ['number', '数値'], ['choice', '選択肢']], d.type || 'string', {
    'aria-label': `${ctx.name} の種類`,
    onchange: (e) => {
      const t = e.target.value;
      // 「次のうちいずれか」の値は、選択肢に移します（逆も同じ）
      const prevVals = d.type === 'choice' ? (d.value.options || []) : d.value.mode === 'one_of' ? (d.value.values || []).map((x) => x.value) : [];
      d.type = t;
      if (t === 'choice') d.value = { options: [...prevVals], selected: [...prevVals] };
      else if (t === 'string' && prevVals.length) d.value = { mode: 'one_of', syntax: 'wildcard', values: prevVals.map((x) => ({ value: escapeWildcard(x), enabled: true })) };
      else d.value = { mode: 'any' };
      changed();
    },
  });

  if (d.type === 'datetime') drawDatetime();
  else if (d.type === 'number') drawNumber();
  else if (d.type === 'choice') drawChoice();
  else drawString();

  function numIn(v, label, onv, cls = 'w-num') {
    const el = textInput(v ?? '', { width: cls, 'aria-label': label, inputmode: 'numeric' });
    el.classList.add('num');
    el.addEventListener('change', () => { onv(el.value.trim() === '' ? null : Number(el.value)); changed(); });
    return el;
  }

  // 除く値（v1.6.0）: 一致した Blob のうち、変数の部分がこの値のものを対象から外します
  function drawExclude(v, numeric) {
    if (!Array.isArray(v.exclude)) {
      body.append(h('div', { class: 'line' }, linkButton('除く値を指定', () => { v.exclude = []; changed(); })));
      return;
    }
    const chips = h('div', { class: 'chips ex-chips' });
    const drawChips = () => {
      clear(chips);
      v.exclude.forEach((it, i) => {
        const cb = h('input', { type: 'checkbox', checked: it.enabled !== false, 'aria-label': `${it.value} を除く`, onchange: (e) => { it.enabled = e.target.checked; drawChips(); changed(); } });
        chips.append(h('span', { class: `chip${it.enabled === false ? ' off' : ''}` }, cb, it.value, iconButton('close', `${it.value} を削除`, () => { v.exclude.splice(i, 1); drawChips(); changed(); })));
      });
    };
    drawChips();
    const inp = textInput('', { width: 'w-mid', placeholder: numeric ? '除く数値を入力' : '除く値を入力', 'aria-label': `${ctx.name} の除く値`, inputmode: numeric ? 'numeric' : undefined });
    const add = (vals) => {
      for (const x of vals.map((y) => y.trim()).filter(Boolean)) {
        if (!v.exclude.some((y) => y.value === x)) v.exclude.push({ value: x, enabled: true });
      }
      drawChips();
      changed();
    };
    inp.addEventListener('keydown', (e) => { if (e.key === 'Enter') { e.preventDefault(); add([inp.value]); inp.value = ''; } });
    inp.addEventListener('paste', (e) => {
      const t = e.clipboardData.getData('text');
      if (t.includes('\n')) { e.preventDefault(); add(t.split(/\r?\n/)); }
    });
    const remove = linkButton('除く値を使わない', () => { delete v.exclude; changed(); });
    body.append(h('div', { class: 'line' }, h('span', { class: 'field-label', text: '除く値' }), chips, inp, remove),
      h('div', { class: 'muted', text: numeric ? 'この数値の Blob は、検索結果に含まれません。'
        : 'この値の Blob は、検索結果に含まれません。「*」は任意の文字列を表します（例: test*）。' }));
  }

  function drawString() {
    const v = toWildcardValues(d.value);
    const modeSel = select([['any', '任意'], ['one_of', '次のうちいずれか'], ['chars', '条件に合う文字列']], v.mode, {
      'aria-label': `${ctx.name} の値の指定`,
      onchange: (e) => {
        const m = e.target.value;
        const ex = d.value.exclude;
        d.value = m === 'one_of' ? { mode: 'one_of', syntax: 'wildcard', values: [] }
          : m === 'chars' ? { mode: 'chars', classes: ['digit', 'lower'], extra: '', allow_slash: false, min: 1, max: null } : { mode: 'any' };
        if (ex) d.value.exclude = ex;
        changed();
      },
    });
    const line = h('div', { class: 'line' }, typeSel, modeSel);
    body.append(line);
    if (v.mode === 'one_of') {
      v.values = v.values || [];
      const chips = h('div', { class: 'chips' });
      const drawChips = () => {
        clear(chips);
        v.values.forEach((it, i) => {
          const cb = h('input', { type: 'checkbox', checked: it.enabled !== false, 'aria-label': `${it.value} を対象にする`, onchange: (e) => { it.enabled = e.target.checked; drawChips(); changed(); } });
          chips.append(h('span', { class: `chip${it.enabled === false ? ' off' : ''}` }, cb, it.value, iconButton('close', `${it.value} を削除`, () => { v.values.splice(i, 1); drawChips(); changed(); })));
        });
      };
      drawChips();
      const inp = textInput('', { width: 'w-mid', placeholder: '値を入力', 'aria-label': `${ctx.name} の値` });
      const add = (vals) => {
        for (const x of vals.map((s) => s.trim()).filter(Boolean)) {
          if (!v.values.some((y) => y.value === x)) v.values.push({ value: x, enabled: true });
        }
        drawChips();
        changed();
      };
      inp.addEventListener('keydown', (e) => {
        if (e.key === 'Enter') { e.preventDefault(); add([inp.value]); inp.value = ''; }
      });
      inp.addEventListener('paste', (e) => {
        const t = e.clipboardData.getData('text');
        if (t.includes('\n')) { e.preventDefault(); add(t.split(/\r?\n/)); }
      });
      const row2 = h('div', { class: 'line' }, chips, inp);
      // Azure から取得した値の「*」は、文字の「*」として扱います
      if (ctx.fetchCandidates) row2.append(linkButton('Azure から候補を取得', () => candidates((vals) => add(vals.map(escapeWildcard)))));
      body.append(row2, h('div', { class: 'muted', text: '「*」は任意の文字列を表します（例: app* は app で始まる値）。文字の「*」は「\\*」と入力します。' }));
    } else if (v.mode === 'chars') {
      v.classes = v.classes || [];
      const boxes = CHAR_CLASSES.map(([k, label]) => checkbox(label, v.classes.includes(k), (on) => {
        v.classes = on ? [...new Set([...v.classes, k])] : v.classes.filter((x) => x !== k);
        changed();
      }));
      const extra = textInput(v.extra || '', { width: 'w-short', placeholder: '-_.', 'aria-label': '指定した文字', mono: true });
      extra.addEventListener('change', () => { v.extra = extra.value; changed(); });
      body.append(h('div', { class: 'line' }, ...boxes),
        h('div', { class: 'line' }, h('span', { class: 'labeled' }, h('span', { text: '指定した文字' }), extra),
          checkbox('/ を含める', !!v.allow_slash, (on) => { v.allow_slash = on; changed(); }),
          h('span', { class: 'labeled' }, h('span', { text: '長さ' }), numIn(v.min, '最小の長さ', (n) => { v.min = n; }), h('span', { text: '〜' }), numIn(v.max, '最大の長さ', (n) => { v.max = n; }))));
    }
    drawExclude(v, false);
  }

  // 選択肢（依頼者の依頼。decisions.md）: 選択肢の一覧から、検索する値を選びます。複数選択とキーワードでの絞り込み
  function drawChoice() {
    const v = d.value;
    v.options = v.options || [];
    v.selected = v.selected || [];
    let filter = ctx.choiceFilter?.get?.() || '';
    // 選択の変更では行を描き直しません（絞り込みと一覧のスクロールの位置を保つため）
    const quiet = () => { d.touched = true; (ctx.onQuietChange || ctx.onChange)(); };
    const count = h('span', { class: 'muted' });
    const list = h('div', { class: 'choice-list', role: 'group', 'aria-label': `${ctx.name} の選択肢` });
    const filterIn = textInput(filter, { width: 'w-mid', placeholder: 'キーワードで絞り込み', 'aria-label': `${ctx.name} の選択肢の絞り込み`, type: 'search' });
    let onlySelected = false;
    const shown = () => {
      const sel = new Set(v.selected);
      return v.options.filter((o) => (!filter || o.toLowerCase().includes(filter.toLowerCase())) && (!onlySelected || sel.has(o)));
    };
    const drawList = () => {
      clear(list);
      const sel = new Set(v.selected);
      const items = shown();
      for (const o of items) {
        const cb = checkbox(o, sel.has(o), (on) => {
          v.selected = on ? [...new Set([...v.selected, o])] : v.selected.filter((x) => x !== o);
          drawCount();
          quiet();
        });
        cb.classList.add('choice-item');
        if ((d.new_options || []).includes(o)) cb.classList.add('is-new');
        cb.title = o;
        list.append(cb);
      }
      if (!v.options.length) list.append(h('div', { class: 'muted choice-empty', text: '選択肢がありません。値を入力して追加してください。' }));
      else if (!items.length) list.append(h('div', { class: 'muted choice-empty', text: onlySelected && !filter ? '選択されている選択肢はありません。' : '絞り込みに一致する選択肢はありません。' }));
      drawCount();
    };
    const drawCount = () => {
      count.textContent = `${v.selected.length} / ${v.options.length} 件を選択` + (filter || onlySelected ? `（${shown().length} 件を表示）` : '');
    };
    filterIn.addEventListener('input', () => { filter = filterIn.value.trim(); ctx.choiceFilter?.set?.(filter); drawList(); });
    const setShown = (on) => {
      const items = new Set(shown());
      v.selected = on ? [...new Set([...v.selected, ...items])] : v.selected.filter((x) => !items.has(x));
      drawList();
      quiet();
    };
    const addIn = textInput('', { width: 'w-mid', placeholder: '選択肢を追加', 'aria-label': `${ctx.name} の選択肢を追加` });
    const addValues = (vals) => {
      for (const x of vals.map((y) => y.trim()).filter(Boolean)) {
        if (!v.options.includes(x)) v.options.push(x);
        if (!v.selected.includes(x)) v.selected.push(x);
      }
      drawList();
      quiet();
    };
    addIn.addEventListener('keydown', (e) => { if (e.key === 'Enter') { e.preventDefault(); addValues([addIn.value]); addIn.value = ''; } });
    addIn.addEventListener('paste', (e) => {
      const t = e.clipboardData.getData('text');
      if (t.includes('\n')) { e.preventDefault(); addValues(t.split(/\r?\n/)); }
    });
    const onlySel = checkbox('選択した項目のみ表示', false, (on) => { onlySelected = on; drawList(); });
    body.append(h('div', { class: 'line' }, typeSel, filterIn, linkButton('表示中の項目をすべて選択', () => setShown(true)),
      linkButton('表示中の項目の選択を解除', () => setShown(false)), onlySel, count));
    body.append(list);
    const foot = h('div', { class: 'line' }, addIn);
    if (ctx.fetchCandidates) foot.append(linkButton('Azure から候補を取得', () => candidates(addValues)));
    body.append(foot);
    if (d.from_sample) {
      body.append(caveat('クイックスキャンでは一部の Blob のみを確認したため、ここに表示されていない値を持つ Blob が存在する場合があります。表示されていない値の Blob は、検索結果に含まれません。'));
    }
    drawList();
  }

  // クイックスキャンの結果の注意書き（v1.6.0）。[すべての Blob を確認] で、値や期間を求め直します
  function caveat(text) {
    const box = h('div', { class: 'notice' }, h('p', { text }));
    if (ctx.scanAll) {
      const st = h('span', { class: 'muted' });
      const btn = button('すべての Blob を確認', async () => {
        btn.disabled = true;
        st.textContent = '確認しています…';
        try { await ctx.scanAll(); } catch (e) { st.textContent = e.message; btn.disabled = false; }
      });
      box.append(h('div', { class: 'line' }, btn, st));
    }
    return box;
  }

  async function candidates(add) {
    const box = h('div', { class: 'folder-list' }, h('div', { class: 'group', text: '読み込み中…' }));
    const dlg = openDialog({ title: `候補（${ctx.name}）`, width: 720, body: box });
    const picked = new Set();
    dlg.setFoot(h('span.push'), button('キャンセル', () => dlg.close(true)), button('追加', () => { add([...picked]); dlg.close(true); }, { primary: true }));
    try {
      const vals = await ctx.fetchCandidates();
      clear(box);
      if (!vals.length) box.append(h('div', { class: 'group', text: '候補が見つかりませんでした。' }));
      for (const x of vals) {
        const cb = checkbox(x, false, (on) => { if (on) picked.add(x); else picked.delete(x); });
        cb.classList.add('folder-check');
        box.append(cb);
      }
    } catch (e) {
      clear(box);
      box.append(h('div', { class: 'group danger', text: e.message }));
    }
  }

  function drawNumber() {
    const v = d.value;
    const modeSel = select([['any', '任意'], ['fixed', '固定'], ['range', '範囲']], v.mode, {
      'aria-label': `${ctx.name} の値の指定`,
      onchange: (e) => {
        const ex = d.value.exclude;
        d.value = e.target.value === 'range' ? { mode: 'range', start: 1, end: 10, step: 1 } : e.target.value === 'fixed' ? { mode: 'fixed', value: 0 } : { mode: 'any' };
        if (ex && e.target.value !== 'fixed') d.value.exclude = ex;
        changed();
      },
    });
    const line = h('div', { class: 'line' }, typeSel, modeSel);
    if (v.mode === 'fixed') line.append(numIn(v.value, '値', (n) => { v.value = n; }));
    if (v.mode === 'range') {
      line.append(h('span', { class: 'labeled' }, numIn(v.start, '開始', (n) => { v.start = n; }), h('span', { text: '〜' }), numIn(v.end, '終了', (n) => { v.end = n; })),
        h('span', { class: 'labeled' }, h('span', { text: '間隔' }), numIn(v.step ?? 1, '間隔', (n) => { v.step = n; })));
    }
    line.append(h('span', { class: 'labeled' }, h('span', { text: '桁数' }), numIn(d.padding, '桁数（ゼロ埋め）', (n) => { d.padding = n || undefined; })));
    body.append(line);
    if (v.mode !== 'fixed') drawExclude(v, true);
  }

  // 日時の変数（依頼者の意見で作り直し。decisions.md）。指定の方法はタブで切り替え、期間は日付・時刻の部品で入力します。
  // データの形（value の mode・start・end の文字など。5.5.3）は変えません。
  function drawDatetime() {
    const v = d.value;
    const tz = ctx.inputTimezone;
    const units = unitsInFormats(ctx.formats);
    const finest = units.length ? units[units.length - 1][0] : 'hour';
    const kind = inputKindOf(finest);
    const found = d.found && d.found.first && d.found.last
      ? { ...periodFromFound(d.found, d.path_timezone || 'utc', tz), complete: d.found.complete } : null;

    const modeSeg = segmented([['period', '期間'], ['relative', '相対'], ['units', '単位ごと'], ['any', 'すべて']], v.mode || 'any', `${ctx.name} の指定の方法`, (m) => {
      if (m === v.mode) return;
      const days = v.days;
      d.value = m === 'relative' ? { mode: 'relative', kind: 'last_days', n: 7 }
        : m === 'period' ? (found && found.complete !== false ? { mode: 'period', start: found.start, end: found.end } : { mode: 'period', start: '', end: '' })
          : m === 'units' ? { mode: 'units', units: {} } : { mode: 'any' };
      if (days) d.value.days = days;
      changed();
    });
    const tzSeg = segmented([['local', 'ローカル時刻'], ['utc', 'UTC']], tz, '期間の入力の基準', (t) => { if (t !== tz) ctx.onInputTimezone(t); });
    body.append(h('div', { class: 'fields' },
      h('div', { class: 'field' }, h('span', { class: 'field-label', text: '種類' }), typeSel),
      h('div', { class: 'field' }, h('span', { class: 'field-label', text: '指定の方法' }), modeSeg),
      v.mode === 'any' ? null : h('div', { class: 'field' }, h('span', { class: 'field-label', text: '入力の基準' }), tzSeg)));

    if (v.mode === 'period') drawPeriod(v, kind, found, tz);
    else if (v.mode === 'relative') drawRelative(v);
    else if (v.mode === 'units') drawUnits(v, units, found);
    else body.append(h('div', { class: 'muted', text: '日時で絞り込みません。パスパターンに一致するすべての Blob を検索します。' }));

    if (v.mode !== 'any') drawSummary(v, found, tz);
    if (v.mode === 'period' || v.mode === 'relative') drawStep(v);
    drawDays(v, units);
    if (d.found && d.found.complete === false && found) {
      body.append(caveat(`クイックスキャンでは一部の Blob のみを確認したため、表示している期間（${found.start} 〜 ${found.end}）より前または後の日時の Blob が存在する場合があります。その場合、期間を既定のまま検索すると、それらの Blob は検索結果に含まれません。`));
    }
  }

  // 曜日・祝日（v1.6.0）。日は入力の基準で判定します。祝日は日本の祝日と、設定で追加した休日です
  function drawDays(v, units) {
    if (!units.some((u) => u[0] === 'day')) return;
    const days = v.days || { weekdays: [0, 1, 2, 3, 4, 5, 6], holidays: 'include' };
    const set = (next) => {
      const all = next.weekdays.length === 7 && next.holidays === 'include';
      if (all) delete v.days; else v.days = { weekdays: [...next.weekdays].sort(), holidays: next.holidays };
      changed();
    };
    const quick = h('div', { class: 'quick' });
    for (const [t, wd, hol] of [['すべての日', [0, 1, 2, 3, 4, 5, 6], 'include'], ['平日（祝日を除く）', [0, 1, 2, 3, 4], 'exclude'], ['土日・祝日', [5, 6], 'add'], ['土日', [5, 6], 'include']]) {
      quick.append(h('button', { type: 'button', class: 'quick-btn', text: t, onclick: () => set({ weekdays: wd, holidays: hol }) }));
    }
    const boxes = WEEKDAYS.map((w, i) => checkbox(w, days.weekdays.includes(i), (on) => {
      set({ ...days, weekdays: on ? [...new Set([...days.weekdays, i])] : days.weekdays.filter((x) => x !== i) });
    }));
    const hol = select([['include', '祝日を区別しない'], ['exclude', '祝日を除く'], ['add', '祝日も含める'], ['only', '祝日のみ']], days.holidays, {
      'aria-label': '祝日の扱い', onchange: (e) => set({ ...days, holidays: e.target.value }),
    });
    body.append(h('div', { class: 'field' }, h('span', { class: 'field-label', text: '曜日・祝日' }),
      h('div', null, quick, h('div', { class: 'line' }, ...boxes, hol),
        v.days ? h('div', { class: 'muted', text: '日付は入力の基準（ローカル時刻または UTC）で判定します。祝日は日本の祝日です。会社の休日は、設定で追加できます。' }) : null)));
  }

  function drawPeriod(v, kind, found, tz) {
    // すぐに入力（入力の基準の今日を使います）
    const quick = h('div', { class: 'quick' });
    const setPeriod = (start, end) => { v.start = start; v.end = end; changed(); };
    const now = wallNow(tz);
    const ymd = (o) => { const x = new Date(Date.UTC(now.y, now.mo - 1, now.d + o)); return `${x.getUTCFullYear()}-${pad2(x.getUTCMonth() + 1)}-${pad2(x.getUTCDate())}`; };
    const ym = (o) => { const x = new Date(Date.UTC(now.y, now.mo - 1 + o, 1)); return `${x.getUTCFullYear()}-${pad2(x.getUTCMonth() + 1)}`; };
    const presets = [['今日', () => setPeriod(ymd(0), ymd(0))], ['昨日', () => setPeriod(ymd(-1), ymd(-1))],
      ['直近 7 日', () => setPeriod(ymd(-6), ymd(0))], ['今月', () => setPeriod(ym(0), ym(0))], ['先月', () => setPeriod(ym(-1), ym(-1))]];
    if (found) presets.push(['Blob が存在する期間', () => setPeriod(found.start, found.end)]);
    for (const [t, fn] of presets) quick.append(h('button', { type: 'button', class: 'quick-btn', text: t, onclick: fn }));
    if (ctx.shiftHint) quick.append(h('button', { type: 'button', class: 'quick-btn', text: '今日に合わせて移動', onclick: ctx.shiftHint }));
    body.append(h('div', { class: 'field' }, h('span', { class: 'field-label', text: 'すぐに入力' }), quick));

    // 年を 4 桁までにします（上限がないと、Chrome は年に 6 桁まで入れられ、4 桁を打っても月に進みません。v1.7.2）
    const MAX = { number: '9999', month: '9999-12', date: '9999-12-31', 'datetime-local': '9999-12-31T23:59:59' };
    const limit = found && found.complete !== false ? { min: toInputValue(found.start, kind, false), max: toInputValue(found.end, kind, true) } : { max: MAX[kind.type] };
    const mk = (val, isEnd, label) => {
      const el = h('input', { type: kind.type, class: 'input dt-input', value: toInputValue(val, kind, isEnd), 'aria-label': label, ...limit });
      if (kind.step) el.step = kind.step;
      el.addEventListener('change', () => {
        const x = fromInputValue(el.value, kind);
        if (isEnd) v.end = x; else v.start = x;
        changed();
      });
      return el;
    };
    body.append(h('div', { class: 'fields' },
      h('label', { class: 'field grow' }, h('span', { class: 'field-label', text: '開始' }), mk(v.start, false, '期間の開始')),
      h('label', { class: 'field grow' }, h('span', { class: 'field-label', text: '終了（この日時を含む）' }), mk(v.end, true, '期間の終了'))));

    // 追加の期間（v1.5.0。依頼者の依頼: 2026/01/01〜03/31 と 05/01〜06/30 のように、複数の期間を指定する）。
    // どれかの期間に入る Blob を検索します。データは value.more = [{start, end}, …]
    const more = Array.isArray(v.more) ? v.more : [];
    more.forEach((p, i) => {
      const mkMore = (isEnd, label) => {
        const el = h('input', { type: kind.type, class: 'input dt-input', value: toInputValue(isEnd ? p.end : p.start, kind, isEnd), 'aria-label': label, ...limit });
        if (kind.step) el.step = kind.step;
        el.addEventListener('change', () => {
          const x = fromInputValue(el.value, kind);
          if (isEnd) p.end = x; else p.start = x;
          changed();
        });
        return el;
      };
      body.append(h('div', { class: 'fields more-period' },
        h('label', { class: 'field grow' }, h('span', { class: 'field-label', text: `開始（${i + 2} つ目の期間）` }), mkMore(false, `${i + 2} つ目の期間の開始`)),
        h('label', { class: 'field grow' }, h('span', { class: 'field-label', text: '終了（この日時を含む）' }), mkMore(true, `${i + 2} つ目の期間の終了`)),
        h('div', { class: 'field' }, h('span', { class: 'field-label', text: ' ' }),
          removeButton('この期間を削除', () => { more.splice(i, 1); if (more.length) v.more = more; else delete v.more; changed(); }))));
    });
    if (more.length < 19) {
      body.append(h('div', { class: 'line' }, addButton('期間を追加', () => { v.more = [...more, { start: '', end: '' }]; changed(); }),
        more.length ? h('span', { class: 'muted', text: 'いずれかの期間に含まれる Blob を検索します。' }) : null));
    }
    if (found) {
      shown.barArgs = [v, found, tz];
      shown.bar = periodBar(v, found, tz);
      body.append(shown.bar);
    }
  }

  function drawRelative(v) {
    const kinds = [['today', '今日'], ['yesterday', '昨日'], ['last_days', '直近 n 日'], ['last_hours', '直近 n 時間'],
      ['this_month', '今月'], ['last_month', '先月'], ['days_ago', 'n 日前〜m 日前']];
    const quick = h('div', { class: 'quick', role: 'group', 'aria-label': '相対的な期間' });
    for (const [k, t] of kinds) {
      quick.append(h('button', {
        type: 'button', class: 'quick-btn', text: t, 'aria-pressed': String((v.kind || 'last_days') === k),
        onclick: () => { v.kind = k; if ((k === 'last_days' || k === 'last_hours') && !v.n) v.n = 7; if (k === 'days_ago') { v.from = v.from ?? 7; v.to = v.to ?? 1; } changed(); },
      }));
    }
    body.append(h('div', { class: 'field' }, h('span', { class: 'field-label', text: '期間（検索のたびに、検索時点の日時を基準に計算します）' }), quick));
    if (v.kind === 'last_days' || v.kind === 'last_hours') {
      body.append(h('div', { class: 'field' }, h('span', { class: 'field-label', text: v.kind === 'last_days' ? '日数' : '時間数' }),
        stepper(v.n ?? 7, 1, v.kind === 'last_days' ? 3660 : 87840, v.kind === 'last_days' ? '日数' : '時間数', (n) => { v.n = n; changed(); })));
    } else if (v.kind === 'days_ago') {
      body.append(h('div', { class: 'fields' },
        h('div', { class: 'field' }, h('span', { class: 'field-label', text: '何日前から' }), stepper(v.from ?? 7, 0, 3660, '何日前から', (n) => { v.from = n; changed(); })),
        h('div', { class: 'field' }, h('span', { class: 'field-label', text: '何日前まで' }), stepper(v.to ?? 1, 0, 3660, '何日前まで', (n) => { v.to = n; changed(); }))));
    }
  }

  function drawUnits(v, units, found) {
    v.units = v.units || {};
    const grid = h('div', { class: 'units-grid' });
    const fa = found ? wallParts(found.start) : null;
    const fb = found ? wallParts(found.end) : null;
    const now = wallNow(ctx.inputTimezone);
    for (const [u, label] of units) {
      const spec = v.units[u] || { mode: 'all' };
      v.units[u] = spec;
      let vals;
      if (u === 'year') vals = fa ? range(fa[0], fb[0]) : range(now.y - 10, now.y + 1);
      else if (u === 'month') vals = fa && fa[0] === fb[0] && found.complete !== false ? range(fa[1], fb[1]) : range(1, 12);
      else if (u === 'day') vals = range(1, 31);
      else if (u === 'hour') vals = range(0, 23);
      else vals = range(0, 59);
      const cur = spec.mode === 'fixed' ? [spec.value, spec.value] : spec.mode === 'range' ? [spec.start, spec.end] : null;
      if (cur) for (const x of cur) if (x != null && !vals.includes(x)) vals = [...vals, x].sort((p, q) => p - q);
      const on = spec.mode !== 'all';
      const seg = segmented([['all', 'すべて'], ['pick', '指定']], on ? 'pick' : 'all', `${label}の指定`, (m) => {
        if (m === 'all') v.units[u] = { mode: 'all' };
        else if (!on) v.units[u] = { mode: 'fixed', value: vals[vals.length - 1] };
        changed();
      });
      const card = h('div', { class: 'unit-card' }, h('div', { class: 'unit-head' }, h('strong', { text: label }), seg));
      if (on) {
        const opts = vals.map((x) => [String(x), String(x)]);
        const set = (a, b) => {
          const step = spec.mode === 'range' ? spec.step : undefined;
          v.units[u] = a === b ? { mode: 'fixed', value: a } : { mode: 'range', start: Math.min(a, b), end: Math.max(a, b), step: step || 1 };
          changed();
        };
        const sa = select(opts, String(cur[0]), { 'aria-label': `${label}の開始`, onchange: (e) => set(Number(e.target.value), cur[1]) });
        const sb = select(opts, String(cur[1]), { 'aria-label': `${label}の終了`, onchange: (e) => set(cur[0], Number(e.target.value)) });
        card.append(h('div', { class: 'pair' }, sa, h('span', { text: '〜' }), sb, h('span', { text: label })));
        if (spec.mode === 'range') {
          card.append(h('div', { class: 'pair' }, h('span', { class: 'muted', text: '間隔' }),
            stepper(spec.step ?? 1, 1, 999, `${label}の間隔`, (n) => { spec.step = n; changed(); })));
        }
      }
      let hint = '';
      if (u === 'year' && fa) hint = `Blob が存在する年: ${fa[0] === fb[0] ? fa[0] : `${fa[0]}〜${fb[0]}`}`;
      else if (u === 'month' && fa && fa[0] === fb[0]) hint = `Blob が存在する月: ${fa[1]}〜${fb[1]}`;
      if (on && spec.mode === 'fixed') hint = `${hint ? `${hint}。` : ''}同じ値を選択すると固定値、異なる値を選択すると範囲になります`;
      if (hint) card.append(h('span', { class: 'muted small', text: hint }));
      grid.append(card);
    }
    if (!units.length) grid.append(h('span', { class: 'muted', text: 'パスパターンの書式に、年・月・日などの単位が含まれていません。' }));
    body.append(grid);
  }

  function drawSummary(v, found, tz) {
    shown.summary = summaryBox(v, found, tz);
    shown.summaryArgs = [v, found, tz];
    body.append(shown.summary);
  }

  function summaryBox(v, found, tz) {
    const tzName = tz === 'utc' ? 'UTC' : 'ローカル時刻';
    const box = h('div', { class: 'dt-summary', 'aria-live': 'polite' });
    const info = ctx.dtInfo;
    if (info && info.input) {
      box.append(h('strong', { text: `検索する期間（${tzName}）: ${info.input}` }));
      if (!info.same_zone && info.path) box.append(h('span', { class: 'muted', text: `パス上の日時（${d.path_timezone === 'local' ? 'ローカル時刻' : 'UTC'}）: ${info.path}` }));
    } else if (v.mode === 'units') {
      const parts = Object.entries(v.units || {}).map(([u, s]) => {
        const l = UNIT_NAMES.find((x) => x[0] === u)?.[1] || u;
        return s.mode === 'fixed' ? `${s.value}${l}` : s.mode === 'range' ? `${s.start}〜${s.end}${l}${s.step > 1 ? `（${s.step} ごと）` : ''}` : `すべての${l}`;
      });
      box.append(h('strong', { text: `${parts.join('・')}（${tzName}）` }));
    } else {
      box.append(h('span', { class: 'muted', text: '期間を確認しています…' }));
    }
    if (v.mode === 'period' && found && found.complete !== false && v.start && v.end) {
      const s = normPoint(v.start, false);
      const e = normPoint(v.end, true);
      const fs = normPoint(found.start, false);
      const fe = normPoint(found.end, true);
      if (e < fs || s > fe) box.append(h('span', { class: 'warn-text', text: '指定した期間は、Blob が存在する期間の範囲外です。この期間に一致する Blob はありません。' }));
      else if (s < fs || e > fe) box.append(h('span', { class: 'warn-text', text: '期間の一部が、Blob が存在する期間の範囲外です。' }));
    }
    if (found && v.mode !== 'period') {
      box.append(h('span', { class: 'muted', text: `Blob が存在する期間（${tzName}）: ${found.start} 〜 ${found.end}${d.found.complete ? '' : '（一部の Blob 名から算出）'}` }));
    }
    return box;
  }

  function drawStep(v) {
    if (v.mode !== 'period') return;
    const det = h('details', { class: 'more' }, h('summary', { text: '詳細な設定' }));
    if (v.step) det.open = true;
    const stepN = stepper(v.step || 0, 0, 9999, '間隔', (n) => {
      v.step = n || undefined;
      if (!v.step) delete v.step_unit; else v.step_unit = v.step_unit || 'day';
      changed();
    });
    const stepU = select([['day', '日'], ['hour', '時間'], ['month', 'か月'], ['year', '年'], ['minute', '分']], v.step_unit || 'day', { 'aria-label': '間隔の単位', onchange: (ev) => { v.step_unit = ev.target.value; changed(); } });
    det.append(h('div', { class: 'field' }, h('span', { class: 'field-label', text: '間隔（0 の場合は指定なし。指定すると、開始日時から一定の間隔ごとの日時のみを検索します）' }),
      h('div', { class: 'pair' }, stepN, stepU)));
    body.append(det);
  }

  function periodBar(v, found, tz) {
    const more = Array.isArray(v.more) ? v.more : [];
    const sels = [[v.start, v.end], ...more.map((p) => [p.start, p.end])].filter(([a, b]) => a && b).map(([a, b]) => [normPoint(a, false), normPoint(b, true)]);
    return rangeBar(found, sels.length ? sels : null, tz);
  }

  function rangeBar(found, sel, tz) {
    const toMs = (s) => { const p = s.match(/\d+/g).map(Number); return Date.UTC(p[0], p[1] - 1, p[2], p[3], p[4]); };
    const f0 = toMs(normPoint(found.start, false));
    const f1 = toMs(normPoint(found.end, true));
    let lo = f0;
    let hi = f1;
    // sel は検索する期間の並び（[[開始, 終了], …]。複数の期間は v1.5.0）
    const sels = (sel || []).map(([a, b]) => [toMs(a), toMs(b)]).filter(([a, b]) => b >= a);
    for (const [a, b] of sels) { lo = Math.min(lo, a); hi = Math.max(hi, b); }
    const span = Math.max(hi - lo, 3600e3);
    lo -= span * 0.03;
    hi += span * 0.03;
    const pct = (ms) => `${((ms - lo) / (hi - lo)) * 100}%`;
    const wid = (a, b) => `${Math.max(((b - a) / (hi - lo)) * 100, 0.8)}%`;
    const md = (s) => { const p = s.match(/\d+/g).map(Number); return `${p[1]}/${p[2]}`; };
    const bar = h('div', { class: 'rangebar', 'aria-hidden': 'true' },
      h('div', { class: 'rb-track' }),
      h('div', { class: 'rb-found', style: { left: pct(f0), width: wid(f0, f1) } }),
      ...sels.map(([a, b]) => h('div', { class: 'rb-sel', style: { left: pct(a), width: wid(a, b) } })),
      h('span', { class: 'rb-tick l', style: { left: pct(f0) }, text: md(found.start) }),
      h('span', { class: 'rb-tick r', style: { left: pct(f1) }, text: md(found.end) }));
    const legend = h('div', { class: 'rb-legend' },
      h('span', {}, h('i', { class: 'rb-key found' }), `Blob が存在する期間（${tz === 'utc' ? 'UTC' : 'ローカル時刻'}）: ${found.start} 〜 ${found.end}${found.complete === false ? '（一部の Blob 名から算出）' : ''}`),
      h('span', {}, h('i', { class: 'rb-key sel' }), '検索する期間'));
    return h('div', { class: 'rangebar-wrap' }, bar, legend);
  }

  function stepper(value, min, max, label, onSet) {
    const inp = h('input', { type: 'number', class: 'input num', value: String(value), min: String(min), max: String(max), 'aria-label': label });
    const set = (n) => { const x = Math.min(Math.max(min, Math.round(Number(n) || 0)), max); inp.value = String(x); onSet(x); };
    inp.addEventListener('change', () => set(inp.value));
    return h('span', { class: 'stepper' },
      h('button', { type: 'button', 'aria-label': `${label}を 1 減らす`, text: '−', onclick: () => set(Number(inp.value) - 1) }), inp,
      h('button', { type: 'button', 'aria-label': `${label}を 1 増やす`, text: '＋', onclick: () => set(Number(inp.value) + 1) }));
  }

  // 入力欄を作り直さずに、表示だけを最新にします（v1.7.2）。日時の入力欄で入力している間は、変数の欄を
  // 描き直さないため（フォーカスが外れるため）、検索する期間の表示と期間の帯は、ここで差し替えます
  row.refreshInfo = (dtInfo) => {
    ctx.dtInfo = dtInfo;
    if (shown.summary && shown.summary.isConnected) {
      const next = summaryBox(...shown.summaryArgs);
      shown.summary.replaceWith(next);
      shown.summary = next;
    }
    if (shown.bar && shown.bar.isConnected) {
      const next = periodBar(...shown.barArgs);
      shown.bar.replaceWith(next);
      shown.bar = next;
    }
  };
  return row;
}

/** 誤りを変数の行に表示します。 */
export function showVariableError(row, message) {
  const input = row.querySelector('input, select') || row;
  setError(input, message);
}
