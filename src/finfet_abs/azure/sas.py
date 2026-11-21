"""SAS（3.1.6）とストレージのドメイン（3.1.3.5）の解析。"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from urllib.parse import parse_qs, unquote, urlsplit

from ..core.clock import from_iso, to_iso

DEFAULT_SUFFIX = "core.windows.net"
ACCOUNT_RE = re.compile(r"^[a-z0-9]{3,24}$")
_LABEL = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?$")


class ParseError(ValueError):
    """入力の誤り（画面に表示する文）。"""


def validate_suffix(text: str) -> str:
    """ストレージのドメイン（「その他」）を確かめ、小文字にして返します。"""
    s = (text or "").strip().lower()
    if not s:
        raise ParseError("ドメインを入力してください。")
    if "://" in s or "/" in s or ":" in s or "?" in s or "@" in s:
        raise ParseError("ドメインには、スキーム・パス・ポートを含めずに入力してください（例: core.chinacloudapi.cn）。")
    labels = s.split(".")
    if len(labels) < 2 or not all(_LABEL.match(x) for x in labels):
        raise ParseError("ドメインの形式が正しくありません（例: core.chinacloudapi.cn）。")
    return s


def validate_account(name: str) -> str:
    """ストレージアカウント名（3〜24 文字の英小文字と数字）。"""
    n = (name or "").strip()
    if not ACCOUNT_RE.match(n):
        raise ParseError("ストレージアカウント名は、3〜24 文字の英小文字と数字です。")
    return n


def parse_blob_host(host: str) -> tuple[str, str, bool]:
    """`<アカウント>.blob.<ドメイン>` を (アカウント, ドメイン, dfs から読み替えたか) にします。"""
    h = (host or "").lower()
    parts = h.split(".")
    if len(parts) < 4 or parts[1] not in ("blob", "dfs"):
        raise ParseError("URL のホスト名が「<ストレージアカウント>.blob.<ドメイン>」の形ではありません（カスタムドメインは使えません）。")
    account = validate_account(parts[0])
    suffix = validate_suffix(".".join(parts[2:]))
    return account, suffix, parts[1] == "dfs"


PERM_LABELS = {"r": "読み取り", "l": "一覧", "w": "書き込み"}


@dataclass
class SasInfo:
    """SAS の解析の結果（秘密情報は含みません）。"""

    kind: str  # account / service / user_delegation
    permissions: str = ""
    resource: str | None = None  # sr
    resource_types: str | None = None  # srt
    services: str | None = None  # ss
    start: datetime | None = None
    expiry: datetime | None = None
    policy_id: str | None = None  # si
    ip: str | None = None  # sip
    protocol: str | None = None  # spr
    version: str | None = None

    @property
    def scope(self) -> str:
        """範囲: account / container / blob / directory。"""
        if self.kind == "account":
            return "account"
        return {"c": "container", "b": "blob", "bs": "blob", "bv": "blob", "d": "directory"}.get(self.resource or "", "blob")

    def allows(self, op: str) -> bool | None:
        """処理に必要な権限があるか（3.1.6.3）。保存されたアクセスポリシーで分からない場合は None。"""
        if self.policy_id and not self.permissions:
            return None
        p = set(self.permissions)
        if op == "list_blobs":
            return "l" in p and (self.resource == "c" or (self.kind == "account" and "c" in (self.resource_types or "")))
        if op == "list_containers":
            return "l" in p and self.kind == "account" and "s" in (self.resource_types or "")
        if op in ("read", "properties"):
            return "r" in p and (self.kind != "account" or "o" in (self.resource_types or ""))
        if op == "set_tier":
            return "w" in p and (self.kind != "account" or "o" in (self.resource_types or ""))
        return None

    def expired(self, now: datetime) -> bool:
        return self.expiry is not None and self.expiry <= now

    def expires_within(self, now: datetime, hours: float) -> bool:
        return self.expiry is not None and now < self.expiry <= now + timedelta(hours=hours)

    def missing(self, ops: list[str]) -> list[str]:
        return [op for op in ops if self.allows(op) is False]

    def to_json(self) -> dict:
        return {
            "kind": self.kind, "scope": self.scope, "permissions": self.permissions,
            "permission_labels": [PERM_LABELS[c] for c in "rlw" if c in self.permissions],
            "start": to_iso(self.start), "expiry": to_iso(self.expiry), "policy": bool(self.policy_id),
            "ip_restricted": bool(self.ip), "protocol": self.protocol,
            "can_list_blobs": self.allows("list_blobs"), "can_list_containers": self.allows("list_containers"),
            "can_read": self.allows("read"), "can_set_tier": self.allows("set_tier"),
        }


def _parse_time(v: str | None) -> datetime | None:
    if not v:
        return None
    try:
        dt = from_iso(v if len(v) > 10 else v + "T00:00:00Z")
    except ValueError:
        raise ParseError("SAS の有効期間（st・se）の形式が正しくありません。") from None
    return dt


def parse_sas_token(token: str) -> SasInfo:
    """SAS のクエリ文字列を解析します。"""
    q = {k: v[0] for k, v in parse_qs((token or "").lstrip("?"), keep_blank_values=True).items()}
    if "sig" not in q:
        raise ParseError("SAS が含まれていません（sig がありません）。")
    if "skoid" in q:
        kind = "user_delegation"
    elif "ss" in q or "srt" in q:
        kind = "account"
    elif "sr" in q:
        kind = "service"
    else:
        raise ParseError("SAS の種類を判定できません（sr・srt がありません）。")
    if kind == "account" and "b" not in q.get("ss", "b"):
        raise ParseError("この SAS は Blob サービスに使えません（ss に b がありません）。")
    return SasInfo(kind=kind, permissions=q.get("sp", ""), resource=q.get("sr"), resource_types=q.get("srt"),
                   services=q.get("ss"), start=_parse_time(q.get("st")), expiry=_parse_time(q.get("se")),
                   policy_id=q.get("si"), ip=q.get("sip"), protocol=q.get("spr"), version=q.get("sv"))


@dataclass
class SasUrl:
    """SAS URL の解析の結果。token は秘密情報です（画面・ログに出しません）。"""

    account: str
    suffix: str
    container: str | None
    blob: str | None
    token: str
    info: SasInfo
    from_dfs: bool = False
    warnings: list[str] = field(default_factory=list)

    @property
    def level(self) -> str:
        """URL の範囲: service / container / blob。"""
        if self.blob:
            return "blob"
        if self.container:
            return "container"
        return "service"

    def public_json(self) -> dict:
        return {"account": self.account, "endpoint_suffix": self.suffix, "container": self.container,
                "blob": self.blob, "level": self.level, "from_dfs": self.from_dfs, "sas": self.info.to_json(),
                "warnings": self.warnings}


def split_blob_url(url: str) -> tuple[str, str, str | None, str | None, str, bool]:
    """Blob の URL を (アカウント, ドメイン, コンテナー, Blob 名, クエリ, dfs か) に分けます。パスはデコードします（7.4 の I1）。"""
    text = (url or "").strip()
    try:
        parts = urlsplit(text)
    except ValueError:
        raise ParseError("URL の形式が正しくありません。") from None
    if parts.scheme.lower() == "http":
        raise ParseError("HTTP の URL は使えません。HTTPS の URL を指定してください。")
    if parts.scheme.lower() != "https" or not parts.hostname:
        raise ParseError("https:// で始まる URL を指定してください。")
    if parts.port not in (None, 443):
        raise ParseError("ポートを指定した URL は使えません。")
    account, suffix, from_dfs = parse_blob_host(parts.hostname)
    path = parts.path.lstrip("/")
    container = None
    blob = None
    if path:
        seg = path.split("/", 1)
        container = unquote(seg[0]) or None
        if len(seg) > 1 and seg[1]:
            blob = unquote(seg[1])
    return account, suffix, container, blob, parts.query, from_dfs


def parse_sas_url(url: str) -> SasUrl:
    """SAS URL を解析します（3.1.6.1）。"""
    account, suffix, container, blob, query, from_dfs = split_blob_url(url)
    info = parse_sas_token(query)
    out = SasUrl(account, suffix, container, blob, query, info, from_dfs)
    if from_dfs:
        out.warnings.append("dfs の URL を blob の URL に読み替えました。")
    if info.kind == "service" and info.resource == "c" and not container:
        raise ParseError("コンテナーの SAS ですが、URL にコンテナー名がありません。")
    return out


def check_expiry(info: SasInfo, now: datetime | None = None) -> None:
    """期限が切れた SAS は使えません。"""
    now = now or datetime.now(timezone.utc)
    if info.expired(now):
        raise ParseError("SAS の有効期限が切れています。")
