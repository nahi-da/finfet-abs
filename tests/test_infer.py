"""Blob 名からパスパターンを作る機能（search/infer.py）。"""

from __future__ import annotations

import re
import unittest

from finfet_abs.azure.backend import AccountRef, AuthRef, BlobInfo, ContainerRef
from finfet_abs.demo.memory import MemoryBackend
from finfet_abs.search import infer
from finfet_abs.search.pattern import compile_path  # noqa: F401（パスパターンとして正しいかの確認に使う）


def pats(names, sizes=None):
    return infer.infer([(n, (sizes or {}).get(n, 1)) for n in names])


class InferTest(unittest.TestCase):
    def test_resource_log_path(self) -> None:
        names = [f"resourceId=/subscriptions/0000/y=2026/m=11/d={d:02d}/h={h:02d}/m=00/PT1H.json"
                 for d in range(1, 4) for h in range(24)]
        c = pats(names)
        self.assertEqual(len(c), 1)
        self.assertEqual(c[0]["pattern"],
                         r"resourceId=/subscriptions/0000/y={{dt=YYYY}}/m={{dt=MM}}/d={{dt=DD}}/h={{dt=hh}}/m={{dt=mm}}/PT1H\.json")
        self.assertEqual(c[0]["count"], 72)
        self.assertEqual((c[0]["first"], c[0]["last"]), ("2026-11-01T00:00:00", "2026-11-03T23:00:00"))

    def test_plain_date_folders_and_compact(self) -> None:
        names = [f"logs/2026/11/{d:02d}/app_202611{d:02d}.log" for d in range(1, 10)]
        c = pats(names)
        self.assertEqual(c[0]["pattern"], r"logs/{{dt=YYYY}}/{{dt=MM}}/{{dt=DD}}/app_{{dt2=YYYYMMDD}}\.log")
        self.assertEqual(c[0]["variables"], {"dt": {"type": "datetime"}, "dt2": {"type": "datetime"}})

    def test_choices_and_classes(self) -> None:
        names = [f"{app}/server{n}.txt" for app in ("web", "api") for n in range(1, 8)]
        c = pats(names)
        # 英字が変わる階層は選択肢の変数、数字だけが変わる階層は \d の形
        self.assertEqual(c[0]["pattern"], r"{{v1}}/server\d{1}\.txt")
        self.assertEqual(c[0]["variables"]["v1"], {"type": "choice", "value": {"options": ["api", "web"], "selected": ["api", "web"]}})
        many = [f"{w}/x.txt" for w in ("alpha", "beta", "gamma", "delta", "epsilon", "zeta")]
        self.assertEqual(pats(many)[0]["pattern"], r"{{v1}}/x\.txt")
        # 値の種類が多い階層は選択肢にしない
        ids = [f"data/{i:08x}-guid.bin" for i in range(150)]
        self.assertEqual(pats(ids)[0]["pattern"], r"data/[^/]+-guid\.bin")

    def test_choice_named_by_key_and_natural_order(self) -> None:
        names = [f"vm=web{n:02d}/2026/11/{d:02d}/app.log" for n in (1, 2, 10) for d in (1, 2)] + ["vm=db01/2026/11/03/app.log"]
        c = pats(names)
        self.assertEqual(len(c), 1)
        self.assertEqual(c[0]["pattern"], r"vm={{vm}}/{{dt=YYYY}}/{{dt=MM}}/{{dt=DD}}/app\.log")
        self.assertEqual(c[0]["variables"]["vm"]["value"]["options"], ["db01", "web01", "web02", "web10"])
        self.assertEqual((c[0]["first"], c[0]["last"]), ("2026-11-01T00:00:00", "2026-11-03T00:00:00"))

    def test_candidates_match_their_names(self) -> None:
        from datetime import datetime, timezone

        from finfet_abs.search import datetime_vars as dtv
        from finfet_abs.search.pattern import CompileContext
        ctx = CompileContext(now_utc=datetime(2026, 11, 22, tzinfo=timezone.utc), local_zone=dtv.FixedZone(9))
        sets = [
            [f"vm=web{n:02d}/2026/11/{d:02d}/app.log" for n in (1, 2, 10) for d in (1, 2)],
            [f"logs/{h}_PT1H.json" for h in ("host-a", "host-b", "host-c")],
            [f"{app}/server{n}.txt" for app in ("web", "api") for n in range(1, 8)],
            [f"data/{i:08x}-guid.bin" for i in range(150)],
            [f"a/{i}.csv" for i in range(10)] + ["b/readme.md", "営業部/報告書 #1 (確定).pdf", "営業部/報告書 #2 (案).pdf"],
            [f"resourceId=/SUBSCRIPTIONS/0000/RESOURCEGROUPS/RG/PROVIDERS/MICROSOFT.COMPUTE/VIRTUALMACHINES/{vm}/y=2026/m=11/d=21/h={h:02d}/m=00/PT1H.json"
             for vm in ("VM-APP01", "VM-APP02", "VM-DB01") for h in range(3)],
        ]
        for names in sets:
            cands = pats(names)
            covered = 0
            for c in cands:
                defs = dict(c["variables"])
                for k, v in defs.items():
                    if v["type"] == "datetime":
                        defs[k] = {"type": "datetime", "value": {"mode": "any"}}
                cp = compile_path(c["pattern"], defs, ctx=ctx)
                hits = [n for n in names if cp.match(n) is not None]
                self.assertGreaterEqual(len(hits), c["count"], (c["pattern"], names[:2]))
                for ex in c["examples"]:
                    self.assertIsNotNone(cp.match(ex), (c["pattern"], ex))
                covered += c["count"]
            self.assertEqual(covered, len(names))

    def test_special_chars_escaped_and_valid(self) -> None:
        names = ["営業部/2025/10/報告書 #387 (確定版).pdf", "営業部/2025/11/報告書 #12 (確定版).pdf"]
        c = pats(names)
        p = c[0]["pattern"]
        self.assertIn(r"\(確定版\)\.pdf", p)
        rx = re.sub(r"\{\{[^}]+\}\}", r"\\d+", p)
        for n in names:
            self.assertTrue(re.fullmatch(rx, n), (p, n))

    def test_groups_sorted_by_count(self) -> None:
        names = [f"a/{i}.csv" for i in range(10)] + ["b/readme.md"]
        c = pats(names)
        self.assertEqual([x["count"] for x in c], [10, 1])


class CollectTest(unittest.TestCase):
    def setUp(self) -> None:
        self.be = MemoryBackend()
        blobs = [BlobInfo(f"y=2026/m=11/d={d:02d}/h={h:02d}/PT1H.json", size=10) for d in range(1, 29) for h in range(24)]
        blobs += [BlobInfo("readme.txt", size=5)]
        self.be.add_blobs("stexample01", "c", blobs)
        self.t = ContainerRef(AccountRef("stexample01", "core.windows.net", AuthRef("account_key")), "c")

    def test_modes(self) -> None:
        allx, info = infer.collect(self.be, self.t, mode="all")
        self.assertEqual(len(allx), 28 * 24 + 1)
        first, info = infer.collect(self.be, self.t, mode="first", limit=50)
        self.assertEqual(len(first), 50)
        self.assertTrue(info["truncated"])
        # 1 ページ（5,000 件）に収まるコンテナーは、1 回の要求ですべて（大きさも分かる）
        sample, info = infer.collect(self.be, self.t, mode="sample", per_level=3)
        self.assertEqual(len(sample), 28 * 24 + 1)
        self.assertEqual(info["requests"], 1)
        self.assertTrue(info["complete"] and info["sizes_known"] and not info["truncated"])
        c = infer.infer(sample)
        self.assertEqual(c[0]["pattern"], r"y={{dt=YYYY}}/m={{dt=MM}}/d={{dt=DD}}/h={{dt=hh}}/PT1H\.json")

    def test_sample_large(self) -> None:
        # 1 ページに収まらない: 1 ページに含まれた階層はたどらず、残りの階層から見本を集める
        be = MemoryBackend()
        blobs = [BlobInfo(f"y=2026/m=11/d={d:02d}/h={h:02d}/m={m:02d}/PT05M.json", size=1)
                 for d in range(1, 22) for h in range(24) for m in range(0, 60, 5)]
        blobs += [BlobInfo(f"zz/{i:05d}.log", size=1) for i in range(6000)]
        be.add_blobs("stexample01", "big", blobs)
        t = ContainerRef(AccountRef("stexample01", "core.windows.net", AuthRef("account_key")), "big")
        sample, info = infer.collect(be, t, mode="sample", per_level=3)
        self.assertLess(info["requests"], 30)
        self.assertFalse(info["complete"])
        self.assertFalse(info["truncated"])
        pats = [c["pattern"] for c in infer.infer(sample)]
        self.assertIn(r"y={{dt=YYYY}}/m={{dt=MM}}/d={{dt=DD}}/h={{dt=hh}}/m={{dt=mm}}/PT05M\.json", pats)
        self.assertTrue(any(p.startswith("zz/") for p in pats), pats)
        self.assertEqual(len({n for n, _ in sample}), len(sample))  # 重複なし
        # 要求の上限
        _, info = infer.collect(be, t, mode="sample", per_level=3, max_requests=3)
        self.assertLessEqual(info["requests"], 3)
        self.assertTrue(info["truncated"] and not info["complete"])

    def test_cancel(self) -> None:
        import threading
        ev = threading.Event()
        ev.set()
        with self.assertRaises(infer.Cancelled):
            infer.collect(self.be, self.t, mode="sample", cancel=ev)


class NameCacheTest(unittest.TestCase):
    def test_reuse_and_expiry(self) -> None:
        now = [1000.0]
        c = infer.NameCache(ttl=3600, clock=lambda: now[0])
        tk = ("stexample01", "core.windows.net", "c", "account_key", None, None)
        items = [(f"a/{i:03d}.log", 1) for i in range(100)] + [(f"b/{i:03d}.log", 1) for i in range(50)]
        info = {"requests": 1, "truncated": False, "complete": True, "sizes_known": True}
        self.assertIsNone(c.get(tk, "all", "", 10000))
        c.put(tk, "all", "", 10000, items, info)
        self.assertEqual(len(c.get(tk, "all", "", 10000)[1]), 150)
        # 「すべて」から: より狭い prefix、先頭から N 件、見本
        self.assertEqual(len(c.get(tk, "all", "b/", 10000)[1]), 50)
        t, sub, inf = c.get(tk, "first", "a/", 10)
        self.assertEqual((len(sub), inf["truncated"], inf["complete"]), (10, True, False))
        self.assertTrue(c.get(tk, "sample", "", 10000)[2]["complete"])
        # 別の接続先には使わない
        self.assertIsNone(c.get(tk[:2] + ("other",) + tk[3:], "all", "", 10000))
        # 期限が過ぎたら使わない
        now[0] += 3601
        self.assertIsNone(c.get(tk, "all", "", 10000))
        # 取得し直し（clear_target）
        c.put(tk, "first", "", 10, items[:10], info)
        c.clear_target(tk)
        self.assertIsNone(c.get(tk, "first", "", 10))


class InferApiTest(unittest.TestCase):
    def test_api(self) -> None:
        import tempfile
        import time
        from pathlib import Path

        from finfet_abs.services import setup_services

        from .helpers import Client, make_app, quiet_logs
        quiet_logs()
        tmp = Path(tempfile.mkdtemp())
        app = make_app(tmp, demo=True)
        app.settings.update({"demo.blob_count": 1000})
        setup_services(app)
        try:
            done: list[dict] = []
            app.events.subscribe(lambda ev: done.append(ev["data"]) if ev["type"] == "task.done" else None)
            c = Client(app.build_wsgi())
            c.login(app.security)
            body = {"target": {"storage_account": "stexample01", "container": "backup", "auth_kind": "account_key"},
                    "mode": "first", "limit": 500}
            def run(b):
                r = c.post("/api/v1/pattern/infer", b)
                self.assertEqual(r.status, 202)
                tid = r.json()["task_id"]
                end = time.time() + 15
                while time.time() < end and not any(d["id"] == tid for d in done):
                    time.sleep(0.05)
                res = next(d for d in done if d["id"] == tid)
                self.assertTrue(res["ok"], res)
                return res["result"]
            res = run(body)
            self.assertGreater(res["names"], 0)
            self.assertTrue(res["candidates"])
            self.assertFalse(res["from_cache"])
            # 2 回目は保持した名前を使う（要求 0 回）。refresh で取得し直す
            again = run(body)
            self.assertTrue(again["from_cache"])
            self.assertEqual(again["requests"], 0)
            self.assertEqual(again["fetched_at"], res["fetched_at"])
            fresh = run({**body, "refresh": True})
            self.assertFalse(fresh["from_cache"])
            self.assertGreater(fresh["requests"], 0)
            self.assertEqual(c.post("/api/v1/pattern/infer", {**body, "mode": "bad"}).status, 400)
        finally:
            app.stop()


if __name__ == "__main__":
    unittest.main()
