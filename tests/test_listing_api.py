"""v1.6.0 の API: [Azure から候補を取得] の修正、[すべての Blob を確認]、一覧の保持の状態と件数の見込み、フルスキャンの制御。"""

from __future__ import annotations

import tempfile
import time
import unittest
from pathlib import Path

from finfet_abs.services import setup_services

from .helpers import Client, make_app, quiet_logs

TARGET = {"storage_account": "stexample01", "container": "applogs", "auth_kind": "account_key"}
PATTERN = r"vm-logs/{{v1}}/{{dt=YYYY}}/{{dt=MM}}/{{dt=DD}}/app_{{dt=YYYYMMDDhh}}\.log"
VMS = ["batch01", "db01", "web01", "web02", "web03"]


class ListingApiTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        quiet_logs()
        cls.app = make_app(Path(tempfile.mkdtemp()), demo=True)
        setup_services(cls.app)
        cls.done: list[dict] = []
        cls.app.events.subscribe(lambda ev: cls.done.append(ev["data"]) if ev["type"] == "task.done" else None)
        cls.c = Client(cls.app.build_wsgi())
        cls.c.login(cls.app.security)

    @classmethod
    def tearDownClass(cls) -> None:
        cls.app.stop()

    def run_task(self, path: str, body: dict) -> dict:
        r = self.c.post(path, body)
        self.assertEqual(r.status, 202, r.text)
        tid = r.json()["task_id"]
        end = time.time() + 30
        while time.time() < end and not any(d["id"] == tid for d in self.done):
            time.sleep(0.05)
        res = next(d for d in self.done if d["id"] == tid)
        self.assertTrue(res["ok"], res)
        return res["result"]

    def lists(self) -> int:
        return len([r for r in self.app.services["backend"].requests if r[0] == "list"])

    def test_1_suggest_values_from_names(self) -> None:
        # 前の変数（日時）が「任意」でも、年の階層ではなく VM の名前を返します
        v = {"v1": {"type": "choice", "value": {"options": ["web01"], "selected": ["web01"]}},
             "dt": {"type": "datetime", "value": {"mode": "any"}}}
        r = self.run_task("/api/v1/suggest/values", {"target": TARGET, "name": "v1", "path_pattern": PATTERN, "variables": v})
        self.assertEqual(r["values"], VMS)
        # ファイル名の途中の変数は、その部分だけを返します
        p2 = r"vm-logs/web01/{{dt=YYYY}}/{{dt=MM}}/{{dt=DD}}/{{kind}}_{{dt=YYYYMMDDhh}}\.log"
        r2 = self.run_task("/api/v1/suggest/values", {"target": TARGET, "name": "kind", "path_pattern": p2,
                                                      "variables": {"dt": v["dt"]}})
        self.assertEqual(r2["values"], ["app"])

    def test_2_full_scan_then_held(self) -> None:
        r = self.run_task("/api/v1/pattern/infer", {"target": TARGET, "mode": "all", "refresh": True})
        self.assertTrue(r["complete"])
        n = self.lists()
        cond = {"targets": [TARGET], "path_pattern": PATTERN, "input_timezone": "utc",
                "variables": {"v1": {"type": "choice", "value": {"options": VMS, "selected": ["web01", "web02"]}},
                              "dt": {"type": "datetime", "value": {"mode": "any"}}}}
        st = self.c.post("/api/v1/listing/status", {"condition": cond}).json()
        self.assertTrue(st["held"])
        self.assertEqual(st["count"], 24 * 30 * 2)
        self.assertTrue(st["fetched_at"])
        # すべての Blob を確認: 保持した一覧を使います（要求なし）
        sc = self.run_task("/api/v1/pattern/scan", {"target": TARGET, "path_pattern": PATTERN, "names": ["v1", "dt"],
                                                    "variables": cond["variables"], "input_timezone": "utc"})
        self.assertEqual(sc["choices"]["v1"], VMS)
        self.assertTrue(sc["found"]["dt"]["first"] < sc["found"]["dt"]["last"])
        self.assertEqual(self.lists(), n)
        # 保持していない接続先は、件数の見込みを出しません
        other = dict(cond, targets=[{**TARGET, "container": "backup"}])
        self.assertFalse(self.c.post("/api/v1/listing/status", {"condition": other}).json()["held"])

    def test_3_decide(self) -> None:
        self.assertEqual(self.c.post("/api/v1/pattern/infer/x/decide", {"action": "bad"}).status, 400)
        self.assertFalse(self.c.post("/api/v1/pattern/infer/x/decide", {"action": "stop"}).json()["ok"])


if __name__ == "__main__":
    unittest.main()
