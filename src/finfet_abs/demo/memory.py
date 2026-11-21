"""メモリ上の架空の `StorageBackend`（テストとデモモードで使います。8.3.3）。"""

from __future__ import annotations

import bisect
import hashlib
import threading
from dataclasses import replace
from datetime import datetime, timezone
from typing import Iterator

from ..azure.backend import AccountRef, BackendError, BlobInfo, ContainerRef, ListPage


class MemoryBackend:
    """コンテナーごとに、名前の順の Blob の一覧を持つ架空の実装。要求の回数を数えます。"""

    def __init__(self) -> None:
        self._lock = threading.RLock()
        # (アカウント, ドメイン) → コンテナー名 → (名前の一覧, 名前 → BlobInfo)
        self.data: dict[tuple[str, str], dict[str, tuple[list[str], dict[str, BlobInfo]]]] = {}
        self.requests: list[tuple[str, str, str]] = []  # (操作, コンテナー, prefix)
        self.hns: set[tuple[str, str]] = set()
        self.denied: dict[tuple[str, str, str], str] = {}  # (アカウント, コンテナー, 操作) → エラーコード

    # ------------------------------------------------------------ データの用意

    def add_blobs(self, account: str, container: str, blobs: list[BlobInfo], suffix: str = "core.windows.net") -> None:
        with self._lock:
            conts = self.data.setdefault((account, suffix), {})
            names, by_name = conts.setdefault(container, ([], {}))
            for b in blobs:
                if b.name not in by_name:
                    bisect.insort(names, b.name)
                by_name[b.name] = b

    def add_container(self, account: str, container: str, suffix: str = "core.windows.net") -> None:
        with self._lock:
            self.data.setdefault((account, suffix), {}).setdefault(container, ([], {}))

    def _container(self, target: ContainerRef):
        conts = self.data.get((target.account.account, target.account.endpoint_suffix))
        if conts is None:
            raise BackendError("ResourceNotFound", "ストレージアカウントが見つかりません。", status=404)
        c = conts.get(target.container)
        if c is None:
            raise BackendError("ContainerNotFound", f"コンテナー「{target.container}」が見つかりません。", status=404)
        return c

    def _check(self, target: ContainerRef, op: str) -> None:
        code = self.denied.get((target.account.account, target.container, op))
        if code:
            raise BackendError(code, code, status=403)

    # ------------------------------------------------------------ StorageBackend

    def list_blobs_page(self, target: ContainerRef, prefix: str, continuation: str | None = None,
                        page_size: int = 5000, include_metadata: bool = False) -> ListPage:
        with self._lock:
            self.requests.append(("list", target.container, prefix))
            self._check(target, "list")
            names, by_name = self._container(target)
            start = int(continuation) if continuation else bisect.bisect_left(names, prefix)
            out = []
            i = start
            while i < len(names) and len(out) < page_size:
                n = names[i]
                if not n.startswith(prefix):
                    break
                out.append(replace(by_name[n]))
                i += 1
            more = i < len(names) and names[i].startswith(prefix)
            return ListPage(out, str(i) if more else None)

    def walk_prefixes(self, target: ContainerRef, prefix: str, limit: int = 1000) -> list[str]:
        with self._lock:
            self.requests.append(("walk", target.container, prefix))
            names, _ = self._container(target)
            out: list[str] = []
            i = bisect.bisect_left(names, prefix)
            while i < len(names) and names[i].startswith(prefix) and len(out) < limit:
                rest = names[i][len(prefix):]
                head = rest.split("/", 1)[0]
                item = head + ("/" if "/" in rest else "")
                if not out or out[-1] != item:
                    out.append(item)
                i += 1
            return out

    def list_containers(self, account: AccountRef, prefix: str = "", limit: int = 5000) -> list[str]:
        with self._lock:
            self.requests.append(("containers", "", prefix))
            conts = self.data.get((account.account, account.endpoint_suffix))
            if conts is None:
                raise BackendError("ResourceNotFound", "ストレージアカウントが見つかりません。", status=404)
            # $logs はコンテナーの一覧に現れません（K16）
            return sorted(c for c in conts if c.startswith(prefix) and c != "$logs")[:limit]

    def account_info(self, account: AccountRef) -> dict:
        return {"is_hns_enabled": (account.account, account.endpoint_suffix) in self.hns, "sku_name": "Standard_LRS",
                "account_kind": "StorageV2"}

    def get_properties(self, target: ContainerRef, blob_name: str) -> BlobInfo:
        with self._lock:
            self.requests.append(("props", target.container, blob_name))
            self._check(target, "read")
            _names, by_name = self._container(target)
            b = by_name.get(blob_name)
            if b is None:
                raise BackendError("BlobNotFound", "Blob が見つかりません。", status=404)
            return replace(b)

    def set_tier(self, target: ContainerRef, blob_name: str, tier: str, priority: str) -> None:
        with self._lock:
            self.requests.append(("tier", target.container, blob_name))
            self._check(target, "write")
            _names, by_name = self._container(target)
            b = by_name.get(blob_name)
            if b is None:
                raise BackendError("BlobNotFound", "Blob が見つかりません。", status=404)
            if b.archive_status:
                if priority == "High" and b.rehydrate_priority != "High":
                    by_name[blob_name] = replace(b, rehydrate_priority="High")
                    return
                raise BackendError("BlobBeingRehydrated", "すでにリハイドレート中です。", status=409)
            if b.tier == "Archive":
                by_name[blob_name] = replace(b, archive_status=f"rehydrate-pending-to-{tier.lower()}",
                                             rehydrate_priority=priority)
            else:
                by_name[blob_name] = replace(b, tier=tier, access_tier_change_time=datetime.now(timezone.utc))

    def complete_rehydration(self, account: str, container: str, blob_name: str, when: datetime | None = None,
                             suffix: str = "core.windows.net") -> None:
        """テスト用: リハイドレートを完了させます。"""
        with self._lock:
            _names, by_name = self.data[(account, suffix)][container]
            b = by_name[blob_name]
            target = (b.archive_status or "rehydrate-pending-to-hot").rsplit("-", 1)[1].capitalize()
            by_name[blob_name] = replace(b, tier=target, archive_status=None, rehydrate_priority=None,
                                         access_tier_change_time=when or datetime.now(timezone.utc))

    def download_range(self, target: ContainerRef, blob_name: str, offset: int, length: int,
                       etag: str | None) -> Iterator[bytes]:
        with self._lock:
            self.requests.append(("get", target.container, blob_name))
            self._check(target, "read")
            _names, by_name = self._container(target)
            b = by_name.get(blob_name)
            if b is None:
                raise BackendError("BlobNotFound", "Blob が見つかりません。", status=404)
            if b.tier == "Archive":
                raise BackendError("BlobArchived", "Archive 層のため、ダウンロードできません。", status=409)
            if etag and b.etag and etag != b.etag:
                raise BackendError("ConditionNotMet", "Blob が更新されました。", status=412)
            size = b.size
        end = min(size, offset + length)
        yield blob_bytes(blob_name, offset, end - offset)


def blob_bytes(name: str, offset: int, length: int) -> bytes:
    """Blob の内容（名前から決まる架空のデータ）。"""
    if length <= 0:
        return b""
    seed = hashlib.sha256(name.encode("utf-8")).digest()
    block = (seed * (4096 // len(seed) + 1))[:4096]
    start = offset % len(block)
    reps = (start + length) // len(block) + 1
    return (block * reps)[start:start + length]


def blob_md5(name: str, size: int) -> str:
    """架空のデータの Content-MD5（Base64）。"""
    import base64
    h = hashlib.md5()  # noqa: S324（Azure の Content-MD5 の照合に使う）
    pos = 0
    while pos < size:
        n = min(8 * 1024 * 1024, size - pos)
        h.update(blob_bytes(name, pos, n))
        pos += n
    return base64.b64encode(h.digest()).decode()
