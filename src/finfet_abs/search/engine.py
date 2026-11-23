"""検索の実行と検索結果の保存（3.2.10、5.3.6）。

検索は、ジョブのキューとは別にすぐに実行します（同時に 2 つまで。3つ目は前の検索が終わるまで待ちます）。
受け取ったページはすぐにフィルターし、一致した Blob だけをまとめて SQLite に書き込みます（7.4 の I9）。
"""

from __future__ import annotations

import json
import logging
import threading
import time
import uuid
from contextlib import nullcontext
from datetime import timedelta
from typing import Callable

from ..azure.backend import AccountRef, AuthRef, BackendError, ContainerRef
from ..core.clock import SYSTEM_CLOCK, Clock, to_iso
from ..core.errors import AppError, NotFound
from . import caseresolve
from . import datetime_vars as dtv
from .listing import Cancelled, LimitReached, ListingCache, ListStats, list_target
from .pattern import CompileContext, PatternError, compile_path, container_literal_prefix, expand_containers
from .sortkey import sort_key

log = logging.getLogger(__name__)

MAX_CONCURRENT = 2
FLUSH_ROWS = 500
FLUSH_SECONDS = 1.0

TARGET_LABELS = {"waiting": "待機中", "listing": "取得中", "completed": "完了", "error": "エラー", "skipped": "スキップ",
                 "cancelled": "中止"}


class SearchRun:
    """1つの検索の実行中の状態。"""

    def __init__(self, sid: str, cond: dict) -> None:
        self.id = sid
        self.cond = cond
        self.cancel = threading.Event()
        self.lock = threading.Lock()
        self.matched = 0
        self.limit_hit = False
        self.targets: dict[int, dict] = {}
        self.started = time.monotonic()
        self.thread: threading.Thread | None = None
        self.history_id: str | None = None


class SearchService:
    def __init__(self, *, db, events, settings, backend: Callable[[], object], history=None, clock: Clock = SYSTEM_CLOCK,
                 local_zone=None, connection: Callable[[], object] | None = None) -> None:
        self.db = db
        self.events = events
        self.settings = settings
        self._backend = backend
        self.history = history
        self.clock = clock
        self.local_zone = local_zone or dtv.LocalZone()
        self.connection = connection
        self._sem = threading.Semaphore(MAX_CONCURRENT)
        self._runs: dict[str, SearchRun] = {}
        self._lock = threading.Lock()
        self._hns: dict[tuple, bool] = {}

    # ------------------------------------------------------------ 開始

    def ctx(self) -> CompileContext:
        return CompileContext(now_utc=self.clock.now(), local_zone=self.local_zone,
                              max_prefixes=int(self.settings.get("search.max_prefixes")))

    def start(self, cond: dict) -> str:
        """検索を始め、検索の ID を返します。"""
        if not cond.get("targets"):
            raise AppError("NO_TARGETS", "接続先を追加してください。")
        for i, t in enumerate(cond["targets"]):
            if not t.get("auth_kind"):
                raise AppError("AUTH_REQUIRED", "認証方式を選択してください。", extra={"target_index": i})
            if not t.get("container"):
                raise AppError("CONTAINER_REQUIRED", "コンテナー名を入力してください。", extra={"target_index": i})
        try:
            compile_path(cond.get("path_pattern", ""), cond.get("variables") or {}, ignore_case=cond.get("ignore_case", False),
                         input_timezone=cond.get("input_timezone", "local"), ctx=self.ctx())
        except PatternError as exc:
            raise AppError("PATTERN_INVALID", "パスパターンに誤りがあります。",
                           extra={"issues": [i.to_json() for i in exc.issues]}) from None
        sid = str(uuid.uuid4())
        now = to_iso(self.clock.now())
        run = SearchRun(sid, cond)

        def fn(c):
            c.execute("INSERT INTO searches(id, condition_json, status, started_at) VALUES(?,?,?,?)",
                      (sid, json.dumps(cond, ensure_ascii=False), "running", now))
            for i, t in enumerate(cond["targets"]):
                c.execute("INSERT INTO search_targets(search_id, target_no, storage_account, endpoint_suffix, container, "
                          "auth_kind, credential_id, tenant_id, account, status) VALUES(?,?,?,?,?,?,?,?,?,?)",
                          (sid, i, t["storage_account"], t.get("endpoint_suffix") or "core.windows.net", t["container"],
                           t.get("auth_kind"), t.get("credential_id"), t.get("tenant"), t.get("account"), "waiting"))
        self.db.write(fn)
        for i, t in enumerate(cond["targets"]):
            run.targets[i] = {"no": i, "status": "waiting", "listed": 0, "matched": 0, "requests": 0, "error": None,
                              "storage_account": t["storage_account"], "container": t["container"]}
        if self.history is not None:
            try:
                run.history_id = self.history.record(cond)
            except Exception:  # noqa: BLE001
                log.exception("検索履歴に記録できませんでした")
        with self._lock:
            self._runs[sid] = run
        log.info("検索を開始しました（接続先 %s 件）", len(cond["targets"]))
        run.thread = threading.Thread(target=self._run, args=(run,), name=f"search-{sid[:8]}", daemon=True)
        run.thread.start()
        self._cleanup()
        return sid

    def _cleanup(self) -> None:
        """検索結果を残す数を超えたら、古い検索から削除します（3.2.10.2）。"""
        keep = int(self.settings.get("search.keep_recent"))
        with self._lock:
            running = set(self._runs)

        def fn(c):
            rows = c.execute("SELECT id FROM searches ORDER BY started_at DESC").fetchall()
            for r in rows[keep:]:
                if r[0] not in running:
                    c.execute("DELETE FROM searches WHERE id=?", (r[0],))
        self.db.submit(fn)

    # ------------------------------------------------------------ 実行

    def _run(self, run: SearchRun, only: list[int] | None = None) -> None:
        with self._sem:
            try:
                nos = sorted(run.targets) if only is None else only
                for no in nos:
                    if run.cancel.is_set():
                        break
                    if run.limit_hit:
                        self._set_target(run, no, status="skipped", error={"message": "上限件数に達したため、取得しませんでした。"})
                        continue
                    self._run_target(run, no)
            except Exception:  # noqa: BLE001
                log.exception("検索でエラーが発生しました")
            finally:
                self._mark_cancelled(run)
                self._finish(run)

    def _mark_cancelled(self, run: SearchRun) -> None:
        """中止したとき、まだ取得を始めていない接続先を「中止」にします。"""
        if not run.cancel.is_set():
            return
        with run.lock:
            waiting = [no for no, t in run.targets.items() if t["status"] in ("waiting", "listing")]
        for no in waiting:
            self._set_target(run, no, status="cancelled")

    def _set_target(self, run: SearchRun, no: int, **kw) -> None:
        with run.lock:
            run.targets[no].update(kw)
            t = dict(run.targets[no])
        err = json.dumps(t["error"], ensure_ascii=False) if t.get("error") else None
        self.db.submit(lambda c: c.execute(
            "UPDATE search_targets SET status=?, listed_count=?, matched_count=?, request_count=?, error_json=?, container=? "
            "WHERE search_id=? AND target_no=?", (t["status"], t["listed"], t["matched"], t["requests"], err,
                                                   t["container"], run.id, no)))
        self._progress(run)

    def _progress(self, run: SearchRun) -> None:
        with run.lock:
            targets = [dict(t) for t in run.targets.values()]
            matched = run.matched
        self.events.publish_coalesced("search.progress", run.id, {
            "id": run.id, "matched": matched, "listed": sum(t["listed"] for t in targets),
            "targets": [{"no": t["no"], "status": t["status"], "label": TARGET_LABELS.get(t["status"], ""),
                         "listed": t["listed"], "matched": t["matched"], "requests": t["requests"],
                         "storage_account": t["storage_account"], "container": t["container"], "error": t["error"]}
                        for t in sorted(targets, key=lambda x: x["no"])]})

    def _auth(self, t: dict) -> AuthRef:
        return AuthRef(t.get("auth_kind") or "entra_user", credential_id=t.get("credential_id"), tenant_id=t.get("tenant"),
                       account=t.get("account"))

    def _containers(self, run: SearchRun, t: dict, account: AccountRef) -> list[tuple[str, dict]]:
        """コンテナー名の変数を展開します（3.2.8）。列挙できなければ、コンテナーの一覧を取得してフィルターします。"""
        cond = run.cond
        expanded = expand_containers(t["container"], cond.get("variables") or {}, input_timezone=cond.get("input_timezone", "local"),
                                     ctx=self.ctx())
        if expanded is not None:
            return expanded
        flt = compile_path(t["container"], cond.get("variables") or {}, input_timezone=cond.get("input_timezone", "local"),
                           ctx=self.ctx(), literal=True)
        names = self._backend().list_containers(account, container_literal_prefix(t["container"]))
        out = []
        for n in names:
            vals = flt.match(n)
            if vals is None:
                continue
            fixed = {}
            for k, v in vals.items():
                fixed[k] = {dtv.UNITS.index(u): x for u, x in v.items()} if isinstance(v, dict) else v
            out.append((n, fixed))
        if len(out) > 100:
            raise AppError("TOO_MANY_CONTAINERS", f"一致するコンテナーが多すぎます（{len(out)} 件。上限 100 件）。")
        return out

    def _run_target(self, run: SearchRun, no: int) -> None:
        cond = run.cond
        t = dict(cond["targets"][no])
        backend = self._backend()
        account = AccountRef(t["storage_account"], t.get("endpoint_suffix") or "core.windows.net", self._auth(t))
        self._set_target(run, no, status="listing", error=None)
        started = time.monotonic()
        try:
            containers = self._containers(run, t, account)
        except (BackendError, AppError, PatternError) as exc:
            self._set_target(run, no, status="error", error=self._err(exc))
            return
        if not containers:
            self._set_target(run, no, status="completed", error={"message": "一致するコンテナーがありません。"})
            return
        # 展開したコンテナーごとに、接続先の行を加えます（最初のものは元の行を使います）
        rows = []
        for k, (name, fixed) in enumerate(containers):
            if k == 0:
                rows.append((no, name, fixed))
                self._set_target(run, no, container=name)
                continue
            with run.lock:
                new_no = max(run.targets) + 1
                run.targets[new_no] = {"no": new_no, "status": "waiting", "listed": 0, "matched": 0, "requests": 0,
                                       "error": None, "storage_account": t["storage_account"], "container": name}
            self.db.write(lambda c, n=new_no, name=name: c.execute(
                "INSERT INTO search_targets(search_id, target_no, storage_account, endpoint_suffix, container, auth_kind, "
                "credential_id, tenant_id, account, status) VALUES(?,?,?,?,?,?,?,?,?,?)",
                (run.id, n, t["storage_account"], account.endpoint_suffix, name, t.get("auth_kind"), t.get("credential_id"),
                 t.get("tenant"), t.get("account"), "waiting")))
            rows.append((new_no, name, fixed))
        for target_no, name, fixed in rows:
            if run.cancel.is_set() or run.limit_hit:
                break
            self._list_one(run, target_no, account, name, fixed, backend, t)
        self.db.submit(lambda c: c.execute("UPDATE search_targets SET elapsed_ms=? WHERE search_id=? AND target_no=?",
                                           (int((time.monotonic() - started) * 1000), run.id, no)))

    def _list_one(self, run: SearchRun, no: int, account: AccountRef, container: str, fixed: dict, backend, t: dict) -> None:
        cond = run.cond
        self._set_target(run, no, status="listing", error=None)
        target = ContainerRef(account, container)
        stats = ListStats()
        pattern = cond.get("path_pattern", "")
        if not cond.get("ignore_case"):
            try:
                pattern = self._resolve_case(pattern, backend, target, stats)
            except BackendError as exc:
                self._set_target(run, no, status="error", error=self._err(exc))
                return
            if pattern is None:
                # 大文字・小文字を区別しない部分に一致する階層がない: 一致する Blob はありません
                with run.lock:
                    run.targets[no]["requests"] = stats.requests
                self._set_target(run, no, status="completed")
                return
        try:
            compiled = compile_path(pattern, cond.get("variables") or {},
                                    ignore_case=cond.get("ignore_case", False),
                                    input_timezone=cond.get("input_timezone", "local"), ctx=self.ctx(), fixed=fixed)
        except PatternError as exc:
            self._set_target(run, no, status="error", error={"message": exc.issues[0].message})
            return
        hns = False
        if self.settings.get("search.exclude_adls_directories"):
            key = (account.account, account.endpoint_suffix)
            if key not in self._hns:
                try:
                    self._hns[key] = bool(backend.account_info(account).get("is_hns_enabled"))
                except Exception:  # noqa: BLE001
                    self._hns[key] = False
            hns = self._hns[key]
        done = self._downloaded(account.account, container)
        max_results = int(self.settings.get("search.max_results"))
        now = self.clock.now()
        writing_after = now - timedelta(hours=1)
        buf: list[tuple] = []
        last_flush = [time.monotonic()]

        def flush() -> None:
            if not buf:
                return
            rows = list(buf)
            buf.clear()
            self.db.submit(lambda c: c.executemany(
                "INSERT INTO search_results(search_id, target_no, storage_account, endpoint_suffix, container, blob_name, "
                "blob_name_lower, sort_key, blob_type, size, etag, content_md5, creation_time, last_modified, "
                "access_tier_change_time, tier, archive_status, rehydrate_priority, vars_json, notes_json) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", rows))
            last_flush[0] = time.monotonic()

        def on_items(items) -> None:
            matched_here = 0
            for b in items:
                if hns and b.is_directory:
                    continue
                vals = compiled.match(b.name)
                if vals is None:
                    continue
                notes = []
                if b.archive_status and b.archive_status.startswith("rehydrate-pending"):
                    notes.append({"code": "rehydrating"})
                if b.name in done:
                    notes.append({"code": "downloaded", "at": done[b.name]})
                if b.last_modified and b.last_modified > writing_after:
                    notes.append({"code": "writing"})
                buf.append((run.id, no, account.account, account.endpoint_suffix, container, b.name, b.name.lower(),
                            sort_key(b.name), b.blob_type, b.size, b.etag, b.content_md5, to_iso(b.creation_time),
                            to_iso(b.last_modified), to_iso(b.access_tier_change_time), b.tier, b.archive_status,
                            b.rehydrate_priority, json.dumps(vals, ensure_ascii=False) if vals else None,
                            json.dumps(notes, ensure_ascii=False) if notes else None))
                matched_here += 1
                with run.lock:
                    run.matched += 1
                    hit = run.matched >= max_results
                if hit:
                    run.limit_hit = True
                    break
            with run.lock:
                run.targets[no]["matched"] += matched_here
                run.targets[no]["listed"] = stats.listed
                run.targets[no]["requests"] = stats.requests
            if len(buf) >= FLUSH_ROWS or time.monotonic() - last_flush[0] >= FLUSH_SECONDS:
                flush()
            self._progress(run)
            if run.limit_hit:
                raise LimitReached()

        status, error = "completed", None
        try:
            list_target(backend, target, compiled.prefixes, on_items=on_items, cancel=run.cancel,
                        trial_pages=int(cond.get("options", {}).get("merge_trial_pages") or
                                        self.settings.get("search.merge_trial_pages")),
                        concurrency=int(self.settings.get("search.list_concurrency")), cache=self._cache(run),
                        stats=stats, include_metadata=hns, connection=self.connection)
        except LimitReached:
            status = "completed"
        except Cancelled:
            status = "cancelled"
        except BackendError as exc:
            status, error = "error", self._err(exc)
        except Exception as exc:  # noqa: BLE001
            log.exception("一覧の取得でエラーが発生しました")
            status, error = "error", self._err(exc)
        flush()
        with run.lock:
            run.targets[no]["listed"] = stats.listed
            run.targets[no]["requests"] = stats.requests
        self._set_target(run, no, status=status, error=error)
        log.info("一覧を取得しました: %s/%s（取得 %s 件・一致 %s 件・要求 %s 回）", account.account, container, stats.listed,
                 run.targets[no]["matched"], stats.requests)

    def _resolve_case(self, pattern: str, backend, target: ContainerRef, stats: ListStats) -> str | None:
        """先頭の大文字・小文字を区別しない部分を、実際の表記に置き換えます（caseresolve）。

        一致する階層がなければ None を返します。置き換えられない場合は、元のパスパターンを返します。
        """
        found = caseresolve.find(pattern)
        if found is None:
            return pattern

        def walk(prefix: str) -> list[str]:
            with (self.connection or nullcontext)():
                stats.add(requests=1)
                return backend.walk_prefixes(target, prefix, 5000)
        actual = caseresolve.resolve(walk, found.head, found.ci_literal)
        if actual is None:
            return pattern
        if not actual:
            log.info("大文字・小文字を区別しない部分に一致する階層がありません: %s", target.container)
            return None
        return caseresolve.apply(pattern, found, actual)

    def _cache(self, run: SearchRun) -> ListingCache:
        if not hasattr(run, "cache"):
            run.cache = ListingCache()
        return run.cache

    def _downloaded(self, account: str, container: str) -> dict[str, str]:
        """過去のジョブでダウンロード済みの Blob（名前 → 完了の日時）。"""
        rows = self.db.query("SELECT blob_name, MAX(download_completed_at) AS t FROM job_items WHERE storage_account=? AND "
                             "container=? AND download_completed_at IS NOT NULL GROUP BY blob_name", (account, container))
        return {r["blob_name"]: r["t"] for r in rows}

    @staticmethod
    def _err(exc: BaseException) -> dict:
        if isinstance(exc, BackendError):
            return {"code": exc.code, "message": exc.message}
        if isinstance(exc, AppError):
            return {"code": exc.code, "message": exc.message}
        if isinstance(exc, PatternError):
            return {"code": "PATTERN_INVALID", "message": exc.issues[0].message}
        return {"code": "INTERNAL", "message": "内部のエラーが発生しました。詳しくはログを確認してください。"}

    def _finish(self, run: SearchRun) -> None:
        with run.lock:
            targets = list(run.targets.values())
        if run.cancel.is_set():
            status = "cancelled"
        elif targets and all(t["status"] in ("error", "skipped") for t in targets) and not any(t["matched"] for t in targets):
            status = "failed"
        else:
            status = "completed"
        elapsed = int((time.monotonic() - run.started) * 1000)
        now = to_iso(self.clock.now())

        def fn(c):
            r = c.execute("SELECT COUNT(*), IFNULL(SUM(size),0) FROM search_results WHERE search_id=?", (run.id,)).fetchone()
            tiers = {row[0] or "": [row[1], row[2]] for row in c.execute(
                "SELECT tier, COUNT(*), IFNULL(SUM(size),0) FROM search_results WHERE search_id=? GROUP BY tier", (run.id,))}
            c.execute("UPDATE searches SET status=?, ended_at=?, matched_count=?, matched_bytes=?, tier_counts_json=?, "
                      "truncated=?, elapsed_ms=? WHERE id=?", (status, now, r[0], r[1], json.dumps(tiers), 1 if run.limit_hit else 0,
                                                               elapsed, run.id))
            return r[0]
        try:
            count = self.db.write(fn)
        except Exception:  # noqa: BLE001
            log.exception("検索の結果を保存できませんでした")
            count = run.matched
        if run.history_id and self.history is not None:
            self.history.set_result_count(run.history_id, count)
        self.events.flush()
        self._progress(run)
        self.events.flush()
        self.events.publish("search.done", {"id": run.id, "status": status, "matched": count, "truncated": run.limit_hit,
                                            "elapsed_ms": elapsed})
        log.info("検索が終わりました（%s・一致 %s 件・%.1f 秒）", status, count, elapsed / 1000)
        # お知らせ（v1.5.0。画面はポップアップでも表示します。見ている検索結果のページでは出しません）
        n_err = sum(1 for t in targets if t["status"] == "error")
        if status == "completed" and n_err:
            self.events.publish("notify", {"level": "warn", "text": f"検索が終わりました（一致 {count:,} 件）。{n_err} 個の接続先でエラーがありました",
                                           "url": f"/searches/{run.id}"})
        elif status == "completed":
            self.events.publish("notify", {"level": "ok", "text": f"検索が終わりました（一致 {count:,} 件）",
                                           "url": f"/searches/{run.id}"})
        elif status == "failed":
            self.events.publish("notify", {"level": "error", "text": "検索に失敗しました。検索結果のページで、接続先ごとのエラーを確かめてください",
                                           "url": f"/searches/{run.id}"})
        with self._lock:
            self._runs.pop(run.id, None)

    # ------------------------------------------------------------ 操作

    def any_running(self) -> bool:
        """実行中の検索があるか。"""
        with self._lock:
            return bool(self._runs)

    def running(self, sid: str) -> bool:
        with self._lock:
            return sid in self._runs

    def cancel(self, sid: str) -> None:
        with self._lock:
            run = self._runs.get(sid)
        if run is None:
            raise AppError("NOT_RUNNING", "この検索は実行中ではありません。", status=409)
        run.cancel.set()

    def retry_target(self, sid: str, no: int) -> None:
        """エラーの接続先を再試行します。"""
        if self.running(sid):
            raise AppError("RUNNING", "検索の実行中は再試行できません。", status=409)
        row = self.db.query_one("SELECT * FROM searches WHERE id=?", (sid,))
        if row is None:
            raise NotFound("検索が見つかりません。")
        cond = json.loads(row["condition_json"])
        trow = self.db.query_one("SELECT * FROM search_targets WHERE search_id=? AND target_no=?", (sid, no))
        if trow is None:
            raise NotFound("接続先が見つかりません。")
        run = SearchRun(sid, cond)
        for r in self.db.query("SELECT * FROM search_targets WHERE search_id=?", (sid,)):
            err = json.loads(r["error_json"]) if r["error_json"] else None
            run.targets[r["target_no"]] = {"no": r["target_no"], "status": r["status"], "listed": r["listed_count"],
                                           "matched": r["matched_count"], "requests": r["request_count"], "error": err,
                                           "storage_account": r["storage_account"], "container": r["container"]}
        t = {"storage_account": trow["storage_account"], "endpoint_suffix": trow["endpoint_suffix"],
             "container": trow["container"], "auth_kind": trow["auth_kind"], "credential_id": trow["credential_id"],
             "tenant": trow["tenant_id"], "account": trow["account"]}
        fixed = self._fixed_for(cond, t)
        self.db.write(lambda c: (c.execute("DELETE FROM search_results WHERE search_id=? AND target_no=?", (sid, no)),
                                 c.execute("UPDATE searches SET status='running' WHERE id=?", (sid,))))
        run.matched = (row["matched_count"] or 0) - (trow["matched_count"] or 0)
        run.targets[no].update(matched=0, listed=0, requests=0)
        with self._lock:
            self._runs[sid] = run
        run.thread = threading.Thread(target=self._run_retry, args=(run, no, t, fixed), daemon=True)
        run.thread.start()

    def _fixed_for(self, cond: dict, t: dict) -> dict:
        """展開したコンテナー名に対応する、コンテナー名の変数の値。"""
        for ot in cond.get("targets") or []:
            if ot["storage_account"] != t["storage_account"] or "{{" not in (ot.get("container") or ""):
                continue
            try:
                expanded = expand_containers(ot["container"], cond.get("variables") or {},
                                             input_timezone=cond.get("input_timezone", "local"), ctx=self.ctx())
            except PatternError:
                continue
            for name, fx in expanded or []:
                if name == t["container"]:
                    return fx
        return {}

    def _run_retry(self, run: SearchRun, no: int, t: dict, fixed: dict) -> None:
        with self._sem:
            try:
                account = AccountRef(t["storage_account"], t.get("endpoint_suffix") or "core.windows.net", self._auth(t))
                self._list_one(run, no, account, t["container"], fixed, self._backend(), t)
            except Exception:  # noqa: BLE001
                log.exception("再試行でエラーが発生しました")
            finally:
                self._finish(run)

    def skip_target(self, sid: str, no: int) -> None:
        """この接続先を除きます（結果から除きます）。"""
        # ほかの接続先の取得が続いている間も除けます（3.2.10）。一致の件数は、検索の終わりにも数え直します
        with self._lock:
            run = self._runs.get(sid)
        if run is not None:
            with run.lock:
                t = run.targets.get(no)
                if t is not None and t["status"] in ("listing", "waiting"):
                    raise AppError("TARGET_RUNNING", "取得中の接続先は除けません。", status=409)
                if t is not None:
                    run.matched -= t["matched"]
                    t.update(status="skipped", matched=0)

        def fn(c):
            c.execute("DELETE FROM search_results WHERE search_id=? AND target_no=?", (sid, no))
            c.execute("UPDATE search_targets SET status='skipped' WHERE search_id=? AND target_no=?", (sid, no))
            r = c.execute("SELECT COUNT(*), IFNULL(SUM(size),0) FROM search_results WHERE search_id=?", (sid,)).fetchone()
            c.execute("UPDATE searches SET matched_count=?, matched_bytes=? WHERE id=?", (r[0], r[1], sid))
        self.db.write(fn)
        self.events.publish("search.done", {"id": sid, "status": "updated"})
