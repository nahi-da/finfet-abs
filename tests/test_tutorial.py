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

    def test_offered(self) -> None:
        app, c = self.client(False)
        self.assertFalse(c.get("/api/v1/session").json()["tutorial_offered"])
        self.assertEqual(c.post("/api/v1/tutorial/offered").status, 200)
        self.assertTrue(c.get("/api/v1/session").json()["tutorial_offered"])
        # 通常のモードの保存先は変えません
        self.assertIn("Downloads", app.settings.get("fetch.directory"))

    def test_launch_next(self) -> None:
        app, c = self.client(False)
        for nxt, where in (("tutorial", "/search?tutorial=1"), ("https://example.com/", "/"), ("", "/")):
            code = app.security.new_launch_code()
            r = c.request("GET", f"/auth/launch?code={code}&next={nxt}", cookie=False)
            self.assertEqual(r.status, 302)
            self.assertEqual(r.header("Location"), where, nxt)

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


if __name__ == "__main__":
    unittest.main()
