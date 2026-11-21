"""`StorageBackend` のインターフェース（2.9）。

`search/` と `jobs/` は、このインターフェースだけを使います。実際の Azure の実装と、
テスト・デモモードの架空の実装（demo/）があります。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Iterator, Protocol

DEFAULT_SUFFIX = "core.windows.net"


@dataclass(frozen=True)
class AuthRef:
    """使う認証（5.5.3 の auth_kind と、保存した認証情報・ジョブの SAS の ID、テナント）。"""

    kind: str  # entra_user / entra_key / account_key / sas
    credential_id: str | None = None
    secret_id: str | None = None
    tenant_id: str | None = None
    account: str | None = None  # Entra ID のユーザーアカウント（ユーザー名の小文字。None は既定のアカウント）


@dataclass(frozen=True)
class AccountRef:
    """ストレージアカウント。"""

    account: str
    endpoint_suffix: str = DEFAULT_SUFFIX
    auth: AuthRef = field(default_factory=lambda: AuthRef("entra_user"))

    @property
    def blob_endpoint(self) -> str:
        return f"https://{self.account}.blob.{self.endpoint_suffix}"


@dataclass(frozen=True)
class ContainerRef:
    """コンテナー（接続先）。"""

    account: AccountRef
    container: str

    @property
    def key(self) -> tuple[str, str, str]:
        return (self.account.account, self.account.endpoint_suffix, self.container)

    def url(self, blob_name: str = "") -> str:
        from urllib.parse import quote
        base = f"{self.account.blob_endpoint}/{quote(self.container)}"
        return f"{base}/{quote(blob_name)}" if blob_name else base


@dataclass
class BlobInfo:
    """Blob の情報（一覧・情報の取得の結果）。"""

    name: str
    size: int = 0
    blob_type: str = "BlockBlob"  # BlockBlob / AppendBlob / PageBlob
    tier: str | None = None  # Hot / Cool / Cold / Archive / P4… / None
    archive_status: str | None = None  # rehydrate-pending-to-hot など
    rehydrate_priority: str | None = None  # Standard / High
    etag: str | None = None
    content_md5: str | None = None  # Base64
    creation_time: datetime | None = None
    last_modified: datetime | None = None
    access_tier_change_time: datetime | None = None
    is_directory: bool = False  # 階層型名前空間のディレクトリ（hdi_isfolder=true）


@dataclass
class ListPage:
    """一覧の1ページ。"""

    items: list[BlobInfo]
    continuation: str | None = None


class StorageBackend(Protocol):
    """Azure の Blob の操作。"""

    def list_blobs_page(self, target: ContainerRef, prefix: str, continuation: str | None = None,
                        page_size: int = 5000, include_metadata: bool = False) -> ListPage:
        """Blob の一覧の1ページ（名前の順）。"""

    def walk_prefixes(self, target: ContainerRef, prefix: str, limit: int = 1000) -> list[str]:
        """prefix の下の階層の名前（`delimiter='/'`）。"""

    def list_containers(self, account: AccountRef, prefix: str = "", limit: int = 5000) -> list[str]:
        """コンテナーの一覧。"""

    def account_info(self, account: AccountRef) -> dict:
        """ストレージアカウントの情報（`is_hns_enabled` など）。"""

    def get_properties(self, target: ContainerRef, blob_name: str) -> BlobInfo:
        """Blob の情報。"""

    def set_tier(self, target: ContainerRef, blob_name: str, tier: str, priority: str) -> None:
        """層の変更（リハイドレートの要求）。"""

    def download_range(self, target: ContainerRef, blob_name: str, offset: int, length: int,
                       etag: str | None) -> Iterator[bytes]:
        """範囲を指定した取得。etag が変わっていれば ConditionNotMet。"""


class BackendError(Exception):
    """Azure の操作の失敗（3.7 の文言に変える元）。"""

    def __init__(self, code: str, message: str = "", *, status: int | None = None, retry_after: float | None = None,
                 request_id: str | None = None, detail: str = "") -> None:
        super().__init__(message or code)
        self.code = code
        self.message = message or code
        self.status = status
        self.retry_after = retry_after
        self.request_id = request_id
        self.detail = detail
