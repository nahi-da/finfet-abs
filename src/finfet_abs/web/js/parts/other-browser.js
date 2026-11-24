// 別のブラウザで開く（v1.7.2。依頼者の依頼: 起動すると既定のブラウザが開き、コンソールの URL は使用済みになるため、
// ほかのブラウザで開けない）。起動コード付きの URL を作り、コピーできるように表示します。
// 起動コードは 1 回だけ・60 秒間使えます（S3）。時間が過ぎたら [新しい URL を作成] で作り直します。

import * as api from '../api.js';
import { button, h } from '../dom.js';
import { openDialog } from './dialog.js';
import { toast, toastError } from './toast.js';

export function openOtherBrowserDialog() {
  let timer = null;
  const d = openDialog({ title: '別のブラウザで開く', width: 640, onClose: () => clearInterval(timer) });
  const urlIn = h('input', { class: 'input mono', readonly: true, 'aria-label': 'URL', style: { flex: '1 1 auto', minWidth: '0' } });
  const left = h('span', { class: 'muted' });
  const copyBtn = button('コピー', async () => {
    try {
      await navigator.clipboard.writeText(urlIn.value);
      toast('URL をコピーしました。');
    } catch {
      urlIn.select();
      toast('URL を選択しました。Ctrl+C でコピーしてください。');
    }
  });
  const newBtn = button('新しい URL を作成', () => make());
  d.body.append(
    h('p', { text: '次の URL を、使いたいブラウザのアドレス欄に貼り付けて開いてください。' }),
    h('div', { class: 'line' }, urlIn, copyBtn),
    h('p', null, left),
    h('p', { class: 'muted', text: 'この URL は、1 回だけ、作成してから 60 秒間使えます。ほかの人に知らせないでください（この URL で、本ツールの画面を開けます）。' }));
  d.setFoot(h('span.push'), newBtn, button('閉じる', () => d.close(true), { primary: true }));

  async function make() {
    clearInterval(timer);
    urlIn.value = '';
    left.textContent = '作成しています…';
    copyBtn.disabled = true;
    try {
      const r = await api.post('/api/v1/app/launch-url', {});
      urlIn.value = r.url;
      copyBtn.disabled = false;
      const end = Date.now() + (r.expires_in || 60) * 1000;
      const tick = () => {
        const s = Math.ceil((end - Date.now()) / 1000);
        if (s > 0) {
          left.textContent = `残り ${s} 秒`;
          return;
        }
        clearInterval(timer);
        urlIn.value = '';
        copyBtn.disabled = true;
        left.textContent = '有効期限が切れました。[新しい URL を作成] で作り直してください。';
      };
      tick();
      timer = setInterval(tick, 1000);
    } catch (e) {
      left.textContent = '';
      toastError(e);
    }
  }
  make();
}
