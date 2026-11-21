// 既知の形式から作成（4.4.5）。

import * as api from '../api.js';
import { button, clear, debounce, h, linkButton } from '../dom.js';
import { openDialog } from './dialog.js';
import { formGrid, row, select, setError, textInput } from './form.js';

/**
 * ダイアログを開きます。
 * @param {{inputs?: Object, formatId?: string}} [opt] 初期値（コンテナー名からの提案）
 * @returns {Promise<Object|null>} 作った検索の部品（container, path_pattern, variables）
 */
export function openKnownFormats(opt = {}) {
  return new Promise(async (resolve) => {
    let result = null;
    const list = h('div', { class: 'kf-list' });
    const right = h('div', { class: 'kf-right' });
    const filter = textInput('', { width: 'w-full', placeholder: '絞り込み', 'aria-label': '形式の絞り込み' });
    const d = openDialog({ title: '既知の形式から作成', width: 960, body: h('div', { class: 'kf' }, h('div', null, filter, list), right), onClose: () => resolve(result) });
    const { formats } = await api.get('/api/v1/known-formats');
    let current = null;
    const inputs = {};

    const drawList = () => {
      clear(list);
      const words = filter.value.toLowerCase().split(/\s+/).filter(Boolean);
      let group = null;
      for (const f of formats) {
        if (!words.every((w) => `${f.group} ${f.name} ${f.description}`.toLowerCase().includes(w))) continue;
        if (f.group !== group) {
          group = f.group;
          list.append(h('div', { class: 'kf-group', text: group }));
        }
        list.append(h('button', {
          type: 'button', class: `kf-item${current && current.id === f.id ? ' selected' : ''}`,
          onclick: () => choose(f),
        }, f.name, f.verified ? null : h('span', { class: 'muted', text: '（未確認）' })));
      }
    };
    filter.addEventListener('input', drawList);

    const preview = h('dl', { class: 'kv kf-preview' });
    const notes = h('div', { class: 'muted kf-notes' });
    const err = h('div');

    const build = async () => {
      if (!current) return null;
      const vals = Object.fromEntries(Object.entries(inputs).map(([k, el]) => [k, el.value.trim()]));
      try {
        setError(err, null);
        const b = await api.post(`/api/v1/known-formats/${current.id}/build`, { inputs: vals });
        clear(preview);
        preview.append(h('dt', { text: 'コンテナー' }), h('dd', { class: 'mono', text: b.container || '（接続先の行で指定）' }),
          h('dt', { text: 'パスパターン' }), h('dd', { class: 'mono', text: b.path_pattern }));
        return b;
      } catch (e) {
        if (e.data && e.data.field && inputs[e.data.field]) setError(inputs[e.data.field], e.message);
        else setError(err, e.message);
        return null;
      }
    };
    const rebuild = debounce(build, 300);

    function choose(f) {
      current = f;
      drawList();
      clear(right);
      for (const k of Object.keys(inputs)) delete inputs[k];
      const rows = [];
      for (const i of f.inputs) {
        let el;
        const init = (opt.formatId === f.id && opt.inputs && opt.inputs[i.name]) || '';
        if (i.kind === 'select') {
          el = select(i.options.map((o) => [o.id, o.label]), init || i.default || '', { 'aria-label': i.label, onchange: rebuild });
        } else {
          el = textInput(init, { width: i.name === 'sub' || i.name === 'tenant' ? 'w-mid' : 'w-mid', 'aria-label': i.label, oninput: rebuild, list: i.suggest ? `kf-${i.name}` : null });
          if (i.suggest) right.append(h('datalist', { id: `kf-${i.name}` }, i.suggest.map((x) => h('option', { value: x }))));
        }
        inputs[i.name] = el;
        rows.push(...row(i.label, el, { optional: !i.required && i.kind !== 'select' }));
      }
      clear(notes);
      for (const n of f.notes || []) notes.append(h('p', { text: n }));
      if (!f.verified) notes.append(h('p', { text: 'この形式は、公式のドキュメントで確認できていません。' }));
      right.append(h('h2', { text: f.name }), formGrid(...rows), preview, notes, err);
      build();
    }

    d.setFoot(h('span.push'), button('キャンセル', () => d.close(true)), button('フォームに入れる', async () => {
      const b = await build();
      if (!b) return;
      result = b;
      d.close(true);
    }, { primary: true }));
    drawList();
    const first = formats.find((f) => f.id === opt.formatId) || formats[0];
    if (first) choose(first);
    void linkButton;
  });
}
