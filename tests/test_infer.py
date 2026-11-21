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
        self.assertEqual(c[0]["variables"], ["dt", "dt2"])

    def test_choices_and_classes(self) -> None:
        names = [f"{app}/server{n}.txt" for app in ("web", "api") for n in range(1, 8)]
        c = pats(names)
        self.assertEqual(c[0]["pattern"], r"(?:api|web)/server\d{1}\.txt")
        many = [f"{w}/x.txt" for w in ("alpha", "beta", "gamma", "delta", "epsilon", "zeta")]
        self.assertEqual(pats(many)[0]["pattern"], r"[A-Za-z]+/x\.txt")

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
        sample, info = infer.collect(self.be, self.t, mode="sample", per_level=3)
        self.assertLess(len(sample), 40)
        self.assertLess(info["requests"], 30)
        c = infer.infer(sample)
        self.assertEqual(c[0]["pattern"], r"y={{dt=YYYY}}/m={{dt=MM}}/d={{dt=DD}}/h={{dt=hh}}/PT1H\.json")

    def test_cancel(self) -> None:
        import threading
        ev = threading.Event()
        ev.set()
        with self.assertRaises(infer.Cancelled):
            infer.collect(self.be, self.t, mode="sample", cancel=ev)


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
            r = c.post("/api/v1/pattern/infer", body)
            self.assertEqual(r.status, 202)
            tid = r.json()["task_id"]
            end = time.time() + 15
            while time.time() < end and not any(d["id"] == tid for d in done):
                time.sleep(0.05)
            res = next(d for d in done if d["id"] == tid)
            self.assertTrue(res["ok"], res)
            self.assertGreater(res["result"]["names"], 0)
            self.assertTrue(res["result"]["candidates"])
            self.assertEqual(c.post("/api/v1/pattern/infer", {**body, "mode": "bad"}).status, 400)
        finally:
            app.stop()


if __name__ == "__main__":
    unittest.main()
