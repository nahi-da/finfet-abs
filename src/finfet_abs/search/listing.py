"""一覧の取得戦略とキャッシュ（3.2.8、7.3.4）。

1. prefix の一覧 P と、そのすべてに共通する先頭部分 c を求めます。
2. P が1つなら、それで取得します。
3. 2つ以上なら、まず c で1ページだけ取得します。続きがなければ全件です。
   続きがあれば prefix ごとの取得に切り替え、取得済みのページで全体を含む prefix は取得しません。
"""

from __future__ import annotations

import logging
import os
import threading
from concurrent.futures import ThreadPoolExecutor
from contextlib import nullcontext
from dataclasses import dataclass, field
from typing import Callable

from ..azure.backend import BlobInfo, ContainerRef, StorageBackend
from .prefix import normalize

log = logging.getLogger(__name__)

#: 1 回の要求で取得する件数の既定と上限（List Blobs の maxresults の上限が 5,000。設定 search.page_size。v1.5.0）
PAGE_SIZE = 5000


class Cancelled(Exception):
    """中止された。"""


class LimitReached(Exception):
    """上限件数に達した。"""


def azure_order(name: str) -> bytes:
    """Azure の一覧の名前の順の比較キー。

    Azure は UTF-16 の符号単位の順で返します（実環境で確認。U+10000 以上の文字（絵文字など）は、
    U+E000〜U+FFFF の文字（全角・半角カナなど）より前になり、Python の文字列の比較とは違います）。
    """
    return name.encode("utf-16-be", "surrogatepass")


def covered_by_trial(prefix: str, last: str) -> bool:
    """まとめて取得したページ（最後の名前 last まで）で、prefix の Blob をすべて取得済みか。"""
    return azure_order(prefix) < azure_order(last) and not last.startswith(prefix)


@dataclass
class _Entry:
    prefix: str
    upper: str | None  # None: prefix の全件。文字列: prefix で始まり upper 以下の範囲
    items: list[BlobInfo]


class ListingCache:
    """1回の検索の間だけ、取得済みの一覧を `(ストレージアカウント, コンテナー, prefix)` ごとに保持します。"""

    def __init__(self, max_items: int = 200_000) -> None:
        self._entries: dict[tuple, list[_Entry]] = {}
        self._lock = threading.Lock()
        self._count = 0
        self.max_items = max_items

    def lookup(self, key: tuple, prefix: str) -> list[BlobInfo] | None:
        """prefix の一覧を、取得済みのものから返します。なければ None。"""
        with self._lock:
            for e in self._entries.get(key, []):
                if not prefix.startswith(e.prefix):
                    continue
                if e.upper is None or covered_by_trial(prefix, e.upper):
                    return [b for b in e.items if b.name.startswith(prefix)]
        return None

    def put(self, key: tuple, prefix: str, items: list[BlobInfo], upper: str | None = None) -> None:
        """完全に取得できた一覧を登録します（途中で中止・失敗したものは登録しません）。"""
        with self._lock:
            if self._count + len(items) > self.max_items:
                return
            self._entries.setdefault(key, []).append(_Entry(prefix, upper, list(items)))
            self._count += len(items)


@dataclass
class _Held:
    prefix: str
    upper: str | None
    items: list[BlobInfo]
    fetched: float


class SharedListing:
    """一覧の保持（v1.6.0）: 検索とパスパターンの自動生成で取得した一覧を、メモリにのみ一定時間保持します。

    キーは接続先（ContainerRef.key）に、認証とメタデータの有無を加えたものです。範囲は prefix と、
    途中までしか取得していない場合はその最後の名前（upper）で表します（ListingCache と同じ考え方）。
    """

    def __init__(self, *, ttl: float = 1800.0, max_items: int = 1_000_000, enabled: bool = True,
                 clock: Callable[[], float] | None = None) -> None:
        import time
        self.ttl, self.max_items, self.enabled = ttl, max_items, enabled
        self.clock = clock or time.time
        self._lock = threading.Lock()
        self._entries: dict[tuple, list[_Held]] = {}
        self._count = 0

    def configure(self, *, ttl: float | None = None, max_items: int | None = None, enabled: bool | None = None) -> None:
        with self._lock:
            if ttl is not None:
                self.ttl = ttl
            if max_items is not None:
                self.max_items = max_items
            if enabled is not None:
                self.enabled = enabled
            if not self.enabled:
                self._entries.clear()
                self._count = 0
            self._purge(self.clock())

    def _purge(self, now: float) -> None:
        for key in list(self._entries):
            keep = [e for e in self._entries[key] if now - e.fetched <= self.ttl]
            self._count -= sum(len(e.items) for e in self._entries[key]) - sum(len(e.items) for e in keep)
            if keep:
                self._entries[key] = keep
            else:
                del self._entries[key]
        while self._count > self.max_items and self._entries:
            key, idx = min(((k, i) for k, es in self._entries.items() for i in range(len(es))),
                           key=lambda ki: self._entries[ki[0]][ki[1]].fetched)
            e = self._entries[key].pop(idx)
            self._count -= len(e.items)
            if not self._entries[key]:
                del self._entries[key]

    def lookup(self, key: tuple, prefix: str) -> tuple[list[BlobInfo], float] | None:
        """prefix の一覧と、取得した時刻。保持していなければ None。"""
        if not self.enabled:
            return None
        with self._lock:
            self._purge(self.clock())
            best = None
            for e in self._entries.get(key, []):
                if not prefix.startswith(e.prefix):
                    continue
                if e.upper is None or covered_by_trial(prefix, e.upper):
                    if best is None or e.fetched > best.fetched:
                        best = e
            if best is None:
                return None
            items = best.items if prefix == best.prefix and best.upper is None else \
                [b for b in best.items if b.name.startswith(prefix)]
            return items, best.fetched

    def put(self, key: tuple, prefix: str, items: list[BlobInfo], upper: str | None = None,
            fetched: float | None = None) -> None:
        if not self.enabled or len(items) > self.max_items:
            return
        with self._lock:
            now = self.clock()
            entries = self._entries.setdefault(key, [])
            # 新しい範囲に含まれる古い範囲は捨てます
            if upper is None:
                for e in [e for e in entries if e.prefix.startswith(prefix)]:
                    entries.remove(e)
                    self._count -= len(e.items)
            entries.append(_Held(prefix, upper, list(items), now if fetched is None else fetched))
            self._count += len(items)
            self._purge(now)

    def clear(self, key_head: tuple) -> None:
        """接続先（キーの先頭が key_head のもの）の保持を消します。"""
        with self._lock:
            for k in [k for k in self._entries if k[:len(key_head)] == key_head]:
                self._count -= sum(len(e.items) for e in self._entries[k])
                del self._entries[k]

    def coverage(self, key: tuple, prefixes: list[str]) -> float | None:
        """prefix の一覧がすべて保持されていれば、そのうち最も古い取得の時刻。"""
        oldest = None
        for p in normalize(prefixes or [""]):
            hit = self.lookup(key, p)
            if hit is None:
                return None
            oldest = hit[1] if oldest is None else min(oldest, hit[1])
        return oldest


class ListingView:
    """1 つの接続先の、保持した一覧の使い方（list_target の cache に渡します）。使った一覧の取得の時刻を記録します。"""

    def __init__(self, shared: SharedListing, extra: tuple) -> None:
        self.shared = shared
        self.extra = extra
        self.reused_at: float | None = None
        self._lock = threading.Lock()

    def lookup(self, key: tuple, prefix: str) -> list[BlobInfo] | None:
        hit = self.shared.lookup(key + self.extra, prefix)
        if hit is None:
            return None
        with self._lock:
            self.reused_at = hit[1] if self.reused_at is None else min(self.reused_at, hit[1])
        return hit[0]

    def put(self, key: tuple, prefix: str, items: list[BlobInfo], upper: str | None = None) -> None:
        self.shared.put(key + self.extra, prefix, items, upper)


@dataclass
class ListStats:
    """接続先ごとの取得の集計。"""

    requests: int = 0
    listed: int = 0
    lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    def add(self, requests: int = 0, listed: int = 0) -> None:
        with self.lock:
            self.requests += requests
            self.listed += listed


def list_target(backend: StorageBackend, target: ContainerRef, prefixes: list[str], *,
                on_items: Callable[[list[BlobInfo]], None], cancel: threading.Event | None = None,
                trial_pages: int = 1, concurrency: int = 4, cache: ListingCache | None = None, page_size: int = PAGE_SIZE,
                stats: ListStats | None = None, include_metadata: bool = False,
                connection: Callable[[], object] | None = None) -> None:
    """1つの接続先（コンテナー）の一覧を取得し、ページごとに on_items に渡します。

    on_items が LimitReached を出したら、取得を止めます。cancel が設定されたら Cancelled を出します。
    connection は、Azure への要求の前に取得する接続数の上限（3.4.2）のコンテキストを返す関数です。
    """
    stats = stats or ListStats()
    cancel = cancel or threading.Event()
    connection = connection or nullcontext
    page_size = max(1, min(PAGE_SIZE, int(page_size)))
    prefixes = normalize(prefixes or [""])
    key = target.key
    emit_lock = threading.Lock()
    stop = threading.Event()

    def emit(items: list[BlobInfo], skip: set[str] | None = None) -> None:
        if skip:
            items = [b for b in items if b.name not in skip]
        if not items:
            return
        with emit_lock:
            if stop.is_set():
                raise LimitReached()
            try:
                on_items(items)
            except LimitReached:
                stop.set()
                raise

    def check() -> None:
        if cancel.is_set():
            raise Cancelled()
        if stop.is_set():
            raise LimitReached()

    def fetch_page(prefix: str, token: str | None):
        check()
        with connection():
            page = backend.list_blobs_page(target, prefix, token, page_size, include_metadata)
        stats.add(1, len(page.items))
        return page

    def fetch_all(prefix: str, skip: set[str] | None = None) -> None:
        cached = cache.lookup(key, prefix) if cache else None
        if cached is not None:
            emit(cached, skip)
            return
        collected: list[BlobInfo] = []
        token = None
        while True:
            page = fetch_page(prefix, token)
            if cache:
                collected.extend(page.items)
            emit(page.items, skip)
            token = page.continuation
            if not token:
                break
        if cache:
            cache.put(key, prefix, collected)

    if len(prefixes) == 1:
        fetch_all(prefixes[0])
        return

    common = os.path.commonprefix(prefixes)
    cached = cache.lookup(key, common) if cache else None
    if cached is not None:
        emit(cached)
        return
    if cache:
        # prefix ごとにすべて保持していれば、要求なしで終わります（一覧の保持。v1.6.0）
        held = [cache.lookup(key, p) for p in prefixes]
        if all(h is not None for h in held):
            for h in held:
                emit(h)
            return
    # まず共通の prefix で、決めたページ数だけ取得します
    trial: list[BlobInfo] = []
    token = None
    for _ in range(max(1, trial_pages)):
        page = fetch_page(common, token)
        trial.extend(page.items)
        emit(page.items)
        token = page.continuation
        if not token:
            break
    if not token:
        if cache:
            cache.put(key, common, trial)
        return
    last = trial[-1].name if trial else common
    if cache:
        cache.put(key, common, trial, upper=last)
    todo = []
    for p in prefixes:
        if covered_by_trial(p, last):
            continue
        skip = {b.name for b in trial if b.name.startswith(p)} if last.startswith(p) else None
        todo.append((p, skip))
    if not todo:
        return
    if concurrency <= 1 or len(todo) == 1:
        for p, skip in todo:
            fetch_all(p, skip)
        return
    with ThreadPoolExecutor(max_workers=min(concurrency, len(todo)), thread_name_prefix="list") as ex:
        futures = [ex.submit(fetch_all, p, skip) for p, skip in todo]
        error = None
        for f in futures:
            try:
                f.result()
            except (Cancelled, LimitReached) as exc:
                stop.set()
                error = error or exc
            except Exception as exc:  # noqa: BLE001
                stop.set()
                error = exc
        if error is not None:
            raise error
