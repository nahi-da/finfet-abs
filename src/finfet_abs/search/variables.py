"""文字列・数値の変数（3.2.3）から正規表現を作ります。"""

from __future__ import annotations

import re
from typing import Callable

from . import charclass

#: 列挙する値の数の上限（超えたら形の正規表現と追加の判定にします）
MAX_ENUM = 2000
NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,31}$")


class VariableError(ValueError):
    """変数の定義の誤り。"""

    def __init__(self, name: str, message: str) -> None:
        super().__init__(message)
        self.name = name


def wildcard_regex(text: str) -> str:
    """値の「*」の書き方（v1.6.0）を正規表現にします。`*` は 1 階層の中の任意の文字列、`\\*` は文字の `*`。"""
    out: list[str] = []
    i = 0
    while i < len(text):
        c = text[i]
        if c == "\\" and i + 1 < len(text) and text[i + 1] == "*":
            out.append(re.escape("*"))
            i += 2
            continue
        out.append(r"[^/]*" if c == "*" else re.escape(c))
        i += 1
    return "".join(out)


def has_wildcard(text: str) -> bool:
    """値に「*」（文字の `\\*` を除く）があるか。"""
    return wildcard_regex(text) != re.escape(unescape_wildcard(text))


def unescape_wildcard(text: str) -> str:
    """`\\*` を `*` に戻した文字列（「*」のない値の、そのままの文字列）。"""
    return text.replace("\\*", "*")


def legacy_to_wildcard(value: str, match: str) -> str:
    """v1.5 までの一致方法（exact / prefix / partial）の値を、「*」の書き方に直します。"""
    v = value.replace("*", "\\*")
    if match == "prefix":
        return v + "*"
    if match == "partial":
        return "*" + v + "*"
    return v


def _enabled_values(items) -> list[str]:
    return [str(v.get("value", "")) for v in items or [] if isinstance(v, dict) and v.get("enabled", True)
            and str(v.get("value", "")) != ""]


def exclude_check(name: str, value: dict, *, number: bool = False,
                  ignore_case: bool = False) -> Callable[[str], bool] | None:
    """除く値（v1.6.0）の判定（値の文字列 → 対象にするか）。除く値がなければ None。"""
    vals = _enabled_values((value or {}).get("exclude"))
    if not vals:
        return None
    if number:
        nums: set[int] = set()
        for v in vals:
            try:
                nums.add(int(v))
            except ValueError:
                raise VariableError(name, f"変数「{name}」の除く値「{v}」は整数で指定してください。") from None
        return lambda text: int(text) not in nums
    for v in vals:
        if "/" in v:
            raise VariableError(name, f"変数「{name}」の除く値に / は使用できません。")
    rx = re.compile("(?:" + "|".join(wildcard_regex(v) for v in vals) + ")", re.IGNORECASE if ignore_case else 0)
    return lambda text: rx.fullmatch(text) is None


def string_fragment(name: str, value: dict) -> str:
    """文字列の変数の正規表現（3.2.3.1）。"""
    mode = (value or {}).get("mode", "any")
    if mode == "any":
        return r"[^/]+"
    if mode == "one_of":
        vals = _enabled_values(value.get("values"))
        if not vals:
            raise VariableError(name, f"変数「{name}」の値を 1 つ以上指定してください。")
        for v in vals:
            if "/" in v:
                raise VariableError(name, f"変数「{name}」の値に / は使用できません（値は 1 つの階層の中でのみ照合します）。")
        if value.get("syntax") == "wildcard":
            # 長い値を先に並べます（短い値が先に一致して終わらないように）
            parts = sorted(dict.fromkeys(wildcard_regex(v) for v in vals), key=lambda s: (-len(s), s))
            return "(?:" + "|".join(parts) + ")"
        # v1.5 までの保存済みの条件（一致方法 match）
        # 長い値を先に並べます（前方一致・部分一致で短い値が先に一致して終わらないように）
        alts = "|".join(re.escape(v) for v in sorted(dict.fromkeys(vals), key=lambda s: (-len(s), s)))
        match = value.get("match", "exact")
        if match == "exact":
            return f"(?:{alts})"
        if match == "prefix":
            return f"(?:{alts})[^/]*"
        if match == "partial":
            return f"[^/]*(?:{alts})[^/]*"
        raise VariableError(name, f"変数「{name}」の一致方法が正しくありません。")
    if mode == "chars":
        try:
            cls = charclass.build_class(value.get("classes") or [], value.get("extra") or "",
                                        bool(value.get("allow_slash")))
            q = charclass.length_quantifier(value.get("min"), value.get("max"))
        except ValueError as exc:
            raise VariableError(name, f"変数「{name}」: {exc}") from None
        return cls + q
    raise VariableError(name, f"変数「{name}」の値の指定が正しくありません。")


def string_values(value: dict) -> list[str] | None:
    """値を列挙できる場合（次のうちいずれか・完全一致。「*」を含まない）の値の一覧。除く値は除きます。"""
    if (value or {}).get("mode") != "one_of":
        return None
    vals = _enabled_values(value.get("values"))
    if value.get("syntax") == "wildcard":
        if any(has_wildcard(v) for v in vals):
            return None
        vals = [unescape_wildcard(v) for v in vals]
    elif value.get("match", "exact") != "exact":
        return None
    ex = _enabled_values(value.get("exclude"))
    if ex:
        rx = re.compile("(?:" + "|".join(wildcard_regex(v) for v in ex) + ")")
        vals = [v for v in vals if not rx.fullmatch(v)]
    return vals


def choice_as_string(name: str, value: dict) -> dict:
    """選択肢の変数（依頼者の依頼。decisions.md）を、文字列の「次のうちいずれか・完全一致」の指定に直します。

    value: {"options": [選択肢], "selected": [選んだ値]}。選んだ値が 1 つもなければ誤りです。
    """
    sel = [str(x) for x in (value or {}).get("selected") or [] if str(x) != ""]
    if not sel:
        raise VariableError(name, f"変数「{name}」の値を 1 つ以上選択してください。")
    return {"mode": "one_of", "match": "exact", "values": [{"value": x, "enabled": True} for x in dict.fromkeys(sel)]}


def _fmt_num(n: int, padding: int | None) -> str:
    return f"{n:0{padding}d}" if padding else str(n)


def number_values(value: dict, padding: int | None) -> list[str] | None:
    """値を列挙できる場合の値（文字列）の一覧。列挙できない（任意・多すぎる）場合は None。"""
    mode = (value or {}).get("mode", "any")
    if mode == "fixed":
        return [_fmt_num(int(value["value"]), padding)]
    if mode == "range":
        start, end, step = int(value["start"]), int(value["end"]), int(value.get("step") or 1)
        count = (end - start) // step + 1 if end >= start and step > 0 else 0
        if count > MAX_ENUM:
            return None
        ex: set[int] = set()
        for v in _enabled_values(value.get("exclude")):
            try:
                ex.add(int(v))
            except ValueError:
                pass
        return [_fmt_num(n, padding) for n in range(start, end + 1, step) if n not in ex]
    return None


def number_fragment(name: str, value: dict, padding: int | None) -> tuple[str, Callable[[str], bool] | None]:
    """数値の変数の正規表現と、必要なら追加の判定（値の文字列 → 一致するか）。"""
    mode = (value or {}).get("mode", "any")
    if padding is not None:
        padding = int(padding)
        if padding < 1 or padding > 18:
            raise VariableError(name, f"変数「{name}」の桁数は 1〜18 の範囲で指定してください。")
    shape = rf"\d{{{padding}}}" if padding else r"\d+"
    try:
        if mode == "any":
            return shape, None
        if mode == "fixed":
            v = int(value["value"])
            if v < 0:
                raise ValueError
            return re.escape(_fmt_num(v, padding)), None
        if mode == "range":
            start, end, step = int(value["start"]), int(value["end"]), int(value.get("step") or 1)
            if start < 0 or end < start:
                raise VariableError(name, f"変数「{name}」の範囲の終了が開始より前です。")
            if step < 1:
                raise VariableError(name, f"変数「{name}」の間隔は 1 以上で指定してください。")
            vals = number_values(value, padding)
            if vals is not None:
                return "(?:" + "|".join(re.escape(v) for v in vals) + ")", None

            def check(text: str, start=start, end=end, step=step) -> bool:
                n = int(text)
                return start <= n <= end and (n - start) % step == 0 and (not padding or len(text) == padding)
            return shape, check
    except VariableError:
        raise
    except (KeyError, TypeError, ValueError):
        raise VariableError(name, f"変数「{name}」の値が正しくありません。") from None
    raise VariableError(name, f"変数「{name}」の値の指定が正しくありません。")
