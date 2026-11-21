"""各部品の組み立てと、起動・終了（2.6）。"""

from __future__ import annotations

import logging
import platform
import sys
import tempfile
import threading
import webbrowser
from pathlib import Path
from typing import Callable

from . import console
from .core import instance, logs, paths, winapi
from .core.clock import SYSTEM_CLOCK, Clock
from .core.db import Database
from .core.events import EventBus
from .core.paths import DataLocation
from .core.settings import Settings
from .server.security import Security
from .server.static import StaticFiles
from .server.wsgi import WsgiApp
from .version import APP_NAME, __version__

log = logging.getLogger(__name__)


class App:
    """本ツールの部品をまとめたもの。テストでは直接作って使います。"""

    def __init__(self, location: DataLocation, *, demo: bool = False, clock: Clock = SYSTEM_CLOCK) -> None:
        self.location = location
        self.demo = demo
        self.clock = clock
        self.settings = Settings(location.settings_path)
        self.db = Database(location.db_path, network=location.is_network)
        self.events = EventBus()
        self.security = Security()
        self.job_log = logs.JobLog(location.job_logs_dir)
        self.shutdown_event = threading.Event()
        self.stopped_event = threading.Event()
        self._stop_hooks: list[Callable[[], None]] = []
        self._stop_lock = threading.Lock()
        self._stopped = False
        self.services: dict[str, object] = {}

    # ------------------------------------------------------------ 起動

    def start(self) -> None:
        """設定・データベース・イベントを初期化します。"""
        self.settings.load()
        self.db.start()
        self.events.start()
        self.settings.on_change(self._on_settings_changed)
        self.job_log.set_level(self.settings.get("log.level"))

    def _on_settings_changed(self, keys: list[str]) -> None:
        if "log.level" in keys:
            logs.set_level(self.settings.get("log.level"))
            self.job_log.set_level(self.settings.get("log.level"))
        if "log.max_files" in keys:
            logs.set_max_files(self.settings.get("log.max_files"))
        if "load.process_priority" in keys:
            winapi.set_process_priority(self.settings.get("load.process_priority"))
        self.events.publish("settings.changed", {"keys": keys})

    def build_wsgi(self) -> WsgiApp:
        """WSGI アプリを組み立てます。"""
        from .api import build_router
        router = build_router(self)
        web = StaticFiles(paths.WEB_DIR)
        docs = StaticFiles(paths.DOCS_DIR, "/docs/")
        signin = self.services.get("signin_return")
        return WsgiApp(router, self.security, web, docs, signin_return=signin)

    # ------------------------------------------------------------ 終了

    def on_stop(self, hook: Callable[[], None]) -> None:
        """終了のときに呼ぶ関数を登録します（後に登録したものから呼びます）。"""
        self._stop_hooks.append(hook)

    def request_shutdown(self) -> None:
        """終了を求めます（実際の終了は主スレッドで行います）。"""
        self.shutdown_event.set()

    def stop(self) -> None:
        """状態を保存して終了します。"""
        with self._stop_lock:
            if self._stopped:
                return
            self._stopped = True
        log.info("終了します")
        for hook in reversed(self._stop_hooks):
            try:
                hook()
            except Exception:  # noqa: BLE001
                log.exception("終了の処理でエラーが発生しました")
        self.events.stop()
        try:
            self.db.flush(10)
        except Exception:  # noqa: BLE001
            log.exception("データベースの書き込みを終えられませんでした")
        self.db.close()
        self.stopped_event.set()


def _log_header(app: App) -> None:
    loc = app.location
    log.info("%s %s を起動します（Python %s、%s）", APP_NAME, __version__, platform.python_version(),
             platform.platform(terse=True))
    log.info("データ保存先: %s（%s）", loc.data_dir, loc.mode_label)
    log.info("プロキシの設定: %s", app.settings.get("network.proxy.mode"))
    if loc.is_network:
        log.info("データ保存先がネットワーク上にあるため、SQLite の journal_mode は %s です", app.db.journal_mode)


def run(args) -> int:
    """通常の起動（2.6.1）。"""
    local = paths.local_app_dir()
    local.mkdir(parents=True, exist_ok=True)
    demo = bool(args.demo)

    lock = instance.InstanceLock(local, demo)
    if not lock.acquire():
        url = instance.wait_for_existing(local, demo)
        if url is None:
            console.error("別の FinFeT が起動していますが、応答がありません。しばらく待ってから、もう一度実行してください。")
            return 1
        if args.no_browser:
            console.started_again(url)
        else:
            webbrowser.open(url)
            console.info("FinFeT はすでに起動しています。画面を開きました。")
        return 0

    demo_dir = Path(tempfile.mkdtemp(prefix="finfet-abs-demo-")) if demo else None
    location = paths.resolve(args.data_dir, demo_dir=demo_dir)
    logs.setup(location.logs_dir)
    app = App(location, demo=demo)
    app.start()
    logs.set_level(app.settings.get("log.level"))
    logs.set_max_files(app.settings.get("log.max_files"))
    _log_header(app)
    winapi.set_process_priority(app.settings.get("load.process_priority"))

    from .services import setup_services
    setup_services(app)

    from .server import runner
    port = args.port if args.port is not None else app.settings.get("server.port")
    try:
        server = runner.start(app.build_wsgi(), port)
    except OSError as exc:
        console.error(f"ローカルWebサーバーを開始できませんでした（ポート {port}）。{exc.strerror or ''}")
        log.error("ローカルWebサーバーを開始できませんでした: %s", exc)
        app.stop()
        return 1
    app.security.port = server.port
    instance.write(local, port=server.port, secret=app.security.instance_secret, version=__version__, demo=demo)
    log.info("ローカルWebサーバーを開始しました（127.0.0.1:%s）", server.port)

    url = app.security.launch_url()
    if not args.no_browser:
        webbrowser.open(url)
    console.started(__version__, url, demo)

    def on_console_event(event: int) -> None:
        app.request_shutdown()
        if event in (winapi.CTRL_CLOSE_EVENT, winapi.CTRL_LOGOFF_EVENT, winapi.CTRL_SHUTDOWN_EVENT):
            # 通知の関数から戻ると、プロセスは終了させられるため、保存が終わるまで待ちます
            app.stopped_event.wait(4.5)

    winapi.set_console_ctrl_handler(on_console_event)
    try:
        while not app.shutdown_event.wait(0.5):
            pass
    except KeyboardInterrupt:
        pass
    console.info("終了しています…")
    app.stop()
    server.close()
    instance.remove(local, demo)
    lock.release()
    console.info("終了しました。")
    return 0


def self_check() -> int:
    """依存パッケージの読み込み、SQLite、DPAPI を確認します（2.6.5 の --self-check）。"""
    import importlib
    import sqlite3

    ok = True
    for name in ("azure.storage.blob", "azure.core", "msal", "msal_extensions", "waitress", "cryptography",
                 "requests", "certifi"):
        try:
            importlib.import_module(name)
            console.info(f"OK  パッケージ {name}")
        except Exception as exc:  # noqa: BLE001
            ok = False
            console.error(f"NG  パッケージ {name}: {type(exc).__name__}: {exc}")
    try:
        c = sqlite3.connect(":memory:")
        c.execute("CREATE TABLE t(x)")
        c.execute("INSERT INTO t VALUES(json('{\"a\":1}'))")
        c.close()
        console.info(f"OK  SQLite {sqlite3.sqlite_version}")
    except Exception as exc:  # noqa: BLE001
        ok = False
        console.error(f"NG  SQLite: {exc}")
    try:
        from .core import dpapi
        from .core.secret import Secret
        text = dpapi.seal(Secret("self-check-value"), "selfcheck")
        if dpapi.unseal(text, "selfcheck").reveal() != "self-check-value":
            raise RuntimeError("値が一致しません")
        console.info("OK  DPAPI")
    except Exception as exc:  # noqa: BLE001
        ok = False
        console.error(f"NG  DPAPI: {exc}")
    console.info(f"Python {platform.python_version()}（{sys.executable}）")
    console.info("確認の結果: " + ("成功" if ok else "失敗"))
    return 0 if ok else 1
