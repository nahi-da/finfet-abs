// チュートリアルの手順（4.12.3。v1.7.0）。
// 手順ごとに、説明する部品（target）、見出しと説明、進み方（done: 操作が済んだら自動で次へ。なければ [次へ]）を定義します。
// 画面を変えたら、devtools/check_tutorial.py で、すべての手順を最後まで進められることを確かめてください。

import * as api from '../api.js';
import { app } from '../main.js';

const $ = (sel, root = document) => root.querySelector(sel);
const byText = (sel, text, root = document) => [...root.querySelectorAll(sel)].find((e) => e.textContent.trim() === text) || null;
const dlg = () => document.querySelector('dialog[open]');
const inDlg = (sel) => (dlg() ? $(sel, dlg()) : null);
const path = () => location.pathname;

/** 検索のページの入力（search.js が sessionStorage に保存しているもの）。 */
function searchState() {
  try { return JSON.parse(sessionStorage.getItem('finfet.search.state') || 'null'); } catch { return null; }
}
const varOf = (type) => {
  const s = searchState();
  return s ? Object.entries(s.variables || {}).find(([, v]) => v.type === type) || null : null;
};
const varRow = (name) => [...document.querySelectorAll('.var-row')].find((r) => r.querySelector('.var-name .mono')?.textContent === name) || null;

/** デモモードの架空のアカウントでサインインします（実際の環境では Microsoft のサインインページを使います）。 */
async function demoSignIn() {
  const r = await api.post('/api/v1/auth/user/sign-in', {});
  // 架空のサインインページ（本ツール自身の「/?code=…」）を、画面を移らずに開いて完了させます。
  // 名前（localhost と 127.0.0.1）が違うと別のサイトとして扱われるため、同じサイトの URL にして呼びます
  const u = new URL(r.auth_uri);
  await fetch(`/${u.search}`, { credentials: 'same-origin' }).catch(() => {});
}

export const STEPS = [
  {
    title: 'チュートリアルへようこそ',
    text: '架空のデータを使って、Blob を検索してダウンロードするまでの流れを体験します。実際の Azure には接続しません。所要時間は約 5 分です。',
  },
  {
    title: 'サインイン',
    target: () => $('#account-btn'),
    text: '本ツールは、Microsoft Entra ID のアカウントで Azure にアクセスします。実際の環境では、Microsoft のサインインページが開きます。チュートリアルでは、架空のアカウントでサインインします。',
    action: '[サインイン] をクリックしてください。',
    button: { text: 'サインイン', run: demoSignIn },
    done: () => !!app.auth?.signed_in,
  },
  {
    title: '接続先を追加',
    page: /^\/search$/,
    target: () => byText('#page button', '接続先を追加'),
    text: '検索するストレージアカウントとコンテナーを「接続先」として追加します。',
    action: '[接続先を追加] をクリックしてください。',
    done: () => !!inDlg('.method-card'),
  },
  {
    title: '追加の方法',
    target: () => inDlg('.method-card.main'),
    text: 'サインインしたアカウントで利用できるストレージアカウントを一覧から選択できます。接続文字列や SAS URL で追加することもできます。',
    action: '「サインイン中のアカウントから選択」をクリックしてください。',
    done: () => !!inDlg('.td-accounts .acct-item'),
  },
  {
    title: 'ストレージアカウント',
    target: () => [...(dlg()?.querySelectorAll('.acct-item') || [])].find((r) => r.querySelector('.acct-name')?.textContent === 'stexample01') || null,
    text: 'サブスクリプション・リソースグループ・リージョンも表示されます。',
    action: '「stexample01」をクリックしてください。',
    done: () => !!inDlg('.acct-item.selected') && !!inDlg('.cont-item'),
  },
  {
    title: 'コンテナー',
    target: () => [...(dlg()?.querySelectorAll('.cont-item') || [])].find((x) => x.textContent.trim() === 'applogs') || null,
    text: '「applogs」には、仮想マシンごとのアプリのログが 1 時間ごとに保存されています。複数のコンテナーを選択することもできます。',
    action: '「applogs」をチェックしてください。',
    done: () => {
      const l = [...(dlg()?.querySelectorAll('.cont-item') || [])].find((x) => x.textContent.trim() === 'applogs');
      return !!l?.querySelector('input:checked');
    },
  },
  {
    title: '接続先に追加',
    target: () => (dlg() ? byText('.dlg-foot button', '追加', dlg()) : null),
    text: '選択したコンテナーを、検索の接続先に追加します。',
    action: '[追加] をクリックしてください。',
    done: () => !dlg() && (searchState()?.targets || []).some((t) => t.container === 'applogs'),
  },
  {
    title: 'パスパターン',
    target: () => byText('#page button', 'パスパターンを自動生成'),
    text: '「パスパターン」は、検索する Blob の名前の規則です。日付や VM の名前の部分を変数にすると、まとめて検索できます。記述方法が分からない場合は、Blob 名から自動で生成できます。',
    action: '[パスパターンを自動生成] をクリックしてください。',
    done: () => !!inDlg('.choice-cards'),
  },
  {
    title: 'フルスキャンとクイックスキャン',
    target: () => inDlg('.choice-cards'),
    text: 'フルスキャンはすべての Blob 名を取得するため、パスパターンを正確に生成できます。Blob が数十万件を超える場合は、時間を短縮できるクイックスキャンも選択できます。ここでは、フルスキャンのまま進めます。',
  },
  {
    title: '自動生成の実行',
    target: () => (dlg() ? byText('.dlg-foot button', '実行', dlg()) : null),
    text: 'Blob 名を取得し、パスパターンの候補を作成します。',
    action: '[実行] をクリックしてください。',
    done: () => !!inDlg('.infer-table tbody tr'),
  },
  {
    title: '候補を使う',
    target: () => {
      const rows = [...(dlg()?.querySelectorAll('.infer-table tbody tr') || [])];
      const r = rows.find((x) => x.textContent.includes('選択肢')) || rows[0];
      return r?.querySelector('button') || null;
    },
    text: '候補は、該当する Blob の件数が多い順に表示されます。VM の名前の部分は「選択肢」の変数、日付の部分は日時の変数になります。',
    action: '[使う] をクリックしてください。',
    done: () => !dlg() && (searchState()?.path_pattern || '').includes('{{'),
  },
  {
    title: '検索する値を選択',
    target: () => { const v = varOf('choice'); return v ? varRow(v[0])?.querySelector('.choice-list') || null : null; },
    text: '選択肢の変数では、検索する値を選択します。ダウンロードする Blob を少なくするため、1 つの VM に絞ります。',
    action: '「web01」のみをチェックした状態にしてください（ほかのチェックを外します）。',
    done: () => { const v = varOf('choice'); const s = v?.[1].value?.selected || []; return s.length === 1 && s[0] === 'web01'; },
  },
  {
    title: '期間を指定',
    target: () => { const v = varOf('datetime'); return v ? byText('.quick-btn', '昨日', varRow(v[0]) || document) : null; },
    text: '日時の変数では、検索する期間を指定します。開始と終了を入力するほか、よく使用する期間をボタンで入力できます。',
    action: '[昨日] をクリックしてください。',
    done: () => { const v = varOf('datetime')?.[1].value; return !!v && v.mode === 'period' && !!v.start && v.start === v.end; },
  },
  {
    title: '検索',
    target: () => $('.search-actions button.primary') || byText('.search-actions button', '検索'),
    text: '指定した条件で Blob を検索します。日時の範囲に当たる prefix のみを Azure から取得するため、Blob が多いコンテナーでも速く検索できます。',
    action: '[検索] をクリックしてください。',
    done: () => /^\/searches\/[^/]+$/.test(path()) && !!$('.vtable .vrow'),
  },
  {
    title: '検索結果',
    target: () => $('.vhead input[type=checkbox]'),
    text: '検索結果では、キーワード・層・最終更新日時などで絞り込めます。ダウンロードする Blob を選択します。見出しのチェックボックスで、絞り込みに一致するすべての Blob を選択できます。',
    action: '見出しのチェックボックスをクリックして、すべて選択してください。',
    done: () => !!$('.vhead input[type=checkbox]')?.checked,
  },
  {
    title: 'ジョブを作成',
    target: () => byText('.sticky-bottom button', 'ジョブを作成'),
    text: 'ダウンロードは「ジョブ」として実行します。Archive 層の Blob は、リハイドレート（読み取りできる層への復元）を待ってから自動でダウンロードします。',
    action: '[ジョブを作成] をクリックしてください。',
    done: () => !!(dlg() && byText('.dlg-foot button', 'キューに追加', dlg())),
  },
  {
    title: '保存先とキューに追加',
    target: () => (dlg() ? byText('.dlg-foot button', 'キューに追加', dlg()) : null),
    text: '保存先・フォルダーの構造・同名のファイルの扱いなどを指定できます。チュートリアルでは、保存先はデモ用の一時フォルダーです。',
    action: '[キューに追加] をクリックしてください。',
    done: () => !dlg() && /^\/searches\//.test(path()),
  },
  {
    title: 'ジョブの一覧',
    target: () => $('.nav a[data-nav=jobs]'),
    text: '作成したジョブは、ジョブのページで進捗を確認できます。',
    action: 'ナビゲーションの「ジョブ」をクリックしてください。',
    skipTo: '/jobs',
    done: () => path() === '/jobs',
  },
  {
    title: 'ジョブの進捗',
    text: 'ジョブは順に実行されます。ジョブをクリックすると、Blob ごとの状態やログを確認できます。本ツールを終了しても、次に起動したときに続きから実行できます。',
  },
  {
    title: '検索履歴',
    target: () => $('.nav a[data-nav=history]'),
    text: '実行した検索の条件は、検索履歴に保存されます。',
    action: 'ナビゲーションの「検索履歴」をクリックしてください。',
    skipTo: '/history',
    done: () => path() === '/history',
  },
  {
    title: '同じ条件で再度検索',
    text: '検索履歴から、同じ条件で再度検索できます。期間は、今日に合わせて移動することもできます。よく使用する条件はピン留めしておくと便利です。',
  },
  {
    title: 'チュートリアルの終了',
    text: 'チュートリアルは以上です。設定の「画面」から、いつでも再度始められます。デモモードのタブを閉じると、デモモードの FinFeT は自動で終了します。',
    last: true,
  },
];
