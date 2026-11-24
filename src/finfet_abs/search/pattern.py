"""パスパターン → 正規表現（3.2.2、3.2.7、7.3.2）。

フィルター（正規表現と追加の判定）と prefix の一覧は、同じ正規表現の文字列から作ります。
"""

from __future__ import annotations

import itertools
import re
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime
from typing import Callable

from . import datetime_vars as dtv
from . import prefix as prefixmod
from .datetime_vars import FmtItem, Zone
from .variables import (MAX_ENUM, NAME_RE, VariableError, choice_as_string, exclude_check, number_fragment,
                        number_values, string_fragment, string_values)

#: コンテナー名の展開の上限（3.2.8）
MAX_CONTAINERS = 100


@dataclass
class Issue:
    """パスパターンの誤り。pos は元の文字列の位置（0 始まり。None は位置なし）。"""

    pos: int | None
    message: str
    name: str | None = None

    def to_json(self) -> dict:
        return {"pos": None if self.pos is None else self.pos + 1, "message": self.message, "name": self.name}


class PatternError(ValueError):
    """パスパターン・変数の誤り（誤りの一覧を持ちます）。"""

    def __init__(self, issues: list[Issue]) -> None:
        super().__init__("; ".join(i.message for i in issues))
        self.issues = issues


@dataclass
class Token:
    kind: str  # "re" / "var"
    text: str
    start: int
    end: int
    name: str = ""
    fmt: str | None = None
    items: list[FmtItem] | None = None


def tokenize(pattern: str, *, literal: bool = False) -> tuple[list[Token], list[Issue]]:
    """`{{…}}` で分けます。literal=True（コンテナー名）では、変数以外の部分を文字そのものとして扱います。"""
    issues: list[Issue] = []
    tokens: list[Token] = []
    s = pattern or ""
    offset = 0
    if not literal:
        while s.startswith("/"):
            s = s[1:]
            offset += 1
    i = 0
    buf = ""
    buf_start = 0

    def flush(end: int) -> None:
        nonlocal buf
        if buf:
            text = re.escape(buf) if literal else buf
            tokens.append(Token("re", text, offset + buf_start, offset + end))
            buf = ""

    while i < len(s):
        if not literal and s[i] == "\\" and i + 1 < len(s):
            if not buf:
                buf_start = i
            buf += s[i:i + 2]
            i += 2
            continue
        if s.startswith("{{", i):
            flush(i)
            j = s.find("}}", i + 2)
            if j < 0:
                issues.append(Issue(offset + i, "「{{」が閉じていません。"))
                break
            inner = s[i + 2:j]
            name, fmt = (inner.split("=", 1) + [None])[:2] if "=" in inner else (inner, None)
            items = None
            if not NAME_RE.match(name):
                issues.append(Issue(offset + i + 2, f"変数名「{name}」は使用できません（英字か _ で始まる英数字と _ で、32 文字以内）。"))
            elif fmt is not None:
                try:
                    items = dtv.parse_format(fmt)
                except dtv.DateTimeFormatError as exc:
                    issues.append(Issue(offset + i + 3 + len(name) + exc.pos, str(exc), name))
            tokens.append(Token("var", s[i:j + 2], offset + i, offset + j + 2, name, fmt, items))
            i = j + 2
            buf_start = i
            continue
        if not buf:
            buf_start = i
        buf += s[i]
        i += 1
    flush(len(s))
    if not literal:
        for t in tokens:
            if t.kind == "re":
                for m in re.finditer(r"\(\?P[<=]", t.text):
                    issues.append(Issue(t.start + m.start(), "正規表現の名前付きグループ（(?P<名前>…)）は使用できません。"))
    return tokens, issues


def variable_names(text: str) -> list[tuple[str, str | None, int]]:
    """文字列の中の変数（名前, 書式, 位置）の一覧。"""
    out = []
    for m in re.finditer(r"\{\{([^{}]*?)\}\}", text or ""):
        inner = m.group(1)
        name, fmt = (inner.split("=", 1) if "=" in inner else (inner, None))
        out.append((name, fmt, m.start()))
    return out


# ---------------------------------------------------------------- 結果


@dataclass
class DateTimeInfo:
    """日時の変数の、表示用の情報。"""

    name: str
    mode: str
    path_timezone: str
    input_ranges: dtv.Ranges = field(default_factory=list)
    path_ranges: dtv.Ranges = field(default_factory=list)
    ordered: bool = False
    granularity: int = 0


@dataclass
class CompiledPath:
    """コンパイルしたパスパターン。"""

    source: str
    regex: re.Pattern
    checks: list[Callable[[re.Match], bool]]
    prefixes: list[str]
    extractors: list[Callable[[re.Match, dict], None]]
    datetimes: dict[str, DateTimeInfo]
    ignore_case: bool = False

    def match(self, name: str) -> dict | None:
        """Blob 名が一致すれば変数の値（`vars_json` の形）、一致しなければ None。"""
        m = self.regex.fullmatch(name)
        if m is None:
            return None
        for c in self.checks:
            if not c(m):
                return None
        values: dict = {}
        for ex in self.extractors:
            ex(m, values)
        return values


@dataclass
class CompileContext:
    """コンパイルに使う時刻とタイムゾーン。"""

    now_utc: datetime
    local_zone: Zone = field(default_factory=dtv.LocalZone)
    max_prefixes: int = 32

    def zone(self, name: str) -> Zone:
        return dtv.UTC_ZONE if name == "utc" else self.local_zone


# ---------------------------------------------------------------- 日時の変数の正規表現


def _top_level_simple(regex_text: str) -> bool:
    """正規表現の断片が、単独でコンパイルでき、最上位に | を含まないか。"""
    try:
        re.compile(regex_text)
    except re.error:
        return False
    depth = 0
    i = 0
    in_class = False
    while i < len(regex_text):
        c = regex_text[i]
        if c == "\\":
            i += 2
            continue
        if in_class:
            if c == "]":
                in_class = False
        elif c == "[":
            in_class = True
            if i + 1 < len(regex_text) and regex_text[i + 1] == "]":
                i += 1
        elif c == "(":
            depth += 1
        elif c == ")":
            depth -= 1
        elif c == "|" and depth == 0:
            return False
        i += 1
    return depth == 0


class _TooMany(Exception):
    pass


class _OrderedBuilder:
    """日時の記号が上位から順に並ぶ場合の、期間を単位ごとの選択肢に分けた正規表現（7.3.1）。"""

    def __init__(self, symbols: list[str], seps: list[str], first_level: int, limit: int = MAX_ENUM) -> None:
        self.symbols = symbols  # レベル first_level から順の記号
        self.seps = seps  # seps[k]: 記号 k の前に置く正規表現（k=0 は空）
        self.first = first_level
        self.last = first_level + len(symbols) - 1
        self.count = 0
        self.limit = limit

    def sym(self, level: int) -> str:
        return self.symbols[level - self.first]

    def sep(self, level: int) -> str:
        return self.seps[level - self.first]

    def _bump(self, n: int = 1) -> None:
        self.count += n
        if self.count > self.limit:
            raise _TooMany()

    def any_suffix(self, level: int, prefix: tuple[int, ...]) -> str:
        """level 以降のすべての値（暦にある値だけ）。"""
        if level > self.last:
            return ""
        if level >= dtv.HOUR:
            return self.sep(level) + dtv.SHAPES[self.sym(level)] + self.any_suffix(level + 1, prefix)
        return self.sep(level) + self.gen(level, prefix, [(dtv.unit_start(prefix), dtv.unit_end(prefix))]
                                          if prefix else [])

    def gen(self, level: int, prefix: tuple[int, ...], ranges: dtv.Ranges) -> str:
        lo, hi = ranges[0][0], ranges[-1][1]
        groups: dict[str, list[str]] = {}
        sym = self.sym(level)
        for v in dtv.unit_values(prefix, lo, hi):
            p = prefix + (v,)
            if not dtv.valid_fields(p):
                continue
            s, e = dtv.unit_start(p), dtv.unit_end(p)
            if not dtv.overlaps(ranges, s, e):
                continue
            if level == self.last:
                suffix = ""
            elif dtv.covers(ranges, s, e):
                suffix = self.any_suffix(level + 1, p)
            else:
                suffix = self.sep(level + 1) + self.gen(level + 1, p, dtv.clip(ranges, s, e))
            groups.setdefault(suffix, []).append(re.escape(dtv.format_value(sym, v)))
            self._bump()
        parts = []
        for suffix, vals in groups.items():
            head = vals[0] if len(vals) == 1 else "(?:" + "|".join(vals) + ")"
            parts.append(head + suffix)
        if not parts:
            raise ValueError("empty")
        return parts[0] if len(parts) == 1 else "(?:" + "|".join(parts) + ")"


# ---------------------------------------------------------------- コンパイル


def compile_path(pattern: str, variables: dict, *, ignore_case: bool = False, input_timezone: str = "local",
                 ctx: CompileContext, fixed: dict | None = None, literal: bool = False) -> CompiledPath:
    """パスパターンをコンパイルします。誤りがあれば PatternError。

    - variables: 変数の定義（5.5.2 の variables）
    - fixed: コンテナー名の展開で決まった値（{名前: 文字列} / 日時は {名前: {単位: 値}}）
    - literal: True ならコンテナー名（変数以外は文字そのもの）
    """
    fixed = fixed or {}
    tokens, issues = tokenize(pattern, literal=literal)
    if issues:
        raise PatternError(issues)
    occ: dict[str, list[int]] = defaultdict(list)
    for idx, t in enumerate(tokens):
        if t.kind == "var":
            occ[t.name].append(idx)
    input_zone = ctx.zone(input_timezone)

    checks: list[Callable[[re.Match], bool]] = []
    extractors: list[Callable[[re.Match, dict], None]] = []
    frags: dict[str, str] = {}
    dt_symbol_frag: dict[str, dict[str, str]] = {}
    ordered_spans: dict[str, tuple[int, int, str]] = {}
    datetimes: dict[str, DateTimeInfo] = {}

    for name, idxs in occ.items():
        is_dt = [tokens[i].fmt is not None for i in idxs]
        defn = variables.get(name) or {}
        if any(is_dt) and not all(is_dt):
            issues.append(Issue(tokens[idxs[0]].start, f"変数「{name}」に、日時の書式があるもの（{{{{{name}=…}}}}）とないものが混在しています。", name))
            continue
        try:
            if all(is_dt):
                _compile_datetime(name, defn, tokens, idxs, fixed.get(name) or {}, ctx, input_zone, checks,
                                  extractors, dt_symbol_frag, ordered_spans, datetimes)
            else:
                vtype = defn.get("type", "string")
                if vtype == "datetime":
                    raise VariableError(name, f"日時の変数は {{{{{name}=YYYY}}}} のように書式を付けて記述してください。")
                if name in fixed:
                    frags[name] = re.escape(str(fixed[name]))
                elif vtype == "number":
                    frag, check = number_fragment(name, defn.get("value") or {}, defn.get("padding"))
                    frags[name] = frag
                    if check is not None:
                        checks.append(lambda m, n=name, c=check: c(m.group(f"v_{n}")))
                    ex = exclude_check(name, defn.get("value") or {}, number=True)
                    if ex is not None:
                        checks.append(lambda m, n=name, c=ex: c(m.group(f"v_{n}")))
                elif vtype == "choice":
                    frags[name] = string_fragment(name, choice_as_string(name, defn.get("value") or {}))
                else:
                    frags[name] = string_fragment(name, defn.get("value") or {})
                    ex = exclude_check(name, defn.get("value") or {}, ignore_case=ignore_case)
                    if ex is not None:
                        checks.append(lambda m, n=name, c=ex: c(m.group(f"v_{n}")))
                extractors.append(lambda m, vals, n=name: vals.__setitem__(n, m.group(f"v_{n}")))
        except VariableError as exc:
            issues.append(Issue(None, str(exc), name))
        except ValueError as exc:
            issues.append(Issue(None, f"変数「{name}」: {exc}", name))
    if issues:
        raise PatternError(issues)

    # 正規表現の組み立て。segs は誤りの位置を元の文字列の位置に直すための対応。
    out: list[str] = []
    segs: list[tuple[int, int, int, bool]] = []  # (出力の開始, 長さ, 元の開始, 文字単位で対応するか)
    pos = 0
    seen: set[str] = set()
    i = 0
    while i < len(tokens):
        t = tokens[i]
        if t.kind == "var" and t.name in ordered_spans and ordered_spans[t.name][0] == i:
            i0, i1, alt = ordered_spans[t.name]
            text = f"(?P<s_{t.name}>{alt})"
            segs.append((pos, len(text), t.start, False))
            out.append(text)
            pos += len(text)
            i = i1 + 1
            continue
        if t.kind == "re":
            text = t.text
            segs.append((pos, len(text), t.start, not literal))
        elif t.name in dt_symbol_frag:
            parts = []
            for it in t.items:
                if it.symbol is None:
                    parts.append(re.escape(it.text))
                else:
                    key = f"d_{t.name}_{it.symbol}"
                    if key in seen:
                        parts.append(f"(?P={key})")
                    else:
                        seen.add(key)
                        parts.append(f"(?P<{key}>{dt_symbol_frag[t.name][it.symbol]})")
            text = "".join(parts)
            segs.append((pos, len(text), t.start, False))
        else:
            key = f"v_{t.name}"
            if key in seen:
                text = f"(?P={key})"
            else:
                seen.add(key)
                text = f"(?P<{key}>{frags[t.name]})"
            segs.append((pos, len(text), t.start, False))
        out.append(text)
        pos += len(text)
        i += 1
    source = "".join(out)
    flags = re.IGNORECASE if ignore_case else 0
    try:
        regex = re.compile(source, flags)
    except re.error as exc:
        orig = None
        if exc.pos is not None:
            for start, length, ostart, charwise in segs:
                if start <= exc.pos < start + length or (exc.pos == start + length and charwise):
                    orig = ostart + (exc.pos - start if charwise else 0)
                    break
            else:
                orig = segs[-1][2] if segs else 0
        raise PatternError([Issue(orig, f"正規表現の誤り: {_re_message(exc)}")]) from None
    prefixes = prefixmod.compute(source, flags, ctx.max_prefixes)
    return CompiledPath(source, regex, checks, prefixes, extractors, datetimes, ignore_case)


def _re_message(exc: re.error) -> str:
    msg = exc.msg
    table = {
        "missing ), unterminated subpattern": "「(」が閉じていません",
        "unbalanced parenthesis": "「)」に対応する「(」がありません",
        "unterminated character set": "「[」が閉じていません",
        "nothing to repeat": "繰り返しの対象がありません",
        "multiple repeat": "繰り返しの記号が重なっています",
        "bad escape (end of pattern)": "末尾の「\\」の後に文字がありません",
    }
    for k, v in table.items():
        if msg.startswith(k):
            return v
    return msg


def _compile_datetime(name: str, defn: dict, tokens: list[Token], idxs: list[int], fixed_fields: dict,
                      ctx: CompileContext, input_zone: Zone, checks: list, extractors: list,
                      dt_symbol_frag: dict, ordered_spans: dict, datetimes: dict) -> None:
    path_tz = defn.get("path_timezone", "utc")
    path_zone = ctx.zone(path_tz)
    value = dtv.resolve_value(defn.get("value") or {"mode": "any"}, now_utc=ctx.now_utc, input_zone=input_zone,
                              path_zone=path_zone)
    fixed_fields = {int(k): int(v) for k, v in fixed_fields.items()}
    fixed_tuple = tuple(fixed_fields[u] for u in range(len(fixed_fields))) if fixed_fields and \
        set(fixed_fields) == set(range(len(fixed_fields))) else None
    if value.mode == "ranges" and fixed_tuple:
        value.ranges = dtv.clip(value.ranges, dtv.unit_start(fixed_tuple), dtv.unit_end(fixed_tuple))
    symbols_all = [it.symbol for i in idxs for it in tokens[i].items if it.symbol]
    gran = max([dtv.SYMBOLS[s][0] for s in symbols_all] + list(fixed_fields))
    info = DateTimeInfo(name, value.mode, path_tz, value.input_ranges, value.ranges, False, gran)
    datetimes[name] = info
    years = list(range(value.ranges[0][0].year, value.ranges[-1][1].year + 1)) if value.ranges else []

    # 上位から順に並ぶか（7.3.1）
    plan = _ordered_plan(tokens, idxs, fixed_fields, fixed_tuple)
    if plan is not None and value.mode == "ranges" and value.ranges:
        symbols, seps, first_level, pre, post, i0, i1 = plan
        builder = _OrderedBuilder(symbols, seps, first_level)
        try:
            body = builder.gen(first_level, fixed_tuple or (), value.ranges)
        except (_TooMany, ValueError):
            body = None
        if body is not None:
            alt = pre + body + post
            ordered_spans[name] = (i0, i1, alt)
            info.ordered = True
            # 値の読み取り用の正規表現（記号を名前付きグループにした形）
            span_parts = [pre]
            for k, s in enumerate(symbols):
                span_parts.append(seps[k] + f"(?P<u{dtv.SYMBOLS[s][0]}_{s}>{dtv.SHAPES[s]})")
            span_parts.append(post)
            span_re = re.compile("".join(span_parts))

            def extract(m, vals, n=name, span_re=span_re, fx=dict(fixed_fields), years=years):
                sm = span_re.fullmatch(m.group(f"s_{n}"))
                fields = dict(fx)
                if sm:
                    for key, v in sm.groupdict().items():
                        unit, sym = int(key[1]), key.split("_", 1)[1]
                        iv = int(v)
                        fields[unit] = dtv.resolve_yy(iv, years)[0] if sym == "YY" else iv
                vals[n] = {dtv.UNITS[u]: fields[u] for u in sorted(fields)}
            extractors.append(extract)
            return

    # 上位から順に並ばない場合など: 共通する上位の単位を固定した広めの正規表現と、追加の判定
    const = _constant_units(value)
    frag: dict[str, str] = {}
    for s in dict.fromkeys(symbols_all):
        unit = dtv.SYMBOLS[s][0]
        if unit in fixed_fields:
            frag[s] = re.escape(dtv.format_value(s, fixed_fields[unit]))
        elif unit in const:
            frag[s] = re.escape(dtv.format_value(s, const[unit]))
        elif value.mode == "units" and value.units.allowed.get(unit) is not None and \
                len(value.units.allowed[unit]) <= MAX_ENUM:
            vals = sorted(dict.fromkeys(dtv.format_value(s, v) for v in value.units.allowed[unit]),
                          key=lambda x: (-len(x), x))
            frag[s] = "(?:" + "|".join(re.escape(v) for v in vals) + ")"
        else:
            frag[s] = dtv.SHAPES[s]
    dt_symbol_frag[name] = frag

    def read_fields(m: re.Match, n=name, syms=list(dict.fromkeys(symbols_all)), fx=dict(fixed_fields),
                    years=years) -> list[dict[int, int]] | None:
        """グループから単位ごとの値を読み取ります。同じ単位で値が食い違えば None。"""
        fields: dict[int, int] = dict(fx)
        yy = None
        for s in syms:
            unit = dtv.SYMBOLS[s][0]
            v = int(m.group(f"d_{n}_{s}"))
            if s == "YY":
                yy = v
                continue
            if unit in fields and fields[unit] != v:
                return None
            fields[unit] = v
        if yy is not None:
            if dtv.YEAR in fields:
                return [fields] if fields[dtv.YEAR] % 100 == yy else None
            out = []
            for y in dtv.resolve_yy(yy, years or [ctx.now_utc.year]):
                f = dict(fields)
                f[dtv.YEAR] = y
                out.append(f)
            return out
        return [fields]

    def check(m: re.Match, value=value) -> bool:
        cands = read_fields(m)
        if not cands:
            return False
        return any(dtv.match_fields(f, value) for f in cands)

    def extract(m, vals, n=name) -> None:
        cands = read_fields(m) or [{}]
        f = cands[0]
        for cand in cands:
            if dtv.match_fields(cand, value):
                f = cand
                break
        vals[n] = {dtv.UNITS[u]: f[u] for u in sorted(f)}

    checks.append(check)
    extractors.append(extract)


def _constant_units(value: dtv.DateTimeValue) -> dict[int, int]:
    """期間の中で共通する上位の単位の値（年 → 月 → … の順に、変わる単位の前まで）。"""
    if value.mode != "ranges" or not value.ranges:
        return {}
    lo = value.ranges[0][0]
    hi = value.ranges[-1][1] - dtv.timedelta(microseconds=1)
    a = (lo.year, lo.month, lo.day, lo.hour, lo.minute, lo.second)
    b = (hi.year, hi.month, hi.day, hi.hour, hi.minute, hi.second)
    out = {}
    for u in range(6):
        if a[u] != b[u]:
            break
        out[u] = a[u]
    return out


def _ordered_plan(tokens: list[Token], idxs: list[int], fixed_fields: dict, fixed_tuple):
    """上位から順に並ぶ場合の (記号, 区切り, 最初の単位, 前, 後, 最初のトークン, 最後のトークン)。"""
    if fixed_fields and fixed_tuple is None:
        return None
    first_level = len(fixed_fields)
    i0, i1 = idxs[0], idxs[-1]
    symbols: list[str] = []
    seps: list[str] = []
    pre = ""
    cur = ""
    for k in range(i0, i1 + 1):
        t = tokens[k]
        if t.kind == "var" and t.items is None:
            return None  # 間にほかの変数がある
        if t.kind == "var" and t.name != tokens[i0].name:
            return None
        if t.kind == "re":
            if not _top_level_simple(t.text):
                return None
            cur += t.text
            continue
        for it in t.items:
            if it.symbol is None:
                cur += re.escape(it.text)
            else:
                if not symbols:
                    pre = cur
                    seps.append("")
                else:
                    seps.append(cur)
                cur = ""
                symbols.append(it.symbol)
    post = cur
    units = [dtv.SYMBOLS[s][0] for s in symbols]
    if units != list(range(first_level, first_level + len(units))):
        return None
    if not units or (first_level == 0 and units[0] != dtv.YEAR):
        return None
    return symbols, seps, first_level, pre, post, i0, i1


# ---------------------------------------------------------------- コンテナー名


def expand_containers(template: str, variables: dict, *, input_timezone: str, ctx: CompileContext
                      ) -> list[tuple[str, dict]] | None:
    """コンテナー名の変数を展開します（3.2.8、7.3.1）。

    値を列挙できれば [(コンテナー名, 決まった値)] を返し、列挙できなければ None を返します。
    上限（100）を超える場合は PatternError。
    """
    tokens, issues = tokenize(template, literal=True)
    if issues:
        raise PatternError(issues)
    if not any(t.kind == "var" for t in tokens):
        return [(template, {})]
    input_zone = ctx.zone(input_timezone)
    choices: dict[str, list[tuple[str | dict, dict]]] = {}
    names = list(dict.fromkeys(t.name for t in tokens if t.kind == "var"))
    for name in names:
        defn = variables.get(name) or {}
        toks = [t for t in tokens if t.kind == "var" and t.name == name]
        if toks[0].fmt is not None:
            value = dtv.resolve_value(defn.get("value") or {"mode": "any"}, now_utc=ctx.now_utc,
                                      input_zone=input_zone, path_zone=ctx.zone(defn.get("path_timezone", "utc")))
            units = sorted({it.unit for t in toks for it in t.items if it.symbol})
            if value.mode != "ranges" or units != list(range(len(units))):
                return None
            gran = units[-1]
            slots = []
            for s, e in value.ranges:
                cur = dtv.truncate(s, gran)
                while cur < e:
                    vals = (cur.year, cur.month, cur.day, cur.hour, cur.minute, cur.second)[:gran + 1]
                    slots.append(vals)
                    cur = dtv.add_unit(cur, gran, 1)
                    if len(slots) > MAX_CONTAINERS:
                        raise PatternError([Issue(None, f"コンテナー名の展開が多すぎます（上限 {MAX_CONTAINERS} 件）。期間を短くしてください。", name)])
            slots = list(dict.fromkeys(slots))
            choices[name] = [({u: v for u, v in enumerate(sl)}, {}) for sl in slots]
        else:
            vtype = defn.get("type", "string")
            if vtype == "number":
                vals = number_values(defn.get("value") or {}, defn.get("padding"))
            elif vtype == "choice":
                vals = [str(x) for x in (defn.get("value") or {}).get("selected") or [] if str(x) != ""] or None
            else:
                vals = string_values(defn.get("value") or {})
            if not vals:
                return None
            choices[name] = [(v, {}) for v in vals]
    total = 1
    for c in choices.values():
        total *= len(c)
    if total > MAX_CONTAINERS:
        raise PatternError([Issue(None, f"コンテナー名の展開が多すぎます（{total:,} 件。上限 {MAX_CONTAINERS} 件）。")])
    out = []
    for combo in itertools.product(*(choices[n] for n in names)):
        fixed = {n: c[0] for n, c in zip(names, combo)}
        parts = []
        for t in tokens:
            if t.kind == "re":
                parts.append(re.sub(r"\\(.)", r"\1", t.text))
            elif t.fmt is not None:
                parts.append(dtv.format_fields(t.items, fixed[t.name]))
            else:
                parts.append(str(fixed[t.name]))
        out.append(("".join(parts), fixed))
    return out


def container_literal_prefix(template: str) -> str:
    """コンテナー名の、最初の変数より前の部分（コンテナーの一覧の取得に使います）。"""
    i = template.find("{{")
    return template if i < 0 else template[:i]
