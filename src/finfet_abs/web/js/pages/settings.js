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
  'search.max_results': '1 回の検索で保持する Blob の件数の上限です。上限に達した場合は、検索を打ち切ります。条件を絞り込めない大規模な検索で、メモリと処理時間を消費しすぎないようにするための設定です。',
  'search.max_prefixes': 'パスパターンから作成する prefix（一覧を取得する範囲）の数の上限です。多くするほど取得する Blob が減り処理は速くなりますが、要求の回数が増えます。上限を超えた場合は、より短い prefix にまとめます。',
  'search.merge_trial_pages': '短い prefix でまとめて一覧を取得した方が速いかを試行するページ数です。通常は変更する必要はありません。',
  'search.page_size': '検索とパスパターンの自動生成で、Blob の一覧を 1 回の要求で取得する件数です。Azure の上限が 5,000 件のため、これより大きな値は指定できません。小さくすると、要求の回数が増えて時間がかかりますが、1 回の応答が速く返るため、進捗がこまめに表示されます。通常は 5,000 のままにしてください。合計の件数の上限は「上限件数」で指定します。',
  'search.list_concurrency': '一覧の取得を同時に実行する数です。増やすと処理は速くなりますが、Azure の要求数の制限（スロットリング）を受けやすくなります。',
  'search.input_timezone': '検索ページで日時を入力する際の、既定の基準です。多くのログでは、パス上の日時が UTC です。',
  'search.exclude_adls_directories': 'Data Lake Storage（階層型の名前空間）のディレクトリは、一覧では Blob として表示されます。有効にすると、ディレクトリを検索結果から除外します。',
  'search.keep_recent': '新しいものから順に保持する検索結果の数です。古い結果は自動で削除します（検索履歴の条件は残ります）。',
  'fetch.directory': 'ダウンロードしたファイルを保存するフォルダーの既定値です。ジョブの作成時に変更できます。%USERPROFILE% のような環境変数を使用できます。',
  'fetch.job_folder': '有効にすると、保存先の下にジョブ名のフォルダーを作成して保存します。ジョブごとにファイルを分けて管理できます。',
  'fetch.path_structure': '保存するファイルの名前の付け方です。「階層を保つ」では、Blob 名の「/」をフォルダーの区切りとして扱います。「階層を名前にする」では、「/」を「_」に置き換えて 1 つのフォルダーに保存します。',
  'fetch.conflict': '保存先に同じ名前のファイルが存在する場合の扱いです。',
  'fetch.set_mtime': '保存したファイルの更新日時を、Blob の最終更新日時に合わせます。',
  'fetch.verify_md5': 'Blob に Content-MD5 が設定されている場合は、ダウンロードした内容と照合します。一致しない場合は、エラーにします。',
  'fetch.extract_archives': 'ダウンロードした圧縮ファイルを展開します。展開に失敗した場合も、ダウンロードは完了として扱います。',
  'fetch.extract_max_gib': '1 つの圧縮ファイルを展開したときの合計サイズの上限です。上限を超えた場合は、展開を中止します。展開後のサイズが極端に大きくなるファイルへの対策です。',
  'fetch.reuse_search_info_minutes': '検索後、この時間以内にジョブを作成した場合は、Blob の状態の確認を省略し、検索結果の情報を使用します。0 にすると、毎回確認します。',
  'queue.resume_on_startup': '本ツールの終了時に実行中だったジョブを、次回の起動時に自動で再開します。',
  'rehydrate.target_tier': 'Archive の Blob をリハイドレートするときの、復元先の層の既定です。',
  'rehydrate.priority': 'リハイドレートの優先度の既定値です。「高」は短時間（多くの場合 1 時間以内）で完了しますが、料金が高くなります。「標準」は完了まで最大で 15 時間程度かかります。',
  'rehydrate.poll_first_minutes.high': 'リハイドレートの完了を確認する間隔です。最初の確認までの時間と、その後の間隔を指定します。',
  'rehydrate.poll_first_minutes.standard': 'リハイドレートの完了を確認する間隔です。最初の確認までの時間と、その後の間隔を指定します。',
  'rehydrate.check_on_startup': '起動時に、リハイドレートの完了を待っているジョブの状態を確認します。',
  'queue.concurrent_jobs': '同時に実行するジョブの数です。1 にすると、キューの順に 1 件ずつ実行します。',
  'transfer.max_connections': '本ツール全体で、Azure に同時に接続する数の上限です。',
  'transfer.check_concurrency': 'Blob の状態の確認を同時に実行する数です。',
  'transfer.rehydrate_concurrency': 'リハイドレートの要求を同時に送信する数です。',
  'transfer.download_concurrency': '同時にダウンロードする Blob の数です。',
  'transfer.connections_per_blob': 'サイズの大きな Blob を、分割して並行にダウンロードする際の接続数です。',
  'transfer.chunk_size_mib': '1 回の要求でダウンロードするサイズです。回線が不安定な場合は、小さくすると再試行する量を減らせます。',
  'transfer.bandwidth_mbps': 'ダウンロード速度の上限です。0 の場合は無制限です。実行中のジョブにも直ちに反映されます。',
  'transfer.request_rate': '1 秒あたりに Azure に送る要求の数の上限です。',
  'transfer.retry_total': '一時的な失敗（通信の切断、スロットリングなど）が発生した場合に、再試行する回数です。',
  'transfer.connect_timeout': '接続と読み取りの待ち時間です。この時間を超えた場合は、失敗として再試行します。',
  'load.process_priority': '本ツールの処理の優先度（Windows のプロセスの優先度）です。低くすると、ほかの作業への影響を抑えられます。',
  'load.prevent_sleep_downloading': 'ジョブの実行中に、PC がスリープ状態にならないようにします。',
  'auth.default_entra_mode': '接続先の追加で、サインイン中のアカウントから選択する場合の既定の認証方式です。',
  'auth.default_tenant': 'サインインするテナントの既定値です。organizations を指定すると、サインインしたアカウントが所属するテナントを使用します。',
  'auth.client_id': 'サインインに使用するアプリケーション（クライアント）の ID です。組織で指定されている場合のみ変更してください。',
  'auth.sas_expiry_warn_hours': 'SAS の有効期限までの残り時間がこの時間を下回った場合に、警告を表示します。',
  'network.proxy.mode': '「自動」では、Windows のプロキシの設定（環境変数・システムの設定）を使用します。',
  'network.proxy.url': 'プロキシの URL です（例: http://proxy.example:8080）。',
  'network.proxy.bypass': 'プロキシを経由しない宛先です。複数指定する場合は「,」で区切ります。',
  'network.ca_bundle': '組織のプロキシが通信を検査する場合などに、追加で信頼する CA 証明書（PEM 形式）のファイルです。',
  'ui.theme': '画面の色の組み合わせです。',
  'ui.font_size': '画面の文字サイズです。「大」では、表の行やボタンも大きくなります。「画面の大きさに合わせる」では、ブラウザの幅が 1,600px 未満の場合は標準、1,600px 以上の場合は大、2,200px 以上の場合はさらに大きくします（幅の狭い画面の表示は変わりません）。',
  'ui.start_page': '本ツールの起動時に最初に開くページです。',
  'ui.activity_popups': 'お知らせを、画面の右上にポップアップでも表示します。クリックすると、ジョブや検索結果の詳細（エラーの場合はエラーの内容）を開きます。マウスポインターを重ねている間は表示が消えません。表示中のページに関するお知らせは、ポップアップで表示しません。すべてのお知らせは、上部のバーの「お知らせ」から後で確認できます。',
  'ui.browser_notifications': 'リハイドレートの完了などを、Windows の通知（ブラウザの通知）でも知らせます。本ツールの画面が背面にある場合のみ通知します。',
  'ui.blob_name_display': '検索結果などでの、Blob 名の表示方法の既定値です。',
  'exit.when_closed': '本ツールの画面をすべて閉じた場合に、本ツールを終了します。',
  'exit.wait_minutes': '画面をすべて閉じてから、本ツールが終了するまでの時間です。',
  'log.level': 'ログに出力する内容の詳細度です。問題を調査する場合は DEBUG にしてください。',
  'log.max_files': 'ログファイルを保持する日数です。古いものから削除します。',
  'log.console': '本ツールの起動時に開くウィンドウ（コンソール）に、ログを表示します。ファイルのログと同様に、秘密の値は伏せ字にします。表示が追いつかない場合は、一部の行を省略します（ファイルにはすべて記録されます）。',
  'log.console_level': 'コンソールに表示するログの詳細度です。ファイルに出力するログのレベルとは別に設定できます。',
  'listing.reuse': '検索とパスパターンの自動生成で取得した Blob の一覧を、メモリ上に一定時間保持します。次の検索では保持した範囲を再利用し、保持していない範囲のみを Azure から取得するため、検索時間を短縮できます。保持した一覧はディスクには保存せず、本ツールの終了時に破棄します。ただし、保持している間に追加・変更された Blob は検索結果に反映されません。最新の状態で検索する場合は、検索ページの [最新の情報に更新] を使用してください。',
  'listing.ttl_minutes': '取得した一覧を保持する時間です。この時間を過ぎた一覧は破棄し、次の検索で再取得します。',
  'listing.max_items': '保持する一覧の合計件数の上限です。上限を超える場合は、古い一覧から破棄します。1 件あたり数百バイトのメモリを使用します。',
  'holidays.extra': '日時の変数で祝日を除外する場合などに、祝日として扱う日（会社の休日など）です。YYYY-MM-DD の形式で、複数指定する場合は「,」で区切ります。',
  'holidays.not_holiday': '日本の祝日のうち、祝日として扱わない日（出勤日とする祝日など）です。YYYY-MM-DD の形式で、複数指定する場合は「,」で区切ります。',
  'history.max_entries': '検索履歴を保持する数の上限です。ピン留めした履歴は削除しません。',
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
        ['search.merge_trial_pages', 'まとめて取得を試行するページ数', 'num'],
        ['search.page_size', '1 回の要求で取得する件数', 'num', { unit: '件' }],
        ['search.list_concurrency', '一覧の取得の並列数', 'num'],
        ['search.input_timezone', '期間の入力の既定', 'select', { options: TZ }],
        ['search.exclude_adls_directories', 'Data Lake のディレクトリを除外する', 'check'],
        ['search.keep_recent', '検索結果を保持する数', 'num', { unit: '件' }],
        ['listing.reuse', '一覧の保持', 'check', { text: '取得した Blob の一覧を一定時間保持し、次の検索で再利用する' }],
        ['listing.ttl_minutes', '一覧を保持する時間', 'num', { unit: '分' }],
        ['listing.max_items', '保持する一覧の上限', 'num', { unit: '件' }],
        ['holidays.extra', '祝日として扱う日', 'text', { width: 'w-mid', placeholder: '例: 2026-12-29, 2026-12-30', optional: true }],
        ['holidays.not_holiday', '祝日として扱わない日', 'text', { width: 'w-mid', placeholder: '例: 2026-11-23', optional: true }],
      ],
    },
    {
      id: 'fetch', title: '取得', items: [
        ['fetch.directory', '保存先', 'dir'],
        ['fetch.job_folder', 'ジョブのフォルダー', 'check', { text: '保存先の下にジョブごとのフォルダーを作成する' }],
        ['fetch.path_structure', 'パス構造', 'select', { options: [['preserve', '階層を保つ'], ['flatten', '階層を名前にする'], ['name_only', 'ファイル名のみ'], ['template', '指定する']] }],
        ['fetch.conflict', '同名のファイル', 'select', { options: [['number', '番号を付ける'], ['skip', 'スキップ'], ['overwrite', '上書き']] }],
        ['fetch.set_mtime', '最終更新日時', 'check', { text: 'ファイルの更新日時を Blob の最終更新日時にする' }],
        ['fetch.verify_md5', 'MD5 の照合', 'check', { text: 'Content-MD5 が設定されている Blob を、ダウンロード後に照合する' }],
        ['fetch.extract_archives', '圧縮ファイルの展開', 'check', { text: 'ダウンロードした圧縮ファイル（zip・tar・gz・bz2・xz）を、同じフォルダーに展開する（.tar.gz のような多重の圧縮もすべて展開し、元のファイルは残す）' }],
        ['fetch.extract_max_gib', '展開の上限', 'num', { unit: 'GiB（1 つの圧縮ファイルを展開したときの合計サイズ。超えた場合は展開を中止）' }],
        ['fetch.reuse_search_info_minutes', '検索結果を確認の代わりに使用する時間', 'num', { unit: '分' }],
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
        ['transfer.chunk_size_mib', 'チャンクサイズ', 'num', { unit: 'MiB' }],
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
        ['ui.font_size', '文字サイズ', 'radios', { options: [['auto', '画面の大きさに合わせる'], ['normal', '標準'], ['large', '大']] }],
        ['ui.start_page', '起動時に開くページ', 'select', { options: [['jobs', 'ジョブ'], ['search', '検索']] }],
        ['ui.activity_popups', 'ポップアップ', 'check', { text: 'ジョブの開始・完了・エラー、検索の終了などを、画面の右上にポップアップで通知する' }],
        ['ui.browser_notifications', 'ブラウザの通知', 'check', { text: 'リハイドレートの完了などを、ブラウザの通知でも知らせる' }],
        ['ui.blob_name_display', 'Blob 名の表示', 'select', { options: [['full', '全体'], ['common', '共通する階層を省略'], ['tail', '末尾のみ']] }],
        ['exit.when_closed', '自動の終了', 'check', { text: '本ツールの画面（ブラウザのタブ）をすべて閉じた場合に、本ツールを終了する（実行中のジョブ・検索がある場合は、完了後に終了します）' }],
        ['exit.wait_minutes', '終了までの時間', 'num', { unit: '分（画面をすべて閉じてから）' }],
        ['__tutorial', 'チュートリアル', 'tutorial'],
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
          fields.get(key).set = () => { input.value = ''; input.placeholder = saved['network.proxy.password_set'] ? '設定済み（変更する場合のみ入力）' : ''; };
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
        return row(label, [checkField(k1, 'ダウンロード中はスリープ状態にしない'), checkField(k2, 'リハイドレート待ちの間もスリープ状態にしない')]);
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
        if (session.acl_warning) parts.push(h('div', { class: 'warn', text: 'データフォルダーのアクセス制限（ACL）を設定できませんでした。ほかのユーザーがデータを閲覧できる可能性があります。' }));
        return row(label, parts);
      }
      case 'tutorial':
        // チュートリアル（4.12）: 通常のモードでは、デモモードの本ツールを起動して新しいタブで始めます
        return row(label, [button('チュートリアルを始める', () => import('../parts/tutorial.js').then((t) => t.launch())),
          h('div', { class: 'muted', text: session.demo ? '架空のデータで、検索からダウンロードまでの流れを体験します。'
            : '架空のデータで、検索からダウンロードまでの流れを体験します。デモモードの FinFeT を起動し、新しいタブで開きます（実際の Azure には接続しません）。' })]);
      case 'reset':
        return row('', button('設定を既定に戻す', async () => {
          const ok = await confirmDialog({ title: '設定を既定に戻しますか', body: 'すべての設定を既定値に戻します。保存されているプロキシのパスワードも削除されます。', okText: '既定に戻す' });
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
  ctx.setGuard(() => (Object.keys(changes()).length ? '保存されていない設定の変更があります。' : null));

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
