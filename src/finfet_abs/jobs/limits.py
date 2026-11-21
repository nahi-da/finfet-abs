"""帯域制限と負荷の制御（3.4）: トークンバケット、要求の速度、全体の接続数の上限、自動の減速（AIMD）。"""

from __future__ import annotations

import threading
import time
from contextlib import contextmanager
from typing import Callable, Iterator

KIB = 1024
MIB = 1024 * 1024

#: 自動の減速で、並列数を1つ戻すまでの成功の続く時間（秒。3.4.3）
AIMD_RECOVER_SECONDS = 30.0


class Cancelled(Exception):
    """待っている間に中止・一時停止された。"""


class TokenBucket:
    """トークンバケット（3.4.1）。rate はバイト/秒（または要求/秒）。0 は無制限。

    容量は max(rate × 1 秒, min_capacity)。容量より大きい要求は、容量まで貯まってから取り、
    足りない分は「借り」として次の要求を待たせます（平均の速度が rate になります）。
    """

    def __init__(self, rate: float = 0.0, *, min_capacity: float = 0.0, clock: Callable[[], float] = time.monotonic) -> None:
        self._cond = threading.Condition()
        self._clock = clock
        self._min_capacity = float(min_capacity)
        self._rate = 0.0
        self._capacity = 0.0
        self._tokens = 0.0
        self._last = clock()
        self.set_rate(rate)

    @property
    def rate(self) -> float:
        return self._rate

    def set_rate(self, rate: float, min_capacity: float | None = None) -> None:
        """速度を変えます（実行中でもすぐに反映します）。"""
        with self._cond:
            self._refill()
            if min_capacity is not None:
                self._min_capacity = float(min_capacity)
            self._rate = max(0.0, float(rate))
            self._capacity = max(self._rate, self._min_capacity)
            self._tokens = min(self._tokens, self._capacity)
            self._cond.notify_all()

    def _refill(self) -> None:
        now = self._clock()
        if self._rate > 0:
            self._tokens = min(self._capacity, self._tokens + (now - self._last) * self._rate)
        self._last = now

    def acquire(self, n: float, cancel: threading.Event | None = None) -> None:
        """n 個のトークンを取ります。足りなければ待ちます。cancel が設定されたら Cancelled。"""
        with self._cond:
            while True:
                if cancel is not None and cancel.is_set():
                    raise Cancelled()
                if self._rate <= 0:
                    return
                self._refill()
                need = min(n, self._capacity)
                if self._tokens >= need:
                    self._tokens -= n
                    return
                wait = (need - self._tokens) / self._rate
                self._cond.wait(min(wait, 0.2))


def chunk_size_for(rate: float, configured: int) -> int:
    """範囲の大きさ（3.4.1）: 制限中は R × 0.5 秒（256 KiB 以上・8 MiB 以下）。"""
    if rate <= 0:
        return configured
    return int(min(8 * MIB, max(256 * KIB, rate * 0.5)))


class ConnectionLimiter:
    """全体の同時接続数の上限（3.4.2）。上限は実行中に変えられます。"""

    def __init__(self, limit: int) -> None:
        self._cond = threading.Condition()
        self._limit = max(1, int(limit))
        self.in_use = 0
        self.peak = 0

    @property
    def limit(self) -> int:
        return self._limit

    def set_limit(self, limit: int) -> None:
        with self._cond:
            self._limit = max(1, int(limit))
            self._cond.notify_all()

    def acquire(self, cancel: threading.Event | None = None) -> None:
        with self._cond:
            while self.in_use >= self._limit:
                if cancel is not None and cancel.is_set():
                    raise Cancelled()
                self._cond.wait(0.2)
            self.in_use += 1
            self.peak = max(self.peak, self.in_use)

    def release(self) -> None:
        with self._cond:
            self.in_use -= 1
            self._cond.notify()

    @contextmanager
    def slot(self, cancel: threading.Event | None = None) -> Iterator[None]:
        self.acquire(cancel)
        try:
            yield
        finally:
            self.release()


class AccountThrottle:
    """ストレージアカウントごとの自動の減速（3.4.3。AIMD）。

    混雑（503 ServerBusy・500 OperationTimedOut・429）で、そのアカウントの並列数を半分にし、
    成功が 30 秒続くごとに1つずつ戻します。Retry-After があれば、その時間は新しい要求を待たせます。
    """

    def __init__(self, max_parallel: int, *, clock: Callable[[], float] = time.monotonic) -> None:
        self._cond = threading.Condition()
        self._clock = clock
        self.max_parallel = max(1, int(max_parallel))
        self._state: dict[str, dict] = {}

    def _st(self, account: str) -> dict:
        st = self._state.get(account)
        if st is None:
            st = {"limit": self.max_parallel, "in_use": 0, "since": self._clock(), "until": 0.0}
            self._state[account] = st
        return st

    def set_max(self, max_parallel: int) -> None:
        with self._cond:
            self.max_parallel = max(1, int(max_parallel))
            for st in self._state.values():
                st["limit"] = min(st["limit"], self.max_parallel)
            self._cond.notify_all()

    def limit(self, account: str) -> int:
        with self._cond:
            return self._st(account)["limit"]

    def acquire(self, account: str, cancel: threading.Event | None = None) -> None:
        with self._cond:
            while True:
                if cancel is not None and cancel.is_set():
                    raise Cancelled()
                st = self._st(account)
                wait_until = st["until"] - self._clock()
                if wait_until <= 0 and st["in_use"] < st["limit"]:
                    st["in_use"] += 1
                    return
                self._cond.wait(min(0.2, wait_until) if wait_until > 0 else 0.2)

    def release(self, account: str) -> None:
        with self._cond:
            self._st(account)["in_use"] -= 1
            self._cond.notify_all()

    def on_success(self, account: str) -> None:
        with self._cond:
            st = self._st(account)
            now = self._clock()
            if st["limit"] < self.max_parallel and now - st["since"] >= AIMD_RECOVER_SECONDS:
                st["limit"] += 1
                st["since"] = now
                self._cond.notify_all()

    def on_busy(self, account: str, retry_after: float | None = None) -> None:
        with self._cond:
            st = self._st(account)
            st["limit"] = max(1, st["limit"] // 2)
            st["since"] = self._clock()
            if retry_after:
                st["until"] = max(st["until"], self._clock() + float(retry_after))


#: 混雑を表すエラー（3.4.3）
BUSY_CODES = {"ServerBusy", "OperationTimedOut", "TooManyRequests"}


def is_busy(exc: BaseException) -> bool:
    code = getattr(exc, "code", None)
    status = getattr(exc, "status", None)
    return code in BUSY_CODES or status in (429, 503)


class Limits:
    """全体で共有する負荷の制御（帯域・要求の速度・接続数・自動の減速）。"""

    def __init__(self, settings) -> None:
        self.settings = settings
        self.bandwidth = TokenBucket(0.0)
        self.requests = TokenBucket(0.0)
        self.connections = ConnectionLimiter(8)
        self.throttle = AccountThrottle(8)
        self.apply_settings()

    def apply_settings(self) -> None:
        s = self.settings
        self.set_bandwidth_mbps(float(s.get("transfer.bandwidth_mbps") or 0))
        rate = int(s.get("transfer.request_rate"))
        self.requests.set_rate(rate, min_capacity=1)
        self.connections.set_limit(int(s.get("transfer.max_connections")))
        self.throttle.set_max(int(s.get("transfer.max_connections")))

    def set_bandwidth_mbps(self, mbps: float) -> None:
        """帯域の上限（MB/s。0 は無制限）。実行中のすべてのジョブで共有します。"""
        rate = max(0.0, float(mbps)) * 1000 * 1000
        chunk = int(self.settings.get("transfer.chunk_size_mib")) * MIB
        self.bandwidth.set_rate(rate, min_capacity=chunk_size_for(rate, chunk))

    def bandwidth_mbps(self) -> float:
        return self.bandwidth.rate / 1000 / 1000

    def chunk_size(self) -> int:
        return chunk_size_for(self.bandwidth.rate, int(self.settings.get("transfer.chunk_size_mib")) * MIB)

    @contextmanager
    def request(self, account: str | None = None, cancel: threading.Event | None = None) -> Iterator[None]:
        """Azure への1回の要求の枠（要求の速度 → 自動の減速 → 全体の接続数）。"""
        self.requests.acquire(1, cancel)
        if account:
            self.throttle.acquire(account, cancel)
        try:
            with self.connections.slot(cancel):
                try:
                    yield
                except Exception as exc:
                    if account and is_busy(exc):
                        self.throttle.on_busy(account, getattr(exc, "retry_after", None))
                    raise
                else:
                    if account:
                        self.throttle.on_success(account)
        finally:
            if account:
                self.throttle.release(account)
