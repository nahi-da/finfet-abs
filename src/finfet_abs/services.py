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

    def job_secret(secret_id: str):
        """ジョブに保存した SAS（URL で指定したジョブ。5.3.5 の job_secrets）。"""
        from .core import dpapi
        row = app.db.query_one("SELECT secret FROM job_secrets WHERE id=?", (secret_id,))
        if row is None:
            from .azure.backend import BackendError
            raise BackendError("NO_CREDENTIAL", "ジョブに保存した SAS が見つかりません。")
        return dpapi.unseal(row["secret"], "job-secret")
    s["job_secrets"] = job_secret
    s["targets"] = TargetService(backend=s["backend"], credstore=s["credstore"], auth=s["auth"], arm=s["arm"],
                                 settings=app.settings, clock=app.clock)

    # 検索・検索結果・検索履歴（M4）
    from .history.exchange import ImportSession
    from .history.service import HistoryService
    from .search.engine import SearchService
    from .search.results import ResultsService
    from .search.urls import UrlSessions
    s["history"] = HistoryService(app.db, app.settings, app.clock)
    from .jobs.limits import Limits
    limits = Limits(app.settings)
    s["limits"] = limits
    s["search"] = SearchService(db=app.db, events=app.events, settings=app.settings, backend=lambda: s["backend"],
                                history=s["history"], clock=app.clock, connection=lambda: limits.request())
    s["results"] = ResultsService(app.db)
    s["url_sessions"] = UrlSessions()
    s["import_sessions"] = ImportSession()

    # ジョブとキュー（M5）
    from .jobs.engine import JobEngine
    from .jobs.service import JobService
    s["jobs"] = JobService(app.db, app.settings, app.events, clock=app.clock, url_sessions=s["url_sessions"])
    s["job_engine"] = JobEngine(db=app.db, settings=app.settings, events=app.events, service=s["jobs"],
                                backend=lambda: s["backend"], limits=limits, logs_dir=app.location.job_logs_dir,
                                clock=app.clock, resolve_auth=lambda t: s["targets"].resolve(t),
                                time_scale=(lambda: app.settings.get("demo.time_scale")) if app.demo else None)

    def on_limits(keys: list[str]) -> None:
        if any(k.startswith("transfer.") for k in keys):
            limits.apply_settings()
    app.settings.on_change(on_limits)
    s["jobs"].on_startup()
    s["job_engine"].start()
    app.on_stop(s["job_engine"].stop)
