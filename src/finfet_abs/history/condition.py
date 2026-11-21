"""検索条件（5.5.2）の検査・正規化・要約。"""

from __future__ import annotations

import copy
import hashlib
import json
import re

from ..core.errors import AppError
from ..search.variables import NAME_RE

AUTH_KINDS = ("entra_user", "entra_key", "account_key", "sas")
MAX_TARGETS = 100
MAX_PATTERN = 4096

RELATIVE_LABELS = {"today": "今日", "yesterday": "昨日", "this_month": "今月", "last_month": "先月"}


def _str(v, limit: int = 1024, default: str = "") -> str:
    if v is None:
        return default
    if not isinstance(v, str):
        raise ValueError("文字列ではありません")
    if len(v) > limit:
        raise ValueError("長すぎます")
    return v


def clean_condition(raw: dict, *, keep_credential_ids: bool = True, keep_credentials: bool = False) -> dict:
    """検索条件を検査し、知っている項目だけの形にします（知らない項目は無視します）。誤りは ValueError。"""
    if not isinstance(raw, dict):
        raise ValueError("検索条件の形式が正しくありません")
    out: dict = {}
    if raw.get("name") is not None:
        out["name"] = _str(raw.get("name"), 200)
    targets = []
    for t in raw.get("targets") or []:
        if not isinstance(t, dict):
            raise ValueError("接続先の形式が正しくありません")
        account = _str(t.get("storage_account"), 24)
        if not re.match(r"^[a-z0-9]{3,24}$", account):
            raise ValueError("ストレージアカウント名が正しくありません")
        item = {
            "storage_account": account,
            "endpoint_suffix": _str(t.get("endpoint_suffix"), 253, "core.windows.net") or "core.windows.net",
            "container": _str(t.get("container"), 512),
            "auth_kind": t.get("auth_kind") if t.get("auth_kind") in AUTH_KINDS else None,
            "tenant": _str(t.get("tenant"), 64) or None,
        }
        if keep_credential_ids and t.get("credential_id"):
            item["credential_id"] = _str(t.get("credential_id"), 64)
        if keep_credentials and isinstance(t.get("credential"), dict):
            c = t["credential"]
            if c.get("source") in ("connection_string", "sas_url", "account_key"):
                item["credential"] = {"source": c["source"], "value": _str(c.get("value"), 8192)}
        targets.append(item)
    if len(targets) > MAX_TARGETS:
        raise ValueError("接続先が多すぎます")
    out["targets"] = targets
    out["path_pattern"] = _str(raw.get("path_pattern"), MAX_PATTERN)
    out["ignore_case"] = bool(raw.get("ignore_case"))
    out["input_timezone"] = raw.get("input_timezone") if raw.get("input_timezone") in ("local", "utc") else "local"
    variables = {}
    for name, v in (raw.get("variables") or {}).items():
        if not NAME_RE.match(str(name)) or not isinstance(v, dict):
            raise ValueError(f"変数「{name}」の形式が正しくありません")
        if v.get("type") not in ("string", "number", "datetime"):
            raise ValueError(f"変数「{name}」の種類が正しくありません")
        if len(json.dumps(v, ensure_ascii=False)) > 64 * 1024:
            raise ValueError(f"変数「{name}」が大きすぎます")
        variables[name] = copy.deepcopy(v)
    out["variables"] = variables
    opts = raw.get("options") or {}
    out["options"] = {k: int(opts[k]) for k in ("merge_trial_pages", "max_prefixes") if isinstance(opts.get(k), int)}
    return out


def condition_hash(cond: dict) -> str:
    """同じ条件かどうかの判定（保存した認証情報の ID と名前を含めません。3.5.1）。"""
    norm = copy.deepcopy(cond)
    norm.pop("name", None)
    for t in norm.get("targets") or []:
        t.pop("credential_id", None)
        t.pop("credential", None)
    text = json.dumps(norm, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def targets_summary(cond: dict) -> str:
    """「stexample01/logs ほか1件」の形。"""
    ts = cond.get("targets") or []
    if not ts:
        return ""
    first = f"{ts[0]['storage_account']}/{ts[0].get('container', '')}"
    return first if len(ts) == 1 else f"{first} ほか{len(ts) - 1}件"


def value_summary(v: dict) -> str:
    """変数の値の要約（`--text-muted` の行に出す文）。"""
    t = v.get("type")
    val = v.get("value") or {}
    mode = val.get("mode", "any")
    if t == "datetime":
        if mode == "relative":
            k = val.get("kind")
            if k == "last_days":
                return f"直近 {val.get('n')} 日"
            if k == "last_hours":
                return f"直近 {val.get('n')} 時間"
            if k == "days_ago":
                return f"{val.get('from')} 日前〜{val.get('to')} 日前"
            return RELATIVE_LABELS.get(k, k or "")
        if mode == "period":
            s, e = val.get("start", ""), val.get("end", "")
            return s if s == e else f"{s}〜{e}"
        if mode == "units":
            return "単位ごと"
        return ""
    if t == "number":
        if mode == "fixed":
            return str(val.get("value"))
        if mode == "range":
            return f"{val.get('start')}〜{val.get('end')}"
        return ""
    if mode == "one_of":
        vals = [x.get("value", "") for x in val.get("values") or [] if x.get("enabled", True)]
        text = ", ".join(vals[:3]) + (f" ほか{len(vals) - 3}件" if len(vals) > 3 else "")
        return text[:60] + ("…" if len(text) > 60 else "")
    if mode == "chars":
        return "条件に合う文字列"
    return ""


def variables_summary(cond: dict) -> str:
    parts = []
    for name, v in (cond.get("variables") or {}).items():
        s = value_summary(v)
        if s:
            parts.append(f"{name}: {s}")
    return "   ".join(parts)


def check_compile(cond: dict, ctx) -> None:
    """パスパターンと変数の書式を確かめます（誤りは AppError）。"""
    from ..search.pattern import PatternError, compile_path
    try:
        compile_path(cond.get("path_pattern", ""), cond.get("variables") or {}, ignore_case=cond.get("ignore_case", False),
                     input_timezone=cond.get("input_timezone", "local"), ctx=ctx)
    except PatternError as exc:
        raise AppError("PATTERN_INVALID", "パスパターンに誤りがあります。", extra={"issues": [i.to_json() for i in exc.issues]}) from None
