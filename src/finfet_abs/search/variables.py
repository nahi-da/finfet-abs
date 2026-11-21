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


def string_fragment(name: str, value: dict) -> str:
    """文字列の変数の正規表現（3.2.3.1）。"""
    mode = (value or {}).get("mode", "any")
    if mode == "any":
        return r"[^/]+"
    if mode == "one_of":
        vals = [str(v.get("value", "")) for v in value.get("values") or [] if v.get("enabled", True)]
        vals = [v for v in vals if v != ""]
        if not vals:
            raise VariableError(name, f"変数「{name}」の値を1つ以上指定してください。")
        for v in vals:
            if "/" in v:
                raise VariableError(name, f"変数「{name}」の値に / は使えません（値は1階層の中だけで照合します）。")
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
    """値を列挙できる場合（次のうちいずれか・完全一致）の値の一覧。"""
    if (value or {}).get("mode") == "one_of" and value.get("match", "exact") == "exact":
        return [str(v["value"]) for v in value.get("values") or [] if v.get("enabled", True) and v.get("value")]
    return None


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
        return [_fmt_num(n, padding) for n in range(start, end + 1, step)]
    return None


def number_fragment(name: str, value: dict, padding: int | None) -> tuple[str, Callable[[str], bool] | None]:
    """数値の変数の正規表現と、必要なら追加の判定（値の文字列 → 一致するか）。"""
    mode = (value or {}).get("mode", "any")
    if padding is not None:
        padding = int(padding)
        if padding < 1 or padding > 18:
            raise VariableError(name, f"変数「{name}」の桁数は 1〜18 にしてください。")
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
                raise VariableError(name, f"変数「{name}」の間隔は 1 以上にしてください。")
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
