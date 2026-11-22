// ジョブの作成のダイアログ（4.4.7）と、保存するパスの重複の確認のダイアログ（4.4.8）。

import * as api from '../api.js';
import { button, clear, debounce, h, linkButton } from '../dom.js';
import { dateShort, num, size } from '../format.js';
import { openDialog } from './dialog.js';
import { pickFolder } from './folder.js';
import { checkbox, checkInput, focusFirstError, formGrid, radios, radioValue, row, select, setError, showRow, textInput } from './form.js';
import { toast } from './toast.js';
import { VirtualTable } from './table.js';

const TIER_ORDER = ['Archive', 'rehydrating', 'Hot', 'Cool', 'Cold', ''];

/** 画面の呼び出し元の指定を、API の source にします。 */
function toSource(opt) {
  if (opt.source === 'search') return { kind: 'search', search_id: opt.searchId, only_visible: opt.filter || null };
  if (opt.source === 'urls') return { kind: 'urls', token: opt.token, auth: opt.auth || {} };
  return { kind: 'job', job_id: opt.jobId, item_ids: opt.itemIds || null };
}

function tierLabel(t) {
  return t === 'rehydrating' ? 'リハイドレート中' : (t || '層なし');
}

/**
 * ジョブの作成のダイアログを開きます。
 * @param {{source: 'search'|'urls'|'job', searchId?: string, filter?: Object, token?: string, auth?: Object,
 *          jobId?: string, itemIds?: number[], title?: string, initial?: Object, submit?: (body: Object) => Promise<string>}} opt
 * @returns {Promise<string|null>} 作ったジョブの ID
 */
export async function openCreateJobDialog(opt) {
  const source = toSource(opt);
  const [pv, st] = await Promise.all([api.post('/api/v1/jobs/preview', { source }), api.get('/api/v1/settings')]);
  const s = st.values;
  const init = {
    method: s['fetch.method'], target_tier: s['rehydrate.target_tier'], priority: s['rehydrate.priority'],
    directory: s['fetch.directory'], job_folder: s['fetch.job_folder'], path_structure: s['fetch.path_structure'],
    path_template: s['fetch.path_template'], conflict: s['fetch.conflict'], set_mtime: s['fetch.set_mtime'],
    verify_md5: s['fetch.verify_md5'], extract: s['fetch.extract_archives'], ...(opt.initial || {}),
  };
  if (pv.url_mode && init.path_structure === 'template' && /\{\{(?!@)/.test(init.path_template || '')) init.path_template = '';

  return new Promise((resolve) => {
    let result = null;
    let dupChoice = null; // 'suggest' / 'number' / 'skip'
    let lastCheck = null;

    // ---------------------------------------------------------------- 対象
    const tiers = Object.entries(pv.tiers).sort((a, b) => TIER_ORDER.indexOf(a[0]) - TIER_ORDER.indexOf(b[0]));
    const tierText = tiers.map(([t, v]) => `${tierLabel(t)} ${num(v.count)} 件・${size(v.bytes)}`).join('、');
    const freeEl = h('span', { class: 'muted', text: pv.free_bytes != null ? `保存先の空き ${size(pv.free_bytes)}` : '' });
    const target = h('div', { class: 'line' }, h('span', { text: `${num(pv.count)} 件・${size(pv.bytes)}（${tierText}）` }), h('span.push'), freeEl);
    const nameIn = textInput(init.name || pv.default_name, { width: 'w-full', 'aria-label': 'ジョブ名', maxlength: 200 });

    // ---------------------------------------------------------------- 取得の設定
    const methodR = radios('job-method', [['rehydrate_download', 'リハイドレートしてダウンロード'], ['rehydrate_only', 'リハイドレートのみ'], ['download_only', 'ダウンロードのみ']], init.method, () => update());
    const tierSel = select([['Hot', 'Hot'], ['Cool', 'Cool'], ['Cold', 'Cold']], init.target_tier, { 'aria-label': '復元先の層' });
    const prioSel = select([['Standard', '標準'], ['High', '高']], init.priority, { 'aria-label': '優先度', onchange: () => update() });
    const dirIn = textInput(init.directory, { width: 'w-long', 'aria-label': '保存先', oninput: () => recheck() });
    const dirBtn = button('選択', async () => {
      const r = await pickFolder({ title: '保存先を選択', initial: dirIn.value });
      if (r) { dirIn.value = r.path; freeEl.textContent = `保存先の空き ${size(r.free_bytes)}`; recheck(); }
    });
    const folderChk = checkbox('ジョブのフォルダーを作る', init.job_folder, () => recheck());
    const structSel = select([['preserve', '階層を保つ'], ['flatten', '階層を名前にする'], ['name_only', 'ファイル名だけ'], ['template', '指定する']], init.path_structure, { 'aria-label': 'パス構造', onchange: () => { update(); recheck(); } });
    const conflictSel = select([['number', '番号を付ける'], ['skip', 'スキップ'], ['overwrite', '上書き']], init.conflict, { 'aria-label': '同名のファイル' });
    const tmplIn = textInput(init.path_template, { width: 'w-full', mono: true, 'aria-label': '保存するパス', list: 'job-tmpl-list', oninput: () => recheck() });
    const tmplList = h('datalist', { id: 'job-tmpl-list' });
    const examples = h('div', { class: 'field-note mono' });
    const dupLine = h('div', { class: 'line status-warn', hidden: true });
    const existLine = h('div', { class: 'field-note', hidden: true });
    const mtimeChk = checkbox('最終更新日時をファイルに設定する', init.set_mtime);
    const md5Chk = checkbox('MD5 を照合する', init.verify_md5);
    const extractChk = checkbox('ダウンロードの後に圧縮ファイルを展開する（zip・tar・gz・bz2・xz）', !!init.extract);

    const fetchRows = row('取得の方法', methodR);
    const tierRows = row('復元先の層', h('div', { class: 'line' }, tierSel, h('span', { class: 'labeled' }, h('span', { text: '優先度' }), prioSel)));
    const dirRows = row('保存先', h('div', null, h('div', { class: 'line grow-first' }, dirIn, dirBtn),
      h('div', { class: 'line' }, folderChk, h('span', { class: 'labeled' }, h('span', { text: 'パス構造' }), structSel), h('span', { class: 'labeled' }, h('span', { text: '同名のファイル' }), conflictSel))));
    const tmplRows = row('保存するパス', h('div', null, tmplIn, tmplList, examples));
    const checkRows = row('', h('div', null, dupLine, existLine, h('div', { class: 'line' }, mtimeChk, md5Chk), h('div', { class: 'line' }, extractChk)));
    const sasRows = row('認証', h('div', { class: 'status-warn' }, (pv.sas_warnings || []).map((w) => h('div', {
      text: `${w.storage_account} の SAS は ${dateShort(w.expires_at)} に期限が切れます。` + (w.before_rehydrate ? 'リハイドレートの完了の前に切れるおそれがあります。' : ''),
    }))));
    showRow(sasRows, (pv.sas_warnings || []).length > 0);

    // ---------------------------------------------------------------- リハイドレートの注意
    const earlyText = pv.early_delete_count ? `Archive に置いてから 180 日未満の Blob が ${num(pv.early_delete_count)} 件あり、早期削除の料金がかかります。` : '';
    const noteText = h('p');
    const confirmChk = checkbox('上の内容を確認しました', false, () => update());
    const rehydSec = h('div', { class: 'part rehydrate-note' }, noteText, confirmChk);

    const startChk = checkbox('追加したらすぐに開始する', s['queue.start_immediately']);
    const err = h('div', { class: 'field-error', role: 'alert', hidden: true });
    const addBtn = button(opt.submitText || 'キューに追加', () => submit(), { primary: true });

    const d = openDialog({
      title: opt.title || 'ジョブを作成',
      width: 960,
      body: [formGrid(...row('対象', target), ...row('ジョブ名', nameIn), ...fetchRows, ...tierRows, ...dirRows, ...tmplRows, ...checkRows, ...sasRows), rehydSec, err],
      onClose: () => resolve(result),
    });
    d.setFoot(startChk, h('span.push'), button('キャンセル', () => d.close(true)), addBtn);

    const options = () => ({
      method: radioValue(methodR), target_tier: tierSel.value, priority: prioSel.value, directory: dirIn.value.trim(),
      job_folder: checkInput(folderChk).checked, path_structure: structSel.value, path_template: tmplIn.value,
      conflict: conflictSel.value, set_mtime: checkInput(mtimeChk).checked, verify_md5: checkInput(md5Chk).checked,
      extract: checkInput(extractChk).checked,
    });

    function needsRehydrate() {
      return pv.archive_count > 0 && radioValue(methodR) !== 'download_only';
    }

    function update() {
      const m = radioValue(methodR);
      showRow(tierRows, m !== 'download_only');
      showRow(tmplRows, structSel.value === 'template');
      rehydSec.hidden = !needsRehydrate();
      const tier = tierSel.value;
      noteText.textContent = `リハイドレートは取り消せません。料金は要求した時点で発生します。復元した Blob は ${tier} 層の料金がかかり、Archive には自動で戻りません。`
        + (prioSel.value === 'High' ? '優先度「高」は料金が高くなります。' : '優先度「標準」は最大15時間程度かかります。') + earlyText;
      addBtn.disabled = needsRehydrate() && !checkInput(confirmChk).checked;
    }
    tierSel.addEventListener('change', update);

    // ---------------------------------------------------------------- 保存するパスの検査（3.3.15）
    const recheck = debounce(async () => {
      // 番号を付ける・取得しないの選択は、入力を変えたら選び直します（提案に変えた後は、重複が出なければそのまま）
      if (dupChoice !== 'suggest') dupChoice = null;
      let r;
      try {
        r = await api.post('/api/v1/jobs/path-check', { source, options: options() });
      } catch (e) {
        setError(dirIn, e.data && e.data.field === 'directory' ? e.message : null);
        return;
      }
      lastCheck = r;
      if (dupChoice === 'suggest' && r.duplicates) dupChoice = null;
      setError(tmplIn, null);
      clear(examples);
      if (!r.ok) {
        if (structSel.value === 'template') setError(tmplIn, r.issues.map((i) => i.message).join(' '));
      } else if (structSel.value === 'template') {
        for (const e of r.examples) examples.append(h('div', { text: `例: …/${e.blob.split('/').slice(-3).join('/')} → ${e.path}` }));
      }
      drawDup();
      existLine.hidden = !r.existing;
      existLine.textContent = r.existing ? `保存先に同じ名前のファイルが ${num(r.existing)} 件あります（同名のファイル: ${conflictSel.selectedOptions[0].text}）` : '';
    }, 300);
    conflictSel.addEventListener('change', () => recheck());

    function drawDup() {
      clear(dupLine);
      const dup = lastCheck && lastCheck.duplicates;
      dupLine.hidden = !dup && !dupChoice;
      if (!dup && !dupChoice) return;
      if (dupChoice) {
        dupLine.className = 'line';
        dupLine.append(h('span', { text: `重複は解決しました（${{ suggest: '提案したパスに変更', number: '番号を付ける', skip: '2件目以降を取得しない' }[dupChoice]}）` }));
        return;
      }
      dupLine.className = 'line status-warn';
      dupLine.append(h('span', { text: `保存するパスが重複する Blob が ${num(dup.blobs)} 件（${num(dup.places)} か所）あります` }),
        linkButton('確認', () => openDup()));
    }

    async function openDup() {
      const choice = await openDuplicateDialog({ source, options: options(), check: lastCheck });
      if (!choice) return;
      if (choice.action === 'edit') { tmplIn.focus(); return; }
      if (choice.action === 'suggest') {
        structSel.value = 'template';
        tmplIn.value = lastCheck.suggestion;
        update();
        await recheckNow();
        dupChoice = lastCheck && lastCheck.duplicates ? null : 'suggest';
      } else {
        dupChoice = choice.action;
      }
      drawDup();
    }

    async function recheckNow() {
      lastCheck = await api.post('/api/v1/jobs/path-check', { source, options: options() });
    }

    // ---------------------------------------------------------------- 追加
    async function submit() {
      err.hidden = true;
      setError(dirIn, null);
      if (!dirIn.value.trim()) { setError(dirIn, '保存先を指定してください。'); dirIn.focus(); return; }
      if (lastCheck && lastCheck.duplicates && !dupChoice) {
        dupLine.scrollIntoView({ block: 'center' });
        dupLine.querySelector('button')?.focus();
        return;
      }
      const body = {
        source, options: options(), name: nameIn.value.trim(), start: checkInput(startChk).checked,
        dup_resolution: dupChoice === 'suggest' ? null : dupChoice, confirm_rehydrate: checkInput(confirmChk).checked,
      };
      addBtn.disabled = true;
      try {
        const id = opt.submit ? await opt.submit(body) : (await api.post('/api/v1/jobs', body)).id;
        result = id;
        d.close(true);
        toast(opt.doneText || 'ジョブをキューに追加しました。', { href: `/jobs/${id}`, linkText: 'ジョブを開く' });
      } catch (e) {
        addBtn.disabled = false;
        if (e.code === 'PATH_DUPLICATE') { await recheckNow(); drawDup(); dupLine.scrollIntoView({ block: 'center' }); return; }
        if (e.code === 'PATH_INVALID') { setError(tmplIn, e.message); tmplIn.focus(); return; }
        if (e.data && e.data.field === 'directory') { setError(dirIn, e.message); dirIn.focus(); return; }
        err.hidden = false;
        err.textContent = e.message;
        focusFirstError(d.body);
      }
    }

    for (const v of pv.recent_templates || []) tmplList.append(h('option', { value: v }));
    update();
    recheck();
    nameIn.focus();
  });
}

/**
 * 保存するパスの重複の確認のダイアログ（4.4.8）。
 * @returns {Promise<{action: 'suggest'|'number'|'skip'|'edit'}|null>}
 */
export function openDuplicateDialog({ source, options, check }) {
  return new Promise((resolve) => {
    let result = null;
    const dup = check.duplicates;
    const table = new VirtualTable({
      columns: [
        { key: 'path', label: '保存するパス', width: 320, render: (r) => h('span', { class: 'mono ellipsis', title: r.first ? r.path : '', text: r.first ? r.path : '' }) },
        { key: 'blob', label: 'Blob', width: 480, flex: true, render: (r) => h('span', { class: 'ellipsis', title: r.blob, text: r.blob }) },
      ],
      rowClass: (r) => (r.first ? 'group-first' : ''),
      fetch: async (offset, limit) => {
        // 組ごとに Blob を並べます（組の数ではなく行の数でページを分けるため、まとめて取得します）
        if (!table.rowsCache) {
          const res = await api.post(`/api/v1/jobs/path-check/groups${api.qs({ offset: 0, limit: 1000 })}`, { source, options });
          const rows = [];
          for (const g of res.items) {
            g.blobs.forEach((b, i) => rows.push({ id: `${g.path}|${i}`, first: i === 0, path: g.path, blob: `${b.storage_account} / ${b.container}   ${b.blob_name}` }));
          }
          table.rowsCache = rows;
        }
        return { total: table.rowsCache.length, items: table.rowsCache.slice(offset, offset + limit) };
      },
      rowId: (r) => r.id,
      minHeight: 240,
      bottomReserve: 320,
    });
    const opts = [];
    if (check.suggestion) opts.push(['suggest', '提案したパスに変える']);
    opts.push(['number', '番号を付ける'], ['skip', '2件目以降を取得しない']);
    const choice = radios('dup-choice', opts, opts[0][0], () => {});
    const notes = h('div', { class: 'kv' },
      check.suggestion ? [h('span', { class: 'muted', text: '提案' }), h('span', { class: 'mono', text: `${check.suggestion}   例: ${(check.suggestion_examples || []).join('、')}` })] : null,
      h('span', { class: 'muted', text: '番号' }), h('span', { class: 'mono', text: `例: ${(check.number_examples || []).join('、')}` }),
      h('span', { class: 'muted', text: '取得しない' }), h('span', { text: `${num(dup.skip_count)} 件を取得しません` }));
    const d = openDialog({
      title: '保存するパスの重複',
      width: 960,
      body: [h('p', { text: `${num(dup.blobs)} 件の Blob が、${num(dup.places)} か所の同じパスに保存されます。` }), table.el,
        h('h3', { text: '解決の方法' }), choice, notes],
      onClose: () => { table.destroy(); resolve(result); },
    });
    d.setFoot(h('span.push'), button('パスを自分で直す', () => { result = { action: 'edit' }; d.close(true); }),
      button('決定', () => { result = { action: radioValue(choice) }; d.close(true); }, { primary: true }));
    table.reload();
  });
}
