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
#: 項目を選択肢の変数にする、値の種類の上限
MAX_SEG_CHOICES = 100
#: 数字だけの項目を選択肢の変数にする、値の種類の上限（多ければ \d の形にします）
MAX_DIGIT_CHOICES = 20
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


#: 階層の中で「項目」を区切る文字。ハイフンは名前の一部（vm-app-01 など）として扱うため、区切りにしません
_DELIM = re.compile(r"([_. ]+)")


def _parts(seg: list[Tok]) -> list[tuple]:
    """階層を、日時（D）・区切り（L）・項目（F）の並びにします。

    日時のトークンを目印にし、その間の文字を `_` `.` 空白で項目に分けます（例: `VM-APP01_監査_20261121.log` →
    F(VM-APP01) L(_) F(監査) L(_) D(YYYYMMDD) L(.) F(log)）。
    """
    out: list[tuple] = []
    buf: list[str] = []

    def flush() -> None:
        text = "".join(buf)
        buf.clear()
        for k, piece in enumerate(_DELIM.split(text)):
            if piece:
                out.append(("L", piece) if k % 2 else ("F", piece))

    for t in seg:
        if t.kind == "date":
            flush()
            out.append(("D", t.var, t.fmt, t.text))
        else:
            buf.append(t.text)
    flush()
    return out


def _seg_key(parts: list[tuple], last: bool) -> tuple:
    """まとめるためのキー: 日時は書式、区切りはその文字、項目は "F"。

    最後の階層（ファイル名）の拡張子（最後の「.」の後の項目）は値を含めます（a/*.csv と b/readme.md をまとめないため）。
    """
    key = []
    for i, p in enumerate(parts):
        if p[0] == "D":
            key.append(f"D:{p[1]}:{p[2]}")
        elif p[0] == "L":
            key.append("L:" + p[1])
        elif last and i == len(parts) - 1 and i > 0 and parts[i - 1][0] == "L" and parts[i - 1][1].endswith(".") \
                and len(p[1]) <= 5:
            key.append("E:" + p[1].lower())
        else:
            key.append("F")
    return tuple(key)


@dataclass
class FieldStat:
    """項目（日時・区切り以外の部分）の値の集計。"""
    values: dict[str, int] = field(default_factory=dict)
    overflow: bool = False  # 値の種類が MAX_SEG_CHOICES を超えた
    shapes: set = field(default_factory=set)  # 英字・数字の並びの形（2 種類まで）
    tokvals: list[set] = field(default_factory=list)  # 最初の形のときの並びごとの値
    prefix: str | None = None  # すべての値に共通の先頭
    suffix: str | None = None  # すべての値に共通の末尾
    minlen: int = 1 << 30  # 値の長さの最小
    digits: bool = True  # すべての値が数字だけか

    def add(self, text: str) -> None:
        if not self.overflow:
            self.values[text] = self.values.get(text, 0) + 1
            if len(self.values) > MAX_SEG_CHOICES:
                self.overflow = True
        self.prefix = text if self.prefix is None else _common_prefix(self.prefix, text)
        self.suffix = text if self.suffix is None else _common_suffix(self.suffix, text)
        self.minlen = min(self.minlen, len(text))
        self.digits = self.digits and text.isdigit()
        runs = _RUN.findall(text)
        shape = tuple("N" if r.isdigit() else "A" if r.isalpha() and r.isascii() else r for r in runs)
        if not self.shapes:
            self.tokvals = [set() for _ in runs]
        if len(self.shapes) < 2:
            self.shapes.add(shape)
        if len(self.shapes) == 1:
            for i, r in enumerate(runs):
                if len(self.tokvals[i]) <= MAX_CHOICES + 1:
                    self.tokvals[i].add(r)


def _common_prefix(a: str, b: str) -> str:
    n = 0
    while n < len(a) and n < len(b) and a[n] == b[n]:
        n += 1
    return a[:n]


@dataclass
class Group:
    key: tuple
    names: list[str] = field(default_factory=list)
    sizes: list[int] = field(default_factory=list)
    stats: list = field(default_factory=list)  # 階層ごと・部分ごとの集計（F は FieldStat、D・L は None）
    dates: list[datetime] = field(default_factory=list)  # 日時の変数 dt の値
    dates_merged: list[datetime] = field(default_factory=list)  # dt と、dt2 以降を合わせた値
    conflict: set = field(default_factory=set)  # dt と値が食い違うことがある日時の変数（dt2 など）


def _date_parts(toks: list[Tok]) -> dict[str, dict[str, int]]:
    """日時の変数ごとの単位の値（{"dt": {"Y": 2026, "M": 11, ...}, "dt2": {...}}）。"""
    out: dict[str, dict[str, int]] = {}
    for t in toks:
        if t.kind == "date":
            parts = out.setdefault(t.var or "dt", {})
            pos = 0
            for f in re.findall(r"YYYY|MM|DD|hh|mm|ss", t.fmt or ""):
                ln = 4 if f == "YYYY" else 2
                parts[_UNIT_OF[f]] = int(t.text[pos:pos + ln])
                pos += ln
    return out


def _to_datetime(parts: dict[str, int] | None) -> datetime | None:
    if not parts or parts.get("Y") is None:
        return None
    try:
        return datetime(parts["Y"], parts.get("M", 1), parts.get("D", 1), parts.get("h", 0), parts.get("m", 0), parts.get("s", 0))
    except ValueError:
        return None


def _date_of(toks: list[Tok]) -> datetime | None:
    return _to_datetime(_date_parts(toks).get("dt"))


def infer(items: Iterable[tuple[str, int | None]], *, max_candidates: int = MAX_CANDIDATES) -> list[dict]:
    """Blob 名（と大きさ）から、パスパターンの候補を件数の多い順に返します。

    候補: pattern、count、bytes、examples、first・last（日時の変数 dt の最小・最大）、variables（変数の定義。
    選択肢の変数は {"type": "choice", "value": {"options": [...], "selected": [...]}}、日時は {"type": "datetime"}）
    """
    groups: dict[tuple, Group] = {}
    for name, size in items:
        toks = tokenize(name)
        segs = [_parts(s) for s in _split_segments(toks)]
        key = tuple(_seg_key(p, i == len(segs) - 1) for i, p in enumerate(segs))
        g = groups.get(key)
        if g is None:
            g = groups[key] = Group(key, stats=[[FieldStat() if k == "F" or k.startswith("E:") else None for k in sk]
                                                for sk in key])
        g.names.append(name)
        g.sizes.append(size or 0)
        for parts, st in zip(segs, g.stats):
            for p, s in zip(parts, st):
                if s is not None:
                    s.add(p[1])
        dparts = _date_parts(toks)
        base = dparts.get("dt")
        d = _to_datetime(base)
        if d is not None:
            g.dates.append(d)
        if len(dparts) > 1:
            # 同じ日時を 2 回書いた名前（2026/11/21/app_2026112109.log）は、値が食い違わなければ 1 つの dt にします
            comb = dict(base or {})
            for var, p in dparts.items():
                if var == "dt":
                    continue
                if base is None or any(base[u] != v for u, v in p.items() if u in base):
                    g.conflict.add(var)
                comb.update({u: v for u, v in p.items() if u not in comb})
            dm = _to_datetime(comb)
            if dm is not None:
                g.dates_merged.append(dm)
    merged: dict[str, dict] = {}
    for g in groups.values():
        pat, variables = _pattern_of(g)
        extra = [v for v in variables if v != "dt" and variables[v].get("type") == "datetime"]
        same = [v for v in extra if v not in g.conflict] if "dt" in variables else []
        for v in same:
            pat = pat.replace("{{" + v + "=", "{{dt=")
            del variables[v]
        if same and len(same) == len(extra) and len(g.dates_merged) == len(g.dates):
            g.dates = g.dates_merged
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
    """英字・数字の並びの形から、正規表現を作ります（種類の多い項目に使います）。"""
    parts: list[str] = []
    for key, vs in zip(shape, vals):
        if key in ("N", "A"):
            if len(vs) == 1:
                parts.append(escape(next(iter(vs))))
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
    segs_out: list[str] = []
    variables: dict[str, dict] = {}
    n_choice = 0
    prev_lit = None  # 直前の階層が 1 種類の値の 1 項目だけのとき、その文字（選択肢の変数の表示名に使います）
    for sk, st in zip(g.key, g.stats):
        prev, prev_lit = prev_lit, None
        whole = len(sk) == 1  # 階層の全体が 1 つの項目
        out: list[str] = []
        for k, s in zip(sk, st):
            if k.startswith("D:"):
                _, var, fmt = k.split(":")
                out.append("{{" + f"{var}={fmt}" + "}}")
                variables.setdefault(var, {"type": "datetime"})
                continue
            if k.startswith("L:"):
                out.append(escape(k[2:]))
                continue
            vals = list(s.values)
            if not s.overflow and len(vals) == 1:
                out.append(escape(vals[0]))
                if whole:
                    prev_lit = vals[0]
                continue
            if s.digits and (s.overflow or len(vals) > MAX_DIGIT_CHOICES):
                # 数字だけの項目で、値の種類が多いもの（連番など）は \d の形にします
                lens = {len(v) for v in vals}
                out.append(f"\\d{{{lens.pop()}}}" if len(lens) == 1 and not s.overflow else "\\d+")
                continue
            if not s.overflow:
                # 値の種類が少ない項目は、項目の全体を選択肢の変数にします（名前の途中の違う部分だけを選択肢にしません）。
                # 共通の先頭が「vm=」の形なら、その部分は文字として残し、変数の名前にします
                pre = _cut_key(s.prefix or "")
                core = [v[len(pre):] for v in vals]
                if any(c == "" for c in core):
                    pre, core = "", vals
                n_choice += 1
                name = _choice_name(pre, n_choice, variables)
                opts = sorted(set(core), key=_natural)
                variables[name] = {"type": "choice", "value": {"options": opts, "selected": list(opts)}}
                if whole and not pre and prev and len(prev) <= 32 and "=" not in prev:
                    variables[name]["label"] = prev
                out.append(escape(pre) + "{{" + name + "}}")
                continue
            if len(s.shapes) == 1:
                out.append(_tokens_pattern(next(iter(s.shapes)), s.tokvals))
            else:
                # 種類が多く、形もそろわない項目: 共通の先頭・末尾（区切りの文字まで）は文字として残します
                pre, suf = _cut_prefix(s.prefix or ""), _cut_suffix(s.suffix or "")
                if len(pre) + len(suf) >= s.minlen:
                    pre, suf = "", ""
                out.append(escape(pre) + "[^/]+" + escape(suf))
        segs_out.append("".join(out))
    return "/".join(segs_out), variables


def _common_suffix(a: str, b: str) -> str:
    n = 0
    while n < len(a) and n < len(b) and a[-1 - n] == b[-1 - n]:
        n += 1
    return a[len(a) - n:]


def _cut_prefix(p: str) -> str:
    """共通の先頭を、最後の区切りの文字（英数字以外）までに切り詰めます。"""
    for i in range(len(p) - 1, -1, -1):
        if not p[i].isalnum():
            return p[:i + 1]
    return ""


def _cut_suffix(s: str) -> str:
    """共通の末尾を、最初の区切りの文字（英数字以外）からに切り詰めます。"""
    for i, c in enumerate(s):
        if not c.isalnum():
            return s[i:]
    return ""


def _cut_key(p: str) -> str:
    """共通の先頭のうち、「vm=」のような「名前=」の部分（なければ空）。"""
    i = p.rfind("=")
    return p[:i + 1] if i >= 0 else ""


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
            connection: Callable[[], object] | None = None, page_size: int = 5000) -> tuple[list[tuple[str, int | None]], dict]:
    """Blob 名を集めます。戻り値は [(名前, 大きさ)] と、要求の回数・打ち切ったかの情報。

    - sample: 階層ごとに、まず名前の順の一覧の 1 ページ（page_size 件。既定 5,000 件）を取ります。1 ページで終われば、その下は
      すべて取れたので、たどりません。終わらなければ、下の階層の名前を取り、最大 per_level 件の見本（先頭・末尾・
      等間隔）のうち、1 ページに含まれなかった階層だけを下へたどります（要求は最大 max_requests 回）
    - first: 名前の順に、先頭から limit 件
    - all: すべて（limit は無視）

    info: requests（要求の回数）、truncated（上限で打ち切った）、complete（すべての Blob を集めた。sample で見本だけを
    たどった場合は False）、sizes_known（すべての大きさが分かっている。sample で階層の名前だけ取れた Blob は不明）
    """
    from contextlib import nullcontext
    page_size = max(1, min(5000, int(page_size)))  # 1 回の要求で取得する件数（設定 search.page_size。v1.5.0）
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
                page = backend.list_blobs_page(target, pre, None, page_size)
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
            page = backend.list_blobs_page(target, prefix, cont, page_size)
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

