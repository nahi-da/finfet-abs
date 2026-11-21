"""API の関数（5.7）。層の依存の向き: web → api → jobs / search / history → azure / core。"""

from __future__ import annotations

from ..server.router import Router


def build_router(app) -> Router:
    """すべての API を登録したルーターを作ります。"""
    from . import fs, session, settings_api

    router = Router()
    session.register(router, app)
    settings_api.register(router, app)
    fs.register(router, app)
    import importlib
    import importlib.util
    for name in ("auth_api", "search_api", "jobs_api", "history_api"):
        if importlib.util.find_spec(f"{__name__}.{name}") is None:
            continue
        importlib.import_module(f"{__name__}.{name}").register(router, app)
    return router
