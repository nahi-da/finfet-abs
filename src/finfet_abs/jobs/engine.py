"""ジョブの実行（3.3.4 の実行の枠、3.3.7〜3.3.10）。

- JobEngine: 実行の枠の割り当て（スケジューラー）と、リハイドレートの完了の確認（ポーリング）。
- JobRunner: 1つのジョブの処理。確認・リハイドレートの要求・ダウンロードの3つの段階が、それぞれ Blob の状態を見て
  並行に進みます（ある Blob がリハイドレートを待っている間も、ほかの Blob の確認やダウンロードは進みます）。
"""

from __future__ import annotations

import base64
import hashlib
import json
import logging
import os
import threading
import time
from collections import deque
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta
from pathlib import Path
from typing import Callable

from ..azure.backend import AccountRef, AuthRef, BackendError, BlobInfo, ContainerRef
from ..core.clock import SYSTEM_CLOCK, Clock, from_iso, to_iso
from ..core.errors import AppError
from . import paths
from .limits import Cancelled, Limits
from .service import ACTIVE_ITEMS, ENDED, ITEM_LABELS, STATUS_LABELS, JobService, expand_dir, free_bytes, windows_env

log = logging.getLogger(__name__)

#: 一覧でまとめて状態を確かめる件数の目安（3.3.7）
LIST_CHECK_MIN = 20
BATCH = 200
LOCK_STALE_SECONDS = 120
HEARTBEAT_SECONDS = 30
DOWNLOAD_RESUME_TRIES = 3
FREE_MARGIN = 1024 ** 3
_READY_TIERS = ("Hot", "Cool", "Cold", "Premium")
#: 認証が使えないことを表すエラー（3.1.9・3.3.9）
SIGNIN_CODES = ("NOT_SIGNED_IN", "INTERACTION_REQUIRED")
CREDENTIAL_CODES = ("NO_CREDENTIAL",)


class JobLog:
    """ジョブのログ（data\\logs\\jobs\\<ジョブID>.log）。"""

    def __init__(self, logs_dir: Path, job_id: str, clock: Clock) -> None:
        self.path = Path(logs_dir) / f"{job_id}.log"
        self.clock = clock
        self._lock = threading.Lock()

    def write(self, level: str, message: str, blob: str | None = None) -> None:
        line = f"{to_iso(self.clock.now())}\t{level}\t{blob or ''}\t{message}\n"
        with self._lock:
            try:
                self.path.parent.mkdir(parents=True, exist_ok=True)
                with open(self.path, "a", encoding="utf-8") as f:
                    f.write(line)
            except OSError:
                log.exception("ジョブのログを書けませんでした")


def item_target(row) -> ContainerRef:
    auth = AuthRef(row["auth_kind"] or "entra_user", credential_id=row["credential_id"], secret_id=row["secret_id"],
                   tenant_id=row["tenant_id"])
    return ContainerRef(AccountRef(row["storage_account"], row["endpoint_suffix"] or "core.windows.net", auth),
                        row["container"])


def classify(info: BlobInfo, method: str) -> tuple[str, str | None]:
    """Blob の情報から、次の状態を決めます（3.3.7 の分類の規則）。"""
    if info.archive_status and info.archive_status.startswith("rehydrate-pending-to-"):
        return "rehydrating", None
    if info.blob_type in ("AppendBlob", "PageBlob") or not info.tier:
        return "ready", None
    if info.tier == "Archive":
        if method == "download_only":
            return "skipped", "Archive のため（取得の方法: ダウンロードのみ）"
        return "rehydrate_queued", None
    if info.tier in _READY_TIERS or info.tier.startswith("P"):
        return "ready", None
    return "error", "不明な層です。"


def _err(exc: BaseException) -> tuple[str, str]:
    if isinstance(exc, (BackendError, AppError)):
        return exc.code, exc.message
    return "INTERNAL", "内部のエラーが発生しました。詳しくはログを確認してください。"


class Speed:
    """直近 5 秒の移動平均の速度と、直近 60 秒の推移（3.4.1）。"""

    def __init__(self, clock: Callable[[], float] = time.monotonic) -> None:
        self._clock = clock
        self._events: deque = deque()
        self._lock = threading.Lock()

    def add(self, n: int) -> None:
        with self._lock:
            self._events.append((self._clock(), n))
            cutoff = self._clock() - 60
            while self._events and self._events[0][0] < cutoff:
                self._events.popleft()

    def rate(self, window: float = 5.0) -> float:
        now = self._clock()
        with self._lock:
            n = sum(b for t, b in self._events if t >= now - window)
        return n / window

    def history(self) -> list[float]:
        """直近 60 秒の 1 秒ごとのバイト数。"""
        now = self._clock()
        out = [0.0] * 60
        with self._lock:
            for t, b in self._events:
                i = int(now - t)
                if 0 <= i < 60:
                    out[59 - i] += b
        return out


class JobEngine:
    def __init__(self, *, db, settings, events, service: JobService, backend: Callable[[], object], limits: Limits,
                 logs_dir: Path, clock: Clock = SYSTEM_CLOCK, time_scale: Callable[[], float] | None = None,
                 resolve_auth: Callable[[dict], dict] | None = None) -> None:
        self.db = db
        self.settings = settings
        self.events = events
        self.service = service
        self.backend = backend
        self.limits = limits
        self.logs_dir = Path(logs_dir)
        self.clock = clock
        #: デモモードで、完了の確認の間隔を縮める割合（8.3.3。1 分 = 1 時間なら 60）
        self.time_scale = time_scale or (lambda: 1.0)
        #: 呼び出したときの認証の決め方（3.1.9）。保存した認証情報が削除されたときの探し直しに使います
        self.resolve_auth = resolve_auth
        self._startup_poll = False
        self.speed = Speed()
        self.md5_lock = threading.Lock()
        self._runners: dict[str, JobRunner] = {}
        self._lock = threading.RLock()
        self._wake = threading.Event()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._last_poll = 0.0
        self._instance = {**windows_env(), "pid": os.getpid()}
        service.on_change = self.wake

    # ------------------------------------------------------------ 開始・終了

    def start(self) -> None:
        # 起動時に、リハイドレート待ちのジョブを確認します（3.3.9。設定でオフにできます）
        self._startup_poll = bool(self.settings.get("rehydrate.check_on_startup"))
        if self._thread is None:
            self._thread = threading.Thread(target=self._loop, name="job-scheduler", daemon=True)
            self._thread.start()

    def stop(self, timeout: float = 10) -> None:
        self._stop.set()
        self._wake.set()
        with self._lock:
            runners = list(self._runners.values())
        for r in runners:
            r.stop_event.set()
        for r in runners:
            r.join(timeout)
        if self._thread is not None:
            self._thread.join(timeout)

    def wake(self) -> None:
        self._wake.set()

    def notify(self, level: str, text: str, job_id: str | None = None) -> None:
        """上部のバーの「お知らせ」に残すもの（4.3.5。リハイドレートの完了、ジョブの終了、エラー）。"""
        self.events.publish("notify", {"level": level, "text": text, "url": f"/jobs/{job_id}" if job_id else None})

    def auth_problem(self, job_id: str, code: str, message: str) -> None:
        """認証が使えないことを、ジョブの一覧と詳細に表示します（3.3.9）。"""
        text = "サインインが必要です。" if code in SIGNIN_CODES else "認証情報がありません。接続先を追加してください。"
        row = self.db.query_one("SELECT last_error_json FROM jobs WHERE id=?", (job_id,))
        cur = json.loads(row["last_error_json"]) if row and row["last_error_json"] else None
        if cur and cur.get("code") == code:
            return
        self.db.execute("UPDATE jobs SET last_error_json=? WHERE id=?",
                        (json.dumps({"code": code, "message": text, "detail": message}, ensure_ascii=False), job_id))
        self.notify("warn", f"「{self.job_name(job_id)}」: {text}", job_id)
        self.service._publish(job_id)

    def clear_auth_problem(self, job_id: str) -> None:
        self.db.execute("UPDATE jobs SET last_error_json=NULL WHERE id=? AND last_error_json LIKE '%\"code\"%' AND "
                        "(last_error_json LIKE '%NOT_SIGNED_IN%' OR last_error_json LIKE '%INTERACTION_REQUIRED%' OR "
                        "last_error_json LIKE '%NO_CREDENTIAL%')", (job_id,))

    def reauth(self, rows: list) -> bool:
        """保存した認証情報が削除された Blob の認証を、3.1.9 の規則で探し直します。変えたものがあれば True。"""
        if self.resolve_auth is None:
            return False
        changed = False
        cache: dict[tuple, dict] = {}
        for r in rows:
            if not r["credential_id"] or r["secret_id"]:
                continue
            key = (r["storage_account"], r["endpoint_suffix"], r["container"], r["auth_kind"], r["credential_id"])
            if key not in cache:
                cache[key] = self.resolve_auth({"storage_account": r["storage_account"], "endpoint_suffix": r["endpoint_suffix"],
                                                "container": r["container"], "auth_kind": r["auth_kind"],
                                                "credential_id": r["credential_id"], "tenant": r["tenant_id"]})
            res = cache[key]
            if res.get("resolved") and (res.get("credential_id") != r["credential_id"] or res.get("auth_kind") != r["auth_kind"]):
                self.db.execute("UPDATE job_items SET auth_kind=?, credential_id=? WHERE id=?",
                                (res.get("auth_kind"), res.get("credential_id"), r["id"]))
                changed = True
        if changed:
            JobLog(self.logs_dir, rows[0]["job_id"], self.clock).write("INFO", "削除された認証情報の代わりに、使える認証情報を使います")
        return changed

    def auth_check(self, job_id: str) -> None:
        """開始の前に、未完了の Blob の認証が使えるかを確かめます（3.3.11）。使えなければ AppError。"""
        if self.resolve_auth is None:
            return
        rows = self.db.query("SELECT * FROM job_items WHERE job_id=? AND status IN ('pending','checking','rehydrate_queued',"
                             "'rehydrating','ready','downloading','completed') AND secret_id IS NULL "
                             "GROUP BY storage_account, endpoint_suffix, container, auth_kind, credential_id", (job_id,))
        for r in rows:
            res = self.resolve_auth({"storage_account": r["storage_account"], "endpoint_suffix": r["endpoint_suffix"],
                                     "container": r["container"], "auth_kind": r["auth_kind"],
                                     "credential_id": r["credential_id"], "tenant": r["tenant_id"]})
            if res.get("resolved"):
                continue
            if r["auth_kind"] in ("entra_user", "entra_key") and not r["credential_id"]:
                code, text = "NOT_SIGNED_IN", "サインインが必要です。サインインしてから開始してください。"
            else:
                code, text = "NO_CREDENTIAL", "認証情報がありません。接続のページで接続先を追加してから開始してください。"
            self.auth_problem(job_id, code, text)
            raise AppError(code, text, status=409)
        all_rows = self.db.query("SELECT * FROM job_items WHERE job_id=? AND credential_id IS NOT NULL AND secret_id IS NULL",
                                 (job_id,))
        if all_rows:
            self.reauth(all_rows)
        self.clear_auth_problem(job_id)

    def job_name(self, job_id: str) -> str:
        return self.db.scalar("SELECT name FROM jobs WHERE id=?", (job_id,)) or "ジョブ"

    def running_ids(self) -> list[str]:
        with self._lock:
            return [k for k, r in self._runners.items() if r.is_alive()]

    def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                self.schedule()
                self.heartbeat()
                if self._startup_poll:
                    self._startup_poll = False
                    self._last_poll = time.monotonic()
                    n = self.poll(force=True)
                    if n:
                        log.info("起動時にリハイドレートの完了を確認しました（%s 件）", n)
                elif time.monotonic() - self._last_poll >= (15 if self.time_scale() <= 1 else 5):
                    self._last_poll = time.monotonic()
                    self.poll()
            except Exception:  # noqa: BLE001
                log.exception("ジョブのスケジューラーでエラーが発生しました")
            self._wake.wait(1.0)
            self._wake.clear()

    # ------------------------------------------------------------ 実行の枠（3.3.4）

    def schedule(self) -> None:
        """実行中のジョブの止める指示を伝え、空いた枠に開始待ちのジョブを割り当てます。"""
        with self._lock:
            for jid, r in list(self._runners.items()):
                if not r.is_alive():
                    del self._runners[jid]
                    continue
                row = self.db.query_one("SELECT status FROM jobs WHERE id=?", (jid,))
                if row is None or row["status"] != "running":
                    r.stop_event.set()
            slots = int(self.settings.get("queue.concurrent_jobs")) - len(self._runners)
            if slots <= 0 or self._stop.is_set():
                return
            for row in self.db.query("SELECT id FROM jobs WHERE status='queued' ORDER BY queue_order"):
                if slots <= 0:
                    break
                if row["id"] in self._runners:
                    continue
                if not self._take_lock(row["id"]):
                    continue
                runner = JobRunner(self, row["id"])
                self._runners[row["id"]] = runner
                runner.start()
                slots -= 1

    def _take_lock(self, job_id: str) -> bool:
        """ジョブのロック（3.3.12）。ほかのプロセスが 2 分以内に更新したロックがあれば取りません。"""
        now = self.clock.now()
        me = json.dumps(self._instance, ensure_ascii=False)

        def fn(c):
            r = c.execute("SELECT status, lock_json, lock_heartbeat_at FROM jobs WHERE id=?", (job_id,)).fetchone()
            if r is None or r[0] != "queued":
                return False
            if r[1] and r[1] != me:
                hb = from_iso(r[2])
                if hb is not None and (now - hb).total_seconds() < LOCK_STALE_SECONDS:
                    return False
            c.execute("UPDATE jobs SET status='running', lock_json=?, lock_heartbeat_at=?, updated_at=?, "
                      "started_at=IFNULL(started_at, ?), ended_at=NULL WHERE id=?",
                      (me, to_iso(now), to_iso(now), to_iso(now), job_id))
            return True
        ok = self.db.write(fn)
        if ok:
            self.service._publish(job_id)
        return ok

    def heartbeat(self) -> None:
        ids = self.running_ids()
        if ids:
            now = to_iso(self.clock.now())
            self.db.submit(lambda c: c.executemany("UPDATE jobs SET lock_heartbeat_at=? WHERE id=?", [(now, i) for i in ids]))

    # ------------------------------------------------------------ 完了の確認（3.3.9）

    def poll_interval(self, priority: str | None, first: bool) -> timedelta:
        p = "high" if priority == "High" else "standard"
        key = f"rehydrate.poll_first_minutes.{p}" if first else f"rehydrate.poll_interval_minutes.{p}"
        return timedelta(minutes=int(self.settings.get(key)) / max(1.0, float(self.time_scale())))

    def poll(self, job_id: str | None = None, force: bool = False) -> int:
        """完了の確認の時刻が来たリハイドレート中の Blob を確かめます。戻り値は確かめた件数。"""
        now = self.clock.now()
        sql = ("SELECT i.* FROM job_items i JOIN jobs j ON j.id=i.job_id WHERE i.status='rehydrating' AND "
               "j.status NOT IN ('paused','held','completed','completed_with_errors','cancelled')")
        params: list = []
        if not force:
            sql += " AND (i.next_check_at IS NULL OR i.next_check_at<=?)"
            params.append(to_iso(now))
        if job_id:
            sql += " AND i.job_id=?"
            params.append(job_id)
        rows = self.db.query(sql, params)
        if not rows:
            return 0
        groups: dict[tuple, list] = {}
        for r in rows:
            key = (r["storage_account"], r["endpoint_suffix"], r["container"], r["auth_kind"], r["credential_id"],
                   r["secret_id"], r["tenant_id"])
            groups.setdefault(key, []).append(r)
        jobs_touched: set[str] = set()
        backend = self.backend()
        for items in groups.values():
            infos, errors = fetch_infos(backend, self.limits, items)
            auth_errs = {errors[r["id"]] for r in items if r["id"] in errors and errors[r["id"]][0] in SIGNIN_CODES + CREDENTIAL_CODES}
            if auth_errs:
                if any(c in CREDENTIAL_CODES for c, _m in auth_errs) and self.reauth(items):
                    continue  # 次の確認で、新しい認証情報で確かめます
                for jid in {r["job_id"] for r in items}:
                    code, msg = sorted(auth_errs)[0]
                    self.auth_problem(jid, code, msg)
            else:
                for jid in {r["job_id"] for r in items}:
                    self.clear_auth_problem(jid)
            updates = []
            for r in items:
                jobs_touched.add(r["job_id"])
                if r["id"] in errors:
                    code, msg = errors[r["id"]]
                    if code == "BlobNotFound":
                        updates.append(("error", r, {"error_code": code, "error_message": msg}))
                    else:
                        updates.append(("recheck", r, {}))
                        JobLog(self.logs_dir, r["job_id"], self.clock).write("WARN", f"完了の確認に失敗しました: {msg}",
                                                                           r["blob_name"])
                    continue
                info = infos[r["id"]]
                if info.archive_status and info.archive_status.startswith("rehydrate-pending"):
                    updates.append(("recheck", r, {}))
                elif info.tier == "Archive":
                    updates.append(("error", r, {"error_code": "RehydrationLost",
                                                 "error_message": "リハイドレートが行われていません。再要求してください。"}))
                else:
                    updates.append(("done", r, {"tier": info.tier, "access_tier_change_time": to_iso(info.access_tier_change_time),
                                                "etag": info.etag, "size": info.size, "content_md5": info.content_md5}))
            self._apply_poll(updates, now)
        for jid in jobs_touched:
            self._after_poll(jid)
        return len(rows)

    def _apply_poll(self, updates: list, now: datetime) -> None:
        method_of = {}

        def fn(c):
            for kind, r, extra in updates:
                if kind == "recheck":
                    nxt = now + self.poll_interval(r["rehydrate_priority"], False)
                    c.execute("UPDATE job_items SET next_check_at=? WHERE id=? AND status='rehydrating'", (to_iso(nxt), r["id"]))
                elif kind == "error":
                    c.execute("UPDATE job_items SET status='error', error_code=?, error_message=?, updated_at=? "
                              "WHERE id=? AND status='rehydrating'",
                              (extra["error_code"], extra["error_message"], to_iso(now), r["id"]))
                else:
                    jid = r["job_id"]
                    if jid not in method_of:
                        method_of[jid] = json.loads(c.execute("SELECT options_json FROM jobs WHERE id=?",
                                                              (jid,)).fetchone()[0])["method"]
                    new = "completed" if method_of[jid] == "rehydrate_only" else "ready"
                    c.execute("UPDATE job_items SET status=?, status_reason=?, tier=?, archive_status=NULL, "
                              "rehydrate_completed_detected_at=?, rehydrate_completed_azure_at=?, access_tier_change_time=?, "
                              "etag=IFNULL(?, etag), size=IFNULL(?, size), content_md5=IFNULL(?, content_md5), "
                              "next_check_at=NULL, updated_at=? WHERE id=? AND status='rehydrating'",
                              (new, "リハイドレート済み" if new == "completed" else None, extra["tier"], to_iso(now),
                               extra["access_tier_change_time"], extra["access_tier_change_time"], extra["etag"],
                               extra["size"], extra["content_md5"], to_iso(now), r["id"]))
        self.db.write(fn)
        done = [r for kind, r, _ in updates if kind == "done"]
        for r in done:
            JobLog(self.logs_dir, r["job_id"], self.clock).write("INFO", "リハイドレートが完了しました", r["blob_name"])
        if done:
            self.events.publish("rehydrate.done", {"count": len(done), "job_ids": sorted({r["job_id"] for r in done})})
            by_job: dict[str, int] = {}
            for r in done:
                by_job[r["job_id"]] = by_job.get(r["job_id"], 0) + 1
            for jid, n in by_job.items():
                self.notify("ok", f"「{self.job_name(jid)}」のリハイドレートが完了しました（{n:,} 件）", jid)

    def _after_poll(self, job_id: str) -> None:
        """リハイドレート待ちのジョブを、状態に合わせて進めます。"""
        row = self.db.query_one("SELECT * FROM jobs WHERE id=?", (job_id,))
        if row is None:
            return
        method = json.loads(row["options_json"])["method"]
        n_ready = self.db.scalar("SELECT COUNT(*) FROM job_items WHERE job_id=? AND status='ready'", (job_id,))
        n_active = self.db.scalar(f"SELECT COUNT(*) FROM job_items WHERE job_id=? AND status IN "
                                  f"({','.join('?' * len(ACTIVE_ITEMS))})", (job_id, *ACTIVE_ITEMS))
        now = to_iso(self.clock.now())
        if row["status"] == "waiting_rehydrate":
            if method != "rehydrate_only" and n_ready:
                self.db.execute("UPDATE jobs SET status='queued', updated_at=? WHERE id=? AND status='waiting_rehydrate'",
                                (now, job_id))
            elif method == "rehydrate_only" and n_active - n_ready == 0:
                if self.db.execute("UPDATE jobs SET status='waiting_download', updated_at=? WHERE id=? AND "
                                   "status='waiting_rehydrate'", (now, job_id)):
                    self.notify("warn", f"「{row['name']}」のリハイドレートが終わりました。ダウンロードを開始できます", job_id)
            elif n_active == 0:
                self.finish(job_id)
        self.service.recount(job_id)
        self.service._publish(job_id)
        self.wake()

    def finish(self, job_id: str) -> str:
        """未完了の処理がないジョブを、完了・一部失敗にします。"""
        n_err = self.db.scalar("SELECT COUNT(*) FROM job_items WHERE job_id=? AND status IN ('error','cancelled')", (job_id,))
        status = "completed_with_errors" if n_err else "completed"
        now = to_iso(self.clock.now())
        self.db.execute("UPDATE jobs SET status=?, ended_at=?, updated_at=?, lock_json=NULL, lock_heartbeat_at=NULL WHERE id=?",
                        (status, now, now, job_id))
        JobLog(self.logs_dir, job_id, self.clock).write("INFO", f"ジョブが終わりました（{STATUS_LABELS[status]}）")
        if status == "completed":
            self.notify("ok", f"「{self.job_name(job_id)}」が完了しました", job_id)
        else:
            self.notify("error", f"「{self.job_name(job_id)}」は一部が失敗しました（エラー {n_err:,} 件）", job_id)
        log.info("ジョブが終わりました（%s）", STATUS_LABELS[status])
        return status

    # ------------------------------------------------------------ Blob への操作（4.6.2）

    def check_now(self, job_id: str) -> int:
        n = self.poll(job_id, force=True)
        return n

    def raise_priority(self, job_id: str, item_ids: list[int]) -> dict:
        """リハイドレート中（標準）の Blob の優先度を高にします（3.3.8）。"""
        rows = self.db.query(f"SELECT * FROM job_items WHERE job_id=? AND id IN ({','.join('?' * len(item_ids))}) AND "
                             "status='rehydrating' AND IFNULL(rehydrate_priority, 'Standard')<>'High'",
                             [job_id, *[int(i) for i in item_ids]])
        backend = self.backend()
        ok, errors = 0, []
        now = self.clock.now()
        for r in rows:
            tier = r["rehydrate_target_tier"] or (r["archive_status"] or "rehydrate-pending-to-hot").rsplit("-", 1)[1].capitalize()
            try:
                with self.limits.request(r["storage_account"]):
                    backend.set_tier(item_target(r), r["blob_name"], tier, "High")
            except BackendError as exc:
                if exc.code != "BlobBeingRehydrated":
                    errors.append({"id": r["id"], "message": exc.message})
                    continue
            nxt = now + self.poll_interval("High", True)
            self.db.execute("UPDATE job_items SET rehydrate_priority='High', rehydrate_priority_raised_at=?, next_check_at=?, "
                            "updated_at=? WHERE id=?", (to_iso(now), to_iso(nxt), to_iso(now), r["id"]))
            JobLog(self.logs_dir, job_id, self.clock).write("INFO", "リハイドレートの優先度を高にしました", r["blob_name"])
            ok += 1
        self.service._publish(job_id)
        return {"changed": ok, "errors": errors}

    def retry_items(self, job_id: str, item_ids: list[int], action: str) -> int:
        """再試行（エラー → 待機中）・再要求（要求が消えたもの → 要求待ち）。"""
        now = to_iso(self.clock.now())
        ids = [int(i) for i in item_ids]
        if action == "re-request":
            sql = "UPDATE job_items SET status='rehydrate_queued', error_code=NULL, error_message=NULL, updated_at=? " \
                  "WHERE job_id=? AND status='error' AND error_code='RehydrationLost' AND id=?"
        else:
            sql = "UPDATE job_items SET status='pending', error_code=NULL, error_message=NULL, updated_at=? " \
                  "WHERE job_id=? AND status='error' AND id=?"
        n = self.db.write(lambda c: sum(c.execute(sql, (now, job_id, i)).rowcount for i in ids))
        if n:
            self._requeue(job_id)
        return n

    def resume(self, job_id: str) -> int:
        """終了したジョブの再開（3.3.13）。未完了の Blob だけを処理します。"""
        row = self.db.query_one("SELECT * FROM jobs WHERE id=?", (job_id,))
        if row is None or row["status"] not in ENDED:
            raise AppError("JOB_NOT_ENDED", "終了したジョブだけを再開できます。", status=409)
        now = self.clock.now()

        def fn(c):
            # リハイドレートを要求済みで、完了を確認していないものは、完了の確認から続けます
            n1 = c.execute("UPDATE job_items SET status='rehydrating', next_check_at=?, updated_at=? WHERE job_id=? AND "
                           "status IN ('cancelled','error') AND rehydrate_requested_known IS NOT NULL AND "
                           "rehydrate_completed_detected_at IS NULL AND IFNULL(error_code,'')<>'RehydrationLost'",
                           (to_iso(now), to_iso(now), job_id)).rowcount
            n2 = c.execute("UPDATE job_items SET status='pending', error_code=NULL, error_message=NULL, updated_at=? "
                           "WHERE job_id=? AND status IN ('cancelled','error')", (to_iso(now), job_id)).rowcount
            if n1 + n2:
                c.execute("UPDATE jobs SET status='queued', ended_at=NULL, updated_at=? WHERE id=?", (to_iso(now), job_id))
            return n1 + n2
        n = self.db.write(fn)
        if not n:
            raise AppError("NOTHING_TO_RESUME", "未完了の Blob がありません。", status=409)
        self.service.recount(job_id)
        self.service._publish(job_id)
        self.wake()
        return n

    def start_download(self, job_id: str, *, mode: str = "all", options: dict | None = None) -> None:
        """後日のダウンロード（3.3.11）。mode: all（残りは完了しだいダウンロード）/ completed_only。"""
        row = self.db.query_one("SELECT * FROM jobs WHERE id=?", (job_id,))
        if row is None:
            raise AppError("NOT_FOUND", "ジョブが見つかりません。", status=404)
        if row["status"] not in ("waiting_download", "waiting_rehydrate", "paused", "held"):
            raise AppError("JOB_BUSY", "このジョブはダウンロードを開始できる状態ではありません。", status=409)
        self.auth_check(job_id)
        opts = json.loads(row["options_json"])
        if options:
            from .service import clean_options
            merged = clean_options({**opts, **options}, self.settings)
            opts = {**opts, **merged}
        if mode == "all":
            opts["method"] = "rehydrate_download"
        opts["download_mode"] = mode
        now = to_iso(self.clock.now())

        def fn(c):
            c.execute("UPDATE job_items SET status='ready', status_reason=NULL, updated_at=? WHERE job_id=? AND "
                      "status='completed' AND download_completed_at IS NULL", (now, job_id))
            c.execute("UPDATE jobs SET options_json=?, status='queued', updated_at=? WHERE id=?",
                      (json.dumps(opts, ensure_ascii=False), now, job_id))
        self.db.write(fn)
        self.service.recount(job_id)
        self.service._publish(job_id)
        self.wake()

    def _requeue(self, job_id: str) -> None:
        now = to_iso(self.clock.now())
        self.db.execute("UPDATE jobs SET status='queued', ended_at=NULL, updated_at=? WHERE id=? AND status IN "
                        "('completed','completed_with_errors','waiting_rehydrate')", (now, job_id))
        self.service.recount(job_id)
        self.service._publish(job_id)
        self.wake()


def fetch_infos(backend, limits: Limits, rows: list) -> tuple[dict[int, BlobInfo], dict[int, tuple[str, str]]]:
    """同じコンテナー・認証の Blob の情報を取得します。多ければ一覧でまとめて、少なければ1件ずつ（3.3.7・3.3.9）。"""
    infos: dict[int, BlobInfo] = {}
    errors: dict[int, tuple[str, str]] = {}
    if not rows:
        return infos, errors
    target = item_target(rows[0])
    account = rows[0]["storage_account"]
    remaining = list(rows)
    if len(rows) >= LIST_CHECK_MIN:
        from ..search.listing import list_target
        from ..search.prefix import normalize
        dirs = normalize([r["blob_name"].rsplit("/", 1)[0] + "/" if "/" in r["blob_name"] else "" for r in rows])
        if len(dirs) > 32:
            common = os.path.commonprefix([r["blob_name"] for r in rows])
            dirs = [common]
        wanted = {r["blob_name"]: r for r in rows}
        found: dict[str, BlobInfo] = {}

        def on_items(items):
            for b in items:
                if b.name in wanted:
                    found[b.name] = b
        try:
            list_target(backend, target, dirs, on_items=on_items, connection=lambda: limits.request(account))
            remaining = []
            for r in rows:
                if r["blob_name"] in found:
                    infos[r["id"]] = found[r["blob_name"]]
                else:
                    remaining.append(r)
        except BackendError as exc:
            log.info("一覧での確認ができないため、1件ずつ確かめます: %s", exc.code)
    for r in remaining:
        try:
            with limits.request(account):
                infos[r["id"]] = backend.get_properties(target, r["blob_name"])
        except BackendError as exc:
            errors[r["id"]] = (exc.code, exc.message)
    return infos, errors


class JobRunner(threading.Thread):
    """1つのジョブの処理（確認・リハイドレートの要求・ダウンロード）。"""

    def __init__(self, engine: JobEngine, job_id: str) -> None:
        super().__init__(name=f"job-{job_id[:8]}", daemon=True)
        self.engine = engine
        self.db = engine.db
        self.job_id = job_id
        self.stop_event = threading.Event()
        self.clock = engine.clock
        self.log = JobLog(engine.logs_dir, job_id, engine.clock)
        self.root: Path | None = None
        self._progress: dict[int, dict] = {}
        self._plock = threading.Lock()
        self._last_publish = 0.0
        self._auth_paused = False

    # ------------------------------------------------------------ 全体

    def options(self) -> dict:
        return json.loads(self.db.query_one("SELECT options_json FROM jobs WHERE id=?", (self.job_id,))["options_json"])

    def run(self) -> None:
        try:
            self._run()
        except Exception as exc:  # noqa: BLE001
            log.exception("ジョブの処理でエラーが発生しました")
            code, msg = _err(exc)
            self._job_error(msg, pause=True)
        finally:
            self.engine.service.recount(self.job_id)
            self.engine.service._publish(self.job_id)
            self.engine.wake()

    def _run(self) -> None:
        opts = self.options()
        self.engine.clear_auth_problem(self.job_id)
        self.log.write("INFO", "ジョブを開始しました")
        if opts["method"] != "rehydrate_only" or opts.get("download_mode"):
            if not self._prepare_folder(opts):
                return
        stages = [threading.Thread(target=self._stage, args=(name, fn), name=f"{self.name}-{name}", daemon=True)
                  for name, fn in (("check", self._check_batch), ("rehydrate", self._rehydrate_batch),
                                   ("download", self._download_batch))]
        self._idle = {name: False for name in ("check", "rehydrate", "download")}
        for t in stages:
            t.start()
        for t in stages:
            t.join()
        if self.stop_event.is_set():
            # 一時停止・中止: 処理中だった Blob を戻します（中止は service が中止にしています）
            now = to_iso(self.clock.now())
            self.db.write(lambda c: (
                c.execute("UPDATE job_items SET status='pending', updated_at=? WHERE job_id=? AND status='checking'",
                          (now, self.job_id)),
                c.execute("UPDATE job_items SET status='ready', updated_at=? WHERE job_id=? AND status='downloading'",
                          (now, self.job_id))))
            self.log.write("INFO", "処理を止めました")
            return
        self._end(opts)

    def _end(self, opts: dict) -> None:
        counts = {r[0]: r[1] for r in self.db.query("SELECT status, COUNT(*) FROM job_items WHERE job_id=? GROUP BY status",
                                                    (self.job_id,))}
        now = to_iso(self.clock.now())
        if counts.get("rehydrating"):
            new = "waiting_rehydrate"
        elif opts["method"] == "rehydrate_only" and counts.get("ready"):
            new = "waiting_download"
        else:
            self.engine.finish(self.job_id)
            return
        self.db.execute("UPDATE jobs SET status=?, updated_at=?, lock_json=NULL, lock_heartbeat_at=NULL WHERE id=? AND "
                        "status='running'", (new, now, self.job_id))
        self.log.write("INFO", STATUS_LABELS[new] + "になりました")
        if new == "waiting_download":
            self.engine.notify("warn", f"「{self.engine.job_name(self.job_id)}」のリハイドレートが終わりました。ダウンロードを開始できます",
                               self.job_id)

    def _job_error(self, message: str, *, pause: bool) -> None:
        now = to_iso(self.clock.now())
        self.db.execute("UPDATE jobs SET last_error_json=?, status=CASE WHEN ? AND status='running' THEN 'paused' ELSE status END, "
                        "updated_at=?, lock_json=NULL WHERE id=?",
                        (json.dumps({"message": message}, ensure_ascii=False), 1 if pause else 0, now, self.job_id))
        self.log.write("ERROR", message)
        self.engine.notify("error", f"「{self.engine.job_name(self.job_id)}」: {message}", self.job_id)

    def _auth_blocked(self, rows: list, errors: dict, cur: str, back: str) -> bool:
        """認証が使えない Blob を戻し、探し直すか、ジョブを一時停止して「サインインが必要です」などを表示します（3.1.9）。

        認証のエラーがなければ False。Blob はエラーにしません（サインインや接続先の追加の後に続けられるように）。
        """
        bad = [r for r in rows if r["id"] in errors and errors[r["id"]][0] in SIGNIN_CODES + CREDENTIAL_CODES]
        if not bad:
            return False
        ids = [r["id"] for r in bad]
        now = to_iso(self.clock.now())
        self.db.write(lambda c: c.executemany("UPDATE job_items SET status=?, updated_at=? WHERE id=? AND status=?",
                                              [(back, now, i, cur) for i in ids]))
        codes = sorted({errors[i][0] for i in ids})
        if any(c in CREDENTIAL_CODES for c in codes):
            fresh = self.db.query(f"SELECT * FROM job_items WHERE id IN ({','.join('?' * len(ids))})", ids)
            if self.engine.reauth(fresh):
                return True  # 次の回で、見つけた認証情報で処理します
        code = codes[0]
        msg = next(errors[i][1] for i in ids if errors[i][0] == code)
        with self._plock:
            if self._auth_paused:
                return True
            self._auth_paused = True
        self.engine.auth_problem(self.job_id, code, msg)
        self.db.execute("UPDATE jobs SET status='paused', updated_at=?, lock_json=NULL WHERE id=? AND status='running'",
                        (now, self.job_id))
        self.log.write("WARN", f"認証が使えないため、一時停止しました: {msg}")
        self.stop_event.set()
        return True

    def _stage(self, name: str, fn: Callable[[], int]) -> None:
        """段階ごとの処理。処理する Blob がなくなり、ほかの段階も止まったら終わります。"""
        while not self.stop_event.is_set():
            try:
                n = fn()
            except Cancelled:
                return
            except Exception:  # noqa: BLE001
                log.exception("ジョブの処理（%s）でエラーが発生しました", name)
                n = 0
            if n:
                self._idle[name] = False
                continue
            self._idle[name] = True
            if all(self._idle.values()) and not self._has_work():
                return
            self.stop_event.wait(0.3)

    def _has_work(self) -> bool:
        opts = self.options()
        states = ["pending", "checking", "rehydrate_queued", "downloading"]
        if opts["method"] != "rehydrate_only" or opts.get("download_mode"):
            states.append("ready")
        return bool(self.db.scalar(f"SELECT COUNT(*) FROM job_items WHERE job_id=? AND status IN ({','.join('?' * len(states))})",
                                   (self.job_id, *states)))

    def _claim(self, from_status: str, to_status: str, limit: int) -> list:
        """状態が from_status の Blob を、名前の順に取り出して to_status にします。"""
        now = to_iso(self.clock.now())

        def fn(c):
            rows = c.execute("SELECT * FROM job_items WHERE job_id=? AND status=? ORDER BY sort_key LIMIT ?",
                             (self.job_id, from_status, limit)).fetchall()
            if rows and to_status != from_status:
                c.executemany("UPDATE job_items SET status=?, updated_at=? WHERE id=?", [(to_status, now, r["id"]) for r in rows])
            return rows
        return self.db.write(fn)

    def _update(self, item_id: int, guard: str | None = None, **cols) -> None:
        cols["updated_at"] = to_iso(self.clock.now())
        sets = ", ".join(f"{k}=?" for k in cols)
        sql = f"UPDATE job_items SET {sets} WHERE id=?" + (" AND status=?" if guard else "")
        params = list(cols.values()) + [item_id] + ([guard] if guard else [])
        self.db.execute(sql, params)

    def _maybe_publish(self) -> None:
        now = time.monotonic()
        if now - self._last_publish >= 0.5:
            self._last_publish = now
            counts = self.engine.service.recount(self.job_id)
            with self._plock:
                prog = {k: dict(v) for k, v in self._progress.items()}
            self.engine.events.publish_coalesced("job.progress", self.job_id, {
                "id": self.job_id, "counts": counts, "speed": self.engine.speed.rate(), "items": prog})

    # ------------------------------------------------------------ 確認（3.3.7）

    def _check_batch(self) -> int:
        rows = self._claim("pending", "checking", BATCH)
        if not rows:
            return 0
        opts = self.options()
        method = opts["method"]
        reuse = int(self.engine.settings.get("fetch.reuse_search_info_minutes"))
        searched = from_iso(opts.get("searched_at"))
        fresh = searched is not None and reuse > 0 and self.clock.now() - searched <= timedelta(minutes=reuse)
        infos: dict[int, BlobInfo] = {}
        errors: dict[int, tuple[str, str]] = {}
        if fresh:
            # 検索の直後は、検索で得た情報をそのまま使い、確認を省きます
            for r in rows:
                infos[r["id"]] = BlobInfo(r["blob_name"], size=r["size"] or 0, blob_type=r["blob_type"] or "BlockBlob",
                                          tier=r["tier"], archive_status=r["archive_status"], etag=r["etag"],
                                          content_md5=r["content_md5"], rehydrate_priority=r["rehydrate_priority_at_search"])
        else:
            groups: dict[tuple, list] = {}
            for r in rows:
                groups.setdefault((r["storage_account"], r["endpoint_suffix"], r["container"], r["auth_kind"],
                                   r["credential_id"], r["secret_id"]), []).append(r)
            backend = self.engine.backend()
            with ThreadPoolExecutor(max_workers=int(opts["check_concurrency"])) as ex:
                for i, e in ex.map(lambda g: fetch_infos(backend, self.engine.limits, g), groups.values()):
                    infos.update(i)
                    errors.update(e)
        if self._auth_blocked(rows, errors, "checking", "pending"):
            return len(rows)
        now = self.clock.now()
        for r in rows:
            if r["id"] in errors:
                code, msg = errors[r["id"]]
                self._update(r["id"], "checking", status="error", error_code=code, error_message=msg,
                             attempts=(r["attempts"] or 0) + 1)
                self.log.write("ERROR", msg, r["blob_name"])
                continue
            info = infos[r["id"]]
            st, reason = classify(info, method)
            cols = {"status": st, "status_reason": reason, "tier": info.tier, "archive_status": info.archive_status,
                    "size": info.size, "etag": info.etag, "content_md5": info.content_md5}
            if st == "error":
                cols.update(error_code="UnknownTier", error_message=reason, status_reason=None)
            if st == "rehydrating":
                # ジョブの開始前から要求済み（要求日時は不明）
                cols.update(rehydrate_requested_known=0, rehydrate_priority=info.rehydrate_priority or "Standard",
                            rehydrate_target_tier=info.archive_status.rsplit("-", 1)[1].capitalize(),
                            next_check_at=to_iso(now + self.engine.poll_interval(info.rehydrate_priority, True)))
            if st == "ready" and method == "rehydrate_only" and not opts.get("download_mode"):
                pass  # [ダウンロードを開始] を待ちます（3.3.6）
            self._update(r["id"], "checking", **cols)
        self._maybe_publish()
        return len(rows)

    # ------------------------------------------------------------ リハイドレートの要求（3.3.8）

    def _rehydrate_batch(self) -> int:
        rows = self._claim("rehydrate_queued", "rehydrate_queued", BATCH)
        if not rows:
            return 0
        opts = self.options()
        backend = self.engine.backend()
        tier, prio = opts["target_tier"], opts["priority"]

        def one(r):
            if self.stop_event.is_set():
                raise Cancelled()
            now = self.clock.now()
            try:
                with self.engine.limits.request(r["storage_account"], self.stop_event):
                    backend.set_tier(item_target(r), r["blob_name"], tier, prio)
                known = 1
            except BackendError as exc:
                if exc.code != "BlobBeingRehydrated":
                    self._update(r["id"], "rehydrate_queued", status="error", error_code=exc.code, error_message=exc.message,
                                 attempts=(r["attempts"] or 0) + 1)
                    self.log.write("ERROR", f"リハイドレートを要求できませんでした: {exc.message}", r["blob_name"])
                    return
                known = 0
            self._update(r["id"], "rehydrate_queued", status="rehydrating",
                         rehydrate_requested_at=to_iso(now) if known else None, rehydrate_requested_known=known,
                         rehydrate_priority=prio, rehydrate_target_tier=tier,
                         next_check_at=to_iso(now + self.engine.poll_interval(prio, True)))
            self.log.write("INFO", f"リハイドレートを要求しました（{tier}・{'高' if prio == 'High' else '標準'}）", r["blob_name"])

        with ThreadPoolExecutor(max_workers=int(opts["rehydrate_concurrency"])) as ex:
            list(ex.map(one, rows))
        self._maybe_publish()
        return len(rows)

    # ------------------------------------------------------------ ダウンロード（3.3.10）

    def _prepare_folder(self, opts: dict) -> bool:
        """保存先とジョブのフォルダーを用意し、空き容量を確かめます（3.3.10.5）。"""
        row = self.db.query_one("SELECT name, job_folder, created_at FROM jobs WHERE id=?", (self.job_id,))
        base = Path(expand_dir(opts["directory"]))
        try:
            base.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            self._job_error(f"保存先のフォルダーを作れません（{exc.strerror or exc}）。", pause=True)
            return False
        if row["job_folder"]:
            self.root = Path(row["job_folder"])
        elif opts["job_folder"]:
            created = from_iso(row["created_at"]) or self.clock.now()
            name = paths.sanitize_segment(f"{created.astimezone():%Y%m%d_%H%M%S}_{row['name']}")[:120]
            self.root = base / name
            self.db.execute("UPDATE jobs SET job_folder=? WHERE id=?", (str(self.root), self.job_id))
        else:
            self.root = base
            self.db.execute("UPDATE jobs SET job_folder=? WHERE id=?", (str(self.root), self.job_id))
        try:
            self.root.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            self._job_error(f"ジョブのフォルダーを作れません（{exc.strerror or exc}）。", pause=True)
            return False
        return self._check_space()

    def _check_space(self) -> bool:
        need = self.db.scalar("SELECT IFNULL(SUM(size - bytes_downloaded),0) FROM job_items WHERE job_id=? AND status IN "
                              "('pending','checking','rehydrate_queued','rehydrating','ready','downloading')",
                              (self.job_id,)) + FREE_MARGIN
        free = free_bytes(str(self.root))
        if free is not None and free < need:
            gb = 1024 ** 3
            self._job_error(f"保存先の空き容量が足りません（必要: {need / gb:.1f} GB、空き: {free / gb:.1f} GB）。", pause=True)
            self.stop_event.set()
            return False
        return True

    def _download_batch(self) -> int:
        opts = self.options()
        if opts["method"] == "rehydrate_only" and not opts.get("download_mode"):
            return 0
        if self.root is None:
            return 0
        n = int(opts["download_concurrency"])
        rows = self._claim("ready", "downloading", n)
        if not rows:
            return 0
        with ThreadPoolExecutor(max_workers=n) as ex:
            list(ex.map(lambda r: self._download_one(r, opts), rows))
        self._maybe_publish()
        return len(rows)

    def _download_one(self, r, opts: dict) -> None:
        name = r["blob_name"]
        try:
            self._download(r, opts)
        except Cancelled:
            self._update(r["id"], "downloading", status="ready")
        except BackendError as exc:
            if exc.code in SIGNIN_CODES + CREDENTIAL_CODES:
                self._auth_blocked([r], {r["id"]: (exc.code, exc.message)}, "downloading", "ready")
                return
            self._update(r["id"], "downloading", status="error", error_code=exc.code, error_message=exc.message)
            self.log.write("ERROR", exc.message, name)
        except OSError as exc:
            msg = f"ファイルに書き込めません（{exc.strerror or exc}）。"
            self._update(r["id"], "downloading", status="error", error_code="WRITE_FAILED", error_message=msg)
            self.log.write("ERROR", msg, name)
        finally:
            with self._plock:
                self._progress.pop(r["id"], None)

    def _download(self, r, opts: dict) -> None:
        if not self._check_space():
            raise Cancelled()
        name = r["blob_name"]
        backend = self.engine.backend()
        target = item_target(r)
        account = r["storage_account"]
        limits = self.engine.limits
        rel = r["planned_path"]
        if not rel or not paths.inside(self.root, rel):
            self._update(r["id"], "downloading", status="error", error_code="PATH_OUTSIDE",
                         error_message="保存するパスが保存先の外を指しています。")
            return
        # 保存先のファイル名を確保します（再開のときは、前に確保した名前を使います）
        final = r["local_path"]
        part = r["part_path"]
        if not final or not part or not os.path.exists(paths.long_path(part)):
            final, clash = paths.reserve(self.root / rel, opts["conflict"])
            if final is None:
                self._update(r["id"], "downloading", status="skipped", status_reason="同名のファイルがある")
                self.log.write("INFO", "同名のファイルがあるため、スキップしました", name)
                return
            part = final + ".part"
            self._update(r["id"], local_path=final, part_path=part, ranges_done_json=None, bytes_downloaded=0)
            ranges_done: list[list[int]] = []
            etag_done = None
        else:
            ranges_done = load_ranges(r["ranges_done_json"])
            etag_done = r["download_etag"]
            if ranges_done:
                self.log.write("INFO", f"取得済みの範囲（{covered(ranges_done) / 1024 / 1024:.1f} MiB）から再開します", name)
        now = self.clock.now()
        self._update(r["id"], download_started_at=r["download_started_at"] or to_iso(now),
                     download_resumed_at=to_iso(now) if r["download_started_at"] else None)
        attempts = 0
        while True:
            if self.stop_event.is_set():
                raise Cancelled()
            attempts += 1
            with limits.request(account, self.stop_event):
                info = backend.get_properties(target, name)
            if info.tier == "Archive":
                raise BackendError("BlobArchived", "Archive 層のため、ダウンロードできません。")
            if etag_done and etag_done != info.etag:
                # Blob が更新された: .part を作り直して最初から
                self.log.write("WARN", "Blob が更新されたため、最初から取得し直します", name)
                ranges_done = []
            etag_done = info.etag
            self._update(r["id"], download_etag=info.etag, size=info.size, content_md5=info.content_md5,
                         attempts=(r["attempts"] or 0) + attempts)
            try:
                self._fetch_ranges(r, info, part, ranges_done, opts)
                break
            except BackendError as exc:
                if exc.code == "ConditionNotMet" and attempts < DOWNLOAD_RESUME_TRIES:
                    ranges_done = []
                    etag_done = None
                    continue
                if exc.code == "ConditionNotMet":
                    raise BackendError(exc.code, "Blob が更新され続けたため、ダウンロードできませんでした。") from None
                if attempts < DOWNLOAD_RESUME_TRIES and exc.code not in ("BlobNotFound", "AuthorizationPermissionMismatch",
                                                                         "AuthorizationFailure", "BlobArchived"):
                    self.log.write("WARN", f"取得済みの範囲から再開します: {exc.message}", name)
                    continue
                raise
        # サイズの確認
        actual = os.path.getsize(paths.long_path(part))
        if actual != info.size:
            raise BackendError("SIZE_MISMATCH", f"ダウンロードしたファイルのサイズが違います（{actual:,} / {info.size:,} バイト）。")
        md5_result = "off"
        if opts["verify_md5"]:
            if info.content_md5:
                with self.engine.md5_lock:  # MD5 の計算は同時に1ファイルまで（3.4.4 の L4）
                    ok = file_md5(part) == info.content_md5
                if not ok:
                    self._update(r["id"], "downloading", status="error", error_code="MD5_MISMATCH", md5_result="mismatch",
                                 error_message="ダウンロードしたファイルの MD5 が一致しません。", ranges_done_json=None)
                    self.log.write("ERROR", "ダウンロードしたファイルの MD5 が一致しません", name)
                    return
                md5_result = "match"
            else:
                md5_result = "none"
        os.replace(paths.long_path(part), paths.long_path(final))
        if opts["set_mtime"] and info.last_modified:
            ts = info.last_modified.timestamp()
            try:
                os.utime(paths.long_path(final), (ts, ts))
            except OSError:
                pass
        self._update(r["id"], "downloading", status="completed", download_completed_at=to_iso(self.clock.now()),
                     bytes_downloaded=info.size, md5_result=md5_result, part_path=None, ranges_done_json=None)
        self.log.write("INFO", "ダウンロードしました", name)

    def _fetch_ranges(self, r, info: BlobInfo, part: str, done: list[list[int]], opts: dict) -> None:
        """足りない区間を、今のチャンクの大きさで取得します（取得済みの区間は [開始, 終わり) で記録します）。"""
        limits = self.engine.limits
        chunk = limits.chunk_size()
        size = info.size
        todo = [(s, min(e, s + chunk)) for gs, ge in missing(done, size) for s, e in [(x, ge) for x in range(gs, ge, chunk)]]
        mode = "r+b" if os.path.exists(paths.long_path(part)) and done else "wb"
        with open(paths.long_path(part), mode) as f:
            if size and mode == "wb":
                f.truncate(size)
            flock = threading.Lock()
            state = {"bytes": covered(done), "last_save": time.monotonic()}
            backend = self.engine.backend()
            target = item_target(r)

            def save(force: bool = False) -> None:
                if force or time.monotonic() - state["last_save"] >= 1.0:
                    state["last_save"] = time.monotonic()
                    self._update(r["id"], ranges_done_json=json.dumps(done), bytes_downloaded=state["bytes"])

            def one(rng: tuple[int, int]) -> None:
                if self.stop_event.is_set():
                    raise Cancelled()
                start, end = rng
                length = end - start
                limits.bandwidth.acquire(length, self.stop_event)
                with limits.request(r["storage_account"], self.stop_event):
                    data = b"".join(backend.download_range(target, r["blob_name"], start, length, info.etag))
                if len(data) != length:
                    raise BackendError("SHORT_READ", "受け取ったデータが足りません。")
                with flock:
                    f.seek(start)
                    f.write(data)
                    add_range(done, start, end)
                    state["bytes"] += length
                    self.engine.speed.add(length)
                    with self._plock:
                        self._progress[r["id"]] = {"bytes": state["bytes"], "size": size}
                    save()

            try:
                if todo:
                    with ThreadPoolExecutor(max_workers=int(opts["connections_per_blob"])) as ex:
                        for fut in [ex.submit(one, t) for t in todo]:
                            fut.result()
            finally:
                save(force=True)
            f.flush()
            os.fsync(f.fileno())


def load_ranges(text: str | None) -> list[list[int]]:
    """記録した取得済みの区間。形式が違う（古い形式など）ものは、最初から取得し直すため空にします。"""
    try:
        data = json.loads(text or "[]")
    except ValueError:
        return []
    if not all(isinstance(x, list) and len(x) == 2 and all(isinstance(v, int) for v in x) for x in data):
        return []
    out: list[list[int]] = []
    for s, e in sorted(data):
        add_range(out, s, e)
    return out


def add_range(done: list[list[int]], start: int, end: int) -> None:
    """区間 [start, end) を加え、重なり・隣り合う区間をまとめます（done を書き換えます）。"""
    merged: list[list[int]] = []
    for s, e in sorted(done + [[start, end]]):
        if merged and s <= merged[-1][1]:
            merged[-1][1] = max(merged[-1][1], e)
        else:
            merged.append([s, e])
    done[:] = merged


def covered(done: list[list[int]]) -> int:
    return sum(e - s for s, e in done)


def missing(done: list[list[int]], size: int) -> list[tuple[int, int]]:
    """[0, size) のうち、取得していない区間。"""
    out = []
    pos = 0
    for s, e in sorted(done):
        if s > pos:
            out.append((pos, min(s, size)))
        pos = max(pos, e)
    if pos < size:
        out.append((pos, size))
    return [(s, e) for s, e in out if e > s]


def file_md5(path: str) -> str:
    h = hashlib.md5()  # noqa: S324（Azure の Content-MD5 の照合に使う）
    with open(paths.long_path(path), "rb") as f:
        while True:
            b = f.read(4 * 1024 * 1024)
            if not b:
                break
            h.update(b)
    return base64.b64encode(h.digest()).decode()
