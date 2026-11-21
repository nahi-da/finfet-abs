// 変数の行（4.4.3）。種類によって、値の指定の欄の形が変わります。

import { button, clear, h, iconButton, linkButton } from '../dom.js';
import { openDialog } from './dialog.js';
import { checkbox, select, setError, textInput } from './form.js';
import { openMenu } from './menu.js';

const CHAR_CLASSES = [['digit', '数字'], ['lower', '英小文字'], ['upper', '英大文字'], ['symbol', '記号'], ['space', '空白'],
  ['hiragana', 'ひらがな'], ['katakana', 'カタカナ'], ['kanji', '漢字']];
const REL = [['today', '今日'], ['yesterday', '昨日'], ['last_days', '直近 n 日'], ['last_hours', '直近 n 時間'],
  ['this_month', '今月'], ['last_month', '先月'], ['days_ago', 'n 日前〜m 日前']];
const UNIT_NAMES = [['year', '年', 'YYYY|YY|year'], ['month', '月', 'MM|M|month'], ['day', '日', 'DD|D|day'],
  ['hour', '時', 'hh|h|hour'], ['minute', '分', 'mm|m|minute'], ['second', '秒', 'ss|s|second']];

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
    side.prepend(h('span', { class: 'muted', text: 'パスパターンにありません' }), iconButton('close', '変数を削除', ctx.onRemove));
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
      else if (t === 'string' && prevVals.length) d.value = { mode: 'one_of', match: 'exact', values: prevVals.map((x) => ({ value: x, enabled: true })) };
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

  function drawString() {
    const v = d.value;
    const modeSel = select([['any', '任意'], ['one_of', '次のうちいずれか'], ['chars', '条件に合う文字列']], v.mode, {
      'aria-label': `${ctx.name} の値の指定`,
      onchange: (e) => {
        const m = e.target.value;
        d.value = m === 'one_of' ? { mode: 'one_of', match: 'exact', values: [] }
          : m === 'chars' ? { mode: 'chars', classes: ['digit', 'lower'], extra: '', allow_slash: false, min: 1, max: null } : { mode: 'any' };
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
      const match = select([['exact', '完全一致'], ['prefix', '前方一致'], ['partial', '部分一致']], v.match || 'exact', {
        'aria-label': '一致方法', onchange: (e) => { v.match = e.target.value; changed(); },
      });
      line.append(match);
      const row2 = h('div', { class: 'line' }, chips, inp);
      if (ctx.fetchCandidates) row2.append(linkButton('Azure から候補を取得', () => candidates(add)));
      body.append(row2);
    } else if (v.mode === 'chars') {
      v.classes = v.classes || [];
      const boxes = CHAR_CLASSES.map(([k, label]) => checkbox(label, v.classes.includes(k), (on) => {
        v.classes = on ? [...new Set([...v.classes, k])] : v.classes.filter((x) => x !== k);
        changed();
      }));
      const extra = textInput(v.extra || '', { width: 'w-short', placeholder: '-_.', 'aria-label': '指定した文字', mono: true });
      extra.addEventListener('change', () => { v.extra = extra.value; changed(); });
      body.append(h('div', { class: 'line' }, ...boxes),
        h('div', { class: 'line' }, h('span', { text: '指定した文字' }), extra,
          checkbox('/ を含める', !!v.allow_slash, (on) => { v.allow_slash = on; changed(); }),
          h('span', { text: '長さ' }), numIn(v.min, '最小の長さ', (n) => { v.min = n; }), h('span', { text: '〜' }), numIn(v.max, '最大の長さ', (n) => { v.max = n; })));
    }
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
    const shown = () => v.options.filter((o) => !filter || o.toLowerCase().includes(filter.toLowerCase()));
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
        cb.title = o;
        list.append(cb);
      }
      if (!v.options.length) list.append(h('div', { class: 'muted choice-empty', text: '選択肢がありません。値を入力して追加してください。' }));
      else if (!items.length) list.append(h('div', { class: 'muted choice-empty', text: '絞り込みに一致する選択肢はありません。' }));
      drawCount();
    };
    const drawCount = () => {
      count.textContent = `${v.selected.length} / ${v.options.length} 件を選択` + (filter ? `（${shown().length} 件を表示）` : '');
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
    body.append(h('div', { class: 'line' }, typeSel, filterIn, linkButton('すべて選ぶ', () => setShown(true)),
      linkButton('選択を外す', () => setShown(false)), count));
    body.append(list);
    const foot = h('div', { class: 'line' }, addIn);
    if (ctx.fetchCandidates) foot.append(linkButton('Azure から候補を取得', () => candidates(addValues)));
    if (d.from_sample) foot.append(h('span', { class: 'muted', text: '見本の Blob 名から作った選択肢です。ほかの値は、追加するか Azure から取得してください。' }));
    body.append(foot);
    drawList();
  }

  async function candidates(add) {
    const box = h('div', { class: 'folder-list' }, h('div', { class: 'group', text: '読み込み中…' }));
    const dlg = openDialog({ title: `候補（${ctx.name}）`, width: 720, body: box });
    const picked = new Set();
    dlg.setFoot(h('span.push'), button('キャンセル', () => dlg.close(true)), button('追加', () => { add([...picked]); dlg.close(true); }, { primary: true }));
    try {
      const vals = await ctx.fetchCandidates();
      clear(box);
      if (!vals.length) box.append(h('div', { class: 'group', text: '候補はありません。' }));
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
      onchange: (e) => { d.value = e.target.value === 'range' ? { mode: 'range', start: 1, end: 10, step: 1 } : e.target.value === 'fixed' ? { mode: 'fixed', value: 0 } : { mode: 'any' }; changed(); },
    });
    const line = h('div', { class: 'line' }, typeSel, modeSel);
    if (v.mode === 'fixed') line.append(numIn(v.value, '値', (n) => { v.value = n; }));
    if (v.mode === 'range') {
      line.append(numIn(v.start, '開始', (n) => { v.start = n; }), h('span', { text: '〜' }), numIn(v.end, '終了', (n) => { v.end = n; }),
        h('span', { text: '間隔' }), numIn(v.step ?? 1, '間隔', (n) => { v.step = n; }));
    }
    line.append(h('span', { text: '桁数' }), numIn(d.padding, '桁数（ゼロ埋め）', (n) => { d.padding = n || undefined; }));
    body.append(line);
  }

  function drawDatetime() {
    const v = d.value;
    const tzSel = select([['local', 'ローカル時刻'], ['utc', 'UTC']], ctx.inputTimezone, {
      'aria-label': '期間の入力の基準', onchange: (e) => ctx.onInputTimezone(e.target.value),
    });
    const modeSel = select([['any', '任意'], ['period', '期間'], ['units', '単位ごと'], ['relative', '相対的な期間']], v.mode, {
      'aria-label': `${ctx.name} の値の指定`,
      onchange: (e) => {
        const m = e.target.value;
        d.value = m === 'relative' ? { mode: 'relative', kind: 'last_days', n: 7 }
          : m === 'period' ? { mode: 'period', start: '', end: '' }
            : m === 'units' ? { mode: 'units', units: {} } : { mode: 'any' };
        changed();
      },
    });
    const line = h('div', { class: 'line' }, typeSel, h('span', { text: '期間の入力' }), tzSel, modeSel);
    body.append(line);
    if (d.found && d.found.first && d.found.last) {
      // パスパターンの自動生成で分かった、Blob がある期間（依頼者の依頼。decisions.md）
      const p = periodFromFound(d.found, d.path_timezone || 'utc', ctx.inputTimezone);
      const tzName = ctx.inputTimezone === 'utc' ? 'UTC' : 'ローカル時刻';
      const already = v.mode === 'period' && v.start === p.start && v.end === p.end;
      body.append(h('div', { class: 'line found-range' },
        h('span', { class: 'muted', text: `Blob がある期間（${tzName}）: ${p.start} 〜 ${p.end}${d.found.complete ? '' : '（見本の Blob 名から。実際はもっと広いことがあります）'}` }),
        already ? null : linkButton('この期間にする', () => { d.value = { mode: 'period', start: p.start, end: p.end }; changed(); })));
    }
    if (v.mode === 'relative') {
      const kind = select(REL, v.kind || 'last_days', { 'aria-label': '相対的な期間', onchange: (e) => { v.kind = e.target.value; if (!v.n) v.n = 7; changed(); } });
      line.append(kind);
      if (v.kind === 'last_days' || v.kind === 'last_hours') line.append(numIn(v.n, v.kind === 'last_days' ? '日数' : '時間数', (n) => { v.n = n; }));
      if (v.kind === 'days_ago') line.append(numIn(v.from, '何日前から', (n) => { v.from = n; }), h('span', { text: '日前〜' }), numIn(v.to, '何日前まで', (n) => { v.to = n; }), h('span', { text: '日前' }));
    } else if (v.mode === 'period') {
      const s = textInput(v.start || '', { width: 'w-short', placeholder: '2026-11-13', 'aria-label': '期間の開始', mono: true });
      const e = textInput(v.end || '', { width: 'w-short', placeholder: '2026-11-20', 'aria-label': '期間の終了', mono: true });
      s.addEventListener('change', () => { v.start = s.value.trim(); changed(); });
      e.addEventListener('change', () => { v.end = e.value.trim(); changed(); });
      line.append(s, h('span', { text: '〜' }), e);
      const stepN = numIn(v.step, '間隔', (n) => { v.step = n || undefined; if (!v.step) delete v.step_unit; else v.step_unit = v.step_unit || 'day'; });
      const stepU = select([['day', '日'], ['hour', '時間'], ['month', 'か月'], ['year', '年'], ['minute', '分']], v.step_unit || 'day', { 'aria-label': '間隔の単位', onchange: (ev) => { v.step_unit = ev.target.value; changed(); } });
      line.append(h('span', { text: '間隔（任意）' }), stepN, stepU);
      if (ctx.shiftHint) line.append(linkButton('今日に合わせて移動', ctx.shiftHint));
    } else if (v.mode === 'units') {
      v.units = v.units || {};
      const units = unitsInFormats(ctx.formats);
      const grid = h('div', { class: 'units-grid' });
      for (const [u, label] of units) {
        const spec = v.units[u] || { mode: 'all' };
        v.units[u] = spec;
        const cell = h('div', { class: 'line' }, h('span', { class: 'unit-label', text: label }));
        const m = select([['all', 'すべて'], ['fixed', '固定'], ['range', '範囲']], spec.mode, {
          'aria-label': `${label}の指定`, onchange: (e) => { v.units[u] = e.target.value === 'fixed' ? { mode: 'fixed', value: 0 } : e.target.value === 'range' ? { mode: 'range', start: 0, end: 0, step: 1 } : { mode: 'all' }; changed(); },
        });
        cell.append(m);
        if (spec.mode === 'fixed') cell.append(numIn(spec.value, `${label}の値`, (n) => { spec.value = n; }));
        if (spec.mode === 'range') cell.append(numIn(spec.start, `${label}の開始`, (n) => { spec.start = n; }), h('span', { text: '〜' }), numIn(spec.end, `${label}の終了`, (n) => { spec.end = n; }), h('span', { text: '間隔' }), numIn(spec.step ?? 1, `${label}の間隔`, (n) => { spec.step = n; }));
        grid.append(cell);
      }
      body.append(grid);
    }
    if (ctx.dtInfo && !ctx.dtInfo.same_zone && ctx.dtInfo.path) {
      side.prepend(h('span', { class: 'muted nowrap', text: `パス（${d.path_timezone === 'local' ? 'ローカル' : 'UTC'}）${ctx.dtInfo.path}` }));
    }
  }

  return row;
}

/** 誤りを変数の行に表示します。 */
export function showVariableError(row, message) {
  const input = row.querySelector('input, select') || row;
  setError(input, message);
}
