"""時計（7.4 の I18・I19）。

時刻に依存する処理は、このモジュールの `Clock` を通して現在時刻を得ます。
内部の時刻はすべてタイムゾーン付きの UTC の `datetime` です。
"""

from __future__ import annotations

import threading
import time
from datetime import datetime, timedelta, timezone

UTC = timezone.utc


class Clock:
    """現在時刻を返す時計。テストでは `FakeClock` に差し替えます。"""

    def now(self) -> datetime:
        """現在の UTC の時刻（タイムゾーン付き）。"""
        return datetime.now(UTC)

    def monotonic(self) -> float:
        """経過時間の計測に使う単調増加の秒数。"""
        return time.monotonic()

    def sleep(self, seconds: float) -> None:
        """指定した秒数だけ待ちます。"""
        if seconds > 0:
            time.sleep(seconds)


class FakeClock(Clock):
    """テスト用の時計。`advance` で時刻を進めます。"""

    def __init__(self, start: datetime | None = None) -> None:
        self._now = start or datetime(2026, 11, 20, 0, 0, 0, tzinfo=UTC)
        if self._now.tzinfo is None:
            raise ValueError("タイムゾーンのない datetime は使えません")
        self._mono = 1000.0
        self._lock = threading.Lock()

    def now(self) -> datetime:
        with self._lock:
            return self._now

    def monotonic(self) -> float:
        with self._lock:
            return self._mono

    def sleep(self, seconds: float) -> None:
        self.advance(seconds)

    def advance(self, seconds: float) -> None:
        """時刻を進めます。"""
        with self._lock:
            self._now += timedelta(seconds=seconds)
            self._mono += seconds

    def set(self, dt: datetime) -> None:
        """時刻を設定します。"""
        if dt.tzinfo is None:
            raise ValueError("タイムゾーンのない datetime は使えません")
        with self._lock:
            self._mono += max(0.0, (dt - self._now).total_seconds())
            self._now = dt


#: 既定の時計
SYSTEM_CLOCK = Clock()


def to_iso(dt: datetime | None) -> str | None:
    """UTC の ISO 8601 の文字列（`2026-11-20T01:15:02Z`）にします。"""
    if dt is None:
        return None
    if dt.tzinfo is None:
        raise ValueError("タイムゾーンのない datetime は使えません")
    dt = dt.astimezone(UTC)
    if dt.microsecond:
        return dt.strftime("%Y-%m-%dT%H:%M:%S.") + f"{dt.microsecond // 1000:03d}Z"
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def from_iso(text: str | None) -> datetime | None:
    """ISO 8601 の文字列を UTC の `datetime` にします。"""
    if not text:
        return None
    s = text.strip()
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"
    dt = datetime.fromisoformat(s)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt.astimezone(UTC)
