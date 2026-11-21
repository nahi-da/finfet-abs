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


class FakeAuthService:
    """架空のサインイン。サインインの URL は、本ツール自身の戻りの URL です（押すとすぐにサインインします）。"""

    cache_persistent = True

    def __init__(self, port, events=None) -> None:
        self._port = port
        self.events = events
        self._lock = threading.Lock()
        self._flows: dict[str, dict] = {}
        self._signed_in = False
        self.tenant_status: dict[str, dict] = {}
        self.tenant_names = {TENANT: "Contoso", TENANT2: "Fabrikam"}

    @property
    def signed_in(self) -> bool:
        return self._signed_in

    def account_info(self) -> dict | None:
        if not self._signed_in:
            return None
        return {"name": "佐藤 一郎", "username": "sato@contoso.com", "tenant_id": TENANT, "tenant_name": "Contoso"}

    def status(self) -> dict:
        return {"signed_in": self._signed_in, "account": self.account_info(),
                "tenants": [{"id": t, "name": self.tenant_names.get(t, ""), "status": s["status"],
                             "label": STATUS_LABELS.get(s["status"], ""), "message": s.get("message", "")}
                            for t, s in self.tenant_status.items()],
                "signin_pending": any(not f.get("cancelled") for f in self._flows.values()), "cache_persistent": True}

    def _changed(self) -> None:
        if self.events is not None:
            self.events.publish("auth.changed", self.status())

    def start_sign_in(self, tenant=None, open_browser=True) -> dict:
        state = secrets.token_urlsafe(16)
        fid = secrets.token_urlsafe(8)
        with self._lock:
            self._flows[fid] = {"state": state, "tenant": tenant, "created": time.monotonic()}
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
        self._signed_in = True
        self.tenant_status[TENANT] = {"status": "ok", "message": ""}
        if flow.get("tenant") == TENANT2:
            self.tenant_status[TENANT2] = {"status": "ok", "message": ""}
        elif TENANT2 not in self.tenant_status:
            self.tenant_status[TENANT2] = {"status": "mfa", "message": "多要素認証が必要です。[このテナントにサインイン] からサインインしてください。"}
        self._changed()
        return True, "サインインしました。このタブを閉じて、本ツールの画面に戻ってください。"

    def restore(self) -> bool:
        return False

    def sign_out(self) -> None:
        self._signed_in = False
        self.tenant_status.clear()
        self._changed()

    def reset_clients(self) -> None:
        pass

    def get_token(self, scopes, tenant_id=None) -> AccessToken:
        if not self._signed_in:
            raise NotSignedIn()
        if tenant_id == TENANT2 and self.tenant_status.get(TENANT2, {}).get("status") != "ok":
            raise InteractionRequired(TENANT2, ErrorInfo("AADSTS50076", "多要素認証が必要です。[このテナントにサインイン] からサインインしてください。"))
        return AccessToken("demo-token", int(time.time()) + 3600)

    def credential(self, tenant_id):
        return UserCredential(self, tenant_id)


class FakeArm:
    """架空の ARM（ストレージアカウントの一覧、コンテナー、アクセスキー）。"""

    def __init__(self, auth: FakeAuthService, backend) -> None:
        self.auth = auth
        self.backend = backend

    def reset(self) -> None:
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
