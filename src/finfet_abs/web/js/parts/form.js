// フォーム（4.1.2、4.3.2）。ラベルを左の列、入力欄を右の列に置く2列の格子。

import { append, clear, h } from '../dom.js';

let seq = 0;
/** 一意の ID を作ります。 */
export function uid(prefix = 'f') {
  seq += 1;
  return `${prefix}-${seq}`;
}

/** 2列の格子。 */
export function formGrid(...rows) {
  return append(h('div.form'), rows);
}

/**
 * 1行（ラベルと入力欄）。
 * @param {string|null} label
 * @param {Node|Node[]} control
 * @param {{optional?: boolean, forId?: string, inline?: boolean, hidden?: boolean}} [opt]
 * @returns {Node[]} ラベルの要素と、入力欄を包む要素
 */
export function row(label, control, opt = {}) {
  const lab = h(opt.forId ? 'label' : 'div', { class: 'label', for: opt.forId }, label || '', opt.optional ? '（任意）' : '');
  const ctl = append(h('div', { class: `control${opt.inline ? ' row' : ''}` }), [control]);
  if (opt.hidden) { lab.hidden = true; ctl.hidden = true; }
  return [lab, ctl];
}

/** 行の表示・非表示を切り替えます（row の戻り値に使います）。 */
export function showRow(rowNodes, visible) {
  rowNodes.forEach((n) => { n.hidden = !visible; });
}

/**
 * 欄の下の誤りを表示・消去します（誤りがあるときだけ赤で表示します）。
 * @param {HTMLElement} input
 * @param {string|null} message
 */
export function setError(input, message) {
  const wrap = input.closest('.control') || input.parentElement;
  let el = wrap.querySelector(':scope > .field-error');
  if (!message) {
    input.classList.remove('invalid');
    input.removeAttribute('aria-invalid');
    if (el) el.remove();
    return;
  }
  input.classList.add('invalid');
  input.setAttribute('aria-invalid', 'true');
  if (!el) {
    el = h('div', { class: 'field-error', role: 'alert' });
    wrap.appendChild(el);
  }
  el.textContent = message;
}

/** 最初の誤りの欄に移動します。 */
export function focusFirstError(root) {
  const el = root.querySelector('.invalid, [aria-invalid="true"]');
  if (el) {
    el.scrollIntoView({ block: 'center' });
    el.focus();
  }
}

/**
 * 選択の欄。
 * @param {[string, string][]} options [値, 表示]
 * @param {string} value
 * @param {Object} [attrs]
 */
export function select(options, value, attrs = {}) {
  const el = h('select', { class: `input ${attrs.class || ''}`.trim(), id: attrs.id, onchange: attrs.onchange, disabled: attrs.disabled, 'aria-label': attrs['aria-label'] });
  for (const [v, label] of options) el.appendChild(h('option', { value: v, selected: String(v) === String(value) }, label));
  return el;
}

/**
 * ラジオボタンの並び。
 * @param {string} name
 * @param {[string, string][]} options
 * @param {string} value
 * @param {(v: string) => void} onChange
 */
export function radios(name, options, value, onChange) {
  const wrap = h('div', { class: 'radios', role: 'radiogroup' });
  for (const [v, label] of options) {
    const input = h('input', { type: 'radio', name, value: v, checked: v === value, onchange: () => onChange(v) });
    wrap.appendChild(h('label.check', null, input, label));
  }
  return wrap;
}

/** ラジオボタンの選んだ値。 */
export function radioValue(wrap) {
  const el = wrap.querySelector('input[type=radio]:checked');
  return el ? el.value : null;
}

/**
 * チェックボックス。
 * @param {string} label
 * @param {boolean} checked
 * @param {(v: boolean) => void} [onChange]
 */
export function checkbox(label, checked, onChange, attrs = {}) {
  const input = h('input', { type: 'checkbox', checked, id: attrs.id, disabled: attrs.disabled, onchange: (e) => onChange && onChange(e.target.checked) });
  return h('label.check', null, input, label);
}

/** チェックボックスの input 要素。 */
export function checkInput(label) {
  return label.querySelector('input');
}

/** テキストの入力欄。 */
export function textInput(value, attrs = {}) {
  const cls = ['input'];
  if (attrs.mono) cls.push('mono');
  if (attrs.width) cls.push(attrs.width);
  const el = h('input', {
    type: attrs.type || 'text',
    class: cls.join(' '),
    id: attrs.id,
    placeholder: attrs.placeholder,
    autocomplete: attrs.autocomplete || 'off',
    spellcheck: 'false',
    inputmode: attrs.inputmode,
    maxlength: attrs.maxlength,
    'aria-label': attrs['aria-label'],
    oninput: attrs.oninput,
    onchange: attrs.onchange,
    onblur: attrs.onblur,
    onkeydown: attrs.onkeydown,
    disabled: attrs.disabled,
    list: attrs.list,
  });
  el.value = value ?? '';
  return el;
}

/** 内容を入れ替えます。 */
export function setContent(el, ...children) {
  clear(el);
  return append(el, children);
}
