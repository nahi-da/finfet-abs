"""テストの補助。"""

from __future__ import annotations

import io
import json
import shutil
import tempfile
import unittest
from pathlib import Path
from wsgiref.util import setup_testing_defaults

from finfet_abs.app import App
from finfet_abs.core import logs
from finfet_abs.core.paths import DataLocation

#: テストで使うダミーの秘密情報（0.6）
DUMMY_SIG = "REDACTED_TEST"


def dummy_key() -> str:
    """アカウントキーの形のダミー（Base64 の 88 文字）。ファイルに実在のキーの形を書かないよう、実行時に作ります。"""
    return ("RHVtbXlLZXk" + "A" * 75 + "==")[:88]


def dummy_jwt() -> str:
    import base64
    def b64(d: dict) -> str:
        return base64.urlsafe_b64encode(json.dumps(d).encode()).decode().rstrip("=")
    return b64({"alg": "none", "typ": "JWT"}) + "." + b64({"sub": "dummy-subject-for-test"}) + ".c2lnbmF0dXJlLXRlc3Q"


class TempDirMixin(unittest.TestCase):
    """一時フォルダーを作るテスト。"""

    def setUp(self) -> None:
        super().setUp()
        self.tmp = Path(tempfile.mkdtemp(prefix="finfet-test-"))

    def tearDown(self) -> None:
        shutil.rmtree(self.tmp, ignore_errors=True)
        super().tearDown()


def make_app(tmp: Path, *, demo: bool = False) -> App:
    """テスト用の App を作って開始します。"""
    loc = DataLocation(Path(tmp) / "data", "custom", Path(tmp) / "local")
    loc.data_dir.mkdir(parents=True, exist_ok=True)
    loc.job_logs_dir.mkdir(parents=True, exist_ok=True)
    loc.local_dir.mkdir(parents=True, exist_ok=True)
    app = App(loc, demo=demo)
    app.start()
    app.security.port = 50000
    return app


class Client:
    """WSGI アプリを直接呼ぶクライアント。"""

    def __init__(self, wsgi, port: int = 50000) -> None:
        self.wsgi = wsgi
        self.port = port
        self.cookie: str | None = None
        self.csrf: str | None = None

    def request(self, method: str, path: str, *, body=None, headers: dict | None = None, host: str | None = None,
                cookie: bool = True, csrf: bool = True, raw_body: bytes | None = None):
        env: dict = {}
        setup_testing_defaults(env)
        if "?" in path:
            path, qs = path.split("?", 1)
        else:
            qs = ""
        env["REQUEST_METHOD"] = method
        env["PATH_INFO"] = path.encode("utf-8").decode("latin-1")
        env["QUERY_STRING"] = qs
        env["HTTP_HOST"] = host if host is not None else f"127.0.0.1:{self.port}"
        data = b""
        if raw_body is not None:
            data = raw_body
        elif body is not None:
            data = json.dumps(body).encode("utf-8")
            env["CONTENT_TYPE"] = "application/json"
        env["CONTENT_LENGTH"] = str(len(data))
        env["wsgi.input"] = io.BytesIO(data)
        if cookie and self.cookie:
            env["HTTP_COOKIE"] = self.cookie
        if csrf and self.csrf and method in ("POST", "PUT", "PATCH", "DELETE"):
            env["HTTP_X_FINFET_CSRF"] = self.csrf
        for k, v in (headers or {}).items():
            key = k.upper().replace("-", "_")
            if key in ("CONTENT_TYPE", "CONTENT_LENGTH"):
                env[key] = v
            else:
                env["HTTP_" + key] = v
        captured = {}

        def start_response(status, hdrs):
            captured["status"] = int(status.split()[0])
            captured["headers"] = hdrs

        out = b"".join(self.wsgi(env, start_response))
        return Response(captured["status"], captured["headers"], out)

    def login(self, security) -> None:
        code = security.new_launch_code()
        r = self.request("GET", f"/auth/launch?code={code}", cookie=False)
        assert r.status == 302, r.status
        set_cookie = r.header("Set-Cookie")
        self.cookie = set_cookie.split(";")[0]
        self.csrf = security.csrf_token

    def get(self, path: str, **kw):
        return self.request("GET", path, **kw)

    def post(self, path: str, body=None, **kw):
        return self.request("POST", path, body=body if body is not None else {}, **kw)

    def put(self, path: str, body=None, **kw):
        return self.request("PUT", path, body=body if body is not None else {}, **kw)


class Response:
    def __init__(self, status: int, headers: list, body: bytes) -> None:
        self.status = status
        self.headers = headers
        self.body = body

    def header(self, name: str) -> str | None:
        for k, v in self.headers:
            if k.lower() == name.lower():
                return v
        return None

    def json(self):
        return json.loads(self.body.decode("utf-8"))

    @property
    def text(self) -> str:
        return self.body.decode("utf-8", "replace")


def quiet_logs() -> None:
    logs.setup(None)
