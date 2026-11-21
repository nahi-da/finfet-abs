"""T-SRCH: 検索の実行と検索結果（接続先ごとの進捗、中止、再試行・除く、同時の検索の上限、保存と残す数、選択）。"""

from __future__ import annotations

import threading
import time
import unittest
from datetime import datetime, timedelta, timezone

from finfet_abs.azure.backend import BlobInfo
from finfet_abs.core.clock import FakeClock
from finfet_abs.core.db import Database
from finfet_abs.core.errors import AppError
from finfet_abs.core.events import EventBus
from finfet_abs.core.settings import Settings
from finfet_abs.demo.memory import MemoryBackend
from finfet_abs.history.service import HistoryService
from finfet_abs.search.engine import SearchService
from finfet_abs.search.results import ResultsService

from .helpers import TempDirMixin

NOW = datetime(2026, 11, 20, 12, 0, tzinfo=timezone.utc)


def blobs(prefix: str, n: int, *, tier: str = "Hot", size: int = 100) -> list[BlobInfo]:
    return [BlobInfo(f"{prefix}{i:04d}.log", size=size + i, tier=tier, last_modified=NOW - timedelta(days=2, minutes=i),
                     creation_time=NOW - timedelta(days=3)) for i in range(n)]


def target(container: str, account: str = "stexample01") -> dict:
    return {"storage_account": account, "endpoint_suffix": "core.windows.net", "container": container,
            "auth_kind": "entra_user"}


def cond(*targets: dict, pattern: str = r"logs/.*\.log") -> dict:
    return {"targets": list(targets), "path_pattern": pattern, "variables": {}, "input_timezone": "utc"}


class BlockingBackend(MemoryBackend):
    """一覧の取得を、合図があるまで止める架空の実装（同時の検索の数・中止の確認用）。"""

    def __init__(self) -> None:
        super().__init__()
        self.gate = threading.Event()
        self.active = 0
        self.max_active = 0
        self._count_lock = threading.Lock()

    def list_blobs_page(self, target, prefix, continuation=None, page_size=5000, include_metadata=False):
        with self._count_lock:
            self.active += 1
            self.max_active = max(self.max_active, self.active)
        try:
            self.gate.wait(10)
            return super().list_blobs_page(target, prefix, continuation, page_size, include_metadata)
        finally:
            with self._count_lock:
                self.active -= 1


class SearchTestBase(TempDirMixin):
    backend_cls = MemoryBackend

    def setUp(self) -> None:
        super().setUp()
        self.db = Database(self.tmp / "finfet.db")
        self.db.start()
        self.settings = Settings(self.tmp / "settings.json")
        self.settings.load()
        self.events = EventBus()
        self.seen: list[dict] = []
        self.events.subscribe(self.seen.append)
        self.clock = FakeClock(NOW)
        self.be = self.backend_cls()
        self.be.add_blobs("stexample01", "c1", blobs("logs/a", 30) + blobs("other/x", 5))
        self.be.add_blobs("stexample01", "c2", blobs("logs/b", 20, tier="Archive", size=1000))
        self.be.add_blobs("stexample02", "c3", blobs("logs/c", 10, tier="Cool"))
        self.history = HistoryService(self.db, self.settings, self.clock)
        self.svc = SearchService(db=self.db, events=self.events, settings=self.settings, backend=lambda: self.be,
                                 history=self.history, clock=self.clock)
        self.results = ResultsService(self.db)

    def tearDown(self) -> None:
        if isinstance(self.be, BlockingBackend):
            self.be.gate.set()
        for _ in range(100):
            with self.svc._lock:
                if not self.svc._runs:
                    break
            time.sleep(0.05)
        self.db.close()
        super().tearDown()

    def wait_done(self, sid: str, timeout: float = 10) -> dict:
        end = time.time() + timeout
        while time.time() < end:
            if not self.svc.running(sid):
                self.db.flush()
                return self.db.query_one("SELECT * FROM searches WHERE id=?", (sid,))
            time.sleep(0.02)
        raise AssertionError("検索が終わりません")

    def targets(self, sid: str) -> list:
        return self.db.query("SELECT * FROM search_targets WHERE search_id=? ORDER BY target_no", (sid,))


class SearchRunTest(SearchTestBase):
    def test_progress_per_target(self) -> None:
        sid = self.svc.start(cond(target("c1"), target("c2"), target("c3", "stexample02")))
        row = self.wait_done(sid)
        self.assertEqual(row["status"], "completed")
        self.assertEqual(row["matched_count"], 60)
        self.assertEqual(row["matched_bytes"], sum(b.size for b in blobs("logs/a", 30) + blobs("logs/b", 20, size=1000)
                                                     + blobs("logs/c", 10)))
        ts = self.targets(sid)
        self.assertEqual([t["status"] for t in ts], ["completed"] * 3)
        self.assertEqual([t["matched_count"] for t in ts], [30, 20, 10])
        self.assertEqual(ts[0]["listed_count"], 30)  # prefix「logs/」で other/ は取得しない
        self.assertTrue(all(t["request_count"] >= 1 for t in ts))
        # 進捗のイベント（接続先ごと）と終わりのイベント
        prog = [e for e in self.seen if e["type"] == "search.progress" and e["data"]["id"] == sid]
        self.assertTrue(prog)
        self.assertEqual([t["matched"] for t in prog[-1]["data"]["targets"]], [30, 20, 10])
        done = [e for e in self.seen if e["type"] == "search.done" and e["data"]["id"] == sid]
        self.assertEqual(done[-1]["data"]["matched"], 60)
        # 検索履歴に件数が残る
        self.db.flush()
        h = self.history.list()["items"][0]
        self.assertEqual(h["last_result_count"], 60)

    def test_error_then_other_targets_continue(self) -> None:
        self.be.denied[("stexample01", "c2", "list")] = "AuthorizationPermissionMismatch"
        sid = self.svc.start(cond(target("c1"), target("c2"), target("c3", "stexample02")))
        row = self.wait_done(sid)
        ts = self.targets(sid)
        self.assertEqual([t["status"] for t in ts], ["completed", "error", "completed"])
        self.assertIn("AuthorizationPermissionMismatch", ts[1]["error_json"])
        self.assertEqual(row["status"], "completed")
        self.assertEqual(row["matched_count"], 40)

    def test_all_failed(self) -> None:
        sid = self.svc.start(cond(target("nothere")))
        self.assertEqual(self.wait_done(sid)["status"], "failed")

    def test_retry_target(self) -> None:
        self.be.denied[("stexample01", "c2", "list")] = "AuthorizationPermissionMismatch"
        sid = self.svc.start(cond(target("c1"), target("c2")))
        self.wait_done(sid)
        del self.be.denied[("stexample01", "c2", "list")]
        self.svc.retry_target(sid, 1)
        row = self.wait_done(sid)
        self.assertEqual([t["status"] for t in self.targets(sid)], ["completed", "completed"])
        self.assertEqual(row["matched_count"], 50)
        self.assertEqual(self.db.scalar("SELECT COUNT(*) FROM search_results WHERE search_id=?", (sid,)), 50)
        # 再試行を繰り返しても、結果は重複しません
        self.svc.retry_target(sid, 1)
        self.assertEqual(self.wait_done(sid)["matched_count"], 50)

    def test_skip_target(self) -> None:
        sid = self.svc.start(cond(target("c1"), target("c2")))
        self.wait_done(sid)
        self.svc.skip_target(sid, 1)
        row = self.db.query_one("SELECT * FROM searches WHERE id=?", (sid,))
        self.assertEqual(row["matched_count"], 30)
        self.assertEqual(self.targets(sid)[1]["status"], "skipped")
        self.assertEqual(self.results.summary(sid, {})["all"]["count"], 30)

    def test_cancel(self) -> None:
        self.be = BlockingBackend()
        self.be.add_blobs("stexample01", "c1", blobs("logs/a", 30))
        self.be.add_blobs("stexample01", "c2", blobs("logs/b", 30))
        sid = self.svc.start(cond(target("c1"), target("c2")))
        time.sleep(0.2)
        self.svc.cancel(sid)
        self.be.gate.set()
        row = self.wait_done(sid)
        self.assertEqual(row["status"], "cancelled")
        self.assertIn("cancelled", [t["status"] for t in self.targets(sid)])
        with self.assertRaises(AppError):
            self.svc.cancel(sid)  # 終わった検索は中止できません

    def test_concurrency_limit(self) -> None:
        self.be = BlockingBackend()
        for k in range(3):
            self.be.add_blobs("stexample01", f"c{k}", blobs("logs/a", 5))
        sids = [self.svc.start(cond(target(f"c{k}"))) for k in range(3)]
        time.sleep(0.4)
        self.assertEqual(self.be.active, 2)  # 3つ目は待ちます
        third = self.targets(sids[2])[0]
        self.assertEqual(third["status"], "waiting")
        self.be.gate.set()
        for sid in sids:
            self.assertEqual(self.wait_done(sid)["status"], "completed")
        self.assertLessEqual(self.be.max_active, 2)

    def test_max_results_truncates(self) -> None:
        self.settings.update({"search.max_results": 25})
        sid = self.svc.start(cond(target("c1"), target("c2")))
        row = self.wait_done(sid)
        self.assertEqual(row["truncated"], 1)
        self.assertEqual(row["matched_count"], 25)
        self.assertEqual(self.targets(sid)[1]["status"], "skipped")

    def test_resource_id_case(self) -> None:
        """既知の形式のリソース ID の部分（大文字・小文字を区別しない）を、実際の表記に合わせて取得します。"""
        from finfet_abs.search import formats
        sub = "00000000-0000-0000-0000-00000000000a"
        mixed = (f"resourceId=/subscriptions/{sub}/resourceGroups/rg-x/providers/Microsoft.Storage/storageAccounts/"
                 "stexample01/blobServices/default")
        upper = mixed.upper().replace("RESOURCEID=", "resourceId=")
        other = mixed.replace("stexample01", "stexample02")
        names = [f"{base}/y=2026/m=11/d=20/h={h:02d}/m=00/PT1H.json" for base in (mixed, upper, other) for h in range(24)]
        self.be.add_blobs("stexample01", "insights-logs-storageread", [BlobInfo(n, size=10) for n in names])
        b = formats.build("resource-logs", {"category": "StorageRead", "sub": sub, "rg": "RG-X", "resource": "STEXAMPLE01"})
        c = cond(dict(target(b["container"])), pattern=b["path_pattern"])
        c["variables"] = {**b["variables"], "dt": {"type": "datetime", "path_timezone": "utc",
                                                  "value": {"mode": "period", "start": "2026-11-20", "end": "2026-11-20"}}}
        self.be.requests.clear()
        from unittest import mock
        from finfet_abs.search import listing
        with mock.patch.object(listing, "PAGE_SIZE", 10):  # 1 ページで足りず、prefix ごとの取得に切り替わる場合
            sid = self.svc.start(c)
            row = self.wait_done(sid)
        self.assertEqual(row["matched_count"], 48)  # 混ざった表記と大文字の表記の両方（ほかのリソースは含めない）
        prefixes = {r[2] for r in self.be.requests if r[0] == "list"}
        self.assertTrue(any(p.startswith(mixed + "/y=2026") for p in prefixes), prefixes)
        self.assertTrue(any(p.startswith(upper + "/y=2026") for p in prefixes), prefixes)
        self.assertFalse(any(p.startswith(other) for p in prefixes))  # 実際の表記の prefix で取得します
        # 一致する階層がなければ、一覧を取得せずに 0 件
        b2 = formats.build("resource-logs", {"category": "StorageRead", "sub": sub, "rg": "rg-none", "resource": "x"})
        c["path_pattern"] = b2["path_pattern"]
        self.be.requests.clear()
        sid = self.svc.start(c)
        self.assertEqual(self.wait_done(sid)["matched_count"], 0)
        self.assertEqual([r for r in self.be.requests if r[0] == "list"], [])
        self.assertEqual(self.targets(sid)[0]["status"], "completed")

    def test_invalid_condition(self) -> None:
        with self.assertRaises(AppError) as cm:
            self.svc.start(cond(target("c1"), pattern="logs/{{x"))
        self.assertEqual(cm.exception.code, "PATTERN_INVALID")
        with self.assertRaises(AppError) as cm:
            self.svc.start(cond(dict(target("c1"), auth_kind=None)))
        self.assertEqual(cm.exception.code, "AUTH_REQUIRED")

    def test_keep_recent(self) -> None:
        self.settings.update({"search.keep_recent": 2})
        sids = []
        for k in range(4):
            self.clock.set(NOW + timedelta(minutes=k))
            sid = self.svc.start(cond(target("c1")))
            self.wait_done(sid)
            sids.append(sid)
        self.db.flush()
        left = {r[0] for r in self.db.query("SELECT id FROM searches")}
        self.assertEqual(left, set(sids[-2:]))
        # 削除した検索の結果も残りません
        self.assertEqual(self.db.scalar("SELECT COUNT(*) FROM search_results WHERE search_id=?", (sids[0],)), 0)

    def test_results_persist_after_restart(self) -> None:
        sid = self.svc.start(cond(target("c1")))
        self.wait_done(sid)
        self.db.close()
        self.db = Database(self.tmp / "finfet.db")
        self.db.start()
        res = ResultsService(self.db)
        self.assertEqual(res.list(sid, {}, "name", 0, 10)["total"], 30)


class ResultsTest(SearchTestBase):
    def setUp(self) -> None:
        super().setUp()
        self.sid = self.svc.start(cond(target("c1"), target("c2"), target("c3", "stexample02")))
        self.wait_done(self.sid)

    def names(self, sort: str = "name", f: dict | None = None, offset: int = 0, limit: int = 500) -> list[str]:
        return [r["blob_name"] for r in self.results.list(self.sid, f or {}, sort, offset, limit)["items"]]

    def test_sorts(self) -> None:
        for key in ("name", "size", "last_modified", "creation_time", "tier_change", "tier", "type", "target", "notes"):
            for s in (key, f"-{key}"):
                got = self.results.list(self.sid, {}, s, 0, 500)
                self.assertEqual(got["total"], 60, s)
                self.assertEqual(len({r["id"] for r in got["items"]}), 60, s)
        sizes = [r["size"] for r in self.results.list(self.sid, {}, "-size", 0, 500)["items"]]
        self.assertEqual(sizes, sorted(sizes, reverse=True))
        tiers = [r["tier"] for r in self.results.list(self.sid, {}, "tier", 0, 500)["items"]]
        self.assertEqual(tiers, sorted(tiers))
        # ページを分けても、全体の並びと同じです
        whole = self.names("-size")
        self.assertEqual(self.names("-size", offset=0, limit=25) + self.names("-size", offset=25, limit=35), whole)

    def test_filters(self) -> None:
        self.assertEqual(self.results.list(self.sid, {"q": "LOGS/B"}, "name", 0, 10)["total"], 20)
        self.assertEqual(self.results.list(self.sid, {"q": "logs/a 001"}, "name", 0, 100)["total"], 11)  # a0001 と a0010〜a0019
        self.assertEqual(self.results.list(self.sid, {"q": "a_0"}, "name", 0, 100)["total"], 0)  # _ は文字として扱う
        self.assertEqual(self.results.list(self.sid, {"tier": ["Archive"]}, "name", 0, 100)["total"], 20)
        self.assertEqual(self.results.list(self.sid, {"tier": ["Hot", "Cool"]}, "name", 0, 100)["total"], 40)
        self.assertEqual(self.results.list(self.sid, {"target": "2"}, "name", 0, 100)["total"], 10)
        s = self.results.summary(self.sid, {"tier": ["Archive"]})
        self.assertEqual(s["filtered"]["count"], 20)
        self.assertEqual(s["all"]["count"], 60)
        self.assertEqual(s["tiers"]["Archive"]["count"], 20)

    def test_select_all_matching_and_hidden(self) -> None:
        self.results.select(self.sid, {"all_matching": True, "filter": {"tier": ["Archive"]}})
        s = self.results.summary(self.sid, {})
        self.assertEqual(s["selected"]["count"], 20)
        self.assertEqual(s["selected"]["archive_count"], 20)
        # 絞り込みを変えると、選んだうち表示されていないものの数が分かります
        s = self.results.summary(self.sid, {"q": "logs/a"})
        self.assertEqual(s["hidden_selected"], 20)
        self.assertEqual(len(self.results.selected_rows(self.sid)), 20)
        self.assertEqual(len(self.results.selected_rows(self.sid, only_visible={"q": "logs/a"})), 0)
        # 一致するものだけ外す
        self.results.select(self.sid, {"all_matching": True, "filter": {"q": "logs/b000"}, "selected": False})
        self.assertEqual(self.results.summary(self.sid, {})["selected"]["count"], 10)
        self.results.select(self.sid, {"clear": True})
        self.assertEqual(self.results.summary(self.sid, {})["selected"]["count"], 0)

    def test_select_ids_and_range(self) -> None:
        items = self.results.list(self.sid, {}, "-size", 0, 500)["items"]
        self.results.select(self.sid, {"ids": [items[0]["id"], items[1]["id"]]})
        got = self.results.list(self.sid, {}, "-size", 0, 3)["items"]
        self.assertEqual([r["selected"] for r in got], [True, True, False])
        # Shift での範囲（並びと絞り込みに従う）
        self.results.select(self.sid, {"range": {"start": 10, "end": 5}, "filter": {}, "sort": "-size"})
        sel = {r["id"] for r in self.results.list(self.sid, {}, "-size", 0, 500)["items"] if r["selected"]}
        self.assertEqual(sel, {items[i]["id"] for i in (0, 1, 5, 6, 7, 8, 9, 10)})
        # ほかの検索の結果の ID は選べません
        self.results.select(self.sid, {"ids": [999999]})
        self.assertEqual(self.results.summary(self.sid, {})["selected"]["count"], 8)

    def test_summary_cache_follows_changes(self) -> None:
        self.assertEqual(self.results.summary(self.sid, {})["all"]["count"], 60)
        self.svc.skip_target(self.sid, 2)
        self.assertEqual(self.results.summary(self.sid, {})["all"]["count"], 50)
        self.assertEqual(self.results.list(self.sid, {}, "name", 0, 1)["total"], 50)

    def test_display_modes(self) -> None:
        tail = self.results.list(self.sid, {}, "name", 0, 1, "tail")["items"][0]
        self.assertTrue(tail["display_name"].endswith("/a0000.log") and not tail["display_name"].startswith("logs"))
        full = self.results.list(self.sid, {}, "name", 0, 1)["items"][0]
        self.assertEqual(full["display_name"], "logs/a0000.log")

    def test_csv(self) -> None:
        text = self.results.csv(self.sid, {"tier": ["Cool"]}, False).decode("utf-8-sig")
        self.assertIn("logs/c0000.log", text)
        self.assertNotIn("logs/a0000.log", text)
        self.results.select(self.sid, {"ids": [self.results.list(self.sid, {}, "name", 0, 1)["items"][0]["id"]]})
        text = self.results.csv(self.sid, {}, True).decode("utf-8-sig")
        self.assertEqual(len([x for x in text.splitlines() if x.strip()]), 2)  # 見出しと1行

    def test_not_found(self) -> None:
        from finfet_abs.core.errors import NotFound
        with self.assertRaises(NotFound):
            self.results.list("missing", {}, "name", 0, 10)


if __name__ == "__main__":
    unittest.main()
