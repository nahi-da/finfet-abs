"""チュートリアル（4.12。v1.7.0）の API。

通常のモードからは、デモモードの本ツールを別のプロセスで起動し、起動コード付きの URL を返します。
デモモードでは、同じタブで始めるための URL を返します。
"""

from __future__ import annotations

import logging
import os
import subprocess
import sys
import threading

from ..core import instance, paths
from ..core.errors import AppError
from ..server.http import Request, json_response

log = logging.getLogger(__name__)

#: 起動コード付きの URL に付ける、チュートリアルを始めるための値（server/wsgi.py の _launch と同じ）
NEXT_TUTORIAL = "tutorial"
#: チュートリアルの始まりのページ
TUTORIAL_PATH = "/search?tutorial=1"

_lock = threading.Lock()


def _spawn_demo() -> None:
    """デモモードの本ツールを、同じ Python の実行環境・作業フォルダーで起動します。"""
    flags = 0
    if os.name == "nt":
        flags = subprocess.CREATE_NEW_CONSOLE | subprocess.CREATE_NEW_PROCESS_GROUP
    subprocess.Popen([sys.executable, "-X", "utf8", "-m", "finfet_abs", "--demo", "--no-browser"],
                     cwd=os.getcwd(), env=os.environ.copy(), creationflags=flags,
                     stdin=subprocess.DEVNULL, close_fds=True)


def demo_launch_url(local=None, *, spawn=_spawn_demo, wait=instance.wait_for_existing, read=instance.read) -> str:
    """起動中のデモモードの本ツール（なければ起動します）の、チュートリアルを始める URL。

    起動の情報のファイルが残っていても、応答がなければ（強制終了した後など）起動します。
    """
    local = local or paths.local_app_dir()
    with _lock:
        url = wait(local, True, timeout=2.0) if read(local, True) else None
        if not url:
            log.info("チュートリアルのため、デモモードの本ツールを起動します")
            spawn()
            url = wait(local, True, timeout=60.0)
    if not url:
        raise AppError("DEMO_START_FAILED", "デモモードの FinFeT を起動できませんでした。しばらく待ってから、再度実行してください。",
                       status=503)
    return f"{url}&next={NEXT_TUTORIAL}"


def register(router, app) -> None:
    @router.post("/api/v1/tutorial/launch")
    def tutorial_launch(req: Request):
        """チュートリアルを始めます。戻り値の same_tab が true なら同じタブで、false なら新しいタブで url を開きます。"""
        if app.demo:
            return json_response({"url": TUTORIAL_PATH, "same_tab": True})
        return json_response({"url": demo_launch_url(), "same_tab": False})

    @router.post("/api/v1/tutorial/offered")
    def tutorial_offered(req: Request):
        """初めて起動したときの確認を表示したことを記録します。"""
        if not app.settings.get("tutorial.offered"):
            app.settings.update({"tutorial.offered": True})
        return json_response({"ok": True})
