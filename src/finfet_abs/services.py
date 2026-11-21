"""サービス層の組み立て。フェーズごとに部品を加えます。"""

from __future__ import annotations

import logging

log = logging.getLogger(__name__)


def setup_services(app) -> None:
    """認証・検索・ジョブなどのサービスを作り、app.services に登録します。"""
    if app.demo:
        from .demo import setup_demo
        setup_demo(app)
