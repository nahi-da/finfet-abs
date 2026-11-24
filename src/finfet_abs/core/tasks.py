"""時間のかかる処理（サインイン、接続テスト、候補の取得など）の実行（5.7.1）。

API は 202 と処理の ID を返し、結果はイベント `task.done` で知らせます。
"""

from __future__ import annotations

import logging
import secrets
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Callable

from .errors import AppError

log = logging.getLogger(__name__)


class TaskRunner:
    def __init__(self, events, max_workers: int = 4) -> None:
        self.events = events
        self._pool = ThreadPoolExecutor(max_workers=max_workers, thread_name_prefix="task")

    def submit(self, fn: Callable[[], Any], kind: str = "") -> str:
        """処理を始め、処理の ID を返します。"""
        tid = secrets.token_urlsafe(10)

        def run() -> None:
            try:
                result = fn()
                self.events.publish("task.done", {"id": tid, "kind": kind, "ok": True, "result": result})
            except AppError as err:
                self.events.publish("task.done", {"id": tid, "kind": kind, "ok": False, "error": err.to_json()["error"]})
            except Exception as exc:  # noqa: BLE001
                from ..azure.backend import BackendError
                if isinstance(exc, BackendError):
                    err = {"code": exc.code, "message": exc.message, "detail": exc.detail}
                else:
                    log.exception("処理でエラーが発生しました（%s）", kind)
                    err = {"code": "INTERNAL", "message": "内部エラーが発生しました。詳細はログを確認してください。"}
                self.events.publish("task.done", {"id": tid, "kind": kind, "ok": False, "error": err})

        self._pool.submit(run)
        return tid

    def shutdown(self) -> None:
        self._pool.shutdown(wait=False, cancel_futures=True)
