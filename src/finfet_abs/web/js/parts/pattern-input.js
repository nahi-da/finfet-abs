// パスパターンの入力欄（4.3.8）。変数を色で、誤りの位置を下線で示します（入力欄の背後に同じ文字を描く方法）。

import { clear, h } from '../dom.js';

/**
 * @param {Object} opt
 * @param {string} opt.value
 * @param {(v: string) => void} opt.onInput
 * @param {string} [opt.placeholder]
 * @param {string} [opt.label]
 * @param {() => string[]} [opt.suggestions] `{{` の後の候補（変数の名前）
 */
export function patternInput(opt) {
  const back = h('div', { class: 'pi-back', 'aria-hidden': 'true' });
  const ta = h('textarea', {
    class: 'pi-text', rows: '1', spellcheck: 'false', autocomplete: 'off', 'aria-label': opt.label || 'パスパターン',
    placeholder: opt.placeholder || '',
  });
  ta.value = opt.value || '';
  const list = h('div', { class: 'menu pi-suggest', hidden: true, role: 'listbox' });
  const wrap = h('div', { class: 'pi' }, back, ta, list);
  let errorPos = null;

  const render = () => {
    clear(back);
    const text = ta.value;
    const re = /\{\{[^{}]*?\}\}/g;
    let last = 0;
    let m;
    const pieces = [];
    while ((m = re.exec(text))) {
      if (m.index > last) pieces.push([text.slice(last, m.index), false, last]);
      pieces.push([m[0], true, m.index]);
      last = m.index + m[0].length;
    }
    if (last < text.length) pieces.push([text.slice(last), false, last]);
    for (const [t, isVar, start] of pieces) {
      if (errorPos !== null && errorPos >= start && errorPos < start + t.length) {
        const k = errorPos - start;
        if (k > 0) back.append(h('span', { class: isVar ? 'pi-var' : '' }, t.slice(0, k)));
        back.append(h('span', { class: `pi-err${isVar ? ' pi-var' : ''}` }, t.slice(k, k + 1) || ' '));
        if (k + 1 < t.length) back.append(h('span', { class: isVar ? 'pi-var' : '' }, t.slice(k + 1)));
      } else {
        back.append(h('span', { class: isVar ? 'pi-var' : '' }, t));
      }
    }
    if (errorPos !== null && errorPos >= text.length) back.append(h('span', { class: 'pi-err' }, ' '));
    back.append('\n');
    ta.style.height = 'auto';
    ta.style.height = `${Math.max(34, ta.scrollHeight)}px`;
    back.style.height = ta.style.height;
  };

  const showSuggest = () => {
    const pos = ta.selectionStart;
    const before = ta.value.slice(0, pos);
    if (!before.endsWith('{{')) {
      list.hidden = true;
      return;
    }
    const names = [...new Set((opt.suggestions ? opt.suggestions() : []))];
    const items = [...names.map((n) => [n, `${n}}}`]), ['日時の変数（{{dt=YYYY}}）', 'dt=YYYY}}']];
    clear(list);
    for (const [label, ins] of items) {
      list.append(h('button', {
        type: 'button', role: 'option',
        onmousedown: (e) => {
          e.preventDefault();
          ta.setRangeText(ins, pos, pos, 'end');
          list.hidden = true;
          ta.dispatchEvent(new Event('input', { bubbles: true }));
        },
      }, label));
    }
    list.hidden = false;
  };

  ta.addEventListener('input', () => {
    render();
    opt.onInput(ta.value);
    showSuggest();
  });
  ta.addEventListener('blur', () => { list.hidden = true; });
  ta.addEventListener('keydown', (e) => {
    if (e.key === 'Escape') list.hidden = true;
    if (e.key === 'Enter' && !e.ctrlKey) e.preventDefault();
  });
  ta.addEventListener('scroll', () => { back.scrollTop = ta.scrollTop; });
  window.addEventListener('resize', render);
  setTimeout(render, 0);

  return {
    el: wrap,
    input: ta,
    get value() { return ta.value; },
    set value(v) { ta.value = v; render(); },
    setError(pos) { errorPos = pos; render(); },
    focus() { ta.focus(); },
  };
}
