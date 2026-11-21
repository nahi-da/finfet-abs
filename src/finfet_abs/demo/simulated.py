"""デモモードの架空の実装の動き（8.3.3）: リハイドレートを縮めた時間で完了させ、ダウンロードを設定の速度で返し、
エラーを一定の割合で起こします。"""

from __future__ import annotations

import random
import threading
import time
from datetime import datetime, timezone

from ..azure.backend import BackendError
from .memory import MemoryBackend

#: リハイドレートにかかる時間（時間。実際の Azure の目安。デモモードでは time_scale で縮めます）
DURATION_HOURS = {"Standard": (1.0, 15.0), "High": (0.3, 1.0)}

_ERRORS = [
    ("ServerBusy", "サーバーが混雑しています。", 503),
    ("OperationTimedOut", "操作がタイムアウトしました。", 500),
    ("AuthorizationPermissionMismatch", "この操作に必要な権限がありません。", 403),
    ("CONNECTION_RESET", "接続が途中で切れました。", None),
]


class DemoBackend(MemoryBackend):
    def __init__(self, settings, *, rnd: random.Random | None = None) -> None:
        super().__init__()
        self.settings = settings
        self.rnd = rnd or random.Random()
        self._due: dict[tuple, float] = {}
        self._dlock = threading.Lock()

    # ------------------------------------------------------------ 時間を縮めたリハイドレート

    def _scale(self) -> float:
        return float(self.settings.get("demo.time_scale"))

    def _advance(self) -> None:
        now = time.monotonic()
        with self._dlock:
            done = [k for k, t in self._due.items() if t <= now]
            for k in done:
                del self._due[k]
        for account, suffix, container, name in done:
            try:
                self.complete_rehydration(account, container, name, suffix=suffix, when=datetime.now(timezone.utc))
            except KeyError:
                pass

    def set_tier(self, target, blob_name, tier, priority):
        self._maybe_error("tier")
        before = self.get_properties_raw(target, blob_name)
        super().set_tier(target, blob_name, tier, priority)
        after = self.get_properties_raw(target, blob_name)
        if after.archive_status and (not before.archive_status or priority == "High"):
            lo, hi = DURATION_HOURS.get(priority, DURATION_HOURS["Standard"])
            secs = self.rnd.uniform(lo, hi) * 3600 / self._scale()
            key = (target.account.account, target.account.endpoint_suffix, target.container, blob_name)
            with self._dlock:
                cur = self._due.get(key)
                due = time.monotonic() + secs
                self._due[key] = min(cur, due) if cur else due

    def get_properties_raw(self, target, blob_name):
        return super().get_properties(target, blob_name)

    def list_blobs_page(self, *args, **kw):
        self._advance()
        return super().list_blobs_page(*args, **kw)

    def get_properties(self, target, blob_name):
        self._advance()
        self._maybe_error("props")
        return super().get_properties(target, blob_name)

    # ------------------------------------------------------------ ダウンロードの速度とエラー

    def download_range(self, target, blob_name, offset, length, etag):
        self._maybe_error("get")
        data = b"".join(super().download_range(target, blob_name, offset, length, etag))
        rate = float(self.settings.get("demo.download_mbps")) * 1000 * 1000
        if rate > 0:
            time.sleep(len(data) / rate)
        yield data

    def _maybe_error(self, op: str) -> None:
        pct = int(self.settings.get("demo.error_percent"))
        if pct and self.rnd.random() * 100 < pct:
            code, msg, status = self.rnd.choice(_ERRORS if op == "get" else _ERRORS[:3])
            raise BackendError(code, msg, status=status)
