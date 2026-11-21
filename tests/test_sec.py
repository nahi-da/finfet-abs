"""T-SEC: ローカルサーバーの安全対策（2.5）。"""

import unittest
from unittest import mock

from finfet_abs.server import runner
from finfet_abs.server.security import CSP, Security

from .helpers import Client, TempDirMixin, make_app, quiet_logs


class SecurityTest(TempDirMixin):
    def setUp(self) -> None:
        super().setUp()
        quiet_logs()
        self.app = make_app(self.tmp)
        self.wsgi = self.app.build_wsgi()
        self.c = Client(self.wsgi)
        self.c.login(self.app.security)

    def tearDown(self) -> None:
        self.app.stop()
        super().tearDown()

    # S5
    def test_host_header(self) -> None:
        self.assertEqual(self.c.get("/api/v1/session", host="evil.example").status, 403)
        self.assertEqual(self.c.get("/api/v1/session", host="127.0.0.1:1").status, 403)
        self.assertEqual(self.c.get("/api/v1/session", host="localhost:50000").status, 200)
        self.assertEqual(self.c.get("/api/v1/session").status, 200)
        self.assertEqual(self.c.get("/api/v1/health", host="evil.example:50000").status, 403)

    # S4
    def test_origin_and_fetch_site(self) -> None:
        self.assertEqual(self.c.get("/api/v1/session", headers={"Origin": "http://evil.example"}).status, 403)
        self.assertEqual(self.c.get("/api/v1/session", headers={"Origin": "http://127.0.0.1:50000"}).status, 200)
        self.assertEqual(self.c.get("/api/v1/session", headers={"Origin": "http://localhost:50000"}).status, 200)
        self.assertEqual(self.c.get("/api/v1/session", headers={"Sec-Fetch-Site": "cross-site"}).status, 403)
        self.assertEqual(self.c.get("/api/v1/session", headers={"Sec-Fetch-Site": "same-origin"}).status, 200)
        r = self.c.post("/api/v1/settings/reset", headers={"Origin": "null"})
        self.assertEqual(r.status, 403)

    def test_csrf_header(self) -> None:
        self.assertEqual(self.c.post("/api/v1/settings/reset", csrf=False).status, 403)
        self.assertEqual(self.c.post("/api/v1/settings/reset", csrf=False, headers={"X-FinFeT-CSRF": "wrong"}).status, 403)
        self.assertEqual(self.c.post("/api/v1/settings/reset").status, 200)

    def test_backend_error_response(self) -> None:
        """Azure の操作の失敗は、文・エラーコード・要求 ID を 502 で返します（3.7。画面の 401・403 と区別）。"""
        from finfet_abs.azure.backend import BackendError
        err = BackendError("AuthorizationPermissionMismatch", "権限がありません。", status=403, request_id="req-123")
        with mock.patch.object(self.wsgi, "handle", side_effect=err):
            r = self.c.get("/api/v1/session")
        self.assertEqual(r.status, 502)
        e = r.json()["error"]
        self.assertEqual((e["code"], e["message"], e["request_id"]), ("AuthorizationPermissionMismatch", "権限がありません。", "req-123"))
        self.assertIn("403", e["detail"])

    # S2
    def test_no_cookie(self) -> None:
        r = self.c.get("/api/v1/session", cookie=False)
        self.assertEqual(r.status, 401)
        self.assertEqual(r.json()["error"]["code"], "UNAUTHORIZED")
        r = self.c.get("/jobs", cookie=False)
        self.assertEqual(r.status, 401)
        self.assertIn("finfet-abs.bat", r.text)
        r = self.c.get("/api/v1/session", cookie=False, headers={"Cookie": "finfet_session=wrong"})
        self.assertEqual(r.status, 401)
        # 生存確認は Cookie がなくても応答し、何も情報を返しません
        r = self.c.get("/api/v1/health", cookie=False)
        self.assertEqual((r.status, r.json()), (200, {}))

    # S3
    def test_launch_code_single_use_and_expiry(self) -> None:
        code = self.app.security.new_launch_code()
        r = self.c.get(f"/auth/launch?code={code}", cookie=False)
        self.assertEqual(r.status, 302)
        self.assertEqual(r.header("Location"), "/")
        cookie = r.header("Set-Cookie")
        self.assertIn("HttpOnly", cookie)
        self.assertIn("SameSite=Strict", cookie)
        self.assertIn("Path=/", cookie)
        self.assertEqual(self.c.get(f"/auth/launch?code={code}", cookie=False).status, 400)
        self.assertEqual(self.c.get("/auth/launch?code=unknown", cookie=False).status, 400)
        t = [1000.0]
        sec = Security(monotonic=lambda: t[0])
        code = sec.new_launch_code()
        t[0] += 61
        self.assertFalse(sec.consume_launch_code(code))
        code = sec.new_launch_code()
        t[0] += 59
        self.assertTrue(sec.consume_launch_code(code))

    def test_signin_return_exception(self) -> None:
        # Cookie がなく、ほかのサイトからの遷移でも受け付けます（state が一致しなければ何もしない 400）
        r = self.c.get("/?code=abc&state=xyz", cookie=False, headers={"Sec-Fetch-Site": "cross-site"})
        self.assertEqual(r.status, 400)
        self.assertEqual(r.header("Content-Security-Policy"), CSP)
        # S5 は行います
        self.assertEqual(self.c.get("/?code=abc&state=xyz", cookie=False, host="evil.example").status, 403)
        # code・state の片方だけなら例外にしません
        self.assertEqual(self.c.get("/?code=abc", cookie=False).status, 401)

    # S6・S7
    def test_response_headers(self) -> None:
        for path in ("/api/v1/session", "/jobs", "/css/tokens.css", "/api/v1/health"):
            r = self.c.get(path)
            self.assertEqual(r.header("Content-Security-Policy"), CSP, path)
            self.assertEqual(r.header("X-Content-Type-Options"), "nosniff")
            self.assertEqual(r.header("Referrer-Policy"), "no-referrer")
            self.assertEqual(r.header("Cache-Control"), "no-store")
            self.assertEqual(r.header("Cross-Origin-Opener-Policy"), "same-origin")
            self.assertEqual(r.header("Cross-Origin-Resource-Policy"), "same-origin")
            self.assertIsNone(r.header("Access-Control-Allow-Origin"))
            self.assertIsNone(r.header("Server"))

    def test_static_files(self) -> None:
        self.assertEqual(self.c.get("/css/tokens.css").status, 200)
        self.assertEqual(self.c.get("/js/main.js").status, 200)
        self.assertEqual(self.c.get("/css/../index.html").status, 404)
        self.assertEqual(self.c.get("/../src/finfet_abs/app.py").status, 404)
        self.assertEqual(self.c.get("/css/nothing.css").status, 404)
        self.assertEqual(self.c.get("/..%2f..%2fsettings.json").status, 404)
        self.assertEqual(self.c.get("/C:/Windows/win.ini").status, 404)
        # ページの URL には index.html を返します
        for p in ("/search", "/search/urls", "/jobs", "/jobs/ended", "/jobs/abc", "/jobs/abc/log", "/history",
                  "/connections", "/settings", "/searches/abc", "/"):
            r = self.c.get(p)
            self.assertEqual(r.status, 200, p)
            self.assertIn(b'<script type="module" src="/js/main.js">', r.body)

    def test_open_folder_rejects_files(self) -> None:
        f = self.tmp / "a.txt"
        f.write_text("x", encoding="utf-8")
        with mock.patch("os.startfile") as sf:
            r = self.c.post("/api/v1/fs/open-folder", {"path": str(f)})
            self.assertEqual(r.status, 404)
            r = self.c.post("/api/v1/fs/open-folder", {"path": "a.txt"})
            self.assertEqual(r.status, 400)
            sf.assert_not_called()
            r = self.c.post("/api/v1/fs/open-folder", {"path": str(self.tmp)})
            self.assertEqual(r.status, 200)
            sf.assert_called_once()

    def test_write_to_data_dir_is_rejected(self) -> None:
        r = self.c.post("/api/v1/fs/check-writable", {"path": str(self.app.location.data_dir)})
        self.assertEqual(r.json()["error"]["code"], "PATH_FORBIDDEN")
        r = self.c.post("/api/v1/fs/mkdir", {"path": str(self.app.location.data_dir), "name": "x"})
        self.assertEqual(r.json()["error"]["code"], "PATH_FORBIDDEN")
        r = self.c.post("/api/v1/fs/check-writable", {"path": str(self.tmp)})
        self.assertEqual(r.status, 200)

    def test_internal_launch_code(self) -> None:
        self.assertEqual(self.c.post("/internal/launch-code", cookie=False, csrf=False).status, 401)
        r = self.c.post("/internal/launch-code", cookie=False, csrf=False,
                        headers={"X-FinFeT-Instance": self.app.security.instance_secret})
        self.assertEqual(r.status, 200)
        code = r.json()["code"]
        self.assertTrue(self.app.security.consume_launch_code(code))

    def test_listens_only_on_localhost(self) -> None:
        handle = runner.start(self.wsgi, 0)
        try:
            self.assertEqual(handle.host, "127.0.0.1")
            self.assertEqual(runner.HOST, "127.0.0.1")
            self.assertGreater(handle.port, 0)
        finally:
            handle.close()

    def test_unknown_api_and_method(self) -> None:
        self.assertEqual(self.c.get("/api/v1/nothing").status, 404)
        self.assertEqual(self.c.request("DELETE", "/api/v1/session").status, 405)
        r = self.c.request("PUT", "/api/v1/settings", raw_body=b"{}", headers={"Content-Type": "text/plain"})
        self.assertEqual(r.status, 415)


if __name__ == "__main__":
    unittest.main()
