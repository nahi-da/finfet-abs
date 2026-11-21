"""waitress でローカルWebサーバーを動かします（2.3）。"""

from __future__ import annotations

import logging
import threading

log = logging.getLogger(__name__)

HOST = "127.0.0.1"


class ServerHandle:
    """動いているサーバー。"""

    def __init__(self, server, thread: threading.Thread) -> None:
        self._server = server
        self.thread = thread

    @property
    def port(self) -> int:
        return int(self._server.effective_port)

    @property
    def host(self) -> str:
        return str(self._server.socket.getsockname()[0])

    def close(self) -> None:
        try:
            self._server.close()
        except Exception:  # noqa: BLE001
            pass


def start(app, port: int = 0) -> ServerHandle:
    """サーバーを別のスレッドで開始します。`127.0.0.1` だけで待ち受けます（2.5 の S1）。"""
    from waitress.server import create_server

    server = create_server(
        app,
        host=HOST,
        port=port,
        threads=12,
        max_request_body_size=10 * 1024 * 1024,
        channel_timeout=120,
        ident="",
        url_scheme="http",
        expose_tracebacks=False,
        clear_untrusted_proxy_headers=True,
    )
    thread = threading.Thread(target=server.run, name="http-server", daemon=True)
    thread.start()
    return ServerHandle(server, thread)
