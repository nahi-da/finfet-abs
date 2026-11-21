// 数・サイズ・日時の表示（4.1.2、4.11）。

/** 3桁ごとに , を入れます。 */
export function num(n) {
  if (n === null || n === undefined || Number.isNaN(n)) return '';
  return Number(n).toLocaleString('en-US');
}

const UNITS = ['B', 'KB', 'MB', 'GB', 'TB', 'PB'];

/** サイズ（1024 進・小数1桁。850 B、12.4 KB）。 */
export function size(bytes) {
  if (bytes === null || bytes === undefined) return '';
  let v = Number(bytes);
  let i = 0;
  while (v >= 1024 && i < UNITS.length - 1) {
    v /= 1024;
    i += 1;
  }
  if (i === 0) return `${num(v)} B`;
  return `${v.toFixed(1)} ${UNITS[i]}`;
}

/** 速度（MB/s 相当）。 */
export function speed(bytesPerSec) {
  if (!bytesPerSec) return '0 B/s';
  return `${size(bytesPerSec)}/s`;
}

/** MB/s を Mbps に換算した表示（「≒ 84 Mbps」）。 */
export function mbps(mbPerSec) {
  const v = Number(mbPerSec) * 1024 * 1024 * 8 / 1e6;
  if (!v) return '';
  return `≒ ${num(Math.round(v))} Mbps`;
}

const pad = (n) => String(n).padStart(2, '0');

/** ISO 8601 → Date。 */
export function toDate(iso) {
  if (!iso) return null;
  const d = new Date(iso);
  return Number.isNaN(d.getTime()) ? null : d;
}

/** 表の日時（同じ年は「11/20 09:12」、別の年は「2025/11/20」）。 */
export function dateShort(iso) {
  const d = toDate(iso);
  if (!d) return '';
  const now = new Date();
  if (d.getFullYear() === now.getFullYear()) return `${pad(d.getMonth() + 1)}/${pad(d.getDate())} ${pad(d.getHours())}:${pad(d.getMinutes())}`;
  return `${d.getFullYear()}/${pad(d.getMonth() + 1)}/${pad(d.getDate())}`;
}

/** 正確な日時（ホバーの表示）。 */
export function dateFull(iso) {
  const d = toDate(iso);
  if (!d) return '';
  return `${d.getFullYear()}/${pad(d.getMonth() + 1)}/${pad(d.getDate())} ${pad(d.getHours())}:${pad(d.getMinutes())}:${pad(d.getSeconds())}`;
}

/** 時刻だけ（「10:40」）。日が違えば日付を付けます。 */
export function timeShort(iso) {
  const d = toDate(iso);
  if (!d) return '';
  const now = new Date();
  const t = `${pad(d.getHours())}:${pad(d.getMinutes())}`;
  if (d.toDateString() === now.toDateString()) return t;
  return `${pad(d.getMonth() + 1)}/${pad(d.getDate())} ${t}`;
}

/** 日付だけ（「11/20」）。 */
export function dayShort(iso) {
  const d = toDate(iso);
  if (!d) return '';
  return `${pad(d.getMonth() + 1)}/${pad(d.getDate())}`;
}

/** 経過時間（「3.2 秒」「4 分」）。 */
export function duration(ms) {
  if (ms === null || ms === undefined) return '';
  if (ms < 60000) return `${(ms / 1000).toFixed(1)} 秒`;
  const m = Math.round(ms / 60000);
  if (m < 60) return `${m} 分`;
  return `${Math.floor(m / 60)} 時間 ${m % 60} 分`;
}

/** ローカル時刻の入力欄（datetime-local）の値。 */
export function toLocalInput(iso) {
  const d = toDate(iso);
  if (!d) return '';
  return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}T${pad(d.getHours())}:${pad(d.getMinutes())}`;
}
