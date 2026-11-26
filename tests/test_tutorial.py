"""チュートリアル（4.12。v1.7.0）: 起動の API、起動コードの next、初めての確認、デモモードの保存先。"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from finfet_abs.api import tutorial_api
from finfet_abs.core.errors import AppError
from finfet_abs.services import setup_services

from .helpers import Client, make_app, quiet_logs


class TutorialApiTest(unittest.TestCase):
    def setUp(self) -> None:
        quiet_logs()
        self.apps = []

    def tearDown(self) -> None:
        for a in self.apps:
            a.stop()

    def client(self, demo: bool):
        app = make_app(Path(tempfile.mkdtemp()), demo=demo)
        setup_services(app)
        self.apps.append(app)
        c = Client(app.build_wsgi())
        c.login(app.security)
        return app, c

    def test_demo_starts_in_same_tab(self) -> None:
        app, c = self.client(True)
        r = c.post("/api/v1/tutorial/launch")
        self.assertEqual(r.json(), {"url": "/search?tutorial=1", "same_tab": True})
        # デモモードの保存先は、デモのデータ保存先の downloads
        self.assertEqual(app.settings.get("fetch.directory"), str(app.location.data_dir / "downloads"))

    def test_launch_url_for_other_browser(self) -> None:
        """別のブラウザで開くための URL（v1.7.2）: 1 回だけ使え、画面の Cookie と CSRF がなければ作れないこと。"""
        app, c = self.client(False)
        r = c.post("/api/v1/app/launch-url").json()
        self.assertEqual(r["expires_in"], 60)
        path = r["url"].split(str(app.security.port), 1)[1]
        self.assertTrue(path.startswith("/auth/launch?code="), r["url"])
        self.assertEqual(c.get(path, cookie=False).status, 302)
        self.assertEqual(c.get(path, cookie=False).status, 400)  # 2 回目は使えません
        self.assertEqual(c.post("/api/v1/app/launch-url", cookie=False).status, 401)
        r = c.post("/api/v1/app/launch-url", headers={"Host": f"localhost:{app.security.port}"}).json()
        self.assertTrue(r["url"].startswith("http://localhost:"), r["url"])

    def test_offered(self) -> None:
        app, c = self.client(False)
        self.assertFalse(c.get("/api/v1/session").json()["tutorial_offered"])
        self.assertEqual(c.post("/api/v1/tutorial/offered").status, 200)
        self.assertTrue(c.get("/api/v1/session").json()["tutorial_offered"])
        # 通常のモードの保存先は変えません
        self.assertIn("Downloads", app.settings.get("fetch.directory"))

    def test_launch_next(self) -> None:
        app, c = self.client(False)
        for nxt in ("https://example.com/", ""):
            code = app.security.new_launch_code()
            r = c.request("GET", f"/auth/launch?code={code}&next={nxt}", cookie=False)
            self.assertEqual(r.status, 302)
            self.assertEqual(r.header("Location"), "/", nxt)
        # チュートリアルは転送せず、Cookie を渡すページから同じサイトの中で移ります（v1.7.4。Firefox 系で S4 に拒否されないように）
        code = app.security.new_launch_code()
        r = c.request("GET", f"/auth/launch?code={code}&next=tutorial", cookie=False)
        self.assertEqual(r.status, 200)
        self.assertIn(app.security.cookie_name + "=", r.header("Set-Cookie") or "")
        self.assertIn('http-equiv="refresh" content="0;url=/search?tutorial=1"', r.text)
        # 同じ起動コードは 2 回目は使えません
        self.assertEqual(c.request("GET", f"/auth/launch?code={code}&next=tutorial", cookie=False).status, 400)

    def test_demo_launch_url(self) -> None:
        spawned = []
        running = {"v": False}
        url = tutorial_api.demo_launch_url(Path("x"), spawn=lambda: spawned.append(1),
                                           read=lambda local, demo: running["v"],
                                           wait=lambda local, demo, timeout: "http://127.0.0.1:1/auth/launch?code=c")
        self.assertEqual(url, "http://127.0.0.1:1/auth/launch?code=c&next=tutorial")
        self.assertEqual(spawned, [1])
        running["v"] = True  # 起動中で応答があれば、起動しません
        tutorial_api.demo_launch_url(Path("x"), spawn=lambda: spawned.append(2), read=lambda local, demo: True,
                                     wait=lambda local, demo, timeout: "http://127.0.0.1:1/auth/launch?code=d")
        self.assertEqual(spawned, [1])
        # 起動の情報が残っていても、応答がなければ起動します（強制終了した後など）
        waits = []

        def wait(local, demo, timeout):
            waits.append(timeout)
            return None if len(waits) == 1 else "http://127.0.0.1:2/auth/launch?code=e"
        url = tutorial_api.demo_launch_url(Path("x"), spawn=lambda: spawned.append(3), read=lambda local, demo: True, wait=wait)
        self.assertEqual((spawned, waits, url), ([1, 3], [2.0, 60.0], "http://127.0.0.1:2/auth/launch?code=e&next=tutorial"))
        with self.assertRaises(AppError):
            tutorial_api.demo_launch_url(Path("x"), spawn=lambda: None, read=lambda local, demo: False,
                                         wait=lambda local, demo, timeout: None)


class SameHostTest(unittest.TestCase):
    def test_same_host(self) -> None:
        """localhost で開いた画面からは、デモモードも localhost で開きます（別のサイトとして拒否されないように。v1.7.1）。"""
        u = "http://127.0.0.1:5000/auth/launch?code=c&next=tutorial"
        self.assertEqual(tutorial_api.same_host(u, "localhost:6000"), "http://localhost:5000/auth/launch?code=c&next=tutorial")
        self.assertEqual(tutorial_api.same_host(u, "LOCALHOST:6000"), "http://localhost:5000/auth/launch?code=c&next=tutorial")
        self.assertEqual(tutorial_api.same_host(u, "127.0.0.1:6000"), u)
        self.assertEqual(tutorial_api.same_host(u, None), u)


if __name__ == "__main__":
    unittest.main()
