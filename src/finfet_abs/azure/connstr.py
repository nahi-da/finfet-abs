"""接続文字列（3.1.7）とアカウントキーの解析。"""

from __future__ import annotations

import base64
import binascii
from dataclasses import dataclass
from urllib.parse import urlsplit

from .sas import DEFAULT_SUFFIX, ParseError, SasInfo, parse_blob_host, parse_sas_token, validate_account, validate_suffix


@dataclass
class ConnInfo:
    """接続文字列の解析の結果（秘密情報は含みません）。"""

    kind: str  # account_key / sas / development
    account: str
    suffix: str
    blob_endpoint: str | None
    sas: SasInfo | None = None

    def public_json(self) -> dict:
        return {"kind": self.kind, "account": self.account, "endpoint_suffix": self.suffix,
                "sas": self.sas.to_json() if self.sas else None, "level": "service"}


def split_pairs(text: str) -> dict[str, str]:
    """`キー=値;` の並びを分けます（値に = を含むことがあるため、最初の = で分けます）。キーは大文字・小文字を区別しません。"""
    out: dict[str, str] = {}
    for part in (text or "").strip().split(";"):
        part = part.strip()
        if not part:
            continue
        if "=" not in part:
            raise ParseError("接続文字列の形式が正しくありません（「キー=値;」の並びではありません）。")
        k, v = part.split("=", 1)
        out[k.strip().lower()] = v.strip()
    return out


def validate_account_key(key: str) -> str:
    """アカウントキー（Base64 として解釈できること）。"""
    k = (key or "").strip()
    try:
        raw = base64.b64decode(k, validate=True)
    except (binascii.Error, ValueError):
        raise ParseError("アカウントキーの形式が正しくありません（Base64 として解釈できません）。") from None
    if len(raw) < 16:
        raise ParseError("アカウントキーの形式が正しくありません（短すぎます）。")
    return k


def parse_connection_string(text: str, *, allow_development: bool = False) -> ConnInfo:
    """接続文字列を解析します。"""
    pairs = split_pairs(text)
    if pairs.get("usedevelopmentstorage", "").lower() == "true":
        if not allow_development:
            raise ParseError("開発用のストレージ（UseDevelopmentStorage=true）は使用できません。")
        return ConnInfo("development", "devstoreaccount1", "localhost", "http://127.0.0.1:10000/devstoreaccount1")
    if pairs.get("defaultendpointsprotocol", "https").lower() != "https":
        raise ParseError("HTTP の接続文字列は使用できません（DefaultEndpointsProtocol=https を指定してください）。")
    endpoint = pairs.get("blobendpoint")
    account = pairs.get("accountname")
    suffix = pairs.get("endpointsuffix")
    if suffix:
        suffix = validate_suffix(suffix)
    if endpoint:
        parts = urlsplit(endpoint)
        if parts.scheme.lower() != "https":
            raise ParseError("BlobEndpoint が HTTPS ではありません。HTTP の接続文字列は使用できません。")
        ep_account, ep_suffix, _dfs = parse_blob_host(parts.hostname or "")
        if account and account.lower() != ep_account:
            raise ParseError("AccountName と BlobEndpoint のストレージアカウントが一致しません。")
        account = ep_account
        suffix = ep_suffix
    if not account:
        raise ParseError("ストレージアカウント名を特定できません（AccountName または BlobEndpoint が必要です）。")
    account = validate_account(account.lower())
    suffix = suffix or DEFAULT_SUFFIX
    blob_endpoint = f"https://{account}.blob.{suffix}"
    if pairs.get("accountkey"):
        validate_account_key(pairs["accountkey"])
        return ConnInfo("account_key", account, suffix, blob_endpoint)
    if pairs.get("sharedaccesssignature"):
        return ConnInfo("sas", account, suffix, blob_endpoint, parse_sas_token(pairs["sharedaccesssignature"]))
    raise ParseError("接続文字列に AccountKey と SharedAccessSignature のいずれも含まれていません。")


def connection_parts(text: str) -> dict[str, str]:
    """接続文字列から、クライアントを作るための値（アカウントキーまたは SAS）を取り出します。"""
    pairs = split_pairs(text)
    return {"account_key": pairs.get("accountkey", ""), "sas": pairs.get("sharedaccesssignature", "")}
