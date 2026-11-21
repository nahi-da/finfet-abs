"""ユーザーアカウントのサインイン（3.1.2）。

MSAL の公開 API で、認可コードフロー（PKCE 付き）を自前で行います。
- サインインの状態（state）はメモリにだけ保持し、10 分で無効にします。
- `get_token` の中ではブラウザを開きません。トークンを取れない場合は InteractionRequired を出します（7.4 の I7）。
- トークンキャッシュは msal-extensions で DPAPI により暗号化して保存します（暗号化できなければメモリだけ）。
"""

from __future__ import annotations

import logging
import secrets
import threading
import time
import webbrowser
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from azure.core.credentials import AccessToken

from ..core.clock import SYSTEM_CLOCK, Clock
from .errors import ErrorInfo, msal_message

log = logging.getLogger(__name__)

ARM_SCOPE = "https://management.azure.com/.default"
STORAGE_SCOPE = "https://storage.azure.com/.default"
AUTHORITY_HOST = "https://login.microsoftonline.com"
FLOW_TTL = 600.0

#: テナントの状態（実行中だけ保持）
STATUS_LABELS = {"ok": "利用可", "ca_blocked": "条件付きアクセスによりブロック", "mfa": "多要素認証が必要",
                 "error": "そのほかのエラー", "unknown": "未確認"}


class InteractionRequired(Exception):
    """利用者の操作（このテナントにサインイン）が必要。"""

    def __init__(self, tenant_id: str | None, info: ErrorInfo) -> None:
        super().__init__(info.message)
        self.tenant_id = tenant_id
        self.info = info


class NotSignedIn(InteractionRequired):
    """サインインしていない。"""

    def __init__(self) -> None:
        super().__init__(None, ErrorInfo("NOT_SIGNED_IN", "サインインしていません。"))


@dataclass
class PendingFlow:
    id: str
    state: str
    flow: dict
    tenant: str | None
    created: float
    cancelled: bool = False


def _status_from_codes(codes: list) -> str:
    cs = {str(c) for c in codes or []}
    if "53003" in cs:
        return "ca_blocked"
    if cs & {"50076", "50079"}:
        return "mfa"
    return "error"


def build_token_cache(path: Path):
    """暗号化したトークンキャッシュ（Windows では DPAPI）。作れなければメモリだけのキャッシュ。"""
    import msal
    try:
        from msal_extensions import PersistedTokenCache, build_encrypted_persistence
        persistence = build_encrypted_persistence(str(path))
        return PersistedTokenCache(persistence), True
    except Exception as exc:  # noqa: BLE001
        log.warning("トークンキャッシュを暗号化して保存できないため、メモリだけで使います（%s）", type(exc).__name__)
        return msal.SerializableTokenCache(), False


class AuthService:
    """サインインとトークンの取得。"""

    def __init__(self, *, client_id: Callable[[], str], session: Callable[[], object], cache_path: Path,
                 port: Callable[[], int], events=None, clock: Clock = SYSTEM_CLOCK,
                 app_factory: Callable | None = None, open_browser: Callable[[str], object] = webbrowser.open,
                 monotonic: Callable[[], float] = time.monotonic) -> None:
        self._client_id = client_id
        self._session = session
        self._port = port
        self.events = events
        self.clock = clock
        self._open_browser = open_browser
        self._monotonic = monotonic
        self._lock = threading.RLock()
        self._flows: dict[str, PendingFlow] = {}
        self._apps: dict[str, object] = {}
        self._account: dict | None = None
        self._claims: dict = {}
        self.tenant_status: dict[str, dict] = {}
        self.tenant_names: dict[str, str] = {}
        if app_factory is None:
            self._cache, self.cache_persistent = build_token_cache(cache_path)
            self._cache_path = cache_path
            import msal

            def app_factory(client_id, authority, token_cache, http_client):
                return msal.PublicClientApplication(client_id, authority=authority, token_cache=token_cache,
                                                    http_client=http_client)
        else:
            import msal
            self._cache = msal.SerializableTokenCache()
            self.cache_persistent = False
            self._cache_path = cache_path
        self._factory = app_factory

    # ------------------------------------------------------------ MSAL

    def _app(self, tenant: str | None):
        authority = f"{AUTHORITY_HOST}/{tenant or 'organizations'}"
        with self._lock:
            app = self._apps.get(authority)
            if app is None:
                app = self._factory(self._client_id(), authority, self._cache, self._session())
                self._apps[authority] = app
            return app

    def reset_clients(self) -> None:
        """クライアント ID・プロキシの変更を反映します。"""
        with self._lock:
            self._apps.clear()

    def _home_account(self, app) -> dict | None:
        accounts = app.get_accounts()
        if not accounts:
            return None
        if self._account:
            for a in accounts:
                if a.get("home_account_id") == self._account.get("home_account_id"):
                    return a
        return accounts[0]

    # ------------------------------------------------------------ 状態

    @property
    def signed_in(self) -> bool:
        return self._account is not None

    def account_info(self) -> dict | None:
        if not self._account:
            return None
        c = self._claims or {}
        home_tid = c.get("tid") or (self._account.get("home_account_id", ".").split(".")[-1])
        return {"name": c.get("name") or "", "username": c.get("preferred_username") or self._account.get("username", ""),
                "tenant_id": home_tid, "tenant_name": self.tenant_names.get(home_tid, "")}

    def status(self) -> dict:
        with self._lock:
            pending = any(not f.cancelled and self._monotonic() - f.created < FLOW_TTL for f in self._flows.values())
            return {
                "signed_in": self.signed_in,
                "account": self.account_info(),
                "tenants": [{"id": tid, "name": self.tenant_names.get(tid, ""), "status": st["status"],
                             "label": STATUS_LABELS.get(st["status"], ""), "message": st.get("message", "")}
                            for tid, st in self.tenant_status.items()],
                "signin_pending": pending,
                "cache_persistent": self.cache_persistent,
            }

    def _changed(self) -> None:
        if self.events is not None:
            self.events.publish("auth.changed", self.status())

    def set_tenant_status(self, tenant: str, status: str, message: str = "") -> None:
        with self._lock:
            self.tenant_status[tenant] = {"status": status, "message": message}

    # ------------------------------------------------------------ サインイン

    def redirect_uri(self) -> str:
        return f"http://localhost:{self._port()}"

    def start_sign_in(self, tenant: str | None = None, open_browser: bool = True) -> dict:
        """サインインを始めます。サインインの URL を返し、既定のブラウザの新しいタブで開きます。"""
        app = self._app(tenant)
        flow = app.initiate_auth_code_flow([ARM_SCOPE], redirect_uri=self.redirect_uri(),
                                           login_hint=(self._account or {}).get("username") if tenant else None)
        if "auth_uri" not in flow:
            info = msal_message(flow)
            raise InteractionRequired(tenant, info)
        fid = secrets.token_urlsafe(12)
        with self._lock:
            now = self._monotonic()
            self._flows = {k: f for k, f in self._flows.items() if now - f.created < FLOW_TTL}
            self._flows[fid] = PendingFlow(fid, flow["state"], flow, tenant, now)
        log.info("サインインを開始しました（テナント: %s）", tenant or "既定")
        if open_browser:
            try:
                self._open_browser(flow["auth_uri"])
            except Exception:  # noqa: BLE001
                log.warning("ブラウザでサインインのページを開けませんでした")
        self._changed()
        return {"flow_id": fid, "auth_uri": flow["auth_uri"]}

    def cancel(self, flow_id: str | None = None) -> None:
        """サインインを取り消します（保持中の状態を破棄します）。"""
        with self._lock:
            for k, f in list(self._flows.items()):
                if flow_id is None or k == flow_id:
                    f.cancelled = True
        self._changed()

    def handle_return(self, query: dict[str, str]) -> tuple[bool, str]:
        """サインインの戻り（`/?code=…&state=…`）を処理します。state が一致する場合だけトークンを取得します。"""
        state = query.get("state", "")
        with self._lock:
            match = next((f for f in self._flows.values() if secrets.compare_digest(f.state, state)), None)
            if match is None or match.cancelled or self._monotonic() - match.created >= FLOW_TTL:
                if match is not None:
                    self._flows.pop(match.id, None)
                return False, "このサインインは取り消されたか、期限が切れています。"
            self._flows.pop(match.id, None)
        app = self._app(match.tenant)
        result = app.acquire_token_by_auth_code_flow(match.flow, query)
        if "access_token" not in result:
            info = msal_message(result)
            log.warning("サインインに失敗しました（%s）", info.code)
            self._changed()
            return False, info.message
        with self._lock:
            claims = result.get("id_token_claims") or {}
            accounts = app.get_accounts()
            acct = None
            for a in accounts:
                if claims.get("oid") and claims.get("oid") in a.get("home_account_id", "") or \
                        a.get("username") == claims.get("preferred_username"):
                    acct = a
            self._account = acct or (accounts[0] if accounts else {"username": claims.get("preferred_username", ""),
                                                                    "home_account_id": ""})
            if not match.tenant or not self._claims:
                self._claims = claims
            tid = match.tenant or claims.get("tid")
            if tid:
                self.tenant_status[tid] = {"status": "ok", "message": ""}
        log.info("サインインしました")
        self._changed()
        return True, "サインインしました。このタブを閉じて、本ツールの画面に戻ってください。"

    def restore(self) -> bool:
        """起動時に、キャッシュのアカウントでブラウザを開かずにサインイン済みの状態にします（3.1.2.2）。"""
        try:
            app = self._app(None)
            acct = self._home_account(app)
            if acct is None:
                return False
            result = app.acquire_token_silent([ARM_SCOPE], account=acct)
        except Exception as exc:  # noqa: BLE001
            log.warning("トークンキャッシュからサインインできませんでした（%s）", type(exc).__name__)
            return False
        if result and "access_token" in result:
            with self._lock:
                self._account = acct
                self._claims = result.get("id_token_claims") or {"preferred_username": acct.get("username")}
                tid = self._claims.get("tid")
                if tid:
                    self.tenant_status[tid] = {"status": "ok", "message": ""}
            log.info("トークンキャッシュからサインインしました")
            return True
        return False

    def sign_out(self) -> None:
        """キャッシュからアカウントを削除し、キャッシュのファイルを削除します。"""
        with self._lock:
            try:
                app = self._app(None)
                for a in app.get_accounts():
                    app.remove_account(a)
            except Exception:  # noqa: BLE001
                log.warning("アカウントをキャッシュから削除できませんでした")
            self._account = None
            self._claims = {}
            self.tenant_status.clear()
            self._apps.clear()
            try:
                if self._cache_path and Path(self._cache_path).exists():
                    Path(self._cache_path).unlink()
            except OSError:
                pass
        log.info("サインアウトしました")
        self._changed()

    # ------------------------------------------------------------ トークン

    def get_token(self, scopes: list[str], tenant_id: str | None = None) -> AccessToken:
        """トークンを取得します。ブラウザは開きません。取れなければ InteractionRequired。"""
        if not self.signed_in:
            raise NotSignedIn()
        app = self._app(tenant_id)
        acct = self._home_account(app)
        if acct is None:
            raise NotSignedIn()
        result = app.acquire_token_silent(list(scopes), account=acct)
        if result and "access_token" in result:
            if tenant_id:
                self.set_tenant_status(tenant_id, "ok")
            exp = int(time.time()) + int(result.get("expires_in", 3600))
            return AccessToken(result["access_token"], exp)
        result = result or {"error": "interaction_required"}
        info = msal_message(result)
        if tenant_id:
            self.set_tenant_status(tenant_id, _status_from_codes(result.get("error_codes")), info.message)
            self._changed()
        raise InteractionRequired(tenant_id, info)

    def credential(self, tenant_id: str | None) -> "UserCredential":
        return UserCredential(self, tenant_id)


class UserCredential:
    """`azure.core.credentials.TokenCredential` の約束を満たすもの（3.1.2.2）。"""

    def __init__(self, auth: AuthService, tenant_id: str | None) -> None:
        self.auth = auth
        self.tenant_id = tenant_id

    def get_token(self, *scopes: str, claims: str | None = None, tenant_id: str | None = None, **kwargs) -> AccessToken:
        return self.auth.get_token(list(scopes), tenant_id or self.tenant_id)

    def close(self) -> None:
        pass

    def __enter__(self):
        return self

    def __exit__(self, *a) -> None:
        pass
