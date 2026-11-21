"""デモモードの架空のサインインと ARM（8.3.3）。名前はすべて架空のものです。"""

from __future__ import annotations

import secrets
import threading
import time

from azure.core.credentials import AccessToken

from ..azure.backend import BackendError
from ..azure.credentials import STATUS_LABELS, InteractionRequired, NotSignedIn, UserCredential
from ..azure.errors import ErrorInfo
from ..core.secret import Secret
from .generator import ACCOUNTS, SUB, TENANT

TENANT2 = "00000000-0000-0000-0000-000000000002"


#: デモのユーザーアカウント（架空）: キー → (表示名, ユーザー名, ホームのテナント)
DEMO_USERS = {
    "sato@contoso.com": ("佐藤 一郎", "sato@contoso.com", TENANT),
    "suzuki@fabrikam.com": ("鈴木 花子", "suzuki@fabrikam.com", TENANT2),
}


class FakeAuthService:
    """架空のサインイン。サインインの URL は、本ツール自身の戻りの URL です（押すとすぐにサインインします）。

    最初のサインインは佐藤、[アカウントを追加] は、まだサインインしていない次のアカウント（鈴木）になります。
    """

    cache_persistent = True

    def __init__(self, port, events=None) -> None:
        self._port = port
        self.events = events
        self._lock = threading.Lock()
        self._flows: dict[str, dict] = {}
        self._accounts: list[str] = []
        self._default: str | None = None
        self._tenant_status: dict[str, dict[str, dict]] = {}
        self.tenant_names = {TENANT: "Contoso", TENANT2: "Fabrikam"}

    @property
    def signed_in(self) -> bool:
        return bool(self._accounts)

    @property
    def default_account(self) -> str | None:
        return self._default if self._default in self._accounts else (self._accounts[0] if self._accounts else None)

    def resolve(self, account):
        return account.strip().lower() if account else self.default_account

    def is_signed_in(self, account) -> bool:
        k = self.resolve(account)
        return bool(k) and k in self._accounts

    def account_info(self, account=None) -> dict | None:
        k = self.resolve(account)
        if not k or k not in self._accounts:
            return None
        name, user, tid = DEMO_USERS[k]
        return {"id": k, "name": name, "username": user, "tenant_id": tid, "tenant_name": self.tenant_names[tid],
                "default": k == self.default_account}

    @property
    def tenant_status(self) -> dict:
        return self._tenant_status.get(self.default_account or "", {})

    def set_tenant_status(self, tenant, status, message="", account=None) -> None:
        k = self.resolve(account)
        if k:
            self._tenant_status.setdefault(k, {})[tenant] = {"status": status, "message": message}

    def status(self) -> dict:
        return {"signed_in": self.signed_in, "account": self.account_info(),
                "accounts": [self.account_info(k) for k in self._accounts],
                "tenants": [{"id": t, "name": self.tenant_names.get(t, ""), "status": s["status"],
                             "label": STATUS_LABELS.get(s["status"], ""), "message": s.get("message", ""), "account": k}
                            for k in self._accounts for t, s in self._tenant_status.get(k, {}).items()],
                "signin_pending": any(not f.get("cancelled") for f in self._flows.values()), "cache_persistent": True}

    def _changed(self) -> None:
        if self.events is not None:
            self.events.publish("auth.changed", self.status())

    def set_default(self, account) -> None:
        k = self.resolve(account)
        if k not in self._accounts:
            raise NotSignedIn(account)
        self._default = k
        self._changed()

    def start_sign_in(self, tenant=None, open_browser=True, *, account=None, add=False) -> dict:
        if account:
            who = self.resolve(account)
        elif add or not self._accounts:
            who = next((k for k in DEMO_USERS if k not in self._accounts), None)
        else:
            who = self.default_account
        state = secrets.token_urlsafe(16)
        fid = secrets.token_urlsafe(8)
        with self._lock:
            self._flows[fid] = {"state": state, "tenant": tenant, "created": time.monotonic(), "who": who}
        self._changed()
        return {"flow_id": fid, "auth_uri": f"http://localhost:{self._port()}/?code=demo&state={state}"}

    def cancel(self, flow_id=None) -> None:
        with self._lock:
            for k, f in self._flows.items():
                if flow_id is None or k == flow_id:
                    f["cancelled"] = True
        self._changed()

    def handle_return(self, query: dict) -> tuple[bool, str]:
        with self._lock:
            match = next((k for k, f in self._flows.items() if f["state"] == query.get("state") and not f.get("cancelled")), None)
            if match is None:
                return False, "このサインインは取り消されたか、期限が切れています。"
            flow = self._flows.pop(match)
        who = flow.get("who")
        if not who:
            return False, "デモモードでサインインできるアカウントは 2 つまでです。"
        if who not in self._accounts:
            self._accounts.append(who)
        if self._default is None:
            self._default = who
        home = DEMO_USERS[who][2]
        st = self._tenant_status.setdefault(who, {})
        st[home] = {"status": "ok", "message": ""}
        if flow.get("tenant"):
            st[flow["tenant"]] = {"status": "ok", "message": ""}
        elif who == "sato@contoso.com" and TENANT2 not in st:
            st[TENANT2] = {"status": "mfa", "message": "多要素認証が必要です。[このテナントにサインイン] からサインインしてください。"}
        self._changed()
        return True, "サインインしました。このタブを閉じて、本ツールの画面に戻ってください。"

    def restore(self) -> bool:
        return False

    def sign_out(self, account=None) -> None:
        keys = [self.resolve(account)] if account else list(self._accounts)
        for k in keys:
            if k in self._accounts:
                self._accounts.remove(k)
            self._tenant_status.pop(k, None)
        if self._default not in self._accounts:
            self._default = self._accounts[0] if self._accounts else None
        self._changed()

    def reset_clients(self) -> None:
        pass

    def get_token(self, scopes, tenant_id=None, account=None) -> AccessToken:
        k = self.resolve(account)
        if not k or k not in self._accounts:
            raise NotSignedIn(account)
        st = self._tenant_status.get(k, {})
        if tenant_id == TENANT2 and st.get(TENANT2, {}).get("status") != "ok":
            raise InteractionRequired(TENANT2, ErrorInfo("AADSTS50076", "多要素認証が必要です。[このテナントにサインイン] からサインインしてください。"), account=k)
        return AccessToken("demo-token", int(time.time()) + 3600)

    def credential(self, tenant_id, account=None):
        return UserCredential(self, tenant_id, account)


class FakeArm:
    """架空の ARM（ストレージアカウントの一覧、コンテナー、アクセスキー）。"""

    def __init__(self, auth: FakeAuthService, backend) -> None:
        self.auth = auth
        self.backend = backend

    def reset(self) -> None:
        pass

    def for_account(self, account):
        """アカウントごとの ARM（デモでは、どのアカウントでも同じストレージアカウントを返します）。"""
        if account and not self.auth.is_signed_in(account):
            raise NotSignedIn(account)
        return self

    def forget(self, account) -> None:
        pass

    def tenants(self) -> list[dict]:
        return [{"id": TENANT, "name": "Contoso"}, {"id": TENANT2, "name": "Fabrikam"}]

    def _rows(self) -> list[dict]:
        out = []
        for name, suffix, sub_name, rg, region in ACCOUNTS:
            if suffix != "core.windows.net":
                continue
            out.append({"id": f"/subscriptions/{SUB}/resourceGroups/{rg}/providers/Microsoft.Storage/storageAccounts/{name}",
                        "name": name, "subscriptionId": SUB, "subscriptionName": sub_name, "resourceGroup": rg,
                        "tenantId": TENANT, "tenantName": "Contoso", "location": region,
                        "allowSharedKeyAccess": name != "stexample03", "isHnsEnabled": False})
        return out

    def list_storage_accounts(self, refresh: bool = False) -> dict:
        if not self.auth.signed_in:
            raise NotSignedIn()
        unavailable = []
        if self.auth.tenant_status.get(TENANT2, {}).get("status") != "ok":
            unavailable.append({"tenant_id": TENANT2, "tenant_name": "Fabrikam",
                                "reason": "多要素認証が必要です。"})
        return {"accounts": self._rows(), "unavailable": unavailable, "multi_tenant": False}

    def find_storage_account(self, name: str):
        return next((r for r in self._rows() if r["name"] == name), None)

    def tenant_of(self, name: str):
        r = self.find_storage_account(name)
        return r["tenantId"] if r else None

    def list_containers(self, name: str) -> list[str]:
        from ..azure.backend import AccountRef
        return self.backend.list_containers(AccountRef(name))

    def get_account_key(self, name: str) -> Secret:
        r = self.find_storage_account(name)
        if r is None:
            raise BackendError("StorageAccountNotFound", f"ストレージアカウント「{name}」が見つかりません。")
        if r["allowSharedKeyAccess"] is False:
            raise BackendError("KeyBasedAuthenticationNotPermitted",
                               "このストレージアカウントでは、アクセスキーによる認証が無効にされています。Entra ID（ユーザーアカウント）を使用してください。")
        return Secret("ZGVtby1rZXktZm9yLWZpbmZldC1hYnMtZGVtby1tb2Rl")
