"""URL で指定（3.2.9）。Blob の URL を解析し、SAS はメモリにだけ保持します（ジョブを作るときに暗号化して保存します）。"""

from __future__ import annotations

import secrets
import threading
import time

from ..azure.sas import ParseError, parse_sas_token, split_blob_url

MAX_URLS = 10000
TTL = 3600.0


class UrlSessions:
    """解析した URL の一覧（SAS を含む）を、ジョブを作るまでメモリに保持します。"""

    def __init__(self) -> None:
        self._items: dict[str, tuple[float, list[dict]]] = {}
        self._lock = threading.Lock()

    def put(self, rows: list[dict]) -> str:
        token = secrets.token_urlsafe(16)
        with self._lock:
            now = time.monotonic()
            self._items = {k: v for k, v in self._items.items() if now - v[0] < TTL}
            self._items[token] = (now, rows)
        return token

    def get(self, token: str) -> list[dict] | None:
        with self._lock:
            v = self._items.get(token)
        return v[1] if v else None


def parse_urls(text: str, now) -> tuple[list[dict], list[dict]]:
    """1行に1つの URL を解析します。戻り値は (行の一覧（SAS を含む）, 画面に返す行の一覧（SAS を含まない）)。"""
    lines = [ln.strip() for ln in (text or "").splitlines() if ln.strip()]
    if len(lines) > MAX_URLS:
        raise ParseError(f"URL が多すぎます（{len(lines):,} 行。上限 {MAX_URLS:,} 行）。")
    rows: list[dict] = []
    seen: set[tuple] = set()
    for i, line in enumerate(lines):
        row = {"line": i + 1, "url": line}
        try:
            account, suffix, container, blob, query, from_dfs = split_blob_url(line)
            if not container or not blob:
                raise ParseError("Blob の URL ではありません（コンテナー名と Blob 名が必要です）。")
            row.update(storage_account=account, endpoint_suffix=suffix, container=container, blob_name=blob)
            if "sig=" in query:
                info = parse_sas_token(query)
                if info.expired(now):
                    raise ParseError("SAS の有効期限が切れています。")
                if info.allows("read") is False:
                    raise ParseError("SAS に読み取り（r）の権限がありません。")
                row["sas"] = info.to_json()
                row["sas_token"] = query
            key = (account, suffix, container, blob)
            if key in seen:
                row["duplicate"] = True
            seen.add(key)
        except ParseError as exc:
            row["error"] = str(exc)
        rows.append(row)
    public = [{k: v for k, v in r.items() if k not in ("sas_token", "url")} | {"url": _strip_query(r["url"])} for r in rows]
    return rows, public


def _strip_query(url: str) -> str:
    return url.split("?", 1)[0]
