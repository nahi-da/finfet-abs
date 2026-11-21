"""デモモード（8.3.3）。Azure の代わりに架空の `StorageBackend` と架空のサインインを使います。"""

from __future__ import annotations

import logging

log = logging.getLogger(__name__)


def setup_demo(app) -> None:
    """デモモードの部品を登録します。"""
    log.info("デモモードで起動しました（Azure には接続しません）")
