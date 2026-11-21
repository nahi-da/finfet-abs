"""変数の自動検出と種類の推定（3.2.5）。"""

from __future__ import annotations

import re

from .pattern import variable_names
from .variables import NAME_RE

#: 名前 → 日時の単位（数値として推定し、1つの日時の変数にまとめる候補）
_UNIT_NAMES = {"year": "year", "yyyy": "year", "month": "month", "day": "day", "hour": "hour",
               "minute": "minute", "min": "minute", "second": "second", "sec": "second"}
_UNIT_SYMBOL = {"year": "YYYY", "month": "MM", "day": "DD", "hour": "hh", "minute": "mm", "second": "ss"}
_NUMBER_WORDS = ("seq", "num", "no", "index", "partition")


def _unit_from_context(before: str, seen_y: bool, seen_h: bool) -> str | None:
    """直前の文字列（`y=` など）から単位を推定します（7.2 の Azure の形式）。"""
    b = before.lower()
    if b.endswith("year=") or b.endswith("y="):
        return "year"
    if b.endswith("d="):
        return "day"
    if b.endswith("h="):
        return "hour"
    if b.endswith("m="):
        if seen_h:
            return "minute"
        if seen_y:
            return "month"
    return None


def infer(name: str, unit: str | None = None) -> dict:
    """名前（と直前の文字列から推定した単位）から、変数の初期の定義を作ります。"""
    low = name.lower()
    unit = unit or _UNIT_NAMES.get(low)
    if unit:
        return {"type": "number", "padding": 4 if unit == "year" else 2, "value": {"mode": "any"}, "unit": unit}
    if any(w in low for w in _NUMBER_WORDS):
        return {"type": "number", "value": {"mode": "any"}}
    if low.endswith("id") or "guid" in low or "uuid" in low:
        return {"type": "string", "value": {"mode": "chars", "classes": ["digit", "lower", "upper"], "extra": "-",
                                            "allow_slash": False, "min": 1, "max": None}}
    return {"type": "string", "value": {"mode": "any"}}


def detect(pattern: str, containers: list[str] | None = None, existing: dict | None = None) -> dict:
    """パスパターンとコンテナー名から変数を検出します。

    戻り値: {"variables": [{name, definition, new, in_pattern}], "issues": [...], "merge": {...}|None}
    """
    existing = existing or {}
    found: dict[str, dict] = {}
    issues: list[dict] = []
    units: dict[str, str] = {}
    sources = [("path", pattern or "")] + [("container", c or "") for c in (containers or [])]
    for source, text in sources:
        seen_y = seen_h = False
        for name, fmt, pos in variable_names(text):
            if not NAME_RE.match(name):
                if source == "path":
                    issues.append({"pos": pos + 1, "message": f"変数の名前「{name}」は使えません。", "name": name})
                continue
            entry = found.setdefault(name, {"name": name, "dt": set(), "where": set()})
            entry["dt"].add(fmt is not None)
            entry["where"].add(source)
            if fmt is None:
                u = _unit_from_context(text[:pos], seen_y, seen_h)
                if u and name not in units:
                    units[name] = u
            before = text[:pos].lower()
            if before.endswith("y=") or before.endswith("year="):
                seen_y = True
            if before.endswith("h="):
                seen_h = True
    out = []
    for name, e in found.items():
        if len(e["dt"]) == 2:
            issues.append({"pos": None, "message": f"変数「{name}」に、日時の書式があるものとないものが混ざっています。",
                           "name": name})
        is_dt = True in e["dt"] and False not in e["dt"]
        if name in existing:
            definition = existing[name]
            new = False
            if is_dt and definition.get("type") != "datetime":
                definition = {"type": "datetime", "path_timezone": "utc", "value": {"mode": "any"}}
        else:
            new = True
            if is_dt:
                definition = {"type": "datetime", "path_timezone": "utc", "value": {"mode": "any"}}
            else:
                definition = infer(name, units.get(name) or _UNIT_NAMES.get(name.lower()))
        out.append({"name": name, "definition": definition, "new": new, "in_pattern": True,
                    "where": sorted(e["where"])})
    for name, definition in existing.items():
        if name not in found:
            out.append({"name": name, "definition": definition, "new": False, "in_pattern": False, "where": []})
    return {"variables": out, "issues": issues, "merge": merge_suggestion(pattern, found, units)}


def merge_suggestion(pattern: str, found: dict, units: dict) -> dict | None:
    """年・月・日などの変数が2つ以上あれば、1つの日時の変数にまとめる書き換えを提案します。"""
    candidates = {}
    for name in found:
        u = units.get(name) or _UNIT_NAMES.get(name.lower())
        if u and False in found[name]["dt"] and True not in found[name]["dt"]:
            candidates[name] = u
    if len(candidates) < 2 or len(set(candidates.values())) < 2:
        return None
    dt = "dt"
    n = 2
    while dt in found:
        dt = f"dt{n}"
        n += 1
    new = pattern
    for name, u in candidates.items():
        new = re.sub(r"\{\{" + re.escape(name) + r"\}\}", "{{" + dt + "=" + _UNIT_SYMBOL[u] + "}}", new)
    return {"pattern": new, "names": sorted(candidates), "datetime_name": dt}


def looks_literal(text: str) -> bool:
    """Blob 名や URL をそのまま貼り付けたように見えるか（[記号を文字として扱う] を表示するか。3.2.2）。"""
    rest = re.sub(r"\{\{[^{}]*\}\}", "", text or "")
    if re.search(r"\\[.()\[\]+?*{}|^$dDsSwW]", rest):
        return False
    return bool(re.search(r"[.()+?\[\]]", rest))


def escape_non_variables(text: str) -> str:
    """変数以外の部分を、文字そのものとして扱う形にします。"""
    from .formats import escape_literal
    out = []
    last = 0
    for m in re.finditer(r"\{\{[^{}]*\}\}", text or ""):
        out.append(escape_literal(text[last:m.start()]))
        out.append(m.group(0))
        last = m.end()
    out.append(escape_literal((text or "")[last:]))
    return "".join(out)
