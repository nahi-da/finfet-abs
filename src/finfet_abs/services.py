"""サービス層の組み立て。フェーズごとに部品を加えます。"""

from __future__ import annotations

import logging
import threading

from .core.tasks import TaskRunner

log = logging.getLogger(__name__)


def setup_services(app) -> None:
    """認証・検索・ジョブなどのサービスを作り、app.services に登録します。"""
    from .azure.credstore import CredentialStore
    from .azure.targets import TargetService

    s = app.services
    s["tasks"] = TaskRunner(app.events)
    app.on_stop(s["tasks"].shutdown)
    s["credstore"] = CredentialStore(app.db, app.clock)
    port = lambda: app.security.port  # noqa: E731

    if app.demo:
        from .demo import setup_demo
        from .demo.fake_auth import FakeArm, FakeAuthService
        setup_demo(app)
        s["auth"] = FakeAuthService(port, app.events)
        s["arm"] = FakeArm(s["auth"], s["backend"])
        s["network"] = None
    else:
        from .azure.arm import ArmClient
        from .azure.credentials import AuthService
        from .azure.proxy import NetworkManager
        from .azure.storage import AzureBackend, ClientFactory, SecretResolver, TenantResolver

        network = NetworkManager(app.settings, app.location.data_dir)
        s["network"] = network
        auth = AuthService(client_id=lambda: app.settings.get("auth.client_id"), session=network.session,
                           cache_path=app.location.local_dir / "msal_cache.bin", port=port, events=app.events,
                           clock=app.clock)
        s["auth"] = auth
        arm = ArmClient(auth, network.session, lambda: app.settings.get("transfer.retry_total"))
        s["arm"] = arm
        resolver = SecretResolver(credential=lambda cid: s["targets"].resolve_secret(cid),
                                  job_secret=lambda sid: s["job_secrets"](sid))
        factory = ClientFactory(settings=app.settings, session=network.session, auth=auth, arm=arm, secrets=resolver,
                                tenants=TenantResolver(arm, network.session))
        s["clients"] = factory
        s["backend"] = AzureBackend(factory, network.config)

        def on_network_reset() -> None:
            auth.reset_clients()
            arm.reset()
            factory.reset()
        network.on_reset(on_network_reset)

        def on_settings(keys: list[str]) -> None:
            if any(k.startswith("network.") for k in keys) or "transfer.max_connections" in keys:
                network.reset()
            elif "auth.client_id" in keys:
                auth.reset_clients()
            elif any(k.startswith("transfer.") for k in keys):
                factory.reset()
        app.settings.on_change(on_settings)
        # 起動時に、トークンキャッシュのアカウントでサインイン済みの状態にします（ブラウザは開きません）
        threading.Thread(target=lambda: auth.restore() and auth._changed(), name="auth-restore", daemon=True).start()

    s["job_secrets"] = lambda sid: (_ for _ in ()).throw(LookupError(sid))
    s["targets"] = TargetService(backend=s["backend"], credstore=s["credstore"], auth=s["auth"], arm=s["arm"],
                                 settings=app.settings, clock=app.clock)

    # 検索・検索結果・検索履歴（M4）
    from .history.exchange import ImportSession
    from .history.service import HistoryService
    from .search.engine import SearchService
    from .search.results import ResultsService
    from .search.urls import UrlSessions
    s["history"] = HistoryService(app.db, app.settings, app.clock)
    s["search"] = SearchService(db=app.db, events=app.events, settings=app.settings, backend=lambda: s["backend"],
                                history=s["history"], clock=app.clock)
    s["results"] = ResultsService(app.db)
    s["url_sessions"] = UrlSessions()
    s["import_sessions"] = ImportSession()
