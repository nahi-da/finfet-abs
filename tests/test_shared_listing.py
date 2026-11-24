"""一覧の保持（v1.6.0）と、フルスキャンの打ち切り・一時停止。"""

from __future__ import annotations

import unittest

from finfet_abs.azure.backend import AccountRef, AuthRef, BlobInfo, ContainerRef
from finfet_abs.demo.memory import MemoryBackend
from finfet_abs.search import infer
from finfet_abs.search.listing import ListingView, SharedListing, list_target

from .test_search import SearchTestBase, cond, target


def b(name: str) -> BlobInfo:
    return BlobInfo(name, size=1)


class SharedListingTest(unittest.TestCase):
    def setUp(self) -> None:
        self.now = [1000.0]
        self.sl = SharedListing(ttl=60, max_items=100, clock=lambda: self.now[0])

    def test_lookup_prefix_and_partial(self) -> None:
        self.sl.put(("k",), "logs/", [b("logs/a/1"), b("logs/b/1")])
        self.assertEqual([x.name for x in self.sl.lookup(("k",), "logs/a/")[0]], ["logs/a/1"])
        self.assertIsNone(self.sl.lookup(("k",), "other/"))
        # 途中まで（upper）の範囲: upper より前で終わる prefix のみ
        self.sl.put(("k",), "x/", [b("x/a/1"), b("x/b/1")], upper="x/b/1")
        self.assertIsNotNone(self.sl.lookup(("k",), "x/a/"))
        self.assertIsNone(self.sl.lookup(("k",), "x/b/"))
        self.assertIsNone(self.sl.lookup(("k",), "x/c/"))
        self.assertEqual(self.sl.coverage(("k",), ["logs/a/", "x/a/"]), 1000.0)
        self.assertIsNone(self.sl.coverage(("k",), ["logs/a/", "x/c/"]))

    def test_ttl_limit_clear_disable(self) -> None:
        self.sl.put(("k",), "a/", [b("a/1")])
        self.now[0] += 61
        self.assertIsNone(self.sl.lookup(("k",), "a/"))
        self.sl.put(("k",), "a/", [b(f"a/{i}") for i in range(60)])
        self.now[0] += 1
        self.sl.put(("k",), "b/", [b(f"b/{i}") for i in range(60)])
        self.assertIsNone(self.sl.lookup(("k",), "a/"))  # 上限を超えたため古いものから破棄
        self.assertIsNotNone(self.sl.lookup(("k",), "b/"))
        self.sl.put(("k", "x"), "c/", [b("c/1")])
        self.sl.clear(("k",))
        self.assertIsNone(self.sl.lookup(("k",), "b/"))
        self.assertIsNone(self.sl.lookup(("k", "x"), "c/"))
        self.sl.put(("k",), "d/", [b("d/1")])
        self.sl.configure(enabled=False)
        self.assertIsNone(self.sl.lookup(("k",), "d/"))

    def test_list_target_reuses_per_prefix(self) -> None:
        be = MemoryBackend()
        be.add_blobs("stexample01", "c", [b(f"p{i}/x{j}") for i in range(3) for j in range(3)])
        t = ContainerRef(AccountRef("stexample01", "core.windows.net", AuthRef("account_key")), "c")
        view = ListingView(self.sl, ("ak",))
        got: list = []
        list_target(be, t, ["p0/", "p1/"], on_items=got.extend, cache=view, page_size=2)
        n = len([r for r in be.requests if r[0] == "list"])
        self.assertGreater(n, 0)
        got2: list = []
        view2 = ListingView(self.sl, ("ak",))
        list_target(be, t, ["p0/", "p1/"], on_items=got2.extend, cache=view2, page_size=2)
        self.assertEqual(len([r for r in be.requests if r[0] == "list"]), n)  # 要求は増えない
        self.assertEqual(sorted(x.name for x in got2), sorted(x.name for x in got))
        self.assertEqual(view2.reused_at, 1000.0)


class SearchReuseTest(SearchTestBase):
    def lists(self) -> int:
        return len([r for r in self.be.requests if r[0] == "list"])

    def test_second_search_uses_held_listing(self) -> None:
        sid = self.svc.start(cond(target("c1")))
        self.wait_done(sid)
        n = self.lists()
        sid2 = self.svc.start(cond(target("c1"), pattern=r"logs/a000.*\.log"))
        row = self.wait_done(sid2)
        self.assertEqual(self.lists(), n)
        self.assertEqual(row["matched_count"], 10)
        prog = [e for e in self.seen if e["type"] == "search.progress" and e["data"]["id"] == sid2]
        self.assertTrue(prog[-1]["data"]["targets"][0]["reused_at"])
        # 最新の情報に更新: 保持を消して取得し直す
        sid3 = self.svc.start(cond(target("c1")), refresh=True)
        self.wait_done(sid3)
        self.assertGreater(self.lists(), n)

    def test_disabled(self) -> None:
        self.settings.update({"listing.reuse": False})
        self.wait_done(self.svc.start(cond(target("c1"))))
        n = self.lists()
        self.wait_done(self.svc.start(cond(target("c1"))))
        self.assertGreater(self.lists(), n)


class FullScanControlTest(unittest.TestCase):
    def setUp(self) -> None:
        self.be = MemoryBackend()
        self.be.add_blobs("stexample01", "c", [b(f"d{i:03d}/f{j}") for i in range(50) for j in range(4)])
        self.t = ContainerRef(AccountRef("stexample01", "core.windows.net", AuthRef("account_key")), "c")

    def test_store_and_threshold(self) -> None:
        stored: list = []
        asked: list = []

        def on_threshold(n, elapsed):
            asked.append(n)
            return "stop"
        items, info = infer.collect(self.be, self.t, mode="all", page_size=50, threshold=100, on_threshold=on_threshold,
                                    store=lambda p, items, upper=None: stored.append((p, len(items), upper)))
        self.assertEqual(asked, [100])
        self.assertEqual(len(items), 100)
        self.assertTrue(info["stopped"])
        self.assertFalse(info["complete"])
        self.assertEqual(stored[-1][1:], (100, items[-1][0]))

    def test_continue_and_quick(self) -> None:
        items, info = infer.collect(self.be, self.t, mode="all", page_size=50, threshold=100,
                                    on_threshold=lambda n, e: "continue")
        self.assertEqual(len(items), 200)
        self.assertTrue(info["complete"])
        with self.assertRaises(infer.SwitchToQuick):
            infer.collect(self.be, self.t, mode="all", page_size=50, threshold=100, on_threshold=lambda n, e: "quick")

    def test_sample_stores_pages(self) -> None:
        sl = SharedListing()
        infer.collect(self.be, self.t, mode="sample", page_size=50, per_level=3,
                      store=lambda p, items, upper=None: sl.put(("k",), p, items, upper))
        self.assertIsNotNone(sl.lookup(("k",), "d000/"))


if __name__ == "__main__":
    unittest.main()
