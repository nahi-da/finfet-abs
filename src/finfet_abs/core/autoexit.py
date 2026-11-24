"""画面をすべて閉じた後の自動の終了（依頼者の意見。v1.3.0）。

画面（ブラウザのタブ）は、イベントのロングポーリング（5.8）に自分の ID（client）を付けて要求します。
タブを閉じると、画面は pagehide で「closed」を送ります。Edge のスリープ中のタブなどで画面が凍結されるときは
freeze で「frozen」を送り、閉じたものとは見なしません。

すべての画面がつながっていない状態が、設定の時間（exit.wait_minutes）続いたら、本ツールを終了します。
ただし、実行中のジョブや検索がある間は終了せず、終わってから終了します。
"""

from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass
from typing import Callable

log = logging.getLogger(__name__)

#: 最後のイベントの要求の後、つながっていると見なす秒数（ロングポーリングの待ちは 25 秒）
GRACE_SECONDS = 45.0
#: 確かめる間隔（秒）
TICK_SECONDS = 5.0
#: 確かめる間隔がこれより空いたら、PC のスリープから戻ったと見なし、画面がつなぎ直すのを待ちます（秒）
SLEEP_GAP_SECONDS = 60.0
#: 覚えておく画面の数の上限
MAX_CLIENTS = 200


@dataclass
class _Client:
    active: int = 0
    last: float = 0.0
    frozen: bool = False


class AutoExit:
    """画面のつながりを見て、すべて閉じたら終了を求めます。"""

    def __init__(self, settings, *, busy: Callable[[], bool], on_exit: Callable[[], None],
                 clock: Callable[[], float] = time.monotonic) -> None:
        self._settings = settings
        self._busy = busy
        self._on_exit = on_exit
        self._clock = clock
        self._lock = threading.Lock()
        self._clients: dict[str, _Client] = {}
        self._ever = False
        self._idle_since: float | None = None
        self._last_tick: float | None = None
        self._fired = False
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    # ------------------------------------------------------------ 画面から

    def poll_begin(self, client: str) -> None:
        """イベントの要求の始まり。"""
        with self._lock:
            c = self._get(client)
            c.active += 1
            c.last = self._clock()
            c.frozen = False
            self._ever = True

    def poll_end(self, client: str) -> None:
        """イベントの要求の終わり。"""
        with self._lock:
            c = self._clients.get(client)
            if c is not None:
                c.active = max(0, c.active - 1)
                c.last = self._clock()

    def set_state(self, client: str, state: str) -> None:
        """画面の状態の知らせ（closed / frozen / active）。"""
        with self._lock:
            if state == "closed":
                self._clients.pop(client, None)
                return
            c = self._get(client)
            c.last = self._clock()
            c.frozen = state == "frozen"
            self._ever = True

    def _get(self, client: str) -> _Client:
        c = self._clients.get(client)
        if c is None:
            if len(self._clients) >= MAX_CLIENTS:
                oldest = min(self._clients, key=lambda k: self._clients[k].last)
                self._clients.pop(oldest, None)
            c = self._clients[client] = _Client()
        return c

    # ------------------------------------------------------------ 判定

    def connected(self) -> bool:
        """つながっている画面があるか。"""
        now = self._clock()
        with self._lock:
            return any(c.active > 0 or c.frozen or now - c.last <= GRACE_SECONDS for c in self._clients.values())

    def check(self) -> bool:
        """一度確かめます。終了を求めたら True を返します。"""
        now = self._clock()
        with self._lock:
            if self._last_tick is not None and now - self._last_tick > SLEEP_GAP_SECONDS:
                # スリープから戻った直後は、画面がつなぎ直すのを待ちます
                for c in self._clients.values():
                    c.last = now
                self._idle_since = None
            self._last_tick = now
            if self._fired or not self._ever or not self._settings.get("exit.when_closed"):
                self._idle_since = None
                return False
            live = any(c.active > 0 or c.frozen or now - c.last <= GRACE_SECONDS for c in self._clients.values())
            if live:
                self._idle_since = None
                return False
            if self._idle_since is None:
                self._idle_since = now
            waited = now - self._idle_since
        if waited < float(self._settings.get("exit.wait_minutes")) * 60:
            return False
        try:
            if self._busy():
                return False
        except Exception:  # noqa: BLE001
            log.exception("実行中の処理の確認に失敗しました")
            return False
        with self._lock:
            self._fired = True
        log.info("画面が閉じられてから %d 分経過したため、終了します", int(waited // 60))
        self._on_exit()
        return True

    # ------------------------------------------------------------ スレッド

    def start(self) -> None:
        if self._thread is None:
            self._thread = threading.Thread(target=self._loop, name="auto-exit", daemon=True)
            self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    def _loop(self) -> None:
        while not self._stop.wait(TICK_SECONDS):
            try:
                if self.check():
                    return
            except Exception:  # noqa: BLE001
                log.exception("自動の終了の確認でエラーが発生しました")
