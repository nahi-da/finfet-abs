// 時間のかかる処理（202 と処理の ID。結果はイベント task.done）の待ち合わせ（5.7.1）。

import { ApiError, request } from './api.js';
import { on } from './events.js';

const done = new Map();
const waiting = new Map();

on('task.done', (d) => {
  const w = waiting.get(d.id);
  if (w) {
    waiting.delete(d.id);
    w(d);
  } else {
    done.set(d.id, d);
    if (done.size > 100) done.delete(done.keys().next().value);
  }
});

/**
 * 処理を始めて、結果を待ちます。
 * @param {string} method
 * @param {string} url
 * @param {any} [body]
 * @param {(taskId: string) => void} [onStart] 処理の ID が決まったときに呼びます（中止や進み具合の表示に使います）
 * @returns {Promise<any>} 処理の結果
 */
export async function runTask(method, url, body, onStart) {
  const res = await request(method, url, method === 'GET' ? undefined : (body ?? {}));
  if (!res || !res.task_id) return res;
  if (onStart) onStart(res.task_id);
  const d = await new Promise((resolve) => {
    if (done.has(res.task_id)) {
      resolve(done.get(res.task_id));
      done.delete(res.task_id);
    } else {
      waiting.set(res.task_id, resolve);
    }
  });
  if (!d.ok) throw new ApiError(400, d.error || {});
  return d.result;
}
