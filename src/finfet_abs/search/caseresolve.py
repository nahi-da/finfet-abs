"""大文字・小文字を区別しない部分（`(?i:…)`）の、実際の Blob 名の表記を求めます。

Azure Monitor などが書き出すログの `resourceId=` の後の部分は、リソースの種類や作られた時期によって
大文字・小文字が違います（すべて大文字のものと、`subscriptions/…/resourceGroups/…` のように
混ざったものがあります。実環境で確認）。既知の形式では、この部分を `(?i:…)` で書きます。

`(?i:…)` の部分は prefix で絞り込めないため（7.3.3）、そのままではコンテナー全体を取得します。そこで、
パスパターンが「文字そのもの」+「`(?i:` 文字そのもの …`)`」で始まる場合は、検索のときに `/` の区切りごとの
一覧（walk）で実際の表記を求め、その部分を実際の表記の選択（`(?:A|B)`）に置き換えます。こうすると、
prefix での絞り込みがこれまでどおり効きます。
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Callable

_META = set(".^$*+?{}[]\\|()")
_QUANT = set("*+?{")

#: 求めた表記の候補の上限（これを超えたら置き換えずに、そのまま検索します）
MAX_CANDIDATES = 16


@dataclass
class CiHead:
    """パスパターンの先頭の、表記を求められる部分。"""

    head: str  # (?i: の前の文字そのもの（正規表現を外した文字列）
    ci_literal: str  # (?i: の中の先頭の、/ で終わる完全な階層の文字そのもの
    start: int  # パスパターンの中の (?i: の位置
    lit_end: int  # パスパターンの中の、ci_literal の終わりの位置（(?i: の中）
    end: int  # パスパターンの中の、(?i:…) の閉じかっこの次の位置


def _literal_run(text: str, i: int, stop: str = "") -> tuple[str, int]:
    """text[i:] の先頭から、文字そのものの部分を読みます。戻り値は (文字列, 読み終えた位置)。"""
    out = []
    while i < len(text):
        c = text[i]
        if c in stop:
            break
        if c == "\\":
            if i + 1 >= len(text) or text[i + 1].isalnum():
                break  # \d などは文字そのものではありません
            ch, nxt = text[i + 1], i + 2
        elif c in _META:
            break
        else:
            ch, nxt = c, i + 1
        if nxt < len(text) and text[nxt] in _QUANT:
            break  # 後ろに量指定子があれば、文字そのものではありません
        out.append(ch)
        i = nxt
    return "".join(out), i


def _group_end(text: str, i: int) -> int | None:
    """text[i] が開きかっこのとき、対応する閉じかっこの次の位置。"""
    depth = 0
    in_class = False
    while i < len(text):
        c = text[i]
        if c == "\\":
            i += 2
            continue
        if in_class:
            if c == "]":
                in_class = False
        elif c == "[":
            in_class = True
        elif c == "(":
            depth += 1
        elif c == ")":
            depth -= 1
            if depth == 0:
                return i + 1
        i += 1
    return None


def _has_alternation(text: str) -> bool:
    """text の直下（かっこの外）に選択 | があるか。"""
    depth = 0
    in_class = False
    i = 0
    while i < len(text):
        c = text[i]
        if c == "\\":
            i += 2
            continue
        if in_class:
            if c == "]":
                in_class = False
        elif c == "[":
            in_class = True
        elif c == "(":
            depth += 1
        elif c == ")":
            depth -= 1
        elif c == "|" and depth == 0:
            return True
        i += 1
    return False


def find(pattern: str) -> CiHead | None:
    """パスパターンの先頭の、表記を求められる部分を探します。なければ None。"""
    if pattern.startswith("/"):
        pattern = pattern[1:]
        offset = 1
    else:
        offset = 0
    head, i = _literal_run(pattern, 0)
    if not pattern.startswith("(?i:", i):
        return None
    end = _group_end(pattern, i)
    # パターン全体の選択や、(?i: の中の直下の選択があると、先頭が決まらないため扱いません
    if end is None or _has_alternation(pattern) or _has_alternation(pattern[i + 4:end - 1]):
        return None
    inner, j = _literal_run(pattern, i + 4, stop=")")
    cut = inner.rfind("/")
    if cut < 0:
        return None
    ci = inner[:cut + 1]
    # ci の終わりの、パスパターンの中の位置を求めます（エスケープを含むため、読み直します）
    k = i + 4
    consumed = 0
    while consumed < len(ci):
        k += 2 if pattern[k] == "\\" else 1
        consumed += 1
    del j
    return CiHead(head, ci, i + offset, k + offset, end + offset)


def resolve(walk: Callable[[str], list[str]], head: str, ci: str) -> list[str] | None:
    """`/` の区切りごとの一覧で、ci の実際の表記を求めます。

    walk(prefix) は、prefix の直下の名前（「階層名/」または Blob 名。prefix からの相対）を返す関数です。
    戻り値: 実際の表記の一覧（空なら、一致する Blob はありません）。候補が多すぎる場合は None。
    """
    segments = [s + "/" for s in ci.split("/")[:-1]]
    current = [""]
    for seg in segments:
        rx = re.compile(re.escape(seg), re.IGNORECASE)
        nxt: list[str] = []
        for c in current:
            for child in walk(head + c):
                if rx.fullmatch(child):
                    nxt.append(c + child)
        current = sorted(set(nxt))
        if not current:
            return []
        if len(current) > MAX_CANDIDATES:
            return None
    return current


def apply(pattern: str, found: CiHead, actual: list[str]) -> str:
    """(?i: の中の先頭の部分を、実際の表記の選択に置き換えたパスパターン。"""
    alt = "(?:" + "|".join(re.escape(a) for a in actual) + ")"
    rest_inner = pattern[found.lit_end:found.end - 1]
    tail = f"(?i:{rest_inner})" if rest_inner else ""
    return pattern[:found.start] + alt + tail + pattern[found.end:]
