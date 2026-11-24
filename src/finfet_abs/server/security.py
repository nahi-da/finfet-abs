"""ローカルWebサーバーの安全対策（2.5）。"""

from __future__ import annotations

import hmac
import secrets
import threading
import time

SESSION_COOKIE = "finfet_session"  # 名前の前半。実際の名前は、ポート番号を付けた Security.cookie_name
CSRF_HEADER = "x-finfet-csrf"
INSTANCE_HEADER = "x-finfet-instance"
LAUNCH_CODE_TTL = 60.0

#: S6 の Content-Security-Policy
CSP = ("default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; connect-src 'self'; "
       "frame-ancestors 'none'; base-uri 'none'; form-action 'self'")

#: S6・S7 の応答ヘッダー
SECURITY_HEADERS = [
    ("Content-Security-Policy", CSP),
    ("X-Content-Type-Options", "nosniff"),
    ("Referrer-Policy", "no-referrer"),
    ("Cache-Control", "no-store"),
    ("Cross-Origin-Opener-Policy", "same-origin"),
    ("Cross-Origin-Resource-Policy", "same-origin"),
    ("X-Frame-Options", "DENY"),
]

UNSAFE_METHODS = {"POST", "PUT", "PATCH", "DELETE"}


def _eq(a: str | None, b: str) -> bool:
    return a is not None and hmac.compare_digest(a.encode("utf-8"), b.encode("utf-8"))


class Security:
    """起動ごとの秘密の値と、要求の検査。"""

    def __init__(self, monotonic=time.monotonic) -> None:
        self.session_secret = secrets.token_urlsafe(32)
        self.csrf_token = secrets.token_urlsafe(32)
        self.instance_secret = secrets.token_urlsafe(32)
        self.port = 0
        self._codes: dict[str, float] = {}
        self._lock = threading.Lock()
        self._monotonic = monotonic

    # ------------------------------------------------------------ 起動コード（S3）

    def new_launch_code(self) -> str:
        """1回限り・60 秒有効の起動コードを作ります。"""
        code = secrets.token_urlsafe(24)
        with self._lock:
            now = self._monotonic()
            self._codes = {c: t for c, t in self._codes.items() if t > now}
            self._codes[code] = now + LAUNCH_CODE_TTL
        return code

    def consume_launch_code(self, code: str | None) -> bool:
        """起動コードを確かめて無効にします。"""
        if not code:
            return False
        with self._lock:
            expiry = self._codes.pop(code, None)
        return expiry is not None and expiry > self._monotonic()

    def launch_url(self) -> str:
        return f"http://127.0.0.1:{self.port}/auth/launch?code={self.new_launch_code()}"

    # ------------------------------------------------------------ 検査

    def allowed_hosts(self) -> set[str]:
        return {f"127.0.0.1:{self.port}", f"localhost:{self.port}"}

    def allowed_origins(self) -> set[str]:
        return {f"http://127.0.0.1:{self.port}", f"http://localhost:{self.port}"}

    def host_ok(self, host: str | None) -> bool:
        """S5: Host ヘッダーが自分のものか。"""
        return host is not None and host.lower() in self.allowed_hosts()

    def origin_ok(self, origin: str | None, fetch_site: str | None) -> bool:
        """S4: Origin と Sec-Fetch-Site の検査。"""
        if origin is not None and origin not in self.allowed_origins():
            return False
        if fetch_site is not None and fetch_site.lower() == "cross-site":
            return False
        return True

    def session_ok(self, cookie_value: str | None) -> bool:
        """S2: Cookie の秘密の値。"""
        return _eq(cookie_value, self.session_secret)

    def csrf_ok(self, header_value: str | None) -> bool:
        """S4: CSRF のヘッダー。"""
        return _eq(header_value, self.csrf_token)

    def instance_ok(self, header_value: str | None) -> bool:
        """2回目の起動のための秘密の値。"""
        return _eq(header_value, self.instance_secret)

    @property
    def cookie_name(self) -> str:
        """セッションの Cookie の名前（v1.7.1 からポート番号を付けます）。

        Cookie はポート番号で区別されないため、同じ 127.0.0.1 で通常のモードとデモモード（チュートリアル）を
        同時に使うと、同じ名前では後から開いた方の Cookie で上書きされ、もう一方の画面が使えなくなります。
        """
        return f"{SESSION_COOKIE}_{self.port}"

    def session_cookie_header(self) -> tuple[str, str]:
        return ("Set-Cookie", f"{self.cookie_name}={self.session_secret}; HttpOnly; SameSite=Strict; Path=/")
