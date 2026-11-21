"""T-TB: トークンバケット / T-LIM: 接続数の上限・自動の減速。"""

from __future__ import annotations

import threading
import time
import unittest

from finfet_abs.azure.backend import BackendError
from finfet_abs.jobs import limits
from finfet_abs.jobs.limits import KIB, MIB, AccountThrottle, ConnectionLimiter, TokenBucket


def pump(bucket: TokenBucket, threads: int, chunk: int, seconds: float) -> float:
    """複数のスレッドで、seconds 秒の間トークンを取り続け、実際の速度（/秒）を返します。"""
    total = [0]
    lock = threading.Lock()
    stop = time.monotonic() + seconds

    def run():
        while time.monotonic() < stop:
            bucket.acquire(chunk)
            with lock:
                total[0] += chunk

    start = time.monotonic()
    ts = [threading.Thread(target=run) for _ in range(threads)]
    for t in ts:
        t.start()
    for t in ts:
        t.join()
    return total[0] / (time.monotonic() - start)


class TokenBucketTest(unittest.TestCase):
    def test_accuracy_shared(self) -> None:
        rate = 4 * MIB
        b = TokenBucket(rate, min_capacity=256 * KIB)
        got = pump(b, 4, 64 * KIB, 1.5)
        self.assertAlmostEqual(got / rate, 1.0, delta=0.1)  # ±10%

    def test_large_request_borrows(self) -> None:
        b = TokenBucket(2 * MIB, min_capacity=256 * KIB)
        got = pump(b, 2, 3 * MIB, 2.0)  # 容量より大きい要求でも、平均の速度は守ります
        self.assertLess(got, 2 * MIB * 1.6)

    def test_change_while_running(self) -> None:
        b = TokenBucket(1 * MIB, min_capacity=64 * KIB)
        done = threading.Event()
        t0 = time.monotonic()

        def big():
            b.acquire(64 * KIB)  # 最初は空
            b.acquire(10 * MIB)
            done.set()

        th = threading.Thread(target=big)
        th.start()
        time.sleep(0.3)
        b.set_rate(0)  # 無制限にすると、待っていた要求もすぐに通ります
        self.assertTrue(done.wait(1.0))
        self.assertLess(time.monotonic() - t0, 1.5)
        th.join()

    def test_unlimited(self) -> None:
        b = TokenBucket(0)
        t = time.monotonic()
        for _ in range(10000):
            b.acquire(8 * MIB)
        self.assertLess(time.monotonic() - t, 0.5)

    def test_cancel(self) -> None:
        b = TokenBucket(1000)
        ev = threading.Event()
        threading.Timer(0.2, ev.set).start()
        with self.assertRaises(limits.Cancelled):
            b.acquire(1000, ev)
            b.acquire(100000, ev)

    def test_chunk_size(self) -> None:
        self.assertEqual(limits.chunk_size_for(0, 8 * MIB), 8 * MIB)
        self.assertEqual(limits.chunk_size_for(100 * KIB, 8 * MIB), 256 * KIB)
        self.assertEqual(limits.chunk_size_for(4 * MIB, 8 * MIB), 2 * MIB)
        self.assertEqual(limits.chunk_size_for(100 * MIB, 8 * MIB), 8 * MIB)


class LimitTest(unittest.TestCase):
    def test_global_limit(self) -> None:
        c = ConnectionLimiter(3)
        active = [0]
        lock = threading.Lock()

        def work():
            for _ in range(20):
                with c.slot():
                    with lock:
                        active[0] += 1
                        self.assertLessEqual(active[0], 3)
                    time.sleep(0.002)
                    with lock:
                        active[0] -= 1

        ts = [threading.Thread(target=work) for _ in range(10)]
        for t in ts:
            t.start()
        for t in ts:
            t.join()
        self.assertEqual(c.peak, 3)
        self.assertEqual(c.in_use, 0)

    def test_aimd(self) -> None:
        now = [0.0]
        th = AccountThrottle(8, clock=lambda: now[0])
        th.on_busy("a")
        self.assertEqual(th.limit("a"), 4)
        th.on_busy("a")
        self.assertEqual(th.limit("a"), 2)
        self.assertEqual(th.limit("b"), 8)  # ほかのアカウントには影響しません
        now[0] += 10
        th.on_success("a")
        self.assertEqual(th.limit("a"), 2)  # 30 秒続くまでは戻しません
        now[0] += 25
        th.on_success("a")
        self.assertEqual(th.limit("a"), 3)
        for _ in range(10):
            now[0] += 31
            th.on_success("a")
        self.assertEqual(th.limit("a"), 8)  # 最大を超えません
        for _ in range(5):
            th.on_busy("a")
        self.assertEqual(th.limit("a"), 1)  # 1 より下げません

    def test_retry_after(self) -> None:
        th = AccountThrottle(4)
        th.on_busy("a", retry_after=0.3)
        t = time.monotonic()
        th.acquire("a")
        self.assertGreaterEqual(time.monotonic() - t, 0.25)
        th.release("a")

    def test_limits_request_busy(self) -> None:
        class S:
            values = {"transfer.bandwidth_mbps": 0, "transfer.request_rate": 1000, "transfer.max_connections": 8,
                      "transfer.chunk_size_mib": 8}

            def get(self, k):
                return self.values[k]

        lim = limits.Limits(S())
        with self.assertRaises(BackendError):
            with lim.request("acct"):
                raise BackendError("ServerBusy", "混雑", status=503)
        self.assertEqual(lim.throttle.limit("acct"), 4)
        with lim.request("acct"):
            pass
        self.assertEqual(lim.connections.in_use, 0)
        lim.set_bandwidth_mbps(10)
        self.assertEqual(lim.bandwidth.rate, 10_000_000)
        self.assertEqual(lim.chunk_size(), 5_000_000)


if __name__ == "__main__":
    unittest.main()
