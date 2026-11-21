"""SQLite（5.3）。

書き込みは1つの書き込み用スレッドに集め、キューで受け付けて、まとめて1つのトランザクションにします。
読み取りはスレッドごとの接続で行います。
"""

from __future__ import annotations

import logging
import queue
import sqlite3
import threading
from concurrent.futures import Future
from pathlib import Path
from typing import Any, Callable, Iterable

log = logging.getLogger(__name__)

#: 1つのトランザクションにまとめる書き込みの最大数（3.4.4 の L5）
BATCH_LIMIT = 500


SCHEMA_V1 = """
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT);

CREATE TABLE searches (
  id TEXT PRIMARY KEY,
  condition_json TEXT NOT NULL,
  status TEXT NOT NULL,
  started_at TEXT, ended_at TEXT,
  matched_count INTEGER NOT NULL DEFAULT 0,
  matched_bytes INTEGER NOT NULL DEFAULT 0,
  tier_counts_json TEXT,
  truncated INTEGER NOT NULL DEFAULT 0,
  elapsed_ms INTEGER,
  error_json TEXT
);
CREATE INDEX idx_searches_started ON searches(started_at);

CREATE TABLE search_targets (
  search_id TEXT NOT NULL REFERENCES searches(id) ON DELETE CASCADE,
  target_no INTEGER NOT NULL,
  storage_account TEXT NOT NULL, endpoint_suffix TEXT NOT NULL, container TEXT NOT NULL,
  auth_kind TEXT, credential_id TEXT, tenant_id TEXT,
  status TEXT NOT NULL,
  listed_count INTEGER NOT NULL DEFAULT 0, matched_count INTEGER NOT NULL DEFAULT 0,
  request_count INTEGER NOT NULL DEFAULT 0, elapsed_ms INTEGER, error_json TEXT,
  PRIMARY KEY (search_id, target_no)
);

CREATE TABLE search_results (
  id INTEGER PRIMARY KEY,
  search_id TEXT NOT NULL REFERENCES searches(id) ON DELETE CASCADE,
  target_no INTEGER NOT NULL,
  storage_account TEXT NOT NULL, endpoint_suffix TEXT NOT NULL, container TEXT NOT NULL,
  blob_name TEXT NOT NULL, blob_name_lower TEXT NOT NULL, sort_key BLOB NOT NULL,
  blob_type TEXT, size INTEGER, etag TEXT, content_md5 TEXT,
  creation_time TEXT, last_modified TEXT, access_tier_change_time TEXT,
  tier TEXT, archive_status TEXT, rehydrate_priority TEXT,
  vars_json TEXT,
  selected INTEGER NOT NULL DEFAULT 0,
  notes_json TEXT
);
CREATE INDEX idx_results_sort ON search_results(search_id, sort_key);
CREATE INDEX idx_results_selected ON search_results(search_id, selected);

CREATE TABLE jobs (
  id TEXT PRIMARY KEY,
  name TEXT NOT NULL,
  source TEXT NOT NULL,
  search_id TEXT, source_job_id TEXT,
  status TEXT NOT NULL,
  queue_order REAL NOT NULL DEFAULT 0,
  search_json TEXT, urls_summary_json TEXT,
  options_json TEXT NOT NULL,
  job_folder TEXT,
  counts_json TEXT,
  created_at TEXT, started_at TEXT, updated_at TEXT, ended_at TEXT,
  created_env_json TEXT, cancelled_by TEXT,
  lock_json TEXT, lock_heartbeat_at TEXT,
  last_error_json TEXT
);
CREATE INDEX idx_jobs_status ON jobs(status, queue_order);
CREATE INDEX idx_jobs_ended ON jobs(ended_at);

CREATE TABLE job_items (
  id INTEGER PRIMARY KEY,
  job_id TEXT NOT NULL REFERENCES jobs(id) ON DELETE CASCADE,
  storage_account TEXT NOT NULL, endpoint_suffix TEXT NOT NULL DEFAULT 'core.windows.net',
  container TEXT NOT NULL, blob_name TEXT NOT NULL, blob_name_lower TEXT NOT NULL,
  sort_key BLOB NOT NULL,
  blob_type TEXT, size INTEGER, etag TEXT, content_md5 TEXT,
  creation_time TEXT, last_modified TEXT, access_tier_change_time TEXT,
  tier_at_search TEXT, archive_status_at_search TEXT, rehydrate_priority_at_search TEXT,
  tier TEXT, archive_status TEXT,
  notes_json TEXT, vars_json TEXT,
  auth_kind TEXT, credential_id TEXT, secret_id TEXT, tenant_id TEXT,
  status TEXT NOT NULL, status_reason TEXT,
  error_code TEXT, error_message TEXT,
  attempts INTEGER NOT NULL DEFAULT 0,
  rehydrate_requested_at TEXT, rehydrate_requested_known INTEGER,
  rehydrate_priority TEXT, rehydrate_target_tier TEXT,
  rehydrate_priority_raised_at TEXT,
  rehydrate_completed_detected_at TEXT, rehydrate_completed_azure_at TEXT,
  next_check_at TEXT,
  download_started_at TEXT, download_resumed_at TEXT, download_completed_at TEXT,
  planned_path TEXT, local_path TEXT, part_path TEXT,
  bytes_downloaded INTEGER NOT NULL DEFAULT 0,
  download_etag TEXT, ranges_done_json TEXT,
  md5_result TEXT,
  updated_at TEXT,
  UNIQUE (job_id, storage_account, container, blob_name)
);
CREATE INDEX idx_items_status ON job_items(job_id, status);
CREATE INDEX idx_items_sort ON job_items(job_id, sort_key);
CREATE INDEX idx_items_check ON job_items(status, next_check_at);
CREATE INDEX idx_items_blob ON job_items(storage_account, container, blob_name);

CREATE TABLE job_secrets (
  id TEXT PRIMARY KEY,
  job_id TEXT NOT NULL REFERENCES jobs(id) ON DELETE CASCADE,
  storage_account TEXT NOT NULL, endpoint_suffix TEXT NOT NULL,
  container TEXT, blob_name TEXT,
  secret TEXT NOT NULL,
  sas_kind TEXT, permissions TEXT,
  expires_at TEXT
);

CREATE TABLE credentials (
  id TEXT PRIMARY KEY,
  storage_account TEXT NOT NULL, endpoint_suffix TEXT NOT NULL,
  scope_container TEXT,
  auth_kind TEXT NOT NULL, source TEXT NOT NULL,
  secret TEXT NOT NULL,
  sas_kind TEXT, permissions TEXT, expires_at TEXT,
  last_test_at TEXT, last_test_ok INTEGER, last_test_message TEXT,
  created_at TEXT, updated_at TEXT, last_used_at TEXT
);
CREATE UNIQUE INDEX idx_credentials_scope
  ON credentials(storage_account, endpoint_suffix, IFNULL(scope_container, ''));

CREATE TABLE search_history (
  id TEXT PRIMARY KEY,
  condition_json TEXT NOT NULL,
  condition_hash TEXT NOT NULL UNIQUE,
  created_at TEXT, last_used_at TEXT,
  use_count INTEGER NOT NULL DEFAULT 1,
  last_result_count INTEGER,
  pinned_at TEXT
);
CREATE INDEX idx_history_used ON search_history(last_used_at);

CREATE TABLE recent_values (
  kind TEXT NOT NULL, value TEXT NOT NULL, context TEXT NOT NULL DEFAULT '',
  last_used_at TEXT,
  PRIMARY KEY (kind, value, context)
);
"""


def run_script(conn: sqlite3.Connection, script: str) -> None:
    """複数の SQL を、現在のトランザクションの中で1つずつ実行します（executescript は暗黙に COMMIT するため使いません）。"""
    for stmt in script.split(";"):
        if stmt.strip():
            conn.execute(stmt)


def _migrate_1(conn: sqlite3.Connection) -> None:
    run_script(conn, SCHEMA_V1)


#: 移行の関数（番号 → 関数）。番号は移行した後の schema_version です。
MIGRATIONS: dict[int, Callable[[sqlite3.Connection], None]] = {
    1: _migrate_1,
}

LATEST_VERSION = max(MIGRATIONS)


class Database:
    """SQLite のデータベース。"""

    def __init__(self, path: Path | str, *, network: bool = False,
                 migrations: dict[int, Callable[[sqlite3.Connection], None]] | None = None) -> None:
        self.path = str(path)
        self.network = network
        self.migrations = migrations or MIGRATIONS
        self._queue: queue.Queue = queue.Queue()
        self._local = threading.local()
        self._writer: threading.Thread | None = None
        self._stop = threading.Event()
        self._all_conns: list[sqlite3.Connection] = []
        self._conns_lock = threading.Lock()
        self.journal_mode = ""

    # ------------------------------------------------------------ 接続

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path, timeout=5.0, check_same_thread=False, isolation_level=None)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("PRAGMA busy_timeout=5000")
        conn.execute("PRAGMA synchronous=NORMAL")
        with self._conns_lock:
            self._all_conns.append(conn)
        return conn

    def _reader(self) -> sqlite3.Connection:
        conn = getattr(self._local, "conn", None)
        if conn is None:
            conn = self._connect()
            self._local.conn = conn
        return conn

    # ------------------------------------------------------------ 起動・移行

    def start(self) -> None:
        """移行を行い、書き込み用のスレッドを開始します。"""
        conn = self._connect()
        mode = "DELETE" if self.network else "WAL"
        self.journal_mode = conn.execute(f"PRAGMA journal_mode={mode}").fetchone()[0].upper()
        self._migrate(conn)
        self._wconn = conn
        self._writer = threading.Thread(target=self._writer_loop, name="db-writer", daemon=True)
        self._writer.start()

    def schema_version(self, conn: sqlite3.Connection | None = None) -> int:
        conn = conn or self._reader()
        try:
            row = conn.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone()
        except sqlite3.OperationalError:
            return 0
        return int(row[0]) if row else 0

    def _migrate(self, conn: sqlite3.Connection) -> None:
        current = self.schema_version(conn)
        latest = max(self.migrations)
        if current > latest:
            raise RuntimeError(f"データベースの版（{current}）が、本ツールの版より新しいため開けません")
        if current == latest:
            return
        if current > 0:
            backup_path = f"{self.path}.bak-{current}"
            dst = sqlite3.connect(backup_path)
            try:
                conn.backup(dst)
            finally:
                dst.close()
            log.info("データベースを移行します（%s → %s）。移行の前の複製: %s", current, latest, Path(backup_path).name)
        for version in range(current + 1, latest + 1):
            fn = self.migrations[version]
            conn.execute("BEGIN")
            try:
                fn(conn)
                conn.execute("INSERT OR REPLACE INTO meta(key, value) VALUES('schema_version', ?)", (str(version),))
                conn.execute("COMMIT")
            except Exception:
                conn.execute("ROLLBACK")
                raise

    # ------------------------------------------------------------ 書き込み

    def _writer_loop(self) -> None:
        conn = self._wconn
        while True:
            item = self._queue.get()
            if item is None:
                break
            batch = [item]
            while len(batch) < BATCH_LIMIT:
                try:
                    nxt = self._queue.get_nowait()
                except queue.Empty:
                    break
                if nxt is None:
                    self._queue.put(None)
                    break
                batch.append(nxt)
            self._run_batch(conn, batch)

    def _run_batch(self, conn: sqlite3.Connection, batch: list[tuple[Callable, Future]]) -> None:
        results: list[tuple[Future, Any, BaseException | None]] = []
        try:
            conn.execute("BEGIN IMMEDIATE")
            for i, (fn, fut) in enumerate(batch):
                sp = f"sp{i}"
                conn.execute(f"SAVEPOINT {sp}")
                try:
                    res = fn(conn)
                    conn.execute(f"RELEASE {sp}")
                    results.append((fut, res, None))
                except BaseException as exc:  # noqa: BLE001
                    conn.execute(f"ROLLBACK TO {sp}")
                    conn.execute(f"RELEASE {sp}")
                    results.append((fut, None, exc))
            conn.execute("COMMIT")
        except BaseException as exc:  # noqa: BLE001
            try:
                conn.execute("ROLLBACK")
            except sqlite3.Error:
                pass
            log.exception("データベースへの書き込みに失敗しました")
            for fn, fut in batch:
                if not fut.done():
                    fut.set_exception(exc)
            return
        for fut, res, exc in results:
            if exc is not None:
                fut.set_exception(exc)
            else:
                fut.set_result(res)

    def submit(self, fn: Callable[[sqlite3.Connection], Any]) -> Future:
        """書き込みの処理を受け付けます（書き込み用のスレッドで実行されます）。"""
        fut: Future = Future()
        if self._writer is None:
            raise RuntimeError("database not started")
        self._queue.put((fn, fut))
        return fut

    def write(self, fn: Callable[[sqlite3.Connection], Any], timeout: float | None = 60) -> Any:
        """書き込みの処理を実行し、終わるまで待って結果を返します。"""
        return self.submit(fn).result(timeout)

    def execute(self, sql: str, params: Iterable = ()) -> int:
        """1つの SQL を書き込み用のスレッドで実行し、変更した行の数を返します。"""
        return self.write(lambda c: c.execute(sql, tuple(params)).rowcount)

    def executemany(self, sql: str, rows: Iterable[Iterable]) -> None:
        rows = [tuple(r) for r in rows]
        self.write(lambda c: c.executemany(sql, rows))

    # ------------------------------------------------------------ 読み取り

    def query(self, sql: str, params: Iterable = ()) -> list[sqlite3.Row]:
        return self._reader().execute(sql, tuple(params)).fetchall()

    def query_one(self, sql: str, params: Iterable = ()) -> sqlite3.Row | None:
        return self._reader().execute(sql, tuple(params)).fetchone()

    def scalar(self, sql: str, params: Iterable = ()) -> Any:
        row = self.query_one(sql, params)
        return row[0] if row else None

    # ------------------------------------------------------------ 終了

    def flush(self, timeout: float = 30) -> None:
        """受け付けた書き込みがすべて終わるまで待ちます。"""
        if self._writer is not None and self._writer.is_alive():
            self.write(lambda c: None, timeout)

    def close(self) -> None:
        """書き込みを終えて、接続を閉じます。"""
        if self._writer is not None and self._writer.is_alive():
            self._queue.put(None)
            self._writer.join(30)
        with self._conns_lock:
            for c in self._all_conns:
                try:
                    c.close()
                except sqlite3.Error:
                    pass
            self._all_conns.clear()
        self._local = threading.local()
