"""デモモード（8.3.3）。Azure の代わりに架空の `StorageBackend` と架空のサインインを使います。"""

from __future__ import annotations

import logging

log = logging.getLogger(__name__)


def setup_demo(app) -> None:
    """デモモードの部品を登録します。"""
    from .generator import populate
    from .simulated import DemoBackend

    backend = DemoBackend(app.settings)
    counts = populate(backend, app.settings.get("demo.blob_count"), now=app.clock.now())
    app.services["backend"] = backend
    # 保存先の既定は、デモのデータ保存先の downloads（実際の「ダウンロード」フォルダーに架空のファイルを書き込まないため。4.12）
    from ..core.settings import defaults
    if app.settings.get("fetch.directory") == defaults()["fetch.directory"]:
        app.settings.update({"fetch.directory": str(app.location.data_dir / "downloads")})
    log.info("デモモードで起動しました（Azure には接続しません。架空の Blob %s 件）", f"{sum(counts.values()):,}")
