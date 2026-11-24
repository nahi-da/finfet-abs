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
    """利用者の操作（このテナントにサインイン）が必要。account は対象のアカウント（分かる場合）。"""

    def __init__(self, tenant_id: str | None, info: ErrorInfo, account: str | None = None) -> None:
        super().__init__(info.message)
        self.tenant_id = tenant_id
        self.info = info
        self.account = account


class NotSignedIn(InteractionRequired):
    """サインインしていない（account を指定した場合は、そのアカウントでサインインしていない）。"""

    def __init__(self, account: str | None = None) -> None:
        msg = f"{account} でサインインしていません。接続ページでサインインしてください。" if account else "サインインしていません。"
        super().__init__(None, ErrorInfo("NOT_SIGNED_IN", msg), account=account)


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
        log.warning("トークンキャッシュを暗号化して保存できないため、メモリ上のみで使用します（%s）", type(exc).__name__)
        return msal.SerializableTokenCache(), False


def account_key(username: str | None) -> str:
    """アカウントの識別に使うキー（ユーザー名の小文字）。"""
    return (username or "").strip().lower()


class AuthService:
    """サインインとトークンの取得。複数のユーザーアカウントで同時にサインインできます（依頼者の意見。decisions.md）。

    - アカウントはユーザー名（preferred_username の小文字）で識別します。接続先は、使うアカウントを account に持ちます。
    - account を指定しない要求は「既定のアカウント」を使います（v1.1 までに作った接続先・ジョブもこれを使います）。
    - 既定のアカウントのユーザー名は、トークンキャッシュの隣の `msal_default.txt` に保存します（秘密情報ではありません）。
    """

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
        #: キー → {"account": MSAL のアカウント, "claims": id_token_claims}（サインインした順）
        self._accounts: dict[str, dict] = {}
        self._default: str | None = None
        #: アカウントのキー → テナント → 状態（実行中だけ）
        self._tenant_status: dict[str, dict[str, dict]] = {}
        self.tenant_names: dict[str, str] = {}
        self._cache_path = cache_path
        self._default_path = Path(cache_path).with_name("msal_default.txt") if cache_path else None
        if app_factory is None:
            self._cache, self.cache_persistent = build_token_cache(cache_path)
            import msal

            def app_factory(client_id, authority, token_cache, http_client):
                return msal.PublicClientApplication(client_id, authority=authority, token_cache=token_cache,
                                                    http_client=http_client)
        else:
            import msal
            self._cache = msal.SerializableTokenCache()
            self.cache_persistent = False
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

    def _msal_account(self, app, key: str) -> dict | None:
        """MSAL のキャッシュから、キーのアカウントを探します。"""
        entry = self._accounts.get(key)
        home = (entry or {}).get("account", {}).get("home_account_id")
        for a in app.get_accounts():
            if home and a.get("home_account_id") == home:
                return a
        for a in app.get_accounts(username=key) if key else []:
            return a
        return None

    # ------------------------------------------------------------ 状態

    @property
    def signed_in(self) -> bool:
        return bool(self._accounts)

    @property
    def default_account(self) -> str | None:
        with self._lock:
            if self._default in self._accounts:
                return self._default
            return next(iter(self._accounts), None)

    def resolve(self, account: str | None) -> str | None:
        """接続先の account（None は既定のアカウント）を、サインイン中のアカウントのキーにします。"""
        return account_key(account) if account else self.default_account

    def is_signed_in(self, account: str | None) -> bool:
        k = self.resolve(account)
        return bool(k) and k in self._accounts

    def account_info(self, account: str | None = None) -> dict | None:
        k = self.resolve(account)
        entry = self._accounts.get(k or "")
        if not entry:
            return None
        c = entry.get("claims") or {}
        acct = entry.get("account") or {}
        home_tid = c.get("tid") or (acct.get("home_account_id", ".").split(".")[-1])
        return {"id": k, "name": c.get("name") or "", "username": c.get("preferred_username") or acct.get("username", "") or k,
                "tenant_id": home_tid, "tenant_name": self.tenant_names.get(home_tid, ""), "default": k == self.default_account}

    def _tenants_of(self, key: str) -> list[dict]:
        return [{"id": tid, "name": self.tenant_names.get(tid, ""), "status": st["status"],
                 "label": STATUS_LABELS.get(st["status"], ""), "message": st.get("message", ""), "account": key}
                for tid, st in self._tenant_status.get(key, {}).items()]

    def status(self) -> dict:
        with self._lock:
            pending = any(not f.cancelled and self._monotonic() - f.created < FLOW_TTL for f in self._flows.values())
            accounts = [self.account_info(k) for k in self._accounts]
            return {
                "signed_in": self.signed_in,
                "account": self.account_info(),
                "accounts": accounts,
                "tenants": [t for k in self._accounts for t in self._tenants_of(k)],
                "signin_pending": pending,
                "cache_persistent": self.cache_persistent,
            }

    @property
    def tenant_status(self) -> dict[str, dict]:
        """既定のアカウントのテナントの状態（v1.1 までと同じ形）。"""
        return self._tenant_status.get(self.default_account or "", {})

    def _changed(self) -> None:
        if self.events is not None:
            self.events.publish("auth.changed", self.status())

    def set_tenant_status(self, tenant: str, status: str, message: str = "", account: str | None = None) -> None:
        with self._lock:
            k = self.resolve(account)
            if k:
                self._tenant_status.setdefault(k, {})[tenant] = {"status": status, "message": message}

    def set_default(self, account: str) -> None:
        k = account_key(account)
        with self._lock:
            if k not in self._accounts:
                raise NotSignedIn(account)
            self._default = k
            self._save_default()
        self._changed()

    def _save_default(self) -> None:
        if not self._default_path:
            return
        try:
            if self._default:
                self._default_path.write_text(self._default, encoding="utf-8")
            elif self._default_path.exists():
                self._default_path.unlink()
        except OSError:
            log.warning("既定のアカウントを保存できませんでした")

    def _load_default(self) -> str | None:
        try:
            if self._default_path and self._default_path.exists():
                return account_key(self._default_path.read_text(encoding="utf-8"))
        except OSError:
            pass
        return None

    # ------------------------------------------------------------ サインイン

    def redirect_uri(self) -> str:
        return f"http://localhost:{self._port()}"

    def start_sign_in(self, tenant: str | None = None, open_browser: bool = True, *, account: str | None = None,
                      add: bool = False) -> dict:
        """サインインを始めます。サインインの URL を返し、既定のブラウザの新しいタブで開きます。

        - tenant: そのテナントにサインイン（[このテナントにサインイン]）
        - account: そのアカウントで（ユーザー名を入力済みにします）
        - add: 別のアカウントを追加（アカウントの選択の画面を表示します）
        """
        app = self._app(tenant)
        hint = None
        if account:
            hint = (self.account_info(account) or {}).get("username") or account
        elif tenant and not add:
            hint = (self.account_info() or {}).get("username")
        kw = {"login_hint": hint} if hint else {}
        if add:
            kw["prompt"] = "select_account"
        flow = app.initiate_auth_code_flow([ARM_SCOPE], redirect_uri=self.redirect_uri(), **kw)
        if "auth_uri" not in flow:
            info = msal_message(flow)
            raise InteractionRequired(tenant, info)
        fid = secrets.token_urlsafe(12)
        with self._lock:
            now = self._monotonic()
            self._flows = {k: f for k, f in self._flows.items() if now - f.created < FLOW_TTL}
            self._flows[fid] = PendingFlow(fid, flow["state"], flow, tenant, now)
        log.info("サインインを開始しました（テナント: %s%s）", tenant or "既定", "、アカウントの追加" if add else "")
        if open_browser:
            try:
                self._open_browser(flow["auth_uri"])
            except Exception:  # noqa: BLE001
                log.warning("ブラウザでサインインページを開けませんでした")
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
            acct = acct or {"username": claims.get("preferred_username", ""), "home_account_id": ""}
            key = account_key(claims.get("preferred_username") or acct.get("username"))
            entry = self._accounts.get(key)
            if entry is None:
                self._accounts[key] = {"account": acct, "claims": claims}
            else:
                entry["account"] = acct
                if not match.tenant:
                    entry["claims"] = claims
            if self.default_account is None or self._default is None:
                self._default = self.default_account
                self._save_default()
            tid = match.tenant or claims.get("tid")
            if tid:
                self._tenant_status.setdefault(key, {})[tid] = {"status": "ok", "message": ""}
        log.info("サインインしました")
        self._changed()
        return True, "サインインしました。このタブを閉じて、本ツールの画面に戻ってください。"

    def restore(self) -> bool:
        """起動時に、キャッシュのアカウントでブラウザを開かずにサインイン済みの状態にします（3.1.2.2）。"""
        try:
            app = self._app(None)
            cached = app.get_accounts()
        except Exception as exc:  # noqa: BLE001
            # MSAL のクライアントを作るときにも通信します。通信できなければ、キャッシュから直接アカウントを読みます
            log.warning("サインインの情報を確認できませんでした（%s）", type(exc).__name__)
            try:
                import msal
                found = self._cache.find(msal.TokenCache.CredentialType.ACCOUNT)
            except Exception:  # noqa: BLE001
                found = []
            with self._lock:
                for a in found:
                    k = account_key(a.get("username"))
                    if k and k not in self._accounts:
                        self._accounts[k] = {"account": a, "claims": {"preferred_username": a.get("username", "")}}
                self._default = self._load_default()
            return self.signed_in
        for acct in cached:
            k = account_key(acct.get("username"))
            if not k or k in self._accounts:
                continue
            try:
                result = app.acquire_token_silent([ARM_SCOPE], account=acct)
            except Exception as exc:  # noqa: BLE001
                # ネットワーク（プロキシなど）の問題: アカウントはサインイン済みとして扱い、
                # 実際の要求のときに原因（プロキシの誤りなど）の文を表示します
                log.warning("トークンを更新できませんでした（%s）。サインイン済みとして扱います", type(exc).__name__)
                with self._lock:
                    self._accounts[k] = {"account": acct, "claims": {"preferred_username": acct.get("username", "")}}
                continue
            if result and "access_token" in result:
                with self._lock:
                    claims = result.get("id_token_claims") or {"preferred_username": acct.get("username")}
                    self._accounts[k] = {"account": acct, "claims": claims}
                    tid = claims.get("tid")
                    if tid:
                        self._tenant_status.setdefault(k, {})[tid] = {"status": "ok", "message": ""}
        with self._lock:
            self._default = self._load_default()
        if self.signed_in:
            log.info("トークンキャッシュからサインインしました（%d アカウント）", len(self._accounts))
        return self.signed_in

    def sign_out(self, account: str | None = None) -> None:
        """サインアウトします。account を指定すればそのアカウントだけ、指定しなければすべて。

        アカウントをキャッシュから削除し、アカウントがなくなればキャッシュのファイルを削除します。
        """
        with self._lock:
            keys = [account_key(account)] if account else list(self._accounts)
            try:
                app = self._app(None)
                for a in app.get_accounts():
                    if account_key(a.get("username")) in keys:
                        app.remove_account(a)
            except Exception:  # noqa: BLE001
                log.warning("アカウントをキャッシュから削除できませんでした")
            for k in keys:
                self._accounts.pop(k, None)
                self._tenant_status.pop(k, None)
            if self._default not in self._accounts:
                self._default = next(iter(self._accounts), None)
            self._save_default()
            if not self._accounts:
                self._claims_cleared()
        log.info("サインアウトしました")
        self._changed()

    def _claims_cleared(self) -> None:
        self._apps.clear()
        try:
            if self._cache_path and Path(self._cache_path).exists():
                Path(self._cache_path).unlink()
        except OSError:
            pass

    # ------------------------------------------------------------ トークン

    def get_token(self, scopes: list[str], tenant_id: str | None = None, account: str | None = None) -> AccessToken:
        """トークンを取得します。ブラウザは開きません。取れなければ InteractionRequired。"""
        key = self.resolve(account)
        if not key or key not in self._accounts:
            raise NotSignedIn(account)
        app = self._app(tenant_id)
        acct = self._msal_account(app, key)
        if acct is None:
            raise NotSignedIn(account)
        result = app.acquire_token_silent(list(scopes), account=acct)
        if result and "access_token" in result:
            if tenant_id:
                self.set_tenant_status(tenant_id, "ok", account=key)
            exp = int(time.time()) + int(result.get("expires_in", 3600))
            return AccessToken(result["access_token"], exp)
        result = result or {"error": "interaction_required"}
        info = msal_message(result)
        if tenant_id:
            self.set_tenant_status(tenant_id, _status_from_codes(result.get("error_codes")), info.message, account=key)
            self._changed()
        raise InteractionRequired(tenant_id, info, account=key)

    def credential(self, tenant_id: str | None, account: str | None = None) -> "UserCredential":
        return UserCredential(self, tenant_id, account)


class UserCredential:
    """`azure.core.credentials.TokenCredential` の約束を満たすもの（3.1.2.2）。account は使うアカウント（None は既定）。"""

    def __init__(self, auth, tenant_id: str | None, account: str | None = None) -> None:
        self.auth = auth
        self.tenant_id = tenant_id
        self.account = account

    def get_token(self, *scopes: str, claims: str | None = None, tenant_id: str | None = None, **kwargs) -> AccessToken:
        return self.auth.get_token(list(scopes), tenant_id or self.tenant_id, account=self.account)

    def close(self) -> None:
        pass

    def __enter__(self):
        return self

    def __exit__(self, *a) -> None:
        pass
