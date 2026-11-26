// チュートリアルの手順（4.12.3。v1.7.0）。
// 手順ごとに、説明する部品（target）、見出しと説明、進み方（done: 操作が済んだら次へ。なければ [次へ]）を定義します。
// v1.7.4: 章に分け、章の初めの説明・画面の各部の説明・操作の結果の説明（after）を加えました（依頼者の意見）。
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

/** 検索のページの欄（見出しで探します）。 */
const part = (title) => [...document.querySelectorAll('#page section.part')].find((s) => s.querySelector('h2')?.textContent.trim() === title) || null;
/** 一覧の表の最初の行（見出しの行を除きます）。 */
const firstRow = () => $('#page .vtable .vbody .vrow');

/**
 * 章（v1.7.4）。吹き出しの上に、全体の中の位置として表示します。0 は「はじめに」（章の表示なし）。
 */
export const CHAPTERS = ['はじめに', 'サインインする', '接続先を追加する', 'パスパターンを作る', '条件を指定して検索する', 'ダウンロードする（ジョブ）', '検索履歴を使う'];

// 手順の項目:
//   chapter: 章の番号 / intro: 章の初めの説明（大きめの吹き出し） / where: いまの画面
//   target: 説明する部品 / text: 説明（文字列か、その配列） / list: 番号付きの一覧 / action: 利用者に求める操作
//   done: 操作が済んだか（済んだら「できました」を表示して次へ） / after: 操作の結果の説明（[次へ] を押すまで待ちます）
//   afterTarget: 結果の説明で示す部品 / afterWhere: 結果の説明での画面（操作で画面が移る場合） / button: 吹き出しに置くボタン / skipTo: 部品がないときに [次へ] で移るページ
export const STEPS = [
  {
    chapter: 0,
    intro: true,
    title: 'チュートリアルへようこそ',
    text: '架空のデータを使って、Blob を検索してダウンロードするまでの流れを体験します。実際の Azure には接続しません。所要時間は約 8 分です。FinFeT での作業は、次の順に進みます。',
    list: ['サインインする', '検索する接続先（ストレージアカウントとコンテナー）を追加する', 'Blob の名前の規則（パスパターン）を作る', '条件を指定して検索する', '選択した Blob をダウンロードする（ジョブ）', '検索履歴から同じ条件で検索する'],
  },
  {
    chapter: 0,
    where: '検索のページ',
    title: '画面の構成',
    target: () => $('.nav'),
    text: ['左のナビゲーションで、ページを切り替えます。いまは「検索」のページです。', '「ジョブ」ではダウンロードの進み具合を、「検索履歴」では過去の検索の条件を確認できます。'],
  },
  // ---------------------------------------------------------------- 1 サインイン
  {
    chapter: 1,
    intro: true,
    title: 'サインインする',
    text: ['Azure のデータを読み取るには、Microsoft Entra ID のアカウントでサインインします。サインインすると、そのアカウントで利用できるストレージアカウントを、一覧から選択できるようになります。', 'アクセスキーや SAS だけを使用する場合は、サインインは不要です。'],
  },
  {
    chapter: 1,
    where: '画面の右上',
    title: 'サインイン',
    target: () => $('#account-btn'),
    text: '実際の環境では、Microsoft のサインインページが開きます。チュートリアルでは、架空のアカウントでサインインします。',
    action: '吹き出しの [サインイン] をクリックしてください。',
    button: { text: 'サインイン', run: demoSignIn },
    done: () => !!app.auth?.signed_in,
    after: '架空のアカウントでサインインしました。右上の [アカウント] で、サインイン中のアカウントの確認や、別のアカウントの追加ができます。',
    afterTarget: () => $('#account-btn'),
  },
  // ---------------------------------------------------------------- 2 接続先
  {
    chapter: 2,
    intro: true,
    where: '検索のページ',
    title: '接続先を追加する',
    text: ['「接続先」は、検索の対象とする「ストレージアカウント」と「コンテナー」の組み合わせです。Blob（ファイル）は、コンテナーの中に保存されています。', 'この章では、架空のストレージアカウント stexample01 の、applogs というコンテナーを追加します。'],
  },
  {
    chapter: 2,
    where: '検索のページ',
    title: '接続先の欄',
    page: /^\/search$/,
    target: () => part('接続先'),
    text: '検索のページの上にある「接続先」の欄です。まだ何も追加していないため、空です。接続先を複数追加すると、まとめて検索できます。',
  },
  {
    chapter: 2,
    where: '検索のページ',
    title: '接続先を追加',
    page: /^\/search$/,
    target: () => byText('#page button', '接続先を追加'),
    text: '接続先の追加の画面を開きます。',
    action: '[接続先を追加] をクリックしてください。',
    done: () => !!inDlg('.method-card'),
  },
  {
    chapter: 2,
    where: '接続先の追加',
    title: '追加の方法',
    target: () => inDlg('.method-card.main'),
    text: ['接続先を追加する方法を選びます。通常は、サインインしたアカウントで利用できるものから選択します。', '管理者から接続文字列や SAS URL を受け取った場合は、下の方法を使用します。'],
    action: '「サインイン中のアカウントから選択」をクリックしてください。',
    done: () => !!inDlg('.td-accounts .acct-item'),
  },
  {
    chapter: 2,
    where: '接続先の追加',
    title: 'ストレージアカウント',
    target: () => [...(dlg()?.querySelectorAll('.acct-item') || [])].find((r) => r.querySelector('.acct-name')?.textContent === 'stexample01') || null,
    text: '左の「1 ストレージアカウント」に、サインインしたアカウントで利用できるストレージアカウントが表示されます。各行には、サブスクリプション・リソースグループ・リージョンも表示されます。',
    action: '「stexample01」をクリックしてください。',
    done: () => !!inDlg('.acct-item.selected') && !!inDlg('.cont-item'),
  },
  {
    chapter: 2,
    where: '接続先の追加',
    title: 'コンテナー',
    target: () => [...(dlg()?.querySelectorAll('.cont-item') || [])].find((x) => x.textContent.trim() === 'applogs') || null,
    text: '右の「2 コンテナー」に、選択したストレージアカウントのコンテナーが表示されます。「applogs」には、仮想マシンごとのアプリのログが 1 時間ごとに保存されています。複数のコンテナーを選択することもできます。',
    action: '「applogs」をチェックしてください。',
    done: () => {
      const l = [...(dlg()?.querySelectorAll('.cont-item') || [])].find((x) => x.textContent.trim() === 'applogs');
      return !!l?.querySelector('input:checked');
    },
  },
  {
    chapter: 2,
    where: '接続先の追加',
    title: '接続先に追加',
    target: () => (dlg() ? byText('.dlg-foot button', '追加', dlg()) : null),
    text: '選択したコンテナーを、検索の接続先に追加します。',
    action: '[追加] をクリックしてください。',
    done: () => !dlg() && (searchState()?.targets || []).some((t) => t.container === 'applogs'),
    afterWhere: '検索のページ',
    after: '「接続先」の欄に stexample01 の applogs が追加されました。「認証」の列は、この接続先にアクセスする方法（ここでは、サインインしたアカウント）です。',
    afterTarget: () => part('接続先'),
  },
  // ---------------------------------------------------------------- 3 パスパターン
  {
    chapter: 3,
    intro: true,
    where: '検索のページ',
    title: 'パスパターンを作る',
    text: ['コンテナーには、大量の Blob が保存されています。FinFeT は、Blob の名前の規則（パスパターン）を手がかりに、必要な Blob だけを探します。', 'たとえば「vm-logs/web01/2026/11/20/app.log」のような名前なら、「web01」と「2026/11/20」の部分を変数にして、VM の名前と日付で絞り込めるようにします。'],
  },
  {
    chapter: 3,
    where: '検索のページ',
    title: 'パスパターンの欄',
    target: () => part('パスパターン'),
    text: 'パスパターンは、直接入力するほか、[既知の形式から作成]（診断設定のログなど、よく使われる形式）、[パスパターンを自動生成]、[履歴から] で作成できます。',
  },
  {
    chapter: 3,
    where: '検索のページ',
    title: 'パスパターン',
    target: () => byText('#page button', 'パスパターンを自動生成'),
    text: '今回は、名前の規則が分からないものとして、コンテナーの実際の Blob 名から自動で作ります。',
    action: '[パスパターンを自動生成] をクリックしてください。',
    done: () => !!inDlg('.choice-cards'),
  },
  {
    chapter: 3,
    where: 'パスパターンの自動生成',
    title: 'フルスキャンとクイックスキャン',
    target: () => inDlg('.choice-cards'),
    text: 'フルスキャンはすべての Blob 名を取得するため、パスパターンを正確に生成できます。Blob が数十万件を超える場合は、時間を短縮できるクイックスキャンも選択できます。ここでは、フルスキャンのまま進めます。',
  },
  {
    chapter: 3,
    where: 'パスパターンの自動生成',
    title: '自動生成の実行',
    target: () => (dlg() ? byText('.dlg-foot button', '実行', dlg()) : null),
    text: 'Blob 名を取得し、名前の規則ごとに候補をまとめます。',
    action: '[実行] をクリックしてください。',
    done: () => !!inDlg('.infer-table tbody tr'),
    after: '名前の規則ごとに、パスパターンの候補ができました。「件数」は、その規則に当てはまる Blob の数です。件数が多い順に表示されます。',
    afterTarget: () => inDlg('.infer-table'),
  },
  {
    chapter: 3,
    where: 'パスパターンの自動生成',
    title: '候補を使う',
    target: () => {
      const rows = [...(dlg()?.querySelectorAll('.infer-table tbody tr') || [])];
      const r = rows.find((x) => x.textContent.includes('選択肢')) || rows[0];
      return r?.querySelector('button') || null;
    },
    text: 'この候補では、VM の名前の部分が「選択肢」の変数、日付の部分が日時の変数になっています。',
    action: '[使う] をクリックしてください。',
    done: () => !dlg() && (searchState()?.path_pattern || '').includes('{{'),
    afterWhere: '検索のページ',
    after: 'パスパターンが入力されました。{{ }} で囲まれた部分が変数です。変数の値を指定すると、その値に一致する Blob だけを検索します。',
    afterTarget: () => part('パスパターン'),
  },
  // ---------------------------------------------------------------- 4 条件と検索
  {
    chapter: 4,
    intro: true,
    where: '検索のページ',
    title: '条件を指定して検索する',
    // 欄全体は画面より高いことがあるため、見出しの行を示します
    target: () => part('変数')?.querySelector('h2')?.parentElement || part('変数'),
    text: '「変数」の欄に、パスパターンの変数ごとの行が表示されました。ここで、検索する値を指定します。選択肢の変数では値を選択し、日時の変数では期間を指定します。',
  },
  {
    chapter: 4,
    where: '検索のページ',
    title: '検索する値を選択',
    target: () => { const v = varOf('choice'); return v ? varRow(v[0])?.querySelector('.choice-list') || null : null; },
    text: '選択肢には、Blob 名に含まれていた VM の名前が並んでいます。ダウンロードする Blob を少なくするため、1 つの VM に絞ります。',
    action: '「web01」のみをチェックした状態にしてください（ほかのチェックを外します）。',
    done: () => { const v = varOf('choice'); const s = v?.[1].value?.selected || []; return s.length === 1 && s[0] === 'web01'; },
  },
  {
    chapter: 4,
    where: '検索のページ',
    title: '期間を指定',
    target: () => { const v = varOf('datetime'); return v ? byText('.quick-btn', '昨日', varRow(v[0]) || document) : null; },
    text: '日時の変数では、開始と終了を入力して期間を指定します。よく使用する期間は、ボタンで入力できます。',
    action: '[昨日] をクリックしてください。',
    done: () => { const v = varOf('datetime')?.[1].value; return !!v && v.mode === 'period' && !!v.start && v.start === v.end; },
    after: '期間が入力されました。行の下には、検索する期間と、その期間に Blob があるかどうかが表示されます。',
    afterTarget: () => { const v = varOf('datetime'); return v ? varRow(v[0]) : null; },
  },
  {
    chapter: 4,
    where: '検索のページ',
    title: '検索',
    target: () => $('.search-actions button.primary') || byText('.search-actions button', '検索'),
    text: '指定した条件で Blob を検索します。日時の範囲に当たる部分のみを Azure から取得するため、Blob が多いコンテナーでも速く検索できます。',
    action: '[検索] をクリックしてください。',
    done: () => /^\/searches\/[^/]+$/.test(path()) && !!$('.vtable .vrow'),
  },
  {
    chapter: 4,
    where: '検索結果のページ',
    title: '検索結果',
    target: () => $('#page .vtable'),
    text: ['条件に一致した Blob の一覧です。名前・サイズ・層・最終更新日時などが表示されます。', '層が Archive の Blob は、そのままでは読み取れません。ダウンロードの前に、読み取れる層に戻す「リハイドレート」が必要です（FinFeT が自動で行います）。'],
  },
  {
    chapter: 4,
    where: '検索結果のページ',
    title: '一覧の絞り込み',
    target: () => $('#page .filters')?.closest('section') || $('#page .filters'),
    text: '上の欄で、キーワード・層・最終更新日時などにより、一覧を絞り込めます。選択した件数と、見つかった件数も表示されます。',
  },
  {
    chapter: 4,
    where: '検索結果のページ',
    title: 'Blob を選択',
    target: () => $('.vhead input[type=checkbox]'),
    text: 'ダウンロードする Blob を選択します。行ごとのチェックボックスのほか、見出しのチェックボックスで、絞り込みに一致するすべての Blob を選択できます。',
    action: '見出しのチェックボックスをクリックして、すべて選択してください。',
    done: () => !!$('.vhead input[type=checkbox]')?.checked,
    after: 'すべての Blob を選択しました。下の帯に、選択した件数と合計のサイズが表示されます。',
    afterTarget: () => $('#page .sticky-bottom'),
  },
  // ---------------------------------------------------------------- 5 ジョブ
  {
    chapter: 5,
    intro: true,
    title: 'ダウンロードする（ジョブ）',
    text: ['FinFeT では、ダウンロードを「ジョブ」として登録し、キューの順に実行します。', 'ジョブは、Archive 層の Blob のリハイドレートの要求、完了の確認（数時間かかります）、ダウンロード、MD5 の照合までを自動で行います。本ツールを終了しても、次に起動したときに続きから実行します。'],
  },
  {
    chapter: 5,
    where: '検索結果のページ',
    title: 'ジョブを作成',
    target: () => byText('.sticky-bottom button', 'ジョブを作成'),
    text: '選択した Blob をダウンロードするジョブを作成します。',
    action: '[ジョブを作成] をクリックしてください。',
    done: () => !!(dlg() && byText('.dlg-foot button', 'キューに追加', dlg())),
  },
  {
    chapter: 5,
    where: 'ジョブの作成',
    title: '保存先とキューに追加',
    target: () => (dlg() ? byText('.dlg-foot button', 'キューに追加', dlg()) : null),
    text: 'この画面では、保存先・フォルダーの構造・同じ名前のファイルがある場合の扱いなどを指定します。チュートリアルでは、保存先はデモ用の一時フォルダーです。',
    action: '[キューに追加] をクリックしてください。',
    done: () => !dlg() && /^\/searches\//.test(path()),
    afterWhere: '検索結果のページ',
    after: 'ジョブを作成し、キューに追加しました。前のジョブがなければ、すぐに開始します。',
  },
  {
    chapter: 5,
    where: '検索結果のページ',
    title: 'ジョブの一覧',
    target: () => $('.nav a[data-nav=jobs]'),
    text: '作成したジョブの進み具合は、ジョブのページで確認します。',
    action: 'ナビゲーションの「ジョブ」をクリックしてください。',
    skipTo: '/jobs',
    done: () => path() === '/jobs',
  },
  {
    chapter: 5,
    where: 'ジョブのページ',
    title: 'ジョブの進捗',
    // ジョブが終わっていれば「キュー」は空のため、タブを示します
    target: () => firstRow() || $('#page .tabs'),
    text: ['「キュー」のタブには、実行中と実行待ちのジョブが、状態・進み具合・件数とともに表示されます。終わったジョブは「終了」のタブに移ります。デモモードでは時間を短縮しているため、リハイドレートも短い時間で終わります。', 'ジョブをクリックすると、Blob ごとの状態やログを確認できます。'],
  },
  // ---------------------------------------------------------------- 6 検索履歴
  {
    chapter: 6,
    intro: true,
    title: '検索履歴を使う',
    text: '実行した検索の条件（接続先・パスパターン・変数の値）は、検索履歴に保存されます。同じ条件で、後から再度検索できます。',
  },
  {
    chapter: 6,
    where: 'ジョブのページ',
    title: '検索履歴',
    target: () => $('.nav a[data-nav=history]'),
    text: '検索履歴のページを開きます。',
    action: 'ナビゲーションの「検索履歴」をクリックしてください。',
    skipTo: '/history',
    done: () => path() === '/history',
  },
  {
    chapter: 6,
    where: '検索履歴のページ',
    title: '同じ条件で再度検索',
    target: firstRow,
    text: ['先ほど実行した検索です。行の右端のメニューの「検索ページで開く」で、同じ条件を検索のページに読み込めます。期間は、今日に合わせて移動することもできます。', 'よく使用する条件は、行の左のピンでピン留めしておくと、一覧の上に表示されます。'],
  },
  {
    chapter: 6,
    title: 'チュートリアルの終了',
    text: 'チュートリアルは以上です。体験した流れは次のとおりです。設定の「画面」から、いつでも再度始められます。デモモードのタブを閉じると、デモモードの FinFeT は自動で終了します。',
    list: ['サインインする', '接続先を追加する', 'パスパターンを作る', '条件を指定して検索する', 'ジョブでダウンロードする', '検索履歴から再度検索する'],
    last: true,
  },
];
