"""デモモード（8.3.3）。Azure の代わりに架空の `StorageBackend` と架空のサインインを使います。"""

from __future__ import annotations

import logging

log = logging.getLogger(__name__)


def setup_demo(app) -> None:
    """デモモードの部品を登録します。"""
    from .generator import populate
    from .memory import MemoryBackend

    backend = MemoryBackend()
    counts = populate(backend, app.settings.get("demo.blob_count"), now=app.clock.now())
    app.services["backend"] = backend
    log.info("デモモードで起動しました（Azure には接続しません。架空の Blob %s 件）", f"{sum(counts.values()):,}")
