// 設定のページ（4.9）。

import * as api from '../api.js';
import { button, h, linkButton, removeButton } from '../dom.js';
import { mbps } from '../format.js';
import { confirmDialog } from '../parts/dialog.js';
import { pickFolder } from '../parts/folder.js';
import { hint } from '../parts/hint.js';
import { checkbox, focusFirstError, formGrid, row, select, setError, showRow, textInput } from '../parts/form.js';
import { toast, toastError } from '../parts/toast.js';

const TZ = [['local', 'ローカル時刻'], ['utc', 'UTC']];

/** 各項目の説明（v1.5.0。[?] で表示します。範囲・既定値は、スキーマから加えます）。 */
const HELP = {
  'search.max_results': '1 回の検索で保持する Blob の数の上限です。上限に達すると、検索を打ち切ります。条件を絞れない大きな検索で、メモリと時間を使いすぎないためのものです。',
  'search.max_prefixes': 'パスパターンから作る prefix（一覧を取る範囲）の数の上限です。多いほど、取得する Blob が減って速くなりますが、要求の数が増えます。超えると、より短い prefix にまとめます。',
  'search.merge_trial_pages': '短い prefix でまとめて一覧を取った方が速いかを試すページ数です。通常は変える必要はありません。',
  'search.page_size': '検索とパスパターンの自動生成で、Blob の一覧を 1 回の要求で取得する件数です。Azure の上限が 5,000 件のため、これより大きくはできません。小さくすると、要求の回数が増えて時間がかかりますが、1 回の応答が速く返るため、進み具合がこまめに表示されます。通常は 5,000 のままにします。合計の件数の上限は「上限件数」です。',
  'search.list_concurrency': '一覧の取得を、同時にいくつ行うかです。増やすと速くなりますが、Azure の要求の速度の制限（スロットリング）にかかりやすくなります。',
  'search.input_timezone': '検索のページで日時を入力するときの、既定の基準です。ログの多くは、パスの日時が UTC です。',
  'search.exclude_adls_directories': 'Data Lake Storage（階層型の名前空間）のディレクトリは、一覧で Blob のように見えます。有効にすると、検索結果に含めません。',
  'search.keep_recent': '検索結果を、新しいものからいくつ残すかです。古い結果は自動で消します（検索履歴の条件は残ります）。',
  'fetch.directory': 'ダウンロードしたファイルを保存するフォルダーの既定です。ジョブの作成のときに変えられます。%USERPROFILE% のような環境変数を使えます。',
  'fetch.job_folder': '有効にすると、保存先の下にジョブの名前のフォルダーを作って保存します。ジョブごとにファイルを分けられます。',
  'fetch.path_structure': '保存するファイルの名前の付け方です。「階層を保つ」は Blob 名の「/」をフォルダーにします。「階層を名前にする」は「/」を「_」に変えて 1 つのフォルダーに置きます。',
  'fetch.conflict': '保存先に同じ名前のファイルがあるときの扱いです。',
  'fetch.set_mtime': '保存したファイルの更新日時を、Blob の最終更新日時にそろえます。',
  'fetch.verify_md5': 'Blob に Content-MD5 があれば、ダウンロードした内容と照合します。一致しなければエラーにします。',
  'fetch.extract_archives': 'ダウンロードした圧縮ファイルを展開します。展開できなくても、ダウンロードは完了のままです。',
  'fetch.extract_max_gib': '1 つの圧縮ファイルを展開した大きさの合計の上限です。超えたら展開を止めます（極端に大きく膨らむファイルへの備え）。',
  'fetch.reuse_search_info_minutes': '検索してからこの時間以内にジョブを作ると、Blob の状態の確認を省き、検索結果の情報を使います。0 にすると、毎回確認します。',
  'queue.resume_on_startup': '本ツールを終了したときに実行中だったジョブを、次に起動したときに自動で再開します。',
  'rehydrate.target_tier': 'Archive の Blob をリハイドレートするときの、復元先の層の既定です。',
  'rehydrate.priority': 'リハイドレートの優先度の既定です。「高」は速く（多くは 1 時間以内）終わりますが、料金が高くなります。「標準」は最大 15 時間ほどかかります。',
  'rehydrate.poll_first_minutes.high': 'リハイドレートの完了を確かめる間隔です。最初の確認までの時間と、その後の間隔を指定します。',
  'rehydrate.poll_first_minutes.standard': 'リハイドレートの完了を確かめる間隔です。最初の確認までの時間と、その後の間隔を指定します。',
  'rehydrate.check_on_startup': '起動したときに、リハイドレートの完了を待っているジョブの状態を確かめます。',
  'queue.concurrent_jobs': '同時に実行するジョブの数です。1 にすると、キューの順に 1 つずつ実行します。',
  'transfer.max_connections': '本ツール全体で、Azure に同時につなぐ数の上限です。',
  'transfer.check_concurrency': 'Blob の状態の確認を、同時にいくつ行うかです。',
  'transfer.rehydrate_concurrency': 'リハイドレートの要求を、同時にいくつ送るかです。',
  'transfer.download_concurrency': '同時にダウンロードする Blob の数です。',
  'transfer.connections_per_blob': '大きな Blob を、いくつの接続に分けて並行してダウンロードするかです。',
  'transfer.chunk_size_mib': '1 回の要求でダウンロードする大きさです。回線が不安定なときは小さくすると、やり直す量が減ります。',
  'transfer.bandwidth_mbps': 'ダウンロードの速さの上限です。0 で無制限です。実行中のジョブにもすぐ反映されます。',
  'transfer.request_rate': '1 秒あたりに Azure に送る要求の数の上限です。',
  'transfer.retry_total': '一時的な失敗（通信の切断、スロットリングなど）のときに、やり直す回数です。',
  'transfer.connect_timeout': '接続と読み取りを待つ時間です。この時間を過ぎると、失敗としてやり直します。',
  'load.process_priority': '本ツールの処理の優先度（Windows のプロセスの優先度）です。低くすると、ほかの作業の邪魔をしにくくなります。',
  'load.prevent_sleep_downloading': 'ジョブの実行中に、PC がスリープしないようにします。',
  'auth.default_entra_mode': '接続先の追加で、Azure にサインインして選ぶときの既定の認証の方式です。',
  'auth.default_tenant': 'サインインするテナントの既定です。organizations は、サインインしたアカウントの所属するテナントです。',
  'auth.client_id': 'サインインに使うアプリケーション（クライアント）の ID です。組織で指定されている場合だけ変えます。',
  'auth.sas_expiry_warn_hours': 'SAS の有効期限がこの時間以内になったら、警告を表示します。',
  'network.proxy.mode': '「自動」は Windows のプロキシの設定（環境変数・システムの設定）を使います。',
  'network.proxy.url': 'プロキシの URL です（例: http://proxy.example:8080）。',
  'network.proxy.bypass': 'プロキシを使わない宛先です。「,」で区切ります。',
  'network.ca_bundle': '組織のプロキシが通信を検査する場合など、追加で信頼する CA 証明書（PEM 形式）のファイルです。',
  'ui.theme': '画面の色の組み合わせです。',
  'ui.font_size': '画面の文字の大きさです。「大」では、表の行やボタンも大きくなります。「画面の大きさに合わせる」では、ブラウザの幅が 1,600px 未満では標準、1,600px 以上では大、2,200px 以上ではさらに大きくします（幅の狭い画面の表示は変わりません）。',
  'ui.start_page': '本ツールを起動したときに最初に開くページです。',
  'ui.activity_popups': 'お知らせを、画面の右上にポップアップでも表示します。クリックすると、ジョブ・検索結果の詳細（エラーのときはエラーの内容）を開きます。マウスを重ねている間は消えません。見ているページのお知らせは、ポップアップにしません。すべてのお知らせは、上部のバーの「お知らせ」で後から見られます。',
  'ui.browser_notifications': 'リハイドレートの完了などを、Windows の通知（ブラウザの通知）でも知らせます。本ツールの画面が裏にあるときだけ通知します。',
  'ui.blob_name_display': '検索結果などでの、Blob 名の表示のしかたの既定です。',
  'exit.when_closed': '本ツールの画面をすべて閉じたら、本ツールを終了します。',
  'exit.wait_minutes': '画面をすべて閉じてから、本ツールを終了するまでの時間です。',
  'log.level': 'ログに書く内容の細かさです。問題を調べるときは DEBUG にします。',
  'log.max_files': 'ログのファイルを残す日数です。古いものから消します。',
  'log.console': '本ツールを起動したときの黒いウィンドウ（コンソール）に、ログを表示します。ファイルのログと同じく、秘密の値は伏せ字にします。表示が追いつかないときは、一部の行を省きます（ファイルにはすべて残ります）。',
  'log.console_level': 'コンソールに表示するログの細かさです。ファイルのログのレベルとは別に決められます。',
  'history.max_entries': '検索履歴を残す数の上限です。ピン留めした履歴は消しません。',
};

/**
 * 節の定義。項目は [キー, ラベル, 種類, 追加の指定]。
 * 種類: num（数値）、check（チェックボックス）、select、text、dir（フォルダー）、radios、password
 */
function sections(session) {
  return [
    {
      id: 'search', title: '検索', items: [
        ['search.max_results', '上限件数', 'num', { unit: '件' }],
        ['search.max_prefixes', 'prefix の上限', 'num'],
        ['search.merge_trial_pages', 'まとめて取得を試すページ数', 'num'],
        ['search.page_size', '1 回の要求で取得する件数', 'num', { unit: '件' }],
        ['search.list_concurrency', '一覧の取得の並列数', 'num'],
        ['search.input_timezone', '期間の入力の既定', 'select', { options: TZ }],
        ['search.exclude_adls_directories', 'Data Lake のディレクトリを除く', 'check'],
        ['search.keep_recent', '検索結果を残す数', 'num', { unit: '件' }],
      ],
    },
    {
      id: 'fetch', title: '取得', items: [
        ['fetch.directory', '保存先', 'dir'],
        ['fetch.job_folder', 'ジョブのフォルダー', 'check', { text: '保存先の下にジョブごとのフォルダーを作る' }],
        ['fetch.path_structure', 'パス構造', 'select', { options: [['preserve', '階層を保つ'], ['flatten', '階層を名前にする'], ['name_only', 'ファイル名だけ'], ['template', '指定する']] }],
        ['fetch.conflict', '同名のファイル', 'select', { options: [['number', '番号を付ける'], ['skip', 'スキップ'], ['overwrite', '上書き']] }],
        ['fetch.set_mtime', '最終更新日時', 'check', { text: 'ファイルの更新日時を Blob の最終更新日時にする' }],
        ['fetch.verify_md5', 'MD5 の照合', 'check', { text: 'Content-MD5 がある Blob を、ダウンロード後に照合する' }],
        ['fetch.extract_archives', '圧縮ファイルの展開', 'check', { text: 'ダウンロードした圧縮ファイル（zip・tar・gz・bz2・xz）を、同じフォルダーに展開する（.tar.gz のような入れ子は最後まで展開し、元のファイルは残す）' }],
        ['fetch.extract_max_gib', '展開の上限', 'num', { unit: 'GiB（1 つの圧縮ファイルを展開した大きさの合計。超えたら展開を止める）' }],
        ['fetch.reuse_search_info_minutes', '検索結果を確認の代わりに使う時間', 'num', { unit: '分' }],
        ['queue.resume_on_startup', '起動時に実行中だったジョブを再開する', 'check'],
      ],
    },
    {
      id: 'rehydrate', title: 'リハイドレート', items: [
        ['rehydrate.target_tier', '復元先の層', 'select', { options: [['Hot', 'Hot'], ['Cool', 'Cool'], ['Cold', 'Cold']] }],
        ['rehydrate.priority', '優先度', 'select', { options: [['Standard', '標準'], ['High', '高']] }],
        [['rehydrate.poll_first_minutes.high', 'rehydrate.poll_interval_minutes.high'], '完了の確認の間隔（高）', 'poll'],
        [['rehydrate.poll_first_minutes.standard', 'rehydrate.poll_interval_minutes.standard'], '完了の確認の間隔（標準）', 'poll'],
        ['rehydrate.check_on_startup', '起動時にリハイドレート待ちのジョブを確認する', 'check'],
      ],
    },
    {
      id: 'load', title: '負荷', items: [
        ['queue.concurrent_jobs', '同時に実行するジョブの数', 'num'],
        ['transfer.max_connections', '全体の同時接続数', 'num'],
        ['transfer.check_concurrency', '状態確認の並列数', 'num'],
        ['transfer.rehydrate_concurrency', 'リハイドレートの要求の並列数', 'num'],
        ['transfer.download_concurrency', 'ダウンロードの同時実行数', 'num', { unit: 'Blob' }],
        ['transfer.connections_per_blob', '1 Blob あたりの接続数', 'num'],
        ['transfer.chunk_size_mib', 'チャンクの大きさ', 'num', { unit: 'MiB' }],
        ['transfer.bandwidth_mbps', '帯域の上限', 'num', { unit: 'MB/s', mbps: true, step: 'any' }],
        ['transfer.request_rate', '要求の数の上限', 'num', { unit: '件/秒' }],
        ['transfer.retry_total', '再試行の回数', 'num', { unit: '回' }],
        [['transfer.connect_timeout', 'transfer.read_timeout'], 'タイムアウト', 'timeouts'],
        ['load.process_priority', '処理の優先度', 'select', { options: [['normal', '通常'], ['below_normal', 'やや低い'], ['background', '低い（バックグラウンド）']] }],
        [['load.prevent_sleep_downloading', 'load.prevent_sleep_waiting'], 'スリープの抑止', 'sleep'],
      ],
    },
    {
      id: 'auth', title: '認証', items: [
        ['auth.default_entra_mode', '既定の Entra ID の方式', 'radios', { options: [['user_account', 'Entra ID ユーザーアカウント'], ['access_key', 'アクセスキー']] }],
        ['auth.default_tenant', '既定のテナント', 'text', { width: 'w-mid', placeholder: 'organizations' }],
        ['auth.client_id', 'クライアント ID', 'text', { width: 'w-mid', mono: true, resetDefault: true }],
        ['auth.sas_expiry_warn_hours', 'SAS の期限の警告', 'num', { unit: '時間前' }],
      ],
    },
    {
      id: 'proxy', title: 'プロキシ', items: [
        ['network.proxy.mode', 'プロキシ', 'radios', { options: [['auto', '自動'], ['manual', '指定する'], ['none', '使わない']] }],
        ['network.proxy.url', 'プロキシの URL', 'text', { width: 'w-long', mono: true, placeholder: 'http://proxy.example:8080', when: 'manual' }],
        ['network.proxy.bypass', '使わない宛先', 'text', { width: 'w-long', mono: true, placeholder: '*.contoso.com, 10.0.0.0/8', when: 'manual', optional: true }],
        ['network.proxy.username', 'ID', 'text', { width: 'w-mid', when: 'manual', optional: true }],
        ['network.proxy.password', 'パスワード', 'password', { width: 'w-mid', when: 'manual', optional: true }],
        ['network.ca_bundle', '追加の CA 証明書', 'text', { width: 'w-long', mono: true, placeholder: 'C:\\certs\\contoso-ca.pem', optional: true }],
        ['__proxy_test', '', 'proxytest'],
      ],
    },
    {
      id: 'ui', title: '画面', items: [
        ['ui.theme', 'テーマ', 'radios', { options: [['dark', 'ダーク'], ['light', 'ライト'], ['system', 'Windows の設定に合わせる']] }],
        ['ui.font_size', '文字の大きさ', 'radios', { options: [['auto', '画面の大きさに合わせる'], ['normal', '標準'], ['large', '大']] }],
        ['ui.start_page', '起動時に開くページ', 'select', { options: [['jobs', 'ジョブ'], ['search', '検索']] }],
        ['ui.activity_popups', 'ポップアップ', 'check', { text: 'ジョブの開始・完了・エラー、検索の終了などを、画面の右上にポップアップで知らせる' }],
        ['ui.browser_notifications', 'ブラウザの通知', 'check', { text: 'リハイドレートの完了などを、ブラウザの通知でも知らせる' }],
        ['ui.blob_name_display', 'Blob 名の表示', 'select', { options: [['full', '全体'], ['common', '共通する階層を省略'], ['tail', '末尾のみ']] }],
        ['exit.when_closed', '自動の終了', 'check', { text: '本ツールの画面（ブラウザのタブ）をすべて閉じたら、本ツールを終了する（実行中のジョブ・検索があるときは、終わってから終了します）' }],
        ['exit.wait_minutes', '終了までの時間', 'num', { unit: '分（画面をすべて閉じてから）' }],
      ],
    },
    {
      id: 'data', title: 'データとログ', items: [
        ['__data_dir', 'データ保存先', 'datadir'],
        ['log.level', 'ログのレベル', 'select', { options: [['DEBUG', 'DEBUG'], ['INFO', 'INFO'], ['WARNING', 'WARNING'], ['ERROR', 'ERROR']] }],
        ['log.max_files', 'ログを保存する数', 'num', { unit: '日分' }],
        ['log.console', 'コンソールの表示', 'check', { text: '本ツールを起動したウィンドウ（コンソール）にもログを表示する' }],
        ['log.console_level', 'コンソールのログのレベル', 'select', { options: [['DEBUG', 'DEBUG'], ['INFO', 'INFO'], ['WARNING', 'WARNING'], ['ERROR', 'ERROR']] }],
        ['history.max_entries', '検索履歴の上限', 'num', { unit: '件' }],
        ['__reset', '', 'reset'],
      ],
    },
  ];
}

export async function render(ctx) {
  const { root } = ctx;
  const res = await api.get('/api/v1/settings');
  const schema = Object.fromEntries(res.schema.map((s) => [s.key, s]));
  let saved = res.values;
  const session = api.session;
  /** @type {Map<string, {get: () => any, set: (v: any) => void, el: HTMLElement}>} */
  const fields = new Map();
  const whenRows = [];
  const bar = h('div', { class: 'sticky-bottom', hidden: true },
    button('保存', () => save(), { primary: true }),
    removeButton('変更を破棄', () => { load(saved); updateDirty(); }));

  const changes = () => {
    const out = {};
    for (const [key, f] of fields) {
      const v = f.get();
      if (key === 'network.proxy.password') {
        if (v !== '') out[key] = v;
        continue;
      }
      if (JSON.stringify(v) !== JSON.stringify(saved[key])) out[key] = v;
    }
    return out;
  };
  const updateDirty = () => { bar.hidden = Object.keys(changes()).length === 0; };
  const updateWhen = () => {
    const mode = fields.get('network.proxy.mode')?.get();
    for (const [nodes, when] of whenRows) showRow(nodes, mode === when);
  };

  const validateNum = (key, input) => {
    const s = schema[key];
    const v = input.value.trim();
    let msg = null;
    if (v === '' || Number.isNaN(Number(v))) msg = '数値を入力してください。';
    else if (s.kind === 'int' && !/^-?\d+$/.test(v)) msg = '整数を入力してください。';
    else if ((s.min !== undefined && Number(v) < s.min) || (s.max !== undefined && Number(v) > s.max)) msg = `${Number(s.min).toLocaleString('en-US')}〜${Number(s.max).toLocaleString('en-US')} の範囲で入力してください。`;
    setError(input, msg);
    return !msg;
  };

  const numInput = (key, opt = {}) => {
    const input = textInput('', { width: 'w-num', inputmode: 'decimal', 'aria-label': opt.label, onblur: () => validateNum(key, input), oninput: () => { updateDirty(); if (opt.after) opt.after(); } });
    input.classList.add('num');
    fields.set(key, { el: input, get: () => (schema[key].kind === 'float' ? Number(input.value) : (/^-?\d+$/.test(input.value.trim()) ? Number(input.value) : input.value)), set: (v) => { input.value = String(v); } });
    return input;
  };
  const checkField = (key, text) => {
    const lab = checkbox(text, false, () => updateDirty());
    const input = lab.querySelector('input');
    fields.set(key, { el: input, get: () => input.checked, set: (v) => { input.checked = Boolean(v); } });
    return lab;
  };

  const buildItem = (item) => {
    const [key, label, kind, opt = {}] = item;
    switch (kind) {
      case 'num': {
        const input = numInput(key, { label });
        const parts = [input];
        if (opt.unit) parts.push(h('span', { class: 'unit', text: opt.unit }));
        if (opt.mbps) {
          const conv = h('span', { class: 'unit' });
          const upd = () => { conv.textContent = Number(input.value) > 0 ? mbps(input.value) : '0 で無制限'; };
          input.addEventListener('input', upd);
          fields.get(key).after = upd;
          parts.push(conv);
          const f = fields.get(key);
          const origSet = f.set;
          f.set = (v) => { origSet(v); upd(); };
        }
        return row(label, parts, { inline: true });
      }
      case 'check':
        return row(opt.text ? label : '', checkField(key, opt.text || label), { inline: true });
      case 'select': {
        const el = select(opt.options, '', { onchange: () => updateDirty(), 'aria-label': label });
        fields.set(key, { el, get: () => el.value, set: (v) => { el.value = v; } });
        return row(label, el);
      }
      case 'radios': {
        const wrap = h('div', { class: 'radios', role: 'radiogroup', 'aria-label': label });
        const inputs = opt.options.map(([v, text]) => {
          const input = h('input', { type: 'radio', name: key, value: v, onchange: () => { updateDirty(); updateWhen(); } });
          wrap.appendChild(h('label.check', null, input, text));
          return input;
        });
        fields.set(key, { el: inputs[0], get: () => (inputs.find((i) => i.checked) || {}).value, set: (v) => inputs.forEach((i) => { i.checked = i.value === v; }) });
        return row(label, wrap);
      }
      case 'text':
      case 'password': {
        const input = textInput('', { width: opt.width, mono: opt.mono, placeholder: opt.placeholder, type: kind === 'password' ? 'password' : 'text', oninput: () => updateDirty(), 'aria-label': label });
        fields.set(key, { el: input, get: () => input.value.trim(), set: (v) => { input.value = v ?? ''; } });
        if (kind === 'password') {
          fields.get(key).set = () => { input.value = ''; input.placeholder = saved['network.proxy.password_set'] ? '設定済み（変えるときだけ入力）' : ''; };
        }
        const parts = [input];
        if (opt.resetDefault) {
          parts.push(linkButton('既定に戻す', () => { input.value = schema[key].default; updateDirty(); }));
        }
        const r = row(label, parts, { inline: true, optional: opt.optional });
        if (opt.when) whenRows.push([r, opt.when]);
        return r;
      }
      case 'dir': {
        const input = textInput('', { width: 'w-mid', 'aria-label': label, oninput: () => updateDirty() });
        fields.set(key, { el: input, get: () => input.value.trim(), set: (v) => { input.value = v; } });
        const pick = button('選択', async () => {
          const r = await pickFolder({ initial: input.value });
          if (r) { input.value = r.path; updateDirty(); }
        });
        return row(label, [input, pick], { inline: true });
      }
      case 'poll': {
        const [k1, k2] = key;
        return row(label, [h('span', { class: 'labeled' }, h('span', { text: '最初は' }), numInput(k1, { label: `${label} 最初` }), h('span', { text: '分後、' })), h('span', { class: 'labeled' }, h('span', { text: '以降は' }), numInput(k2, { label: `${label} 以降` }), h('span', { text: '分ごと' }))], { inline: true });
      }
      case 'timeouts': {
        const [k1, k2] = key;
        return row(label, [h('span', { class: 'labeled' }, h('span', { text: '接続' }), numInput(k1, { label: '接続のタイムアウト' }), h('span', { text: '秒、' })), h('span', { class: 'labeled' }, h('span', { text: '読み取り' }), numInput(k2, { label: '読み取りのタイムアウト' }), h('span', { text: '秒' }))], { inline: true });
      }
      case 'sleep': {
        const [k1, k2] = key;
        return row(label, [checkField(k1, 'ダウンロード中はスリープしない'), checkField(k2, 'リハイドレート待ちの間もスリープしない')]);
      }
      case 'proxytest': {
        const out = h('div', { class: 'pre' });
        const btn = button('接続を確認', async () => {
          out.textContent = '確認しています…';
          try {
            const r = await api.post('/api/v1/settings/proxy/test', {});
            out.textContent = r.results.map((x) => `${x.ok ? 'OK' : 'NG'}  ${x.target}  ${x.via}${x.message ? `  ${x.message}` : ''}`).join('\n');
          } catch (e) {
            out.textContent = e.message;
          }
        });
        return row('', [btn, out]);
      }
      case 'datadir': {
        const parts = [h('div', null, h('span', { class: 'mono', text: res.data_dir }), h('span', { class: 'muted', text: `　${res.mode_label}` })),
          linkButton('フォルダーを開く', () => api.post('/api/v1/fs/open-folder', { path: res.data_dir }).catch((e) => toastError(e)))];
        if (session.acl_warning) parts.push(h('div', { class: 'warn', text: 'データのフォルダーに、ほかの人から読めないようにする設定（ACL）をできませんでした。ほかの人から読めるおそれがあります。' }));
        return row(label, parts);
      }
      case 'reset':
        return row('', button('設定を既定に戻す', async () => {
          const ok = await confirmDialog({ title: '設定を既定に戻しますか', body: 'すべての設定を既定の値に戻します。プロキシのパスワードも消えます。', okText: '既定に戻す' });
          if (!ok) return;
          const r = await api.post('/api/v1/settings/reset', {});
          saved = r.values;
          load(saved);
          updateDirty();
          toast('設定を既定に戻しました');
        }));
      default:
        return [];
    }
  };

  // 項目の説明と、範囲・既定値（v1.5.0）
  const fmtNum = (v) => Number(v).toLocaleString('en-US');
  const metaOf = (key, kind, opt) => {
    const s = schema[key];
    if (!s) return '';
    const unit = opt.unit && !opt.unit.includes('（') ? ` ${opt.unit}` : '';
    if (s.kind === 'int' || s.kind === 'float') {
      const zero = opt.mbps ? '（0 で無制限）' : '';
      return `範囲: ${fmtNum(s.min)}〜${fmtNum(s.max)}${unit}${zero}　既定: ${fmtNum(s.default)}${unit}`;
    }
    if (s.kind === 'bool') return `既定: ${s.default ? '有効' : '無効'}`;
    if (opt.options) {
      const o = opt.options.find(([v]) => v === s.default);
      return o ? `既定: ${o[1]}` : '';
    }
    if (s.kind === 'str' && s.default) return `既定: ${s.default}`;
    return '';
  };
  const helpFor = (item) => {
    const [key, label, kind, opt = {}] = item;
    const keys = Array.isArray(key) ? key : [key];
    const text = HELP[keys[0]];
    const metas = keys.map((k, i) => {
      const m = metaOf(k, kind, opt);
      if (!m || keys.length === 1) return m;
      const names = { poll: ['最初', '以降'], timeouts: ['接続（秒）', '読み取り（秒）'], sleep: ['ダウンロード中', 'リハイドレート待ち'] }[kind] || [];
      return `${names[i] || k}: ${m}`;
    }).filter(Boolean);
    if (!text && !metas.length) return null;
    return hint(() => [text ? h('p', { text }) : null, ...metas.map((m) => h('p', { class: 'hint-meta', text: m }))].filter(Boolean), `${label || opt.text || ''} の説明`);
  };
  const buildWithHelp = (item) => {
    const nodes = buildItem(item);
    if (!Array.isArray(nodes) || nodes.length !== 2) return nodes;
    const btn = helpFor(item);
    if (!btn) return nodes;
    const [lab, ctl] = nodes;
    if (lab.textContent.trim()) lab.append(btn);
    else ctl.append(btn);
    return nodes;
  };

  const secs = sections(session);
  const toc = h('nav', { class: 'settings-toc', 'aria-label': '設定の節' });
  const body = h('div', { class: 'settings-body' });
  for (const sec of secs) {
    toc.appendChild(h('a', { href: `#${sec.id}`, dataset: { sec: sec.id } }, sec.title));
    body.appendChild(h('section', { id: sec.id }, h('h2', { text: sec.title }), formGrid(...sec.items.map(buildWithHelp))));
  }

  const load = (values) => {
    for (const [key, f] of fields) {
      f.set(values[key]);
      setError(f.el, null);
    }
    updateWhen();
  };

  async function save() {
    let ok = true;
    for (const [key, f] of fields) {
      if (schema[key] && (schema[key].kind === 'int' || schema[key].kind === 'float')) ok = validateNum(key, f.el) && ok;
    }
    if (!ok) {
      focusFirstError(body);
      return;
    }
    const ch = changes();
    try {
      const r = await api.put('/api/v1/settings', { changes: ch });
      saved = r.values;
      load(saved);
      updateDirty();
      if (ch['ui.browser_notifications'] && 'Notification' in window && Notification.permission === 'default') {
        Notification.requestPermission().catch(() => {});
      }
      toast('設定を保存しました');
    } catch (e) {
      if (e.fields) {
        for (const [k, msg] of Object.entries(e.fields)) {
          const f = fields.get(k);
          if (f) setError(f.el, msg);
        }
        focusFirstError(body);
      } else {
        toastError(e);
      }
    }
  }

  root.append(h('div', { class: 'page-head' }, h('h1', { text: '設定' })), h('div', { class: 'settings-layout' }, toc, body), bar);
  load(saved);
  updateDirty();
  ctx.setGuard(() => (Object.keys(changes()).length ? '保存していない設定の変更があります。' : null));

  // 現在の節を目次で示します。上端を過ぎた最後の節を現在の節とし、最後までスクロールしたら最後の節にします
  // （v1.5.0: 最後の節が短いと、上端まで来ずにハイライトされなかったため）。目次をクリックした節は、次に手で動かすまで保ちます
  const links = new Map([...toc.querySelectorAll('a')].map((a) => [a.dataset.sec, a]));
  const sectionEls = [...body.querySelectorAll(':scope > section')];
  const scroller = (() => {
    for (let el = body.parentElement; el && el !== document.body; el = el.parentElement) {
      const oy = getComputedStyle(el).overflowY;
      if ((oy === 'auto' || oy === 'scroll') && el.scrollHeight > el.clientHeight) return el;
    }
    return document.scrollingElement || document.documentElement;
  })();
  let pinned = null;
  const setCurrent = (id) => links.forEach((a, k) => a.classList.toggle('current', k === id));
  const updateCurrent = () => {
    if (pinned) { setCurrent(pinned); return; }
    const top = (scroller === document.scrollingElement || scroller === document.documentElement) ? 0 : scroller.getBoundingClientRect().top;
    let cur = sectionEls[0]?.id;
    let curTop = -Infinity;
    for (const s of sectionEls) {
      const t = s.getBoundingClientRect().top - top;
      // 広い画面の 2 列では、同じ行の節は上端が同じため、左（先）の節を優先します
      if (t <= 80 && t > curTop + 1) { cur = s.id; curTop = t; }
    }
    if (scroller.scrollTop + scroller.clientHeight >= scroller.scrollHeight - 4 && scroller.scrollTop > 0) cur = sectionEls[sectionEls.length - 1]?.id;
    setCurrent(cur);
  };
  const scrollTarget = scroller === document.scrollingElement || scroller === document.documentElement ? window : scroller;
  let raf = 0;
  const onScroll = () => { if (!raf) raf = requestAnimationFrame(() => { raf = 0; updateCurrent(); }); };
  const unpin = () => { pinned = null; };
  scrollTarget.addEventListener('scroll', onScroll, { passive: true });
  for (const ev of ['wheel', 'touchstart', 'keydown']) window.addEventListener(ev, unpin, { passive: true });
  toc.addEventListener('click', (e) => { const a = e.target.closest('a[data-sec]'); if (a) { pinned = a.dataset.sec; setCurrent(pinned); } });
  ctx.onLeave(() => {
    scrollTarget.removeEventListener('scroll', onScroll);
    for (const ev of ['wheel', 'touchstart', 'keydown']) window.removeEventListener(ev, unpin);
  });
  updateCurrent();
  if (ctx.hash) document.getElementById(ctx.hash)?.scrollIntoView();
}
