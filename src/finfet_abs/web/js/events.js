// イベントの受信（ロングポーリング。5.8）。

import { get } from './api.js';

/** @type {Map<string, Set<Function>>} */
const listeners = new Map();
let last = null;
let running = false;
const RETRY = [1000, 2000, 5000];

/**
 * イベントを受け取る関数を登録します。戻り値の関数で解除します。
 * type に '*' を指定すると、すべてのイベントを受け取ります。'reset' は取りこぼしたときに呼ばれます。
 * @param {string} type
 * @param {(data: any, ev: any) => void} fn
 */
export function on(type, fn) {
  if (!listeners.has(type)) listeners.set(type, new Set());
  listeners.get(type).add(fn);
  return () => listeners.get(type)?.delete(fn);
}

function emit(type, data, ev) {
  for (const key of [type, '*']) {
    for (const fn of listeners.get(key) || []) {
      try { fn(data, ev); } catch (e) { console.error(e); }
    }
  }
}

/** 接続の状態の表示を切り替える関数（main.js が設定します）。 */
export let onConnection = () => {};
export function setConnectionHandler(fn) { onConnection = fn; }

/**
 * 受信を開始します。
 * @param {number} start 最初の番号
 */
export async function start(start) {
  if (running) return;
  running = true;
  last = start;
  let failures = 0;
  while (running) {
    try {
      const res = await get(`/api/v1/events?after=${last}`);
      if (failures) onConnection(true);
      failures = 0;
      if (res.reset) {
        last = res.last;
        emit('reset', {}, null);
        continue;
      }
      for (const ev of res.events || []) emit(ev.type, ev.data, ev);
      last = res.last;
    } catch (e) {
      failures += 1;
      onConnection(false);
      await new Promise((r) => setTimeout(r, RETRY[Math.min(failures - 1, RETRY.length - 1)]));
    }
  }
}
