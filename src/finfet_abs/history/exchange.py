"""検索条件のエクスポートとインポート（3.5.4、5.5）。"""

from __future__ import annotations

import json
import logging
import re
import secrets
import threading
import time

from ..azure.credstore import CredentialStore, new_from_input
from ..core import dpapi
from ..core.clock import SYSTEM_CLOCK, Clock, to_iso
from ..core.errors import AppError
from ..version import __version__
from .condition import clean_condition, targets_summary, variables_summary

log = logging.getLogger(__name__)

FORMAT = "finfet-abs-search"
SCHEMA_VERSION = 1
MAX_FILE = 1024 * 1024
INCLUDES = ("condition", "targets", "credentials")
PREVIEW_TTL = 600.0

_SECRETISH = re.compile(r"(sig=[^&\s\"']+|AccountKey=[^;\s\"']+|SharedAccessSignature=[^;\s\"']+|"
                        r"eyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]*)", re.IGNORECASE)


def export_file(conditions: list[dict], includes: str, credstore: CredentialStore, *, clock: Clock = SYSTEM_CLOCK,
                count_only: bool = False) -> tuple[dict, dict]:
    """エクスポートのファイルの内容と、認証情報の件数（含めた・含められない）を返します。"""
    if includes not in INCLUDES:
        raise AppError("BAD_INCLUDES", "含める情報を選択してください。")
    out = []
    included = 0
    not_included = 0
    for cond in conditions:
        c = clean_condition(cond, keep_credential_ids=True)
        if includes == "condition":
            c["targets"] = []
        for t in c["targets"]:
            cid = t.pop("credential_id", None)
            if includes != "credentials":
                continue
            if t.get("auth_kind") in ("entra_user", "entra_key") or not cid:
                if t.get("auth_kind") not in ("entra_user", "entra_key"):
                    not_included += 1
                continue
            row = credstore.get(cid)
            if row is None:
                not_included += 1
                continue
            try:
                secret = dpapi.unseal(row["secret"], "credential")
            except dpapi.DecryptError:
                not_included += 1
                continue
            t["credential"] = {"source": row["source"], "value": secret.reveal()}
            included += 1
        out.append(c)
    data = {"format": FORMAT, "schema_version": SCHEMA_VERSION, "app_version": __version__, "includes": includes,
            "exported_at": to_iso(clock.now()), "searches": out}
    if includes == "credentials" and not count_only:
        log.info("認証情報を含めてエクスポートしました（%s 件）", included)
    return data, {"included": included, "not_included": not_included}


def credential_counts(conditions: list[dict], credstore: CredentialStore) -> dict:
    """エクスポートのダイアログに出す、含める・含められない認証情報の件数。"""
    _data, counts = export_file(conditions, "credentials", credstore, count_only=True) if conditions else ({}, {"included": 0, "not_included": 0})
    entra = sum(1 for c in conditions for t in c.get("targets") or [] if t.get("auth_kind") in ("entra_user", "entra_key"))
    return {**counts, "entra": entra}


def _strip_secrets(obj, path: str, warnings: list[str]):
    """認証情報の項目以外にある、秘密情報らしい値を取り除きます。"""
    if isinstance(obj, dict):
        return {k: _strip_secrets(v, f"{path}.{k}", warnings) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_strip_secrets(v, f"{path}[]", warnings) for v in obj]
    if isinstance(obj, str) and _SECRETISH.search(obj):
        warnings.append("秘密情報と思われる値が認証情報の項目以外に含まれていたため、削除しました。")
        return _SECRETISH.sub("", obj)
    return obj


class ImportSession:
    """インポートの確認の間だけ、読み込んだ内容をメモリに保持します（認証情報を画面に返さないため）。"""

    def __init__(self) -> None:
        self._items: dict[str, tuple[float, dict]] = {}
        self._lock = threading.Lock()

    def put(self, data: dict) -> str:
        token = secrets.token_urlsafe(16)
        with self._lock:
            now = time.monotonic()
            self._items = {k: v for k, v in self._items.items() if now - v[0] < PREVIEW_TTL}
            self._items[token] = (now, data)
        return token

    def get(self, token: str) -> dict:
        with self._lock:
            v = self._items.get(token)
        if v is None:
            raise AppError("IMPORT_EXPIRED", "ファイルの読み込みから時間が経過しすぎています。もう一度ファイルを選択してください。")
        return v[1]

    def drop(self, token: str) -> None:
        with self._lock:
            self._items.pop(token, None)


def parse_import(raw: bytes, ctx) -> dict:
    """インポートのファイルを検査します（3.5.4.2）。"""
    from .condition import check_compile
    if len(raw) > MAX_FILE:
        raise AppError("IMPORT_TOO_LARGE", "ファイルのサイズが上限（1 MB）を超えています。")
    try:
        data = json.loads(raw.decode("utf-8-sig"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        raise AppError("IMPORT_INVALID", "ファイルの形式が正しくありません（JSON ではありません）。") from None
    if not isinstance(data, dict) or data.get("format") != FORMAT:
        raise AppError("IMPORT_INVALID", "本ツールの検索条件のファイルではありません。")
    ver = data.get("schema_version")
    if not isinstance(ver, int):
        raise AppError("IMPORT_INVALID", "ファイルの形式の版を判別できません。")
    if ver > SCHEMA_VERSION:
        raise AppError("IMPORT_TOO_NEW", "新しいバージョンの本ツールで作成されたファイルです。本ツールを更新してから読み込んでください。")
    warnings: list[str] = []
    items = []
    creds = 0
    for i, s in enumerate(data.get("searches") or []):
        if not isinstance(s, dict):
            continue
        targets = s.get("targets") or []
        cred_parts = [t.get("credential") if isinstance(t, dict) else None for t in targets]
        stripped = _strip_secrets({k: v for k, v in s.items() if k != "targets"}, "", warnings)
        stripped["targets"] = []
        for t in targets:
            if isinstance(t, dict):
                tt = {k: v for k, v in t.items() if k not in ("credential", "credential_id")}
                stripped["targets"].append(_strip_secrets(tt, "", warnings))
        try:
            cond = clean_condition(stripped, keep_credential_ids=False)
        except ValueError as exc:
            items.append({"index": i, "error": str(exc)})
            continue
        error = None
        try:
            check_compile(cond, ctx)
        except AppError as exc:
            error = exc.extra.get("issues", [{}])[0].get("message") if exc.extra else exc.message
        for t, c in zip(cond["targets"], cred_parts):
            if isinstance(c, dict) and c.get("source") in ("connection_string", "sas_url", "account_key") and c.get("value"):
                t["credential"] = {"source": c["source"], "value": str(c["value"])[:8192]}
                creds += 1
        items.append({"index": i, "condition": cond, "error": error})
    if not items:
        raise AppError("IMPORT_EMPTY", "ファイルに検索条件が含まれていません。")
    return {"items": items, "credentials": creds, "warnings": sorted(set(warnings))}


def preview_json(parsed: dict) -> dict:
    """画面に返す確認の内容（認証情報の値は返しません）。"""
    out = []
    for it in parsed["items"]:
        if "condition" not in it:
            out.append({"index": it["index"], "error": it["error"]})
            continue
        c = it["condition"]
        out.append({"index": it["index"], "name": c.get("name"), "path_pattern": c["path_pattern"],
                    "targets_summary": targets_summary(c), "variables_summary": variables_summary(c), "error": it["error"],
                    "credentials": sum(1 for t in c["targets"] if t.get("credential"))})
    return {"items": out, "credentials": parsed["credentials"], "warnings": parsed["warnings"]}


def commit_credentials(parsed: dict, credstore: CredentialStore, *, decisions: dict[str, bool],
                       allow_development: bool = False) -> tuple[list[dict], list[dict], list[str]]:
    """ファイルの認証情報を保存し、接続先に保存した認証情報の ID を付けます。

    戻り値: (検索条件の一覧, 置き換えるかの確認が必要なもの, 誤り)。
    """
    conflicts: list[dict] = []
    errors: list[str] = []
    conds = []
    for it in parsed["items"]:
        if "condition" not in it:
            continue
        c = json.loads(json.dumps(it["condition"]))
        for t in c["targets"]:
            cred = t.pop("credential", None)
            if not cred:
                continue
            try:
                nc, _info = new_from_input(cred["source"], value=cred.get("value", ""), account=t["storage_account"],
                                           suffix=t.get("endpoint_suffix") or "core.windows.net",
                                           key=cred.get("value", ""), allow_development=allow_development)
            except Exception as exc:  # noqa: BLE001
                errors.append(f"{t['storage_account']}/{t.get('container', '')}: {exc}")
                continue
            key = f"{nc.account}|{nc.suffix}|{nc.scope_container or ''}"
            existing = credstore.find_same_scope(nc.account, nc.suffix, nc.scope_container)
            if existing is not None:
                try:
                    same = dpapi.unseal(existing["secret"], "credential").reveal() == nc.secret.reveal()
                except dpapi.DecryptError:
                    same = False
                if same:
                    t["credential_id"] = existing["id"]
                    continue
                if key not in decisions:
                    conflicts.append({"key": key, "storage_account": nc.account, "endpoint_suffix": nc.suffix,
                                      "scope_container": nc.scope_container, "existing": credstore.public(existing)})
                    continue
                if not decisions[key]:
                    t["credential_id"] = existing["id"]
                    continue
                cid, _ = credstore.add(nc, replace=True)
            else:
                cid, _ = credstore.add(nc)
            t["credential_id"] = cid
        conds.append(c)
    return conds, conflicts, errors
