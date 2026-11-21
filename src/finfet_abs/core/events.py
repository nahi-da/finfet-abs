"""イベント（5.8）。プロセス内の発行・購読と、番号付きのイベントの保持（直近 2,000 件）。"""

from __future__ import annotations

import logging
import threading
import time
from collections import deque
from typing import Any, Callable

log = logging.getLogger(__name__)

KEEP = 2000
#: 進捗のイベントをまとめる間隔（秒）
COALESCE_INTERVAL = 0.5


class EventBus:
    """イベントの発行・購読。"""

    def __init__(self, keep: int = KEEP) -> None:
        self._events: deque[dict] = deque(maxlen=keep)
        self._seq = 0
        self._cond = threading.Condition()
        self._pending: dict[tuple[str, str], dict] = {}
        self._pending_lock = threading.Lock()
        self._subscribers: list[Callable[[dict], None]] = []
        self._stop = threading.Event()
        self._flusher: threading.Thread | None = None

    def start(self) -> None:
        """まとめた進捗を 0.5 秒ごとに発行するスレッドを開始します。"""
        if self._flusher is None:
            self._flusher = threading.Thread(target=self._flush_loop, name="event-flusher", daemon=True)
            self._flusher.start()

    def stop(self) -> None:
        self._stop.set()

    def subscribe(self, callback: Callable[[dict], None]) -> None:
        """プロセス内でイベントを受け取る関数を登録します。"""
        self._subscribers.append(callback)

    @property
    def last(self) -> int:
        with self._cond:
            return self._seq

    def publish(self, type_: str, data: Any = None) -> int:
        """イベントを発行し、番号を返します。"""
        with self._cond:
            self._seq += 1
            ev = {"seq": self._seq, "type": type_, "data": data if data is not None else {}, "at": time.time()}
            self._events.append(ev)
            self._cond.notify_all()
        for cb in list(self._subscribers):
            try:
                cb(ev)
            except Exception:  # noqa: BLE001
                log.exception("イベントの処理でエラーが発生しました")
        return ev["seq"]

    def publish_coalesced(self, type_: str, key: str, data: Any) -> None:
        """進捗のイベントを、まとめて 0.5 秒ごとに発行します（同じキーの古いものは置き換えます）。"""
        with self._pending_lock:
            self._pending[(type_, key)] = data

    def flush(self) -> None:
        """まとめている進捗のイベントを、すぐに発行します。"""
        with self._pending_lock:
            pending = self._pending
            self._pending = {}
        for (type_, _key), data in pending.items():
            self.publish(type_, data)

    def _flush_loop(self) -> None:
        while not self._stop.wait(COALESCE_INTERVAL):
            try:
                self.flush()
            except Exception:  # noqa: BLE001
                log.exception("イベントの発行でエラーが発生しました")

    def wait(self, after: int, timeout: float = 25.0) -> dict:
        """`after` より新しいイベントを返します。なければ最大 timeout 秒待ちます。

        `after` が古すぎて保持していない場合（または、この実行より新しい番号の場合）は
        `{"reset": True, "last": …}` を返します。
        """
        deadline = time.monotonic() + timeout
        with self._cond:
            while True:
                if after > self._seq or (self._events and after < self._events[0]["seq"] - 1):
                    return {"reset": True, "last": self._seq, "events": []}
                if after < self._seq:
                    events = [e for e in self._events if e["seq"] > after]
                    return {"last": self._seq, "events": [
                        {"seq": e["seq"], "type": e["type"], "data": e["data"]} for e in events]}
                remaining = deadline - time.monotonic()
                if remaining <= 0 or self._stop.is_set():
                    return {"last": self._seq, "events": []}
                self._cond.wait(min(remaining, 1.0))
