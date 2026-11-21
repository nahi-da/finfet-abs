"""ARM の REST API（3.1.5）。`azure-mgmt-*` を使わず、azure-core の PipelineClient で呼び出します。"""

from __future__ import annotations

import logging
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed

from ..core.secret import Secret
from ..version import __version__
from .credentials import ARM_SCOPE, AuthService, InteractionRequired
from .errors import from_exception
from .sas import ACCOUNT_RE

log = logging.getLogger(__name__)

ARM = "https://management.azure.com"
API_TENANTS = "2022-12-01"
API_SUBSCRIPTIONS = "2022-12-01"
API_GRAPH = "2024-04-01"
API_STORAGE = "2026-09-01"
MAX_ACCOUNTS = 5000
LIST_CACHE_SECONDS = 600


class ArmClient:
    """ARM の呼び出し（テナントごとのトークンを使います）。"""

    def __init__(self, auth: AuthService, session, retry_total=lambda: 5, account: str | None = None) -> None:
        self.auth = auth
        self.account = account  # 使うユーザーアカウント（None は既定のアカウント）
        self._session = session
        self._retry_total = retry_total
        self._clients: dict[str | None, object] = {}
        self._lock = threading.Lock()
        self._found: dict[str, dict] = {}
        self._keys: dict[str, Secret] = {}
        self._list_cache: tuple[float, dict] | None = None

    def reset(self) -> None:
        with self._lock:
            self._clients.clear()
            self._list_cache = None

    def _client(self, tenant: str | None):
        from azure.core import PipelineClient
        from azure.core.pipeline.policies import (BearerTokenCredentialPolicy, HeadersPolicy, RequestIdPolicy,
                                                  RetryPolicy, UserAgentPolicy)
        from azure.core.pipeline.transport import RequestsTransport
        with self._lock:
            c = self._clients.get(tenant)
            if c is None:
                policies = [
                    RequestIdPolicy(), HeadersPolicy(),
                    UserAgentPolicy(base_user_agent=f"finfet-abs/{__version__}", user_agent_use_env=False),
                    RetryPolicy(retry_total=self._retry_total()),
                    BearerTokenCredentialPolicy(self.auth.credential(tenant, self.account), ARM_SCOPE),
                ]
                c = PipelineClient(ARM, policies=policies,
                                   transport=RequestsTransport(session=self._session(), session_owner=False))
                self._clients[tenant] = c
            return c

    def request(self, method: str, url: str, tenant: str | None, *, params: dict | None = None,
                body: dict | None = None) -> dict:
        from azure.core.rest import HttpRequest
        if not url.startswith("https://"):
            url = ARM + url
        req = HttpRequest(method, url, params=params or {}, json=body)
        resp = self._client(tenant).send_request(req)
        try:
            resp.raise_for_status()
        except Exception as exc:  # noqa: BLE001
            code = ""
            try:
                code = (resp.json().get("error") or {}).get("code", "")
            except Exception:  # noqa: BLE001
                pass
            err = from_exception(exc)
            if code:
                err.code = code
                if code == "AuthorizationFailed":
                    err.message = "アクセスキーを取得する権限がありません。" if "listKeys" in url else \
                        "この操作の権限がありません。"
            raise err from None
        return resp.json() if resp.text() else {}

    # ------------------------------------------------------------ テナント・ストレージアカウント

    def tenants(self) -> list[dict]:
        data = self.request("GET", "/tenants", None, params={"api-version": API_TENANTS})
        out = []
        for t in data.get("value", []):
            tid = t.get("tenantId")
            out.append({"id": tid, "name": t.get("displayName") or t.get("defaultDomain") or tid})
            self.auth.tenant_names[tid] = out[-1]["name"]
        return out

    def _graph(self, tenant: str, query: str) -> list[dict]:
        rows: list[dict] = []
        skip = None
        while True:
            body = {"query": query, "options": {"$top": 1000}}
            if skip:
                body["options"]["$skipToken"] = skip
            data = self.request("POST", "/providers/Microsoft.ResourceGraph/resources", tenant,
                                params={"api-version": API_GRAPH}, body=body)
            rows.extend(data.get("data") or [])
            skip = data.get("$skipToken")
            if not skip or len(rows) >= MAX_ACCOUNTS:
                return rows[:MAX_ACCOUNTS]

    def list_storage_accounts(self, refresh: bool = False) -> dict:
        """サインインしたアカウントで使えるテナントごとに、ストレージアカウントを取得します（3.1.3.2）。"""
        with self._lock:
            if not refresh and self._list_cache and time.monotonic() - self._list_cache[0] < LIST_CACHE_SECONDS:
                return self._list_cache[1]
        accounts: list[dict] = []
        unavailable: list[dict] = []
        for t in self.tenants():
            tid = t["id"]
            try:
                rows = self._graph(tid, "resources | where type =~ 'microsoft.storage/storageaccounts' | project id, "
                                        "name, subscriptionId, resourceGroup, tenantId, location, "
                                        "allowSharedKeyAccess=properties.allowSharedKeyAccess, "
                                        "isHnsEnabled=properties.isHnsEnabled")
                subs = {r["subscriptionId"]: r.get("name") for r in self._graph(
                    tid, "resourcecontainers | where type =~ 'microsoft.resources/subscriptions' "
                         "| project subscriptionId, name")}
            except InteractionRequired as exc:
                unavailable.append({"tenant_id": tid, "tenant_name": t["name"], "reason": exc.info.message})
                continue
            except Exception as exc:  # noqa: BLE001
                err = from_exception(exc)
                unavailable.append({"tenant_id": tid, "tenant_name": t["name"], "reason": err.message})
                continue
            for r in rows:
                r["subscriptionName"] = subs.get(r.get("subscriptionId"), "")
                r["tenantName"] = t["name"]
                self._remember(r)
                accounts.append(r)
        result = {"accounts": accounts[:MAX_ACCOUNTS], "unavailable": unavailable,
                  "multi_tenant": len({a.get("tenantId") for a in accounts}) > 1}
        with self._lock:
            self._list_cache = (time.monotonic(), result)
        return result

    def _remember(self, row: dict) -> None:
        name = (row.get("name") or "").lower()
        prev = self._found.get(name)
        # 同じストレージアカウントが複数の経路で見つかった場合は、属するテナントの経路を優先します
        if prev is None or row.get("tenantId") == row.get("_viaTenant", row.get("tenantId")):
            self._found[name] = row

    def find_storage_account(self, name: str) -> dict | None:
        """ストレージアカウントを見つけます（3.1.5.1）。結果は実行中だけ保持します。"""
        name = name.lower()
        if not ACCOUNT_RE.match(name):
            return None
        if name in self._found:
            return self._found[name]
        q = ("resources | where type =~ 'microsoft.storage/storageaccounts' and name =~ '" + name + "' | project id, "
             "name, subscriptionId, resourceGroup, tenantId, location, sku, kind, "
             "allowSharedKeyAccess=properties.allowSharedKeyAccess, isHnsEnabled=properties.isHnsEnabled")
        graph_failed = False
        for t in self.tenants():
            try:
                rows = self._graph(t["id"], q)
            except InteractionRequired:
                continue
            except Exception:  # noqa: BLE001
                graph_failed = True
                continue
            for r in rows:
                r["_viaTenant"] = t["id"]
                self._remember(r)
        if name in self._found:
            return self._found[name]
        if graph_failed:
            return self._find_by_subscriptions(name)
        return None

    def _find_by_subscriptions(self, name: str) -> dict | None:
        """Resource Graph を使えない場合: サブスクリプションごとに一覧を呼び出します（並列 4）。"""
        subs = []
        for t in self.tenants():
            try:
                data = self.request("GET", "/subscriptions", t["id"], params={"api-version": API_SUBSCRIPTIONS})
                subs += [(t["id"], s["subscriptionId"]) for s in data.get("value", [])]
            except Exception:  # noqa: BLE001
                continue
        found = None
        with ThreadPoolExecutor(max_workers=4) as ex:
            futs = {ex.submit(self.request, "GET", f"/subscriptions/{sid}/providers/Microsoft.Storage/storageAccounts",
                              tid, params={"api-version": API_STORAGE}): tid for tid, sid in subs}
            for f in as_completed(futs):
                try:
                    for a in f.result().get("value", []):
                        if a.get("name", "").lower() == name:
                            found = {"id": a["id"], "name": a["name"], "tenantId": futs[f], "location": a.get("location"),
                                     "allowSharedKeyAccess": (a.get("properties") or {}).get("allowSharedKeyAccess"),
                                     "isHnsEnabled": (a.get("properties") or {}).get("isHnsEnabled")}
                            break
                except Exception:  # noqa: BLE001
                    continue
                if found:
                    break
        if found:
            self._found[name] = found
        return found

    def tenant_of(self, name: str) -> str | None:
        r = self.find_storage_account(name)
        return r.get("tenantId") if r else None

    # ------------------------------------------------------------ アクセスキー・コンテナー

    def get_account_key(self, name: str) -> Secret:
        """アクセスキー（key1）を取得します。メモリだけに保持します（3.1.5.2）。"""
        name = name.lower()
        if name in self._keys:
            return self._keys[name]
        from .backend import BackendError
        r = self.find_storage_account(name)
        if r is None:
            raise BackendError("StorageAccountNotFound",
                               f"ストレージアカウント「{name}」が見つかりません（ARM で読み取れるストレージアカウントだけ、アクセスキーを取得できます）。")
        if r.get("allowSharedKeyAccess") is False:
            raise BackendError("KeyBasedAuthenticationNotPermitted",
                               "このストレージアカウントでは、アクセスキーによる認証が無効にされています。Entra ID（ユーザーアカウント）を使用してください。")
        data = self.request("POST", f"{r['id']}/listKeys", r.get("tenantId"), params={"api-version": API_STORAGE})
        keys = data.get("keys") or []
        if not keys:
            raise BackendError("NoKeys", "アクセスキーを取得できませんでした。")
        key = Secret(keys[0]["value"])
        self._keys[name] = key
        return key

    def list_containers(self, name: str) -> list[str]:
        r = self.find_storage_account(name)
        if r is None:
            raise LookupError(name)
        out = []
        url = f"{r['id']}/blobServices/default/containers"
        params = {"api-version": API_STORAGE}
        while url:
            data = self.request("GET", url, r.get("tenantId"), params=params)
            out += [c["name"] for c in data.get("value", [])]
            url = data.get("nextLink")
            params = None
        return out


class ArmPool:
    """ユーザーアカウントごとの ArmClient（複数のアカウントでサインインできるため。decisions.md）。

    for_account(None) は既定のアカウントのもの。そのほかの属性は、既定のアカウントの ArmClient に渡します。
    """

    def __init__(self, auth: AuthService, session, retry_total=lambda: 5) -> None:
        self.auth = auth
        self._session = session
        self._retry_total = retry_total
        self._pool: dict[str, ArmClient] = {}
        self._pool_lock = threading.Lock()

    def for_account(self, account: str | None) -> ArmClient:
        key = self.auth.resolve(account) or ""
        with self._pool_lock:
            c = self._pool.get(key)
            if c is None:
                c = self._pool[key] = ArmClient(self.auth, self._session, self._retry_total, account=key or None)
            return c

    def reset(self) -> None:
        with self._pool_lock:
            for c in self._pool.values():
                c.reset()

    def forget(self, account: str) -> None:
        """サインアウトしたアカウントの ArmClient（取得したアクセスキーなど）を捨てます。"""
        with self._pool_lock:
            self._pool.pop(account, None)

    def __getattr__(self, name: str):
        return getattr(self.for_account(None), name)

