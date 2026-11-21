"""Azure の `StorageBackend` の実装（azure-storage-blob）とクライアントの生成・使い回し。"""

from __future__ import annotations

import logging
import threading
import urllib.request
from typing import Callable, Iterator

from ..core.secret import Secret
from ..version import __version__
from .backend import AccountRef, AuthRef, BackendError, BlobInfo, ContainerRef, ListPage
from .connstr import connection_parts
from .credentials import AuthService, InteractionRequired
from .errors import from_exception
from .proxy import proxy_label, resolve_proxy

log = logging.getLogger(__name__)


def arm_for(arm, account: str | None):
    """アカウントの ArmClient（ArmPool なら for_account、そうでなければそのもの）。"""
    f = getattr(type(arm), "for_account", None)
    return arm.for_account(account) if f is not None else arm


class SecretResolver:
    """認証情報の ID から秘密情報を取り出す関数の組（保存した認証情報、ジョブに保存した SAS）。"""

    def __init__(self, credential: Callable[[str], tuple[str, Secret]], job_secret: Callable[[str], Secret]) -> None:
        self.credential = credential  # ID → (source, 秘密情報)
        self.job_secret = job_secret


class TenantResolver:
    """Entra ID のデータ操作で使うテナントの決定（3.1.4）。結果は実行中だけ保持します。"""

    def __init__(self, arm, session: Callable[[], object]) -> None:
        self.arm = arm
        self._session = session
        self._cache: dict[str, str] = {}
        self._lock = threading.Lock()

    def resolve(self, account: AccountRef, container: str | None = None) -> str | None:
        if account.auth.tenant_id:
            return account.auth.tenant_id
        key = f"{account.account}.{account.endpoint_suffix}|{account.auth.account or ''}"
        with self._lock:
            if key in self._cache:
                return self._cache[key]
        tid = None
        try:
            tid = arm_for(self.arm, account.auth.account).tenant_of(account.account) if self.arm else None
        except InteractionRequired:
            tid = None
        except Exception:  # noqa: BLE001
            tid = None
        if not tid:
            tid = self.challenge(account, container)
        if tid:
            with self._lock:
                self._cache[key] = tid
        return tid

    def challenge(self, account: AccountRef, container: str | None) -> str | None:
        """認証なしの要求に対する 401 の WWW-Authenticate からテナントを求めます。"""
        import re
        url = f"{account.blob_endpoint}/{container or '$root'}?restype=container&comp=list&maxresults=1"
        try:
            # 認証のヘッダーを付けずに送ります（空の Bearer を付けると 400 になり、テナントが分かりません）
            resp = self._session().get(url, headers={"x-ms-version": "2025-01-05"}, timeout=20)
        except Exception:  # noqa: BLE001
            return None
        h = resp.headers.get("WWW-Authenticate", "")
        m = re.search(r"authorization_uri=\"?https://[^/]+/([0-9a-fA-F-]{36})", h)
        return m.group(1) if m else None


class ClientFactory:
    """BlobServiceClient を作り、使い回します（5.6.2: 作ったクライアントを使い回します）。"""

    def __init__(self, *, settings, session: Callable[[], object], auth: AuthService | None, arm, secrets: SecretResolver,
                 tenants: TenantResolver) -> None:
        self.settings = settings
        self._session = session
        self.auth = auth
        self.arm = arm
        self.secrets = secrets
        self.tenants = tenants
        self._clients: dict[tuple, object] = {}
        self._lock = threading.Lock()

    def reset(self) -> None:
        with self._lock:
            self._clients.clear()

    def _kwargs(self) -> dict:
        from azure.core.pipeline.transport import RequestsTransport
        s = self.settings
        chunk = int(s.get("transfer.chunk_size_mib")) * 1024 * 1024
        return {
            "transport": RequestsTransport(session=self._session(), session_owner=False,
                                           connection_timeout=s.get("transfer.connect_timeout"),
                                           read_timeout=s.get("transfer.read_timeout")),
            "retry_total": s.get("transfer.retry_total"),
            "max_single_get_size": max(chunk, 64 * 1024 * 1024),
            "max_chunk_get_size": max(chunk, 64 * 1024 * 1024),
            "user_agent": f"finfet-abs/{__version__}",
        }

    def service(self, account: AccountRef, container: str | None = None):
        from azure.core.credentials import AzureNamedKeyCredential, AzureSasCredential
        from azure.storage.blob import BlobServiceClient
        a = account.auth
        user = None
        if a.kind in ("entra_user", "entra_key") and self.auth is not None and hasattr(self.auth, "resolve"):
            # 使うアカウントを決めてからキーにします（既定のアカウントを変えたら、別のクライアントにするため）
            user = self.auth.resolve(a.account)
        key = (account.account, account.endpoint_suffix, a.kind, a.credential_id, a.secret_id, a.tenant_id, user)
        with self._lock:
            c = self._clients.get(key)
        if c is not None:
            return c
        url = account.blob_endpoint
        if a.kind == "entra_user":
            if self.auth is None:
                raise BackendError("NOT_SIGNED_IN", "サインインしていません。")
            tid = self.tenants.resolve(account, container)
            cred = self.auth.credential(tid, user)
        elif a.kind == "entra_key":
            cred = AzureNamedKeyCredential(account.account, arm_for(self.arm, user).get_account_key(account.account).reveal())
        elif a.kind in ("account_key", "sas"):
            if a.secret_id:
                secret = self.secrets.job_secret(a.secret_id)
                source = "sas_url"
            elif a.credential_id:
                source, secret = self.secrets.credential(a.credential_id)
            else:
                raise BackendError("NO_CREDENTIAL", "認証情報がありません。")
            value = secret.reveal()
            if source == "connection_string":
                parts = connection_parts(value)
                cred = AzureNamedKeyCredential(account.account, parts["account_key"]) if parts["account_key"] \
                    else AzureSasCredential(parts["sas"].lstrip("?"))
            elif source == "sas_url":
                cred = AzureSasCredential(value.split("?", 1)[1] if "?" in value else value)
            else:
                cred = AzureNamedKeyCredential(account.account, value)
            del value
        else:
            raise BackendError("BAD_AUTH", "認証方式が正しくありません。")
        c = BlobServiceClient(url, credential=cred, **self._kwargs())
        with self._lock:
            self._clients[key] = c
        return c


def _blob_info(p) -> BlobInfo:
    """azure-storage-blob の BlobProperties → BlobInfo。"""
    md5 = None
    settings = getattr(p, "content_settings", None)
    if settings is not None and getattr(settings, "content_md5", None):
        import base64
        raw = settings.content_md5
        md5 = base64.b64encode(bytes(raw)).decode() if not isinstance(raw, str) else raw
    meta = getattr(p, "metadata", None) or {}
    bt = getattr(p, "blob_type", None)
    bt = getattr(bt, "value", bt) or "BlockBlob"
    tier = getattr(p, "blob_tier", None)
    tier = getattr(tier, "value", tier)
    pr = getattr(p, "rehydrate_priority", None)
    return BlobInfo(
        name=p.name, size=int(p.size or 0), blob_type=str(bt), tier=tier, archive_status=getattr(p, "archive_status", None),
        rehydrate_priority=getattr(pr, "value", pr), etag=p.etag, content_md5=md5, creation_time=p.creation_time,
        last_modified=p.last_modified, access_tier_change_time=getattr(p, "blob_tier_change_time", None),
        is_directory=str(meta.get("hdi_isfolder", "")).lower() == "true")


class AzureBackend:
    """Azure の StorageBackend。"""

    def __init__(self, factory: ClientFactory, proxy_config: Callable[[], object] | None = None) -> None:
        self.factory = factory
        self._proxy_config = proxy_config

    def _err(self, exc: BaseException, target: ContainerRef | AccountRef | None) -> BackendError:
        account = target.account.account if isinstance(target, ContainerRef) else (target.account if target else None)
        container = target.container if isinstance(target, ContainerRef) else None
        proxy = None
        if self._proxy_config and account:
            label = proxy_label(resolve_proxy(self._proxy_config(), f"https://{account}.blob.core.windows.net"))
            proxy = label.replace("プロキシ ", "", 1) if label != "直接" else None
        if isinstance(exc, InteractionRequired):
            return BackendError("INTERACTION_REQUIRED", exc.info.message, detail=exc.info.code)
        err = from_exception(exc, account=account, container=container, proxy=proxy)
        auth = target.account.auth if isinstance(target, ContainerRef) else (target.auth if target else None)
        if err.code == "AuthorizationPermissionMismatch" and auth is not None and auth.kind == "sas":
            err.message = "SAS に、この操作に必要な権限がありません（検索は l、ダウンロードは r、リハイドレートは w が必要です）。"
        return err

    def _container(self, target: ContainerRef):
        return self.factory.service(target.account, target.container).get_container_client(target.container)

    def list_blobs_page(self, target: ContainerRef, prefix: str, continuation: str | None = None,
                        page_size: int = 5000, include_metadata: bool = False) -> ListPage:
        try:
            cc = self._container(target)
            pages = cc.list_blobs(name_starts_with=prefix or None, include=["metadata"] if include_metadata else None,
                                  results_per_page=page_size).by_page(continuation_token=continuation)
            page = next(pages)
            items = [_blob_info(p) for p in page]
            return ListPage(items, pages.continuation_token)
        except StopIteration:
            return ListPage([], None)
        except Exception as exc:  # noqa: BLE001
            raise self._err(exc, target) from None

    def walk_prefixes(self, target: ContainerRef, prefix: str, limit: int = 1000) -> list[str]:
        try:
            out = []
            for item in self._container(target).walk_blobs(name_starts_with=prefix or None, delimiter="/"):
                out.append(item.name[len(prefix):])
                if len(out) >= limit:
                    break
            return out
        except Exception as exc:  # noqa: BLE001
            raise self._err(exc, target) from None

    def list_containers(self, account: AccountRef, prefix: str = "", limit: int = 5000) -> list[str]:
        try:
            out = []
            for c in self.factory.service(account).list_containers(name_starts_with=prefix or None):
                out.append(c.name)
                if len(out) >= limit:
                    break
            return out
        except Exception as exc:  # noqa: BLE001
            raise self._err(exc, account) from None

    def account_info(self, account: AccountRef) -> dict:
        try:
            info = self.factory.service(account).get_account_information()
            return {"is_hns_enabled": bool(info.get("is_hns_enabled")), "sku_name": info.get("sku_name"),
                    "account_kind": info.get("account_kind")}
        except Exception as exc:  # noqa: BLE001
            raise self._err(exc, account) from None

    def get_properties(self, target: ContainerRef, blob_name: str) -> BlobInfo:
        try:
            return _blob_info(self._container(target).get_blob_client(blob_name).get_blob_properties())
        except Exception as exc:  # noqa: BLE001
            raise self._err(exc, target) from None

    def set_tier(self, target: ContainerRef, blob_name: str, tier: str, priority: str) -> None:
        try:
            self._container(target).get_blob_client(blob_name).set_standard_blob_tier(tier, rehydrate_priority=priority)
        except Exception as exc:  # noqa: BLE001
            raise self._err(exc, target) from None

    def download_range(self, target: ContainerRef, blob_name: str, offset: int, length: int,
                       etag: str | None) -> Iterator[bytes]:
        from azure.core import MatchConditions
        try:
            bc = self._container(target).get_blob_client(blob_name)
            kw = {"etag": etag, "match_condition": MatchConditions.IfNotModified} if etag else {}
            stream = bc.download_blob(offset=offset, length=length, max_concurrency=1, **kw)
            for chunk in stream.chunks():
                yield chunk
        except Exception as exc:  # noqa: BLE001
            raise self._err(exc, target) from None

    def test_target(self, target: ContainerRef | AccountRef) -> str:
        """接続テスト（3.1.10）。成功の文を返し、失敗は BackendError。"""
        if isinstance(target, ContainerRef):
            self.list_blobs_page(target, "", None, 1)
            return "一覧を取得できました。"
        self.account_info(target)
        return "ストレージアカウントに接続できました。"
