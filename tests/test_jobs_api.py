"""ジョブの API（5.7.2）の通し: デモモードで検索 → 選択 → ジョブの作成 → 完了 → Blob の一覧・CSV・ログ。"""

from __future__ import annotations

import time

from finfet_abs.services import setup_services

from .helpers import Client, TempDirMixin, make_app, quiet_logs


class JobsApiTest(TempDirMixin):
    def setUp(self) -> None:
        super().setUp()
        self.app = make_app(self.tmp, demo=True)
        self.app.settings.update({"demo.blob_count": 1000, "fetch.directory": str(self.tmp / "out")})
        setup_services(self.app)
        self.c = Client(self.app.build_wsgi())
        self.c.login(self.app.security)
        auth = self.app.services["auth"]
        flow = auth.start_sign_in()
        auth.handle_return({"state": auth._flows[flow["flow_id"]]["state"]})

    def tearDown(self) -> None:
        self.app.stop()
        super().tearDown()

    def wait(self, fn, timeout=15):
        end = time.time() + timeout
        while time.time() < end:
            v = fn()
            if v:
                return v
            time.sleep(0.1)
        raise AssertionError("時間内に終わりません")

    def test_flow(self) -> None:
        quiet_logs()
        cond = {"targets": [{"storage_account": "stexample01", "endpoint_suffix": "core.windows.net",
                             "container": "backup", "auth_kind": "entra_user"}],
                "path_pattern": "営業部/.*", "variables": {}, "input_timezone": "utc"}
        sid = self.c.post("/api/v1/searches", {"condition": cond}).json()["search_id"]
        self.wait(lambda: self.c.get(f"/api/v1/searches/{sid}").json()["status"] != "running")
        r = self.c.post(f"/api/v1/searches/{sid}/results/select", {"all_matching": True, "filter": {"q": "#1"}})
        self.assertEqual(r.status, 200)
        source = {"kind": "search", "search_id": sid}
        pv = self.c.post("/api/v1/jobs/preview", {"source": source}).json()
        self.assertGreater(pv["count"], 0)
        pc = self.c.post("/api/v1/jobs/path-check", {"source": source, "options": {"path_structure": "template",
                                                                                 "path_template": "{{@nope}}"}}).json()
        self.assertFalse(pc["ok"])
        r = self.c.post("/api/v1/jobs", {"source": source, "options": {"method": "download_only"}, "start": True})
        self.assertEqual(r.status, 201, r.text)
        jid = r.json()["id"]
        self.assertEqual(self.c.get("/api/v1/jobs?view=queue").json()["counts"]["queue"] +
                         self.c.get("/api/v1/jobs?view=ended").json()["counts"]["ended"], 1)
        self.wait(lambda: self.c.get(f"/api/v1/jobs/{jid}").json()["status"] in ("completed", "completed_with_errors"))
        items = self.c.get(f"/api/v1/jobs/{jid}/items?sort=-size").json()
        self.assertEqual(items["total"], pv["count"])
        self.assertTrue(any(i["status"] == "completed" for i in items["items"]))
        d = self.c.get(f"/api/v1/jobs/{jid}/items/{items['items'][0]['id']}").json()
        self.assertNotIn("secret_id", d)
        csv = self.c.get(f"/api/v1/jobs/{jid}/items.csv").body.decode("utf-8-sig")
        self.assertIn("ストレージアカウント", csv.splitlines()[0])
        log = self.c.get(f"/api/v1/jobs/{jid}/log").json()["lines"]
        self.assertTrue(any("ジョブを開始しました" in x["message"] for x in log))
        bw = self.c.put("/api/v1/jobs/bandwidth", {"mbps": 5}).json()
        self.assertEqual(bw["mbps"], 5)
        self.assertEqual(self.c.get("/api/v1/jobs/bandwidth").json()["mbps"], 5)
        # 終了したジョブの再実行（新しいジョブ）と削除
        r = self.c.post(f"/api/v1/jobs/{jid}/actions/rerun-all", {"start": False, "dup_resolution": "number"})
        self.assertEqual(r.status, 201, r.text)
        self.assertNotEqual(r.json()["id"], jid)
        self.assertEqual(self.c.get(f"/api/v1/jobs/{r.json()['id']}").json()["source_job"]["id"], jid)
        r = self.c.post("/api/v1/jobs/actions/delete", {"ids": [jid]})
        self.assertEqual(r.json()["deleted"], 1)
