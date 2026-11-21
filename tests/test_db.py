"""T-DB: SQLite（移行、まとめての書き込み、journal_mode、30 万件の絞り込みの速さ）。"""

import threading
import time
import unittest
from pathlib import Path

from finfet_abs.core import db as dbmod
from finfet_abs.core.db import Database

from .helpers import TempDirMixin


class DbTest(TempDirMixin):
    def test_migrate_fresh(self) -> None:
        d = Database(self.tmp / "a.db")
        d.start()
        try:
            self.assertEqual(d.schema_version(), dbmod.LATEST_VERSION)
            names = {r[0] for r in d.query("SELECT name FROM sqlite_master WHERE type='table'")}
            for t in ("meta", "searches", "search_targets", "search_results", "jobs", "job_items", "job_secrets",
                      "credentials", "search_history", "recent_values", "search_selection"):
                self.assertIn(t, names)
            self.assertEqual(d.journal_mode, "WAL")
            self.assertEqual(d.scalar("PRAGMA foreign_keys"), 1)
        finally:
            d.close()

    def test_migrate_upgrade_with_backup(self) -> None:
        path = self.tmp / "b.db"
        d = Database(path)
        d.start()
        d.execute("INSERT INTO recent_values(kind, value, context) VALUES('container','logs','')")
        d.close()

        def m2(conn):
            conn.execute("ALTER TABLE recent_values ADD COLUMN extra TEXT")

        migrations = dict(dbmod.MIGRATIONS)
        v1 = max(migrations)
        migrations[v1 + 1] = m2
        d2 = Database(path, migrations=migrations)
        d2.start()
        try:
            self.assertEqual(d2.schema_version(), v1 + 1)
            self.assertTrue(Path(f"{path}.bak-{v1}").exists())
            self.assertEqual(d2.scalar("SELECT value FROM recent_values"), "logs")
            d2.execute("UPDATE recent_values SET extra='x'")
        finally:
            d2.close()
        # 新しすぎる版は開きません
        d3 = Database(path)
        with self.assertRaises(RuntimeError):
            d3.start()
        d3.close()

    def test_network_journal_mode(self) -> None:
        d = Database(self.tmp / "c.db", network=True)
        d.start()
        try:
            self.assertEqual(d.journal_mode, "DELETE")
        finally:
            d.close()

    def test_reader_connections_reused(self) -> None:
        """短い間だけ動くスレッドが多数読み取っても、接続が増え続けないこと（メモリマップの増加を防ぐ。N4）。"""
        d = Database(self.tmp / "r.db")
        d.start()
        try:
            def work():
                for _ in range(5):
                    d.scalar("SELECT 1")
            for _ in range(50):
                ts = [threading.Thread(target=work) for _ in range(4)]
                for t in ts:
                    t.start()
                for t in ts:
                    t.join()
            # 書き込み用の 1 つと、置き場の読み取り用（同時に使った数まで）だけ
            self.assertLessEqual(len(d._all_conns), 1 + dbmod.MAX_IDLE_READERS)
        finally:
            d.close()

    def test_batched_writes(self) -> None:
        d = Database(self.tmp / "d.db")
        d.start()
        try:
            futs = []
            lock = threading.Lock()

            def writer(n):
                for i in range(200):
                    f = d.submit(lambda c, n=n, i=i: c.execute(
                        "INSERT INTO recent_values(kind, value, context) VALUES('container', ?, '')", (f"{n}-{i}",)))
                    with lock:
                        futs.append(f)

            ths = [threading.Thread(target=writer, args=(n,)) for n in range(5)]
            for t in ths:
                t.start()
            for t in ths:
                t.join()
            for f in futs:
                f.result(10)
            self.assertEqual(d.scalar("SELECT COUNT(*) FROM recent_values"), 1000)

            # 失敗した書き込みは、その書き込みだけを取り消します
            def bad(c):
                c.execute("INSERT INTO recent_values(kind, value, context) VALUES('container','bad','')")
                raise ValueError("x")

            f1 = d.submit(bad)
            f2 = d.submit(lambda c: c.execute("INSERT INTO recent_values(kind, value, context) VALUES('container','good','')"))
            with self.assertRaises(ValueError):
                f1.result(10)
            f2.result(10)
            self.assertEqual(d.scalar("SELECT COUNT(*) FROM recent_values WHERE value='bad'"), 0)
            self.assertEqual(d.scalar("SELECT COUNT(*) FROM recent_values WHERE value='good'"), 1)
        finally:
            d.close()

    def test_filter_300k_speed(self) -> None:
        d = Database(self.tmp / "e.db")
        d.start()
        try:
            d.execute("INSERT INTO searches(id, condition_json, status) VALUES('s1','{}','completed')")
            rows = []
            for i in range(300_000):
                day = i // 24 % 28 + 1
                hour = i % 24
                name = (f"resourceId=/SUBSCRIPTIONS/00000000-0000-0000-0000-000000000000/RESOURCEGROUPS/RG{i % 7}/"
                        f"PROVIDERS/MICROSOFT.STORAGE/STORAGEACCOUNTS/STEXAMPLE01/BLOBSERVICES/DEFAULT/"
                        f"y=2026/m=11/d={day:02d}/h={hour:02d}/m=00/PT1H_{i}.json")
                rows.append(("s1", 0, "stexample01", "core.windows.net", "logs", name, name.lower(),
                             f"{i:09d}".encode(), "BlockBlob", 1000 + i, "Archive"))
            d.executemany("INSERT INTO search_results(search_id, target_no, storage_account, endpoint_suffix, container, "
                          "blob_name, blob_name_lower, sort_key, blob_type, size, tier) VALUES(?,?,?,?,?,?,?,?,?,?,?)", rows)
            t0 = time.perf_counter()
            total = d.scalar("SELECT COUNT(*) FROM search_results WHERE search_id='s1' AND blob_name_lower LIKE ? "
                             "ESCAPE '\\' AND blob_name_lower LIKE ? ESCAPE '\\'", ("%rg3%", "%h=05%"))
            page = d.query("SELECT id, blob_name FROM search_results WHERE search_id='s1' AND blob_name_lower LIKE ? "
                           "ESCAPE '\\' ORDER BY sort_key LIMIT 200", ("%rg3%",))
            elapsed = time.perf_counter() - t0
            self.assertGreater(total, 0)
            self.assertEqual(len(page), 200)
            self.assertLess(elapsed, 0.5, f"{elapsed:.3f} 秒")
        finally:
            d.close()


if __name__ == "__main__":
    unittest.main()
