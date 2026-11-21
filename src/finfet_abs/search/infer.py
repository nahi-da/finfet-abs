"""Blob 名からパスパターンを作る（依頼者の依頼による機能。decisions.md）。

1. collect: コンテナーの Blob 名を集めます（すべて / 先頭から N 件 / 階層をたどって見本）。
2. infer: Blob 名を「/」の階層と、階層の中の数字・英字・記号の並びに分け、同じ形の Blob をまとめて、
   パスパターンの候補（日時の変数 `{{dt=…}}` を含む）を、件数の多い順に返します。
"""

from __future__ import annotations

import re
import threading
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime
from typing import Callable, Iterable

from ..azure.backend import ContainerRef

#: 正規表現で文字として扱うために「\」を付ける文字（{ は {{ を避けるため）
_SPECIAL = set(".^$*+?{}[]\\|()")
_RUN = re.compile(r"\d+|[A-Za-z]+|[^\dA-Za-z]+")
_KEYS = {"y": "YYYY", "year": "YYYY", "m": None, "month": "MM", "d": "DD", "day": "DD", "h": "hh", "hour": "hh",
         "min": "mm", "minute": "mm", "s": "ss", "sec": "ss", "second": "ss"}
#: 階層の中の数字の並び（桁数）→ 日時の書式
_COMPACT = {8: ("YYYYMMDD", "%Y%m%d"), 10: ("YYYYMMDDhh", "%Y%m%d%H"), 12: ("YYYYMMDDhhmm", "%Y%m%d%H%M"),
            14: ("YYYYMMDDhhmmss", "%Y%m%d%H%M%S")}
_UNIT_OF = {"YYYY": "Y", "MM": "M", "DD": "D", "hh": "h", "mm": "m", "ss": "s"}
_ORDER = "YMDhms"
MAX_CHOICES = 4
MAX_CANDIDATES = 30


def escape(text: str) -> str:
    return "".join("\\" + c if c in _SPECIAL else c for c in text)


@dataclass
class Tok:
    kind: str  # lit（記号・区切り）/ num / word / date
    text: str
    fmt: str | None = None  # 日時の書式（YYYY、YYYYMMDD など）
    var: str | None = None  # 日時の変数の名前（dt、dt2…）


def _valid(text: str, pyfmt: str) -> datetime | None:
    try:
        return datetime.strptime(text, pyfmt)
    except ValueError:
        return None


def tokenize(name: str) -> list[Tok]:
    """Blob 名を、区切り・数字・英字の並びに分け、日時と見なせる数字に書式を付けます。"""
    toks: list[Tok] = []
    for m in _RUN.finditer(name):
        t = m.group(0)
        if t.isdigit():
            toks.append(Tok("num", t))
        elif t.isalpha() and t.isascii():
            toks.append(Tok("word", t))
        else:
            toks.append(Tok("lit", t))
    _mark_dates(toks)
    return toks


def _mark_dates(toks: list[Tok]) -> None:
    """日時の部分に書式と変数の名前を付けます。同じ単位がもう一度現れたら、別の日時（dt2…）にします。"""
    var_no = 1
    seen: set[str] = set()
    last_unit = ""

    def assign(tok: Tok, fmt: str) -> None:
        nonlocal var_no, seen, last_unit
        units = [_UNIT_OF[f] for f in re.findall(r"YYYY|MM|DD|hh|mm|ss", fmt)]
        if any(u in seen for u in units):
            var_no += 1
            seen = set()
        seen.update(units)
        last_unit = units[-1]
        tok.kind, tok.fmt, tok.var = "date", fmt, "dt" if var_no == 1 else f"dt{var_no}"

    for i, tok in enumerate(toks):
        if tok.kind != "num":
            continue
        n = tok.text
        prev_word = toks[i - 2].text.lower() if i >= 2 and toks[i - 1].text == "=" and toks[i - 2].kind == "word" else None
        if len(n) in _COMPACT and _valid(n, _COMPACT[len(n)][1]):
            assign(tok, _COMPACT[len(n)][0])
            continue
        if len(n) == 4 and 1970 <= int(n) <= 2100 and (prev_word in (None, "y", "year")):
            assign(tok, "YYYY")
            continue
        if len(n) != 2:
            continue
        v = int(n)
        if prev_word is not None and prev_word in _KEYS:
            fmt = _KEYS[prev_word]
            if prev_word == "m":  # m= は、時の後なら分、それ以外は月
                fmt = "mm" if last_unit in ("h", "m") else "MM"
            if fmt and _in_range(fmt, v):
                assign(tok, fmt)
            continue
        # key= のない 2 桁: 直前の日時の単位の次の単位と見なせれば日時
        if last_unit and last_unit in _ORDER[:-1]:
            nxt = _ORDER[_ORDER.index(last_unit) + 1]
            fmt = {"M": "MM", "D": "DD", "h": "hh", "m": "mm", "s": "ss"}[nxt]
            if _in_range(fmt, v) and _near_previous_date(toks, i):
                assign(tok, fmt)


def _near_previous_date(toks: list[Tok], i: int) -> bool:
    """直前の日時との間が、区切り（記号）1 つだけか。"""
    return i >= 2 and toks[i - 1].kind == "lit" and len(toks[i - 1].text) <= 2 and toks[i - 2].kind == "date"


def _in_range(fmt: str, v: int) -> bool:
    return {"MM": 1 <= v <= 12, "DD": 1 <= v <= 31, "hh": 0 <= v <= 23, "mm": 0 <= v <= 59, "ss": 0 <= v <= 59}.get(fmt, False)


def _shape(toks: list[Tok]) -> tuple:
    """まとめるためのキー: 区切りはそのまま、日時は書式、数字は桁数を問わず N、英字は A。"""
    out = []
    for t in toks:
        if t.kind == "lit":
            out.append(t.text)
        elif t.kind == "date":
            out.append(f"D:{t.var}:{t.fmt}")
        elif t.kind == "num":
            out.append("N")
        else:
            out.append("A")
    return tuple(out)


@dataclass
class Group:
    shape: tuple
    names: list[str] = field(default_factory=list)
    sizes: list[int] = field(default_factory=list)
    values: list[set] = field(default_factory=list)
    dates: list[datetime] = field(default_factory=list)


def _date_of(toks: list[Tok]) -> datetime | None:
    parts = {"Y": None, "M": 1, "D": 1, "h": 0, "m": 0, "s": 0}
    for t in toks:
        if t.kind == "date" and t.var == "dt":
            pos = 0
            for f in re.findall(r"YYYY|MM|DD|hh|mm|ss", t.fmt or ""):
                ln = 4 if f == "YYYY" else 2
                parts[_UNIT_OF[f]] = int(t.text[pos:pos + ln])
                pos += ln
    if parts["Y"] is None:
        return None
    try:
        return datetime(parts["Y"], parts["M"], parts["D"], parts["h"], parts["m"], parts["s"])
    except ValueError:
        return None


def infer(items: Iterable[tuple[str, int | None]], *, max_candidates: int = MAX_CANDIDATES) -> list[dict]:
    """Blob 名（と大きさ）から、パスパターンの候補を件数の多い順に返します。"""
    groups: dict[tuple, Group] = {}
    for name, size in items:
        toks = tokenize(name)
        key = _shape(toks)
        g = groups.get(key)
        if g is None:
            g = groups[key] = Group(key, values=[set() for _ in toks])
        g.names.append(name)
        g.sizes.append(size or 0)
        for i, t in enumerate(toks):
            if len(g.values[i]) <= MAX_CHOICES + 1:
                g.values[i].add(t.text)
        d = _date_of(toks)
        if d is not None:
            g.dates.append(d)
    merged: dict[str, dict] = {}
    for g in groups.values():
        pat = _pattern_of(g)
        c = merged.get(pat)
        if c is None:
            c = merged[pat] = {"pattern": pat, "count": 0, "bytes": 0, "examples": [], "first": None, "last": None,
                               "variables": sorted({s.split(":")[1] for s in g.shape if s.startswith("D:")})}
        c["count"] += len(g.names)
        c["bytes"] += sum(g.sizes)
        for n in g.names[:3]:
            if len(c["examples"]) < 3:
                c["examples"].append(n)
        if g.dates:
            lo, hi = min(g.dates), max(g.dates)
            c["first"] = lo if c["first"] is None else min(c["first"], lo)
            c["last"] = hi if c["last"] is None else max(c["last"], hi)
    out = sorted(merged.values(), key=lambda c: (-c["count"], c["pattern"]))[:max_candidates]
    for c in out:
        c["first"] = c["first"].isoformat() if c["first"] else None
        c["last"] = c["last"].isoformat() if c["last"] else None
    return out


def _pattern_of(g: Group) -> str:
    parts: list[str] = []
    for key, vals in zip(g.shape, g.values):
        if key.startswith("D:"):
            _, var, fmt = key.split(":")
            parts.append("{{" + f"{var}={fmt}" + "}}")
        elif key in ("N", "A"):
            if len(vals) == 1:
                parts.append(escape(next(iter(vals))))
            elif len(vals) <= MAX_CHOICES:
                parts.append("(?:" + "|".join(escape(v) for v in sorted(vals)) + ")")
            elif key == "N":
                lens = {len(v) for v in vals}
                parts.append(f"\\d{{{lens.pop()}}}" if len(lens) == 1 else "\\d+")
            else:
                parts.append("[A-Za-z]+")
        else:
            parts.append(escape(key))
    return "".join(parts)


# ---------------------------------------------------------------- Blob 名の収集

MODES = ("sample", "first", "all")


class Cancelled(Exception):
    pass


def collect(backend, target: ContainerRef, *, mode: str = "sample", prefix: str = "", limit: int = 10000,
            per_level: int = 10, max_depth: int = 40, max_requests: int = 500, cancel: threading.Event | None = None,
            progress: Callable[[int, int], None] | None = None,
            connection: Callable[[], object] | None = None) -> tuple[list[tuple[str, int | None]], dict]:
    """Blob 名を集めます。戻り値は [(名前, 大きさ)] と、要求の回数・打ち切ったかの情報。

    - sample: 階層をたどり、各階層で最大 per_level 件の見本（先頭・末尾・等間隔）だけを下へたどります（大きさは不明。
      要求は最大 max_requests 回）
    - first: 名前の順に、先頭から limit 件
    - all: すべて（limit は無視）
    """
    from contextlib import nullcontext
    cancel = cancel or threading.Event()
    connection = connection or nullcontext
    progress = progress or (lambda n, r: None)
    out: list[tuple[str, int | None]] = []
    info = {"requests": 0, "truncated": False}
    if mode == "sample":
        stack = [(prefix, 0)]
        while stack:
            if cancel.is_set():
                raise Cancelled()
            if info["requests"] >= max_requests:
                info["truncated"] = True
                break
            pre, depth = stack.pop()
            with connection():
                children = backend.walk_prefixes(target, pre, limit=5000)
            info["requests"] += 1
            files = [c for c in children if not c.endswith("/")]
            dirs = [c for c in children if c.endswith("/")]
            out.extend((pre + f, None) for f in files)
            if depth < max_depth:
                # 深さ優先で、見本の階層を逆の順に積みます（先頭の階層から先にたどるため）
                for d in reversed(_spread(dirs, per_level)):
                    stack.append((pre + d, depth + 1))
            elif dirs:
                info["truncated"] = True
            progress(len(out), info["requests"])
            if len(out) >= 200000:
                info["truncated"] = True
                break
        return out, info
    cont = None
    cap = None if mode == "all" else max(1, int(limit))
    while True:
        if cancel.is_set():
            raise Cancelled()
        with connection():
            page = backend.list_blobs_page(target, prefix, cont)
        info["requests"] += 1
        for b in page.items:
            out.append((b.name, b.size))
            if cap is not None and len(out) >= cap:
                info["truncated"] = page.continuation is not None or b is not page.items[-1]
                progress(len(out), info["requests"])
                return out, info
        progress(len(out), info["requests"])
        cont = page.continuation
        if not cont:
            return out, info


def _spread(items: list[str], k: int) -> list[str]:
    """先頭・末尾を含めて、等間隔に最大 k 件を選びます。"""
    if len(items) <= k:
        return items
    if k <= 1:
        return items[:1]
    step = (len(items) - 1) / (k - 1)
    return [items[round(i * step)] for i in range(k)]
