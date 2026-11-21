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
from .listing import covered_by_trial

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
#: 日時を含まない階層を選択肢の変数にする、値の種類の上限
MAX_SEG_CHOICES = 100
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


def _split_segments(toks: list[Tok]) -> list[list[Tok]]:
    """トークンの並びを「/」の階層ごとに分けます（「/」を含む区切りのトークンは、「/」の前後で分けます）。"""
    segs: list[list[Tok]] = [[]]
    for t in toks:
        if t.kind == "lit" and "/" in t.text:
            parts = t.text.split("/")
            for k, p in enumerate(parts):
                if k:
                    segs.append([])
                if p:
                    segs[-1].append(Tok("lit", p))
        else:
            segs[-1].append(t)
    return segs


def _tok_shape(toks: list[Tok]) -> tuple:
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


_EXT = re.compile(r"\.([A-Za-z0-9]{1,5})$")


def _seg_key(seg: list[Tok], last: bool):
    """階層のキー: 日時を含む階層はトークンの形、含まない階層は "S"（値をまとめて扱う）。
    最後の階層（ファイル名）は、拡張子が違えば別のグループにします（a/*.csv と b/readme.md をまとめないため）。"""
    if any(t.kind == "date" for t in seg):
        return ("D", _tok_shape(seg))
    if last:
        m = _EXT.search("".join(t.text for t in seg))
        return "S." + (m.group(1).lower() if m else "")
    return "S"


@dataclass
class SegStat:
    """日時を含まない階層の値の集計。"""
    values: dict[str, int] = field(default_factory=dict)
    overflow: bool = False  # 値の種類が MAX_SEG_CHOICES を超えた
    shapes: set = field(default_factory=set)  # トークンの形（2 種類まで）
    tokvals: list[set] = field(default_factory=list)  # 最初の形のときのトークンごとの値
    prefix: str | None = None  # すべての値に共通の先頭
    suffix: str | None = None  # すべての値に共通の末尾
    minlen: int = 1 << 30  # 値の長さの最小

    def add(self, seg: list[Tok]) -> None:
        text = "".join(t.text for t in seg)
        self.minlen = min(self.minlen, len(text))
        if not self.overflow:
            self.values[text] = self.values.get(text, 0) + 1
            if len(self.values) > MAX_SEG_CHOICES:
                self.overflow = True
        self.prefix = text if self.prefix is None else _common_prefix(self.prefix, text)
        self.suffix = text if self.suffix is None else _common_suffix(self.suffix, text)
        shape = _tok_shape(seg)
        if not self.shapes:
            self.tokvals = [set() for _ in seg]
        if len(self.shapes) < 2:
            self.shapes.add(shape)
        if len(self.shapes) == 1:
            for i, t in enumerate(seg):
                if len(self.tokvals[i]) <= MAX_CHOICES + 1:
                    self.tokvals[i].add(t.text)


def _common_prefix(a: str, b: str) -> str:
    n = 0
    while n < len(a) and n < len(b) and a[n] == b[n]:
        n += 1
    return a[:n]


def _common_suffix(a: str, b: str) -> str:
    n = 0
    while n < len(a) and n < len(b) and a[-1 - n] == b[-1 - n]:
        n += 1
    return a[len(a) - n:]


def _cut_prefix(p: str) -> str:
    """共通の先頭を、最後の区切りの文字（英数字以外）までに切り詰めます（「vm-web01」「vm-db01」→「vm-」）。"""
    for i in range(len(p) - 1, -1, -1):
        if not p[i].isalnum():
            return p[:i + 1]
    return ""


def _cut_suffix(s: str) -> str:
    """共通の末尾を、最初の区切りの文字（英数字以外）からに切り詰めます（「web01.log」「db01.log」→「.log」）。"""
    for i, c in enumerate(s):
        if not c.isalnum():
            return s[i:]
    return ""


@dataclass
class Group:
    key: tuple
    names: list[str] = field(default_factory=list)
    sizes: list[int] = field(default_factory=list)
    segs: list = field(default_factory=list)  # 階層ごと: 日時の階層はトークンごとの値の集合の並び、そのほかは SegStat
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
    """Blob 名（と大きさ）から、パスパターンの候補を件数の多い順に返します。

    候補: pattern、count、bytes、examples、first・last（日時の変数 dt の最小・最大）、variables（変数の定義。
    選択肢の変数は {"type": "choice", "value": {"options": [...], "selected": [...]}}、日時は {"type": "datetime"}）
    """
    groups: dict[tuple, Group] = {}
    for name, size in items:
        toks = tokenize(name)
        segs = _split_segments(toks)
        key = tuple(_seg_key(s, i == len(segs) - 1) for i, s in enumerate(segs))
        g = groups.get(key)
        if g is None:
            g = groups[key] = Group(key, segs=[[set() for _ in s] if not isinstance(k, str) else SegStat() for s, k in zip(segs, key)])
        g.names.append(name)
        g.sizes.append(size or 0)
        for s, k, st in zip(segs, key, g.segs):
            if isinstance(k, str):
                st.add(s)
            else:
                for i, t in enumerate(s):
                    if len(st[i]) <= MAX_CHOICES + 1:
                        st[i].add(t.text)
        d = _date_of(toks)
        if d is not None:
            g.dates.append(d)
    merged: dict[str, dict] = {}
    for g in groups.values():
        pat, variables = _pattern_of(g)
        c = merged.get(pat)
        if c is None:
            c = merged[pat] = {"pattern": pat, "count": 0, "bytes": 0, "examples": [], "first": None, "last": None,
                               "variables": variables}
        else:
            for vn, vd in variables.items():
                old = c["variables"].get(vn)
                if old and old.get("type") == "choice" and vd.get("type") == "choice":
                    opts = sorted(set(old["value"]["options"]) | set(vd["value"]["options"]), key=_natural)
                    old["value"] = {"options": opts, "selected": list(opts)}
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


def _natural(s: str):
    """自然な順（web2 が web10 より前）の比較キー。"""
    return [(0, int(p), "") if p.isdigit() else (1, 0, p.lower()) for p in re.findall(r"\d+|\D+", s)]


def _tokens_pattern(shape: tuple, vals: list[set]) -> str:
    parts: list[str] = []
    for key, vs in zip(shape, vals):
        if key.startswith("D:"):
            _, var, fmt = key.split(":")
            parts.append("{{" + f"{var}={fmt}" + "}}")
        elif key in ("N", "A"):
            if len(vs) == 1:
                parts.append(escape(next(iter(vs))))
            elif len(vs) <= MAX_CHOICES:
                parts.append("(?:" + "|".join(escape(v) for v in sorted(vs)) + ")")
            elif key == "N":
                lens = {len(v) for v in vs}
                parts.append(f"\\d{{{lens.pop()}}}" if len(lens) == 1 else "\\d+")
            else:
                parts.append("[A-Za-z]+")
        else:
            parts.append(escape(key))
    return "".join(parts)


def _pattern_of(g: Group) -> tuple[str, dict]:
    """グループのパスパターンと、変数の定義。"""
    out: list[str] = []
    variables: dict[str, dict] = {}
    n_choice = 0
    prev_lit = None  # 直前の階層が 1 種類の値のとき、その文字（選択肢の変数の表示名に使います）
    for k, st in zip(g.key, g.segs):
        prev, prev_lit = prev_lit, None
        if not isinstance(k, str):
            shape = k[1]
            out.append(_tokens_pattern(shape, st))
            for key in shape:
                if key.startswith("D:"):
                    variables.setdefault(key.split(":")[1], {"type": "datetime"})
            continue
        vals = list(st.values)
        if not st.overflow and len(vals) == 1:
            out.append(escape(vals[0]))
            prev_lit = vals[0]
            continue
        any_pre, suf = _cut_prefix(st.prefix or ""), _cut_suffix(st.suffix or "")
        # 選択肢では、共通の先頭は「vm=」の形のときだけ文字として残します（選択肢は、なるべく名前の全体で示すため）
        pre = any_pre if any_pre.endswith("=") else ""
        if len(st.shapes) == 1:
            shape = next(iter(st.shapes))
            if all(shape[i] == "N" for i, vs in enumerate(st.tokvals) if len(vs) > 1):
                # 数字だけが変わる階層（server1、server2…）は、選択肢にせず \d の形にします
                out.append(_tokens_pattern(shape, st.tokvals))
                continue
        if not st.overflow and "" not in vals:
            # 値の種類が少ない階層は、選択肢の変数（共通の先頭・末尾の区切りまでは文字として残します）
            core = [v[len(pre):len(v) - len(suf)] for v in vals]
            if any(c == "" for c in core) or len(pre) + len(suf) >= st.minlen:
                pre, suf, core = "", "", vals
            n_choice += 1
            name = _choice_name(pre, n_choice, variables)
            opts = sorted(set(core), key=_natural)
            variables[name] = {"type": "choice", "value": {"options": opts, "selected": list(opts)}}
            if not pre and prev and len(prev) <= 32 and "=" not in prev:
                variables[name]["label"] = prev
            out.append(escape(pre) + "{{" + name + "}}" + escape(suf))
            continue
        if len(st.shapes) == 1:
            out.append(_tokens_pattern(next(iter(st.shapes)), st.tokvals))
        else:
            if len(any_pre) + len(suf) >= st.minlen:
                any_pre, suf = "", ""  # 先頭と末尾が重なる短い名前があるとき
            out.append(escape(any_pre) + "[^/]+" + escape(suf))
    return "/".join(out), variables


_IDENT = re.compile(r"([A-Za-z_][A-Za-z0-9_]{0,31})=$")


def _choice_name(prefix: str, n: int, used: dict) -> str:
    """選択肢の変数の名前。「vm=」のような先頭があればその名前、なければ v1、v2…。"""
    m = _IDENT.search(prefix)
    if m and m.group(1) not in used and not m.group(1).lower().startswith("dt"):
        return m.group(1)
    name = f"v{n}"
    while name in used:
        n += 1
        name = f"v{n}"
    return name


# ---------------------------------------------------------------- Blob 名の収集

MODES = ("sample", "first", "all")


class Cancelled(Exception):
    pass


class NameCache:
    """集めた Blob 名の一時的な保持（依頼者の依頼。decisions.md）。

    接続先と範囲（mode・prefix・limit）ごとに、ttl 秒の間、メモリにだけ保持します（保存しません）。
    「すべて」で集めた名前があれば、同じ接続先のより狭い prefix・「先頭から N 件」・「見本」にも使います。
    """

    def __init__(self, ttl: float = 3600.0, max_entries: int = 3, max_names: int = 500_000,
                 clock: Callable[[], float] | None = None) -> None:
        import time
        self.ttl, self.max_entries, self.max_names = ttl, max_entries, max_names
        self.clock = clock or time.time
        self._lock = threading.Lock()
        self._items: dict[tuple, tuple[float, list, dict]] = {}

    def _purge(self, now: float) -> None:
        for k in [k for k, (t, _, _) in self._items.items() if now - t > self.ttl]:
            del self._items[k]

    def get(self, target_key: tuple, mode: str, prefix: str, limit: int):
        """(集めた時刻, 名前の一覧, info) か None。"""
        now = self.clock()
        with self._lock:
            self._purge(now)
            hit = self._items.get((target_key, mode, prefix, limit if mode == "first" else None))
            if hit is not None:
                return hit
            # 「すべて」の結果（prefix が要求の prefix の先頭と一致するもの）から作ります
            for (tk, m, p, _), (t, items, info) in self._items.items():
                if tk != target_key or m != "all" or not prefix.startswith(p):
                    continue
                sub = [x for x in items if x[0].startswith(prefix)] if prefix != p else items
                out_info = dict(info)
                if mode == "first" and len(sub) > limit:
                    sub = sub[:limit]
                    out_info.update(truncated=True, complete=False)
                return t, sub, out_info
        return None

    def put(self, target_key: tuple, mode: str, prefix: str, limit: int, items: list, info: dict) -> float:
        now = self.clock()
        if len(items) > self.max_names:
            return now
        with self._lock:
            self._purge(now)
            self._items[(target_key, mode, prefix, limit if mode == "first" else None)] = (now, items, dict(info))
            while len(self._items) > self.max_entries:
                oldest = min(self._items, key=lambda k: self._items[k][0])
                del self._items[oldest]
        return now

    def clear_target(self, target_key: tuple) -> None:
        with self._lock:
            for k in [k for k in self._items if k[0] == target_key]:
                del self._items[k]


def collect(backend, target: ContainerRef, *, mode: str = "sample", prefix: str = "", limit: int = 10000,
            per_level: int = 10, max_depth: int = 40, max_requests: int = 500, cancel: threading.Event | None = None,
            progress: Callable[[int, int], None] | None = None,
            connection: Callable[[], object] | None = None) -> tuple[list[tuple[str, int | None]], dict]:
    """Blob 名を集めます。戻り値は [(名前, 大きさ)] と、要求の回数・打ち切ったかの情報。

    - sample: 階層ごとに、まず名前の順の一覧の 1 ページ（最大 5,000 件）を取ります。1 ページで終われば、その下は
      すべて取れたので、たどりません。終わらなければ、下の階層の名前を取り、最大 per_level 件の見本（先頭・末尾・
      等間隔）のうち、1 ページに含まれなかった階層だけを下へたどります（要求は最大 max_requests 回）
    - first: 名前の順に、先頭から limit 件
    - all: すべて（limit は無視）

    info: requests（要求の回数）、truncated（上限で打ち切った）、complete（すべての Blob を集めた。sample で見本だけを
    たどった場合は False）、sizes_known（すべての大きさが分かっている。sample で階層の名前だけ取れた Blob は不明）
    """
    from contextlib import nullcontext
    cancel = cancel or threading.Event()
    connection = connection or nullcontext
    progress = progress or (lambda n, r: None)
    out: list[tuple[str, int | None]] = []
    info = {"requests": 0, "truncated": False, "sizes_known": True, "complete": True}
    if mode == "sample":
        seen: set[str] = set()

        def add(name: str, size: int | None) -> None:
            if name not in seen:
                seen.add(name)
                out.append((name, size))
                if size is None:
                    info["sizes_known"] = False

        stack = [(prefix, 0)]
        while stack:
            if cancel.is_set():
                raise Cancelled()
            if info["requests"] + 2 > max_requests:
                info["truncated"] = True
                break
            pre, depth = stack.pop()
            with connection():
                page = backend.list_blobs_page(target, pre, None)
            info["requests"] += 1
            for b in page.items:
                add(b.name, b.size)
            if page.continuation is None:
                progress(len(out), info["requests"])
                continue
            last = page.items[-1].name if page.items else pre
            with connection():
                children = backend.walk_prefixes(target, pre, limit=5000)
            info["requests"] += 1
            dirs = []
            for c in children:
                if not c.endswith("/"):
                    add(pre + c, None)
                elif not covered_by_trial(pre + c, last):
                    dirs.append(c)
            if depth < max_depth:
                # 深さ優先で、見本の階層を逆の順に積みます（先頭の階層から先にたどるため）
                for d in reversed(_spread(dirs, per_level)):
                    stack.append((pre + d, depth + 1))
                if len(dirs) > per_level or len(children) >= 5000:
                    info["complete"] = False
            elif dirs:
                info["truncated"] = True
            progress(len(out), info["requests"])
            if len(out) >= 200000:
                info["truncated"] = True
                break
        if info["truncated"]:
            info["complete"] = False
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
                info["complete"] = not info["truncated"]
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

