"""T-QUEUE・T-JOB・T-DL・T-MD5・T-POLL: ジョブの作成、キュー、実行（確認・リハイドレート・完了の確認・ダウンロード）。"""

from __future__ import annotations

import json
import os
import threading
import time
import unittest
from dataclasses import replace
from datetime import datetime, timedelta, timezone

from finfet_abs.azure.backend import BackendError, BlobInfo
from finfet_abs.core.clock import FakeClock
from finfet_abs.core.db import Database
from finfet_abs.core.errors import AppError
from finfet_abs.core.events import EventBus
from finfet_abs.core.settings import Settings
from finfet_abs.demo.memory import MemoryBackend, blob_bytes, blob_md5
from finfet_abs.jobs.engine import JobEngine
from finfet_abs.jobs.limits import Limits
from finfet_abs.jobs.service import JobService
from finfet_abs.search.engine import SearchService
from finfet_abs.search.results import ResultsService

from .helpers import TempDirMixin

NOW = datetime(2026, 11, 20, 12, 0, tzinfo=timezone.utc)
KB = 1024


def blob(name: str, size: int = 10 * KB, tier: str = "Hot", md5: bool = True, **kw) -> BlobInfo:
    return BlobInfo(name, size=size, tier=tier, etag='"0x1"', content_md5=blob_md5(name, size) if md5 else None,
                    last_modified=NOW - timedelta(days=3), creation_time=NOW - timedelta(days=3),
                    access_tier_change_time=NOW - timedelta(days=200), **kw)


class JobTestBase(TempDirMixin):
    def setUp(self) -> None:
        super().setUp()
        self.db = Database(self.tmp / "finfet.db")
        self.db.start()
        self.settings = Settings(self.tmp / "settings.json")
        self.settings.load()
        self.out = self.tmp / "out"
        self.settings.update({"fetch.directory": str(self.out), "transfer.chunk_size_mib": 1})
        self.events = EventBus()
        self.clock = FakeClock(NOW)
        self.be = MemoryBackend()
        self.search = SearchService(db=self.db, events=self.events, settings=self.settings, backend=lambda: self.be,
                                    clock=self.clock)
        self.results = ResultsService(self.db)
        self.svc = JobService(self.db, self.settings, self.events, clock=self.clock)
        self.limits = Limits(self.settings)
        self.engine = JobEngine(db=self.db, settings=self.settings, events=self.events, service=self.svc,
                                backend=lambda: self.be, limits=self.limits, logs_dir=self.tmp / "logs", clock=self.clock)

    def tearDown(self) -> None:
        self.engine.stop()
        self.db.close()
        super().tearDown()

    # -------------------------------------------------------------- 補助

    def add(self, container: str, blobs: list[BlobInfo], account: str = "stexample01") -> None:
        self.be.add_blobs(account, container, blobs)

    def make_search(self, container: str = "logs", pattern: str = ".*", account: str = "stexample01",
                    select: dict | None = None) -> str:
        cond = {"targets": [{"storage_account": account, "endpoint_suffix": "core.windows.net", "container": container,
                             "auth_kind": "account_key"}], "path_pattern": pattern, "variables": {}, "input_timezone": "utc"}
        sid = self.search.start(cond)
        for _ in range(200):
            if not self.search.running(sid):
                break
            time.sleep(0.02)
        self.db.flush()
        self.results.select(sid, {"all_matching": True, "filter": select or {}})
        return sid

    def create(self, sid: str, **opts) -> str:
        start = opts.pop("start", True)
        dup = opts.pop("dup", None)
        return self.svc.create({"kind": "search", "search_id": sid}, opts, start=start, dup_resolution=dup,
                               confirm_rehydrate=True)

    def run_until(self, cond, timeout: float = 10.0) -> None:
        end = time.time() + timeout
        while time.time() < end:
            self.engine.schedule()
            if cond():
                return
            time.sleep(0.05)
        raise AssertionError("時間内に終わりません")

    def job(self, jid: str) -> dict:
        return self.svc.get(jid)

    def items(self, jid: str) -> list:
        return self.db.query("SELECT * FROM job_items WHERE job_id=? ORDER BY sort_key", (jid,))

    def status(self, jid: str) -> str:
        return self.db.scalar("SELECT status FROM jobs WHERE id=?", (jid,))


class JobCreateTest(JobTestBase):
    def test_create_from_search_independent(self) -> None:
        self.add("logs", [blob("a/1.log"), blob("a/2.log", tier="Archive"), blob("b/3.log")])
        sid = self.make_search()
        prev = self.svc.preview({"kind": "search", "search_id": sid})
        self.assertEqual(prev["count"], 3)
        self.assertEqual(prev["archive_count"], 1)
        self.assertEqual(prev["early_delete_count"], 0)  # Archive に置いてから 200 日
        jid = self.create(sid, method="rehydrate_download", start=False)
        j = self.job(jid)
        self.assertEqual((j["status"], j["counts"]["total"]), ("held", 3))
        self.assertTrue(j["name"].startswith("stexample01/logs"))
        # 元の検索結果を削除しても、ジョブの記録は残ります
        self.db.execute("DELETE FROM searches WHERE id=?", (sid,))
        self.assertEqual(len(self.items(jid)), 3)
        self.assertEqual(self.items(jid)[0]["planned_path"], "logs\\a\\1.log")

    def test_rehydrate_confirm_and_duplicates(self) -> None:
        self.add("logs", [blob("x/a.log", tier="Archive"), blob("y/a.log"), blob("z/A.LOG")])
        sid = self.make_search()
        with self.assertRaises(AppError) as cm:
            self.svc.create({"kind": "search", "search_id": sid}, {"path_structure": "name_only"})
        self.assertEqual(cm.exception.code, "REHYDRATE_CONFIRM")
        with self.assertRaises(AppError) as cm:
            self.create(sid, path_structure="name_only")
        self.assertEqual(cm.exception.code, "PATH_DUPLICATE")
        chk = self.svc.path_check({"kind": "search", "search_id": sid}, {"path_structure": "name_only"})
        self.assertEqual(chk["duplicates"], {"blobs": 3, "places": 1, "skip_count": 2})
        self.assertIsNotNone(chk["suggestion"])
        jid = self.create(sid, path_structure="name_only", dup="number")
        self.assertEqual(sorted(r["planned_path"] for r in self.items(jid)), ["A(2).LOG", "a(1).log", "a.log"])
        jid2 = self.create(sid, path_structure="name_only", dup="skip")
        self.assertEqual(sum(1 for r in self.items(jid2) if r["status"] == "skipped"), 2)

    def test_create_from_urls(self) -> None:
        from finfet_abs.search.urls import UrlSessions, parse_urls
        self.add("logs", [blob("a.log")])
        sessions = UrlSessions()
        self.svc.url_sessions = sessions
        rows, _ = parse_urls("https://stexample01.blob.core.windows.net/logs/a.log?sv=2024&sr=b&sp=r"
                             "&se=2027-01-01T00:00:00Z&sig=REDACTED_TEST\nhttps://stexample02.blob.core.windows.net/c/b.log",
                             NOW)
        token = sessions.put(rows)
        with self.assertRaises(AppError) as cm:
            self.svc.create({"kind": "urls", "token": token}, {})
        self.assertEqual(cm.exception.code, "AUTH_REQUIRED")
        jid = self.svc.create({"kind": "urls", "token": token,
                               "auth": {"stexample02": {"auth_kind": "entra_user"}}}, {}, start=False)
        its = self.items(jid)
        self.assertEqual([r["auth_kind"] for r in its], ["sas", "entra_user"])
        sec = self.db.query_one("SELECT * FROM job_secrets WHERE job_id=?", (jid,))
        self.assertNotIn("REDACTED_TEST", sec["secret"])  # 暗号化して保存
        self.assertEqual(its[0]["secret_id"], sec["id"])
        self.assertIsNone(self.db.scalar("SELECT search_json FROM jobs WHERE id=?", (jid,)))


class QueueTest(JobTestBase):
    def setUp(self) -> None:
        super().setUp()
        self.add("logs", [blob(f"f{i}.log", size=4 * KB) for i in range(3)])
        self.sid = self.make_search()

    def test_slots_and_order(self) -> None:
        a = self.create(self.sid, job_folder=True)
        b = self.create(self.sid, job_folder=True, start=False)
        c = self.create(self.sid, job_folder=True)
        self.assertEqual([self.status(x) for x in (a, b, c)], ["queued", "held", "queued"])
        gate = threading.Event()
        orig = self.be.download_range

        def slow(*args, **kw):
            gate.wait(5)
            return orig(*args, **kw)
        self.be.download_range = slow
        self.engine.schedule()
        time.sleep(0.2)
        self.assertEqual([self.status(x) for x in (a, b, c)], ["running", "held", "queued"])  # 枠は 1
        # 順番の変更: c を b の前へ。先頭に移動して開始で b を先頭へ
        self.svc.reorder(c, before=b)
        self.svc.move_top_and_start(b)
        order = [j["id"] for j in self.svc.list("queue")["items"]]
        self.assertEqual(order, [b, a, c])
        self.assertEqual(self.status(a), "running")  # 実行中のジョブは止めません
        gate.set()
        self.run_until(lambda: self.status(a) == "completed")
        self.run_until(lambda: self.status(b) == "completed")  # 次は先頭に移した b
        self.assertEqual(self.status(c), "running" if self.status(c) != "completed" else "completed")
        self.run_until(lambda: self.status(c) == "completed")
        self.assertEqual(self.svc.list("ended")["counts"]["ended"], 3)

    def test_pause_start_cancel(self) -> None:
        a = self.create(self.sid, start=False)
        self.assertEqual(self.svc.start([a]), 1)
        self.assertEqual(self.svc.pause([a]), 1)
        self.engine.schedule()
        self.assertEqual(self.status(a), "paused")  # 一時停止中は開始しません
        self.svc.start_all()
        self.assertEqual(self.status(a), "paused")  # すべて開始は未開始だけ
        self.svc.cancel([a])
        self.assertEqual(self.status(a), "cancelled")
        self.assertTrue(all(r["status"] == "cancelled" for r in self.items(a)))

    def test_startup_resume(self) -> None:
        a = self.create(self.sid)
        self.db.execute("UPDATE jobs SET status='running' WHERE id=?", (a,))
        self.db.execute("UPDATE job_items SET status='downloading' WHERE job_id=?", (a,))
        self.svc.on_startup()
        self.assertEqual(self.status(a), "queued")
        self.assertTrue(all(r["status"] == "ready" for r in self.items(a)))
        self.settings.update({"queue.resume_on_startup": False})
        self.db.execute("UPDATE jobs SET status='running' WHERE id=?", (a,))
        self.svc.on_startup()
        self.assertEqual(self.status(a), "paused")


class DownloadTest(JobTestBase):
    def test_download_and_verify(self) -> None:
        self.add("logs", [blob("d/big.bin", size=3 * 1024 * KB + 5), blob("d/no-md5.bin", md5=False), blob("d/e.bin", size=0),
                          BlobInfo("d/app.log", size=2000, blob_type="AppendBlob", etag='"0x2"')])
        sid = self.make_search()
        jid = self.create(sid, job_folder=False)
        self.run_until(lambda: self.status(jid) == "completed")
        its = {r["blob_name"]: r for r in self.items(jid)}
        self.assertEqual(its["d/big.bin"]["md5_result"], "match")
        self.assertEqual(its["d/no-md5.bin"]["md5_result"], "none")
        p = self.out / "logs" / "d" / "big.bin"
        self.assertEqual(p.read_bytes(), blob_bytes("d/big.bin", 0, 3 * 1024 * KB + 5))
        self.assertEqual(os.path.getsize(self.out / "logs" / "d" / "e.bin"), 0)
        self.assertFalse(any(x.name.endswith(".part") for x in (self.out / "logs" / "d").iterdir()))
        # 最終更新日時をファイルに設定します
        self.assertAlmostEqual(os.path.getmtime(p), (NOW - timedelta(days=3)).timestamp(), delta=2)

    def test_md5_mismatch_keeps_part(self) -> None:
        self.add("logs", [replace(blob("m.bin"), content_md5=blob_md5("other", 10 * KB))])
        jid = self.create(self.make_search())
        self.run_until(lambda: self.status(jid) == "completed_with_errors")
        it = self.items(jid)[0]
        self.assertEqual((it["status"], it["md5_result"]), ("error", "mismatch"))
        self.assertTrue(os.path.exists(it["part_path"]))

    def test_conflict_modes(self) -> None:
        self.add("logs", [blob("c.log")])
        sid = self.make_search()
        (self.out / "logs").mkdir(parents=True)
        (self.out / "logs" / "c.log").write_text("old", encoding="utf-8")
        for mode, expect in (("number", "c(1).log"), ("skip", None), ("overwrite", "c.log")):
            jid = self.create(sid, job_folder=False, conflict=mode)
            self.run_until(lambda: self.status(jid) in ("completed", "completed_with_errors"))
            it = self.items(jid)[0]
            if expect is None:
                self.assertEqual((it["status"], it["status_reason"]), ("skipped", "同名のファイルがある"))
            else:
                self.assertEqual(os.path.basename(it["local_path"]), expect)
                self.assertEqual(os.path.getsize(it["local_path"]), 10 * KB)

    def test_resume_ranges_and_etag_change(self) -> None:
        size = 4 * 1024 * KB
        self.add("logs", [blob("r.bin", size=size)])
        jid = self.create(self.make_search(), job_folder=False, connections_per_blob=1, start=False)
        calls = []
        orig = self.be.download_range

        def failing(target, name, offset, length, etag):
            calls.append(offset)
            if len(calls) == 3:
                raise BackendError("ServerBusy", "混雑", status=503)
            return orig(target, name, offset, length, etag)
        self.be.download_range = failing
        self.svc.start([jid])
        self.run_until(lambda: self.status(jid) == "completed")
        # 失敗した範囲から再開し、取得済みの範囲は取り直しません
        self.assertEqual(sorted(calls), sorted([0, 1024 * KB, 2 * 1024 * KB, 2 * 1024 * KB, 3 * 1024 * KB]))
        self.assertEqual((self.out / "logs" / "r.bin").read_bytes(), blob_bytes("r.bin", 0, size))

    def test_resume_with_different_chunk_size(self) -> None:
        """前回と範囲の大きさが違っても（帯域の制限の有無で変わる）、足りない部分だけを正しく取得します。"""
        size = 3 * 1024 * KB + 100
        self.add("logs", [blob("c.bin", size=size)])
        jid = self.create(self.make_search(), job_folder=False, start=False)
        it = self.items(jid)[0]
        final = str(self.out / "logs" / "c.bin")
        part = final + ".part"
        os.makedirs(os.path.dirname(final), exist_ok=True)
        open(final, "wb").close()
        with open(part, "wb") as f:
            f.truncate(size)
            f.seek(0)
            f.write(blob_bytes("c.bin", 0, 256 * KB))  # 前回は 256 KiB だけ取得した
        self.db.execute("UPDATE job_items SET status='ready', local_path=?, part_path=?, ranges_done_json=?, "
                        "download_etag=?, download_started_at=? WHERE id=?",
                        (final, part, json.dumps([[0, 256 * KB]]), '"0x1"', "2026-11-20T00:00:00Z", it["id"]))
        got = []
        orig = self.be.download_range
        self.be.download_range = lambda t, n, o, ln, e: (got.append((o, ln)), orig(t, n, o, ln, e))[1]
        self.svc.start([jid])
        self.run_until(lambda: self.status(jid) == "completed")
        self.assertEqual((self.out / "logs" / "c.bin").read_bytes(), blob_bytes("c.bin", 0, size))
        self.assertIn((256 * KB, 1024 * KB), got)  # 取得済みの続きから、今の大きさで
        self.assertEqual(min(o for o, _ in got), 256 * KB)  # 取得済みの部分は取り直しません
        self.assertTrue(any("から再開します" in ln for ln in open(self.tmp / "logs" / f"{jid}.log", encoding="utf-8")))

    def test_412_restart_then_error(self) -> None:
        self.add("logs", [blob("u.bin", size=2 * 1024 * KB)])
        jid = self.create(self.make_search())
        self.be.download_range = lambda *a, **k: (_ for _ in ()).throw(BackendError("ConditionNotMet", "x", status=412))
        self.run_until(lambda: self.status(jid) == "completed_with_errors")
        it = self.items(jid)[0]
        self.assertEqual(it["error_code"], "ConditionNotMet")
        self.assertIn("更新され続けた", it["error_message"])

    def test_name_conversion_and_long_path(self) -> None:
        long = "/".join(["d" * 60] * 4) + "/a:b?.log"
        self.add("logs", [blob(long), blob("con/x.log")])
        jid = self.create(self.make_search())
        self.run_until(lambda: self.status(jid) == "completed")
        paths_ = sorted(r["local_path"] for r in self.items(jid))
        self.assertTrue(any(p.endswith("_con\\x.log") for p in paths_))
        self.assertTrue(any(p.endswith("a_b_.log") and len(p) > 240 for p in paths_))


class RehydrateTest(JobTestBase):
    def setUp(self) -> None:
        super().setUp()
        self.add("logs", [blob("arc/1.log", tier="Archive"), blob("arc/2.log", tier="Archive"), blob("hot/3.log")])
        self.sid = self.make_search()

    def test_rehydrate_then_download(self) -> None:
        jid = self.create(self.sid, method="rehydrate_download", priority="Standard")
        self.run_until(lambda: self.status(jid) == "waiting_rehydrate")
        its = {r["blob_name"]: r for r in self.items(jid)}
        self.assertEqual(its["arc/1.log"]["status"], "rehydrating")
        self.assertEqual(its["arc/1.log"]["rehydrate_requested_known"], 1)
        self.assertEqual(its["hot/3.log"]["status"], "completed")  # 待っている間も、ほかの Blob は進みます
        # 最初の確認は 60 分後
        self.assertEqual(self.engine.poll(), 0)
        self.clock.advance(61 * 60)
        self.assertEqual(self.engine.poll(), 2)
        nxt = from_iso(self.items(jid)[0]["next_check_at"])
        self.assertEqual(nxt, self.clock.now() + timedelta(minutes=30))  # 以降は 30 分ごと
        self.be.complete_rehydration("stexample01", "logs", "arc/1.log", when=self.clock.now())
        self.clock.advance(31 * 60)
        self.engine.poll()
        it = {r["blob_name"]: r for r in self.items(jid)}["arc/1.log"]
        self.assertEqual(it["status"], "ready")
        self.assertIsNotNone(it["rehydrate_completed_azure_at"])
        self.assertEqual(self.status(jid), "queued")  # 完了した Blob が出ると開始待ちに戻ります
        self.run_until(lambda: self.status(jid) == "waiting_rehydrate" and
                       {r["blob_name"]: r for r in self.items(jid)}["arc/1.log"]["status"] == "completed")

    def test_raise_priority_and_lost(self) -> None:
        jid = self.create(self.sid, method="rehydrate_only")
        self.run_until(lambda: self.status(jid) == "waiting_rehydrate")
        ids = [r["id"] for r in self.items(jid) if r["status"] == "rehydrating"]
        res = self.engine.raise_priority(jid, ids)
        self.assertEqual(res["changed"], 2)
        it = self.items(jid)[0]
        self.assertEqual(it["rehydrate_priority"], "High")
        self.assertEqual(from_iso(it["next_check_at"]), self.clock.now() + timedelta(minutes=15))
        # 要求が消えた（Archive のまま）→ エラー
        _n, by = self.be.data[("stexample01", "core.windows.net")]["logs"]
        by["arc/2.log"] = replace(by["arc/2.log"], archive_status=None)
        self.be.complete_rehydration("stexample01", "logs", "arc/1.log")
        self.clock.advance(16 * 60)
        self.engine.poll()
        its = {r["blob_name"]: r for r in self.items(jid)}
        self.assertEqual(its["arc/2.log"]["error_code"], "RehydrationLost")
        self.assertEqual(its["arc/1.log"]["status"], "completed")  # リハイドレートのみ
        self.assertEqual(self.status(jid), "waiting_download")
        # 再要求
        self.assertEqual(self.engine.retry_items(jid, [its["arc/2.log"]["id"]], "re-request"), 1)

    def test_paused_not_polled(self) -> None:
        jid = self.create(self.sid, method="rehydrate_only")
        self.run_until(lambda: self.status(jid) == "waiting_rehydrate")
        self.svc.pause([jid])
        self.clock.advance(3 * 3600)
        self.assertEqual(self.engine.poll(), 0)

    def test_list_check_for_many(self) -> None:
        self.add("many", [blob(f"x/{i:03d}.log", tier="Archive") for i in range(30)])
        sid = self.make_search(container="many")
        jid = self.create(sid, method="rehydrate_only")
        self.run_until(lambda: self.status(jid) == "waiting_rehydrate")
        self.clock.advance(61 * 60)
        self.be.requests.clear()
        self.assertEqual(self.engine.poll(), 30)
        self.assertFalse([r for r in self.be.requests if r[0] == "props"])  # 一覧でまとめて確認
        self.assertTrue([r for r in self.be.requests if r[0] == "list"])

    def test_download_only_skips_archive_and_start_download(self) -> None:
        jid = self.create(self.sid, method="download_only")
        self.run_until(lambda: self.status(jid) == "completed")
        st = {r["blob_name"]: (r["status"], r["status_reason"]) for r in self.items(jid)}
        self.assertEqual(st["arc/1.log"][0], "skipped")
        self.assertEqual(st["hot/3.log"][0], "completed")
        # リハイドレートのみ → 後日のダウンロード
        jid2 = self.create(self.sid, method="rehydrate_only")
        self.run_until(lambda: self.status(jid2) == "waiting_rehydrate")
        for n in ("arc/1.log", "arc/2.log"):
            self.be.complete_rehydration("stexample01", "logs", n)
        self.clock.advance(61 * 60)
        self.engine.poll()
        self.assertEqual(self.status(jid2), "waiting_download")
        self.engine.start_download(jid2, mode="completed_only")
        self.run_until(lambda: self.status(jid2) == "completed")
        self.assertTrue(all(r["download_completed_at"] for r in self.items(jid2)))

    def test_resume_after_cancel(self) -> None:
        jid = self.create(self.sid, method="rehydrate_download")
        self.run_until(lambda: self.status(jid) == "waiting_rehydrate")
        self.svc.cancel([jid])
        n = self.engine.resume(jid)
        self.assertEqual(n, 2)
        its = {r["blob_name"]: r["status"] for r in self.items(jid)}
        self.assertEqual(its["arc/1.log"], "rehydrating")  # 要求し直しません
        self.assertEqual(its["hot/3.log"], "completed")


def from_iso(text):
    from finfet_abs.core.clock import from_iso as f
    return f(text)


if __name__ == "__main__":
    unittest.main()
