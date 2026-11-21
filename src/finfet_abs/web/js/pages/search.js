// 検索のページ（準備中）。

import { h } from '../dom.js';

export async function render(ctx) {
  ctx.root.append(h('div', { class: 'page-head' }, h('h1', { text: '検索' })), h('p', { class: 'placeholder', text: 'このページは準備中です。' }));
}
