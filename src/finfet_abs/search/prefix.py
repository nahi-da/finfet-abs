"""prefix の算出（7.3.3）。

正規表現を `re._parser`（Python の非公開の API）で解析し、先頭から確定する文字を prefix に足します。
使えない場合は `''`（コンテナー全体）にします（結果は正しく、遅くなるだけです。7.4 の I13）。

不変条件: 正規表現に一致するすべての文字列は、いずれかの prefix で始まる。prefix 同士は包含関係にない。
"""

from __future__ import annotations

import logging
import re

log = logging.getLogger(__name__)

try:  # Python 3.11 以降
    import re._constants as _c
    import re._parser as _p
except Exception:  # noqa: BLE001
    _p = None
    _c = None

#: 1文字の選択肢（[abc]）を分ける最大の文字の数
MAX_CLASS_CHARS = 16


class _Stop(Exception):
    pass


def compute(regex: str, flags: int = 0, limit: int = 32) -> list[str]:
    """prefix の一覧を返します。"""
    if flags & re.IGNORECASE:
        return [""]
    if _p is None:
        log.warning("正規表現の解析の機能（re._parser）が使えないため、コンテナー全体を取得します")
        return [""]
    try:
        parsed = _p.parse(regex, flags)
        if parsed.state.flags & re.IGNORECASE:
            return [""]
        prefixes, _stopped = _walk(list(parsed), [""], max(1, limit))
    except Exception as exc:  # noqa: BLE001
        log.warning("prefix を求められないため、コンテナー全体を取得します（%s）", type(exc).__name__)
        return [""]
    return normalize(prefixes)


def normalize(prefixes: list[str]) -> list[str]:
    """重複と、ほかの prefix で始まる prefix を除きます。"""
    out: list[str] = []
    for p in sorted(set(prefixes)):
        if out and p.startswith(out[-1]):
            continue
        out.append(p)
    return out


def _walk(items: list, current: list[str], limit: int) -> tuple[list[str], bool]:
    """items を先頭から見て prefix を伸ばします。戻り値は (prefix の一覧, 途中で打ち切ったか)。"""
    for op, av in items:
        if op is _c.LITERAL:
            ch = chr(av)
            current = [p + ch for p in current]
            continue
        if op is _c.SUBPATTERN:
            _group, add_flags, _del_flags, sub = av
            if add_flags & re.IGNORECASE:
                return current, True
            current, stopped = _walk(list(sub), current, limit)
            if stopped:
                return current, True
            continue
        if op is _c.BRANCH:
            alts = av[1]
            current, stopped = _branch([list(a) for a in alts], current, limit)
            if stopped:
                return current, True
            continue
        if op is _c.IN:
            chars = _class_chars(av)
            if chars is None or len(chars) > MAX_CLASS_CHARS:
                return current, True
            current, stopped = _branch([[(_c.LITERAL, ord(ch))] for ch in chars], current, limit)
            if stopped:
                return current, True
            continue
        if op in (_c.MAX_REPEAT, _c.MIN_REPEAT):
            lo, _hi, sub = av
            sub = list(sub)
            if lo >= 1 and len(sub) == 1 and sub[0][0] is _c.LITERAL:
                current = [p + chr(sub[0][1]) for p in current]
            return current, True
        # 文字の範囲・任意の文字・後方参照・先読み・アンカーなど
        return current, True
    return current, False


def _class_chars(av) -> list[str] | None:
    """範囲や否定を含まない文字クラスの文字の一覧。それ以外は None。"""
    out = []
    for op, v in av:
        if op is _c.LITERAL:
            out.append(chr(v))
        else:
            return None
    return sorted(set(out))


def _branch(alts: list[list], current: list[str], limit: int) -> tuple[list[str], bool]:
    """選択（|）を選択肢ごとの prefix に分けます。上限を超える場合は分けずに打ち切ります。"""
    budget = limit // max(1, len(current))
    n = len(alts)
    if n > budget:
        return current, True
    inner = max(1, budget // n)
    results: list[list[str]] = []
    any_stopped = False
    for alt in alts:
        res, stopped = _walk(alt, [""], inner)
        if len(res) > inner:
            res = [_common_prefix(res)]
            stopped = True
        any_stopped = any_stopped or stopped
        results.append(res)
    combined = [p + r for p in current for alt_res in results for r in alt_res]
    if len(combined) > limit:
        return current, True
    return combined, any_stopped


def _common_prefix(items: list[str]) -> str:
    if not items:
        return ""
    s1, s2 = min(items), max(items)
    i = 0
    while i < len(s1) and i < len(s2) and s1[i] == s2[i]:
        i += 1
    return s1[:i]
