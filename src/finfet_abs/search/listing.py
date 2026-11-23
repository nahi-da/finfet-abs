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
