// フォルダーの選択（4.3.7）。ブラウザからは PC のフォルダーのパスを得られないため、本ツールのダイアログで選びます。

import * as api from '../api.js';
import { button, debounce, h, replace } from '../dom.js';
import { size } from '../format.js';
import { openDialog } from './dialog.js';
import { setError, textInput } from './form.js';

/**
 * フォルダーを選びます。
 * @param {{title?: string, initial?: string}} [opt]
 * @returns {Promise<{path: string, free_bytes: number}|null>}
 */
export function pickFolder(opt = {}) {
  return new Promise((resolve) => {
    let result = null;
    let current = '';
    const pathInput = textInput(opt.initial || '', { width: 'w-full', 'aria-label': 'フォルダーのパス', placeholder: 'D:\\work\\logs' });
    const places = h('div', { class: 'folder-list', 'aria-label': 'ドライブと最近使ったフォルダー' });
    const list = h('div', { class: 'folder-list', 'aria-label': 'フォルダーの中のフォルダー' });
    const status = h('div', { class: 'muted' });
    const layout = h('div', { class: 'folder-layout' }, places, list);

    const d = openDialog({
      title: opt.title || 'フォルダーを選択',
      width: 720,
      body: [pathInput, layout, status],
      onClose: () => resolve(result),
    });

    const go = async (path) => {
      setError(pathInput, null);
      try {
        const res = await api.get(`/api/v1/fs/dirs${api.qs({ path })}`);
        current = res.path;
        pathInput.value = res.path;
        const items = [];
        if (res.parent) items.push(h('button', { type: 'button', onclick: () => go(res.parent) }, '..'));
        for (const name of res.dirs) {
          const full = res.path.endsWith('\\') ? res.path + name : `${res.path}\\${name}`;
          items.push(h('button', { type: 'button', ondblclick: () => go(full), onclick: () => { pathInput.value = full; } }, name));
        }
        if (!res.dirs.length) items.push(h('div', { class: 'group', text: 'フォルダーはありません。' }));
        replace(list, items);
      } catch (e) {
        setError(pathInput, e.message);
      }
    };

    const loadPlaces = async () => {
      const res = await api.get('/api/v1/fs/dirs');
      const items = [h('div', { class: 'group', text: 'ドライブ' })];
      for (const dr of res.drives) items.push(h('button', { type: 'button', onclick: () => go(dr) }, dr));
      if (res.special.length) {
        items.push(h('div', { class: 'group', text: 'よく使うフォルダー' }));
        for (const s of res.special) items.push(h('button', { type: 'button', title: s.path, onclick: () => go(s.path) }, s.label));
      }
      if (res.recent.length) {
        items.push(h('div', { class: 'group', text: '最近使ったフォルダー' }));
        for (const r of res.recent) items.push(h('button', { type: 'button', title: r, onclick: () => go(r) }, r));
      }
      replace(places, items);
    };

    const onType = debounce(() => {
      const v = pathInput.value.trim();
      if (/^[A-Za-z]:\\?$/.test(v) || v.endsWith('\\')) go(v);
    }, 300);
    pathInput.addEventListener('input', onType);
    pathInput.addEventListener('keydown', (e) => {
      if (e.key === 'Enter') {
        e.preventDefault();
        go(pathInput.value.trim());
      }
    });

    const mkdir = async () => {
      if (!current) {
        setError(pathInput, '先にフォルダーを開いてください。');
        return;
      }
      const nameInput = textInput('', { width: 'w-mid', 'aria-label': '新しいフォルダーの名前' });
      const sub = openDialog({ title: '新しいフォルダー', width: 480, body: nameInput });
      const create = async () => {
        try {
          const res = await api.post('/api/v1/fs/mkdir', { path: current, name: nameInput.value });
          sub.close(true);
          await go(current);
          pathInput.value = res.path;
        } catch (e) {
          setError(nameInput, e.message);
        }
      };
      nameInput.addEventListener('keydown', (e) => { if (e.key === 'Enter') create(); });
      sub.setFoot(h('span.push'), button('キャンセル', () => sub.close(true)), button('作成', create, { primary: true }));
      nameInput.focus();
    };

    const choose = async () => {
      try {
        const res = await api.post('/api/v1/fs/check-writable', { path: pathInput.value.trim(), remember: true });
        result = res;
        status.textContent = `空き ${size(res.free_bytes)}`;
        d.close(true);
      } catch (e) {
        setError(pathInput, e.message);
      }
    };

    d.setFoot(button('新しいフォルダー', mkdir), h('span.push'), button('キャンセル', () => d.close(true)), button('選択', choose, { primary: true }));
    loadPlaces().catch(() => {});
    if (opt.initial) go(opt.initial);
    pathInput.focus();
  });
}
