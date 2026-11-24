// fetch のラッパー（CSRF のヘッダーの付与、エラーの共通処理）。

let csrf = null;
/** @type {Object|null} */
export let session = null;

/** API のエラー。 */
export class ApiError extends Error {
  /**
   * @param {number} status
   * @param {{code?: string, message?: string, detail?: string, fields?: Object}} err
   */
  constructor(status, err) {
    super(err.message || '要求に失敗しました。');
    this.status = status;
    this.code = err.code || 'ERROR';
    this.detail = err.detail || '';
    this.fields = err.fields || null;
    this.data = err;
  }
}

/** セッションを取得します（起動時に1回）。 */
export async function initSession() {
  session = await get('/api/v1/session');
  csrf = session.csrf;
  return session;
}

/**
 * 要求を送ります。
 * @param {string} method
 * @param {string} url
 * @param {any} [body]
 * @param {{signal?: AbortSignal, raw?: boolean, keepalive?: boolean}} [opt]
 */
export async function request(method, url, body, opt = {}) {
  const headers = { Accept: 'application/json' };
  const init = { method, headers, credentials: 'same-origin', signal: opt.signal, cache: 'no-store' };
  // タブを閉じるときの知らせは、ページが閉じた後も送り終えるようにします
  if (opt.keepalive) init.keepalive = true;
  if (method !== 'GET' && method !== 'HEAD') {
    if (csrf) headers['X-FinFeT-CSRF'] = csrf;
    if (body !== undefined) {
      headers['Content-Type'] = 'application/json';
      init.body = JSON.stringify(body);
    }
  }
  let res;
  try {
    res = await fetch(url, init);
  } catch (e) {
    if (e.name === 'AbortError') throw e;
    throw new ApiError(0, { code: 'NETWORK', message: '本ツールに接続できません（本ツールが終了した可能性があります）。' });
  }
  if (opt.raw) {
    if (!res.ok) throw await toError(res);
    return res;
  }
  const text = await res.text();
  let data = null;
  if (text) {
    try { data = JSON.parse(text); } catch { data = null; }
  }
  if (!res.ok) {
    if (res.status === 409 && data && !data.error) return { ...data, _status: 409 };
    throw new ApiError(res.status, (data && data.error) || { message: `要求に失敗しました（${res.status}）。` });
  }
  return data;
}

async function toError(res) {
  try {
    const data = await res.json();
    return new ApiError(res.status, data.error || {});
  } catch {
    return new ApiError(res.status, { message: `要求に失敗しました（${res.status}）。` });
  }
}

export const get = (url, opt) => request('GET', url, undefined, opt);
export const post = (url, body, opt) => request('POST', url, body ?? {}, opt);
export const put = (url, body, opt) => request('PUT', url, body ?? {}, opt);
export const patch = (url, body, opt) => request('PATCH', url, body ?? {}, opt);
export const del = (url, opt) => request('DELETE', url, undefined, opt);

/**
 * クエリの文字列を作ります（空の値は除きます）。
 * @param {Object} params
 */
export function qs(params) {
  const sp = new URLSearchParams();
  for (const [k, v] of Object.entries(params)) {
    if (v === null || v === undefined || v === '') continue;
    if (Array.isArray(v)) v.forEach((x) => sp.append(k, x));
    else sp.set(k, String(v));
  }
  const s = sp.toString();
  return s ? `?${s}` : '';
}

/**
 * POST の応答をファイルとして保存させます（ブラウザのダウンロード）。
 * @param {string} url
 * @param {any} body
 * @param {string} fallbackName
 */
export async function downloadPost(url, body, fallbackName) {
  const res = await request('POST', url, body, { raw: true });
  return saveResponse(res, fallbackName);
}

/** GET の応答をファイルとして保存させます。 */
export async function downloadGet(url, fallbackName) {
  const res = await request('GET', url, undefined, { raw: true });
  return saveResponse(res, fallbackName);
}

async function saveResponse(res, fallbackName) {
  const blob = await res.blob();
  let name = fallbackName;
  const disp = res.headers.get('Content-Disposition') || '';
  const m = disp.match(/filename\*=UTF-8''([^;]+)/);
  if (m) name = decodeURIComponent(m[1]);
  const a = document.createElement('a');
  const href = URL.createObjectURL(blob);
  a.href = href;
  a.download = name;
  document.body.appendChild(a);
  a.click();
  a.remove();
  setTimeout(() => URL.revokeObjectURL(href), 10000);
  return name;
}
