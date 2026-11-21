"""日時の変数（3.2.4、7.3.1）。

- 書式の解析（`YYYY`・`MM`… と文字の列）
- 期間の入力の解析、間隔、単位ごとの指定、相対的な期間
- 期間の入力の基準（ローカル / UTC）から、パス上の日時の基準への変換
- 一致の判定（粒度の区間と期間の重なり）

時刻は、パス上の日時の基準の「壁時計の時刻」（タイムゾーンのない datetime）で扱います。
期間は、開始を含み終了を含まない区間 `[start, end)` の並びです。
"""

from __future__ import annotations

import calendar
import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

UNITS = ("year", "month", "day", "hour", "minute", "second")
UNIT_LABELS = ("年", "月", "日", "時", "分", "秒")
YEAR, MONTH, DAY, HOUR, MINUTE, SECOND = range(6)

#: 記号 → (単位, ゼロ埋めの桁数。None はゼロ埋めなし)
SYMBOLS: dict[str, tuple[int, int | None]] = {
    "YYYY": (YEAR, 4), "YY": (YEAR, 2),
    "MM": (MONTH, 2), "M": (MONTH, None),
    "DD": (DAY, 2), "D": (DAY, None),
    "hh": (HOUR, 2), "h": (HOUR, None),
    "mm": (MINUTE, 2), "m": (MINUTE, None),
    "ss": (SECOND, 2), "s": (SECOND, None),
}
#: 長い記号から順に照合します
_SYMBOL_ORDER = ["YYYY", "YY", "MM", "DD", "hh", "mm", "ss", "M", "D", "h", "m", "s"]
#: 書き間違いとして誤りにする文字
_TYPO = set("ydHS")
#: 書式の全体の別名
ALIASES = {"year": "YYYY", "month": "MM", "day": "DD", "hour": "hh", "minute": "mm", "second": "ss"}

#: 記号の形（値の指定が「任意」のときの正規表現）
SHAPES = {
    "YYYY": r"\d{4}", "YY": r"\d{2}",
    "MM": r"(?:0[1-9]|1[0-2])", "M": r"(?:1[0-2]|[1-9])",
    "DD": r"(?:0[1-9]|[12]\d|3[01])", "D": r"(?:3[01]|[12]\d|[1-9])",
    "hh": r"(?:[01]\d|2[0-3])", "h": r"(?:2[0-3]|1\d|\d)",
    "mm": r"[0-5]\d", "m": r"(?:[1-5]\d|\d)",
    "ss": r"[0-5]\d", "s": r"(?:[1-5]\d|\d)",
}

MAX_STEPS = 5000


class DateTimeFormatError(ValueError):
    """日時の書式の誤り。pos は書式の中の位置（0 始まり）。"""

    def __init__(self, message: str, pos: int = 0) -> None:
        super().__init__(message)
        self.pos = pos


@dataclass(frozen=True)
class FmtItem:
    """書式の1要素。symbol が None なら文字（text）。"""

    symbol: str | None
    text: str = ""

    @property
    def unit(self) -> int:
        return SYMBOLS[self.symbol][0]


def parse_format(fmt: str) -> list[FmtItem]:
    """書式を解析します（7.3.1）。"""
    if fmt in ALIASES:
        fmt = ALIASES[fmt]
    if not fmt:
        raise DateTimeFormatError("日時の書式が空です。", 0)
    items: list[FmtItem] = []
    i = 0
    lit = ""
    while i < len(fmt):
        for sym in _SYMBOL_ORDER:
            if fmt.startswith(sym, i):
                if lit:
                    items.append(FmtItem(None, lit))
                    lit = ""
                items.append(FmtItem(sym))
                i += len(sym)
                break
        else:
            c = fmt[i]
            if c in _TYPO:
                hint = {"y": "年は YYYY", "d": "日は DD", "H": "時は hh", "S": "秒は ss"}[c]
                raise DateTimeFormatError(f"日時の書式に「{c}」は使えません（{hint} と書きます）。", i)
            lit += c
            i += 1
    if lit:
        items.append(FmtItem(None, lit))
    if not any(it.symbol for it in items):
        raise DateTimeFormatError("日時の書式に、年・月・日などの記号がありません。", 0)
    return items


def format_value(symbol: str, value: int) -> str:
    """記号に合わせて値を文字にします。"""
    unit, width = SYMBOLS[symbol]
    if symbol == "YY":
        value %= 100
    return f"{value:0{width}d}" if width else str(value)


def format_fields(items: list[FmtItem], fields: dict[int, int]) -> str:
    """書式と単位ごとの値から文字列を作ります。"""
    out = []
    for it in items:
        if it.symbol is None:
            out.append(it.text)
        else:
            out.append(format_value(it.symbol, fields[it.unit]))
    return "".join(out)


# ---------------------------------------------------------------- 単位の区間


def unit_start(fields: tuple[int, ...]) -> datetime:
    """上位から並んだ値（年, 月, …）の区間の開始。"""
    y = fields[0]
    mo = fields[1] if len(fields) > 1 else 1
    d = fields[2] if len(fields) > 2 else 1
    hh = fields[3] if len(fields) > 3 else 0
    mi = fields[4] if len(fields) > 4 else 0
    ss = fields[5] if len(fields) > 5 else 0
    return datetime(y, mo, d, hh, mi, ss)


def add_unit(dt: datetime, unit: int, n: int = 1) -> datetime:
    """dt に単位 n 個分を足します（月・年は月末に丸めます）。"""
    if unit == YEAR:
        return add_months(dt, 12 * n)
    if unit == MONTH:
        return add_months(dt, n)
    return dt + timedelta(**{("days", "hours", "minutes", "seconds")[unit - 2]: n})


def add_months(dt: datetime, n: int) -> datetime:
    m = dt.month - 1 + n
    y = dt.year + m // 12
    m = m % 12 + 1
    d = min(dt.day, calendar.monthrange(y, m)[1])
    return dt.replace(year=y, month=m, day=d)


def unit_end(fields: tuple[int, ...]) -> datetime:
    """上位から並んだ値の区間の終了（含まない）。"""
    return add_unit(unit_start(fields), len(fields) - 1, 1)


def truncate(dt: datetime, unit: int) -> datetime:
    """dt を unit の区間の開始に切り捨てます。"""
    vals = (dt.year, dt.month, dt.day, dt.hour, dt.minute, dt.second)
    return unit_start(vals[:unit + 1])


def valid_fields(fields: tuple[int, ...]) -> bool:
    """暦にある値か（2月30日などを除く）。"""
    try:
        unit_start(fields)
        return True
    except ValueError:
        return False


def unit_values(fields: tuple[int, ...], lo: datetime, hi: datetime) -> range:
    """親の値 fields の下で、[lo, hi) に重なりうる次の単位の値の範囲。"""
    level = len(fields)
    if level == YEAR:
        return range(lo.year, (hi - timedelta(microseconds=1)).year + 1)
    if level == MONTH:
        return range(1, 13)
    if level == DAY:
        return range(1, calendar.monthrange(fields[0], fields[1])[1] + 1)
    if level == HOUR:
        return range(0, 24)
    return range(0, 60)


# ---------------------------------------------------------------- 期間（区間の並び）

Ranges = list[tuple[datetime, datetime]]


def merge_ranges(ranges: Ranges) -> Ranges:
    """重なる区間・隣り合う区間を1つにまとめます。"""
    out: Ranges = []
    for s, e in sorted(r for r in ranges if r[0] < r[1]):
        if out and s <= out[-1][1]:
            if e > out[-1][1]:
                out[-1] = (out[-1][0], e)
        else:
            out.append((s, e))
    return out


def clip(ranges: Ranges, lo: datetime, hi: datetime) -> Ranges:
    out = []
    for s, e in ranges:
        s2, e2 = max(s, lo), min(e, hi)
        if s2 < e2:
            out.append((s2, e2))
    return out


def overlaps(ranges: Ranges, lo: datetime, hi: datetime) -> bool:
    return any(s < hi and e > lo for s, e in ranges)


def covers(ranges: Ranges, lo: datetime, hi: datetime) -> bool:
    """[lo, hi) が区間の並びに完全に含まれるか（並びはまとめ済み）。"""
    return any(s <= lo and e >= hi for s, e in ranges)


_PERIOD_SEP = re.compile(
    r"^(\d{4})(?:[-/](\d{1,2})(?:[-/](\d{1,2})(?:[ T](\d{1,2})(?::(\d{1,2})(?::(\d{1,2}))?)?)?)?)?$")
_PERIOD_DIGITS = re.compile(r"^\d{4}(?:\d{2}){0,5}$")


def parse_period_point(text: str) -> tuple[tuple[int, ...], int]:
    """期間の入力の1つの端を解析し、(上位からの値, 最も細かい単位) を返します（7.3.1）。"""
    s = (text or "").strip()
    m = _PERIOD_SEP.match(s)
    if m:
        vals = tuple(int(g) for g in m.groups() if g is not None)
    elif _PERIOD_DIGITS.match(s):
        vals = tuple(int(s[0:4]) if i == 0 else int(s[2 + 2 * i:4 + 2 * i]) for i in range((len(s) - 2) // 2))
    else:
        raise ValueError(f"日時「{text}」の形式が正しくありません（例: 2026-11-20、2026-11-20 09:00）。")
    if not valid_fields(vals):
        raise ValueError(f"日時「{text}」は暦にありません。")
    return vals, len(vals) - 1


def period_ranges(start: str, end: str, step: int | None = None, step_unit: str | None = None) -> Ranges:
    """期間（開始・終了、任意で間隔）を区間の並びにします。"""
    s_vals, s_unit = parse_period_point(start)
    e_vals, _ = parse_period_point(end)
    lo = unit_start(s_vals)
    hi = unit_end(e_vals)
    if hi <= lo:
        raise ValueError("期間の終了が開始より前です。")
    if not step:
        return [(lo, hi)]
    if step_unit not in UNITS:
        raise ValueError("間隔の単位を選んでください。")
    su = UNITS.index(step_unit)
    if step < 1:
        raise ValueError("間隔は 1 以上にしてください。")
    out: Ranges = []
    k = 0
    while True:
        if su <= MONTH:
            # 短い月にない日は月末に丸めます（開始の日を基準に毎回計算します）
            occ = add_months(lo, (12 if su == YEAR else 1) * step * k)
        else:
            occ = add_unit(lo, su, step * k)
        if occ >= hi:
            break
        out.append((occ, min(add_unit(occ, s_unit, 1), hi)))
        k += 1
        if k > MAX_STEPS:
            raise ValueError(f"間隔の回数が多すぎます（上限 {MAX_STEPS:,} 回）。")
    return merge_ranges(out)


def relative_ranges(kind: str, now_wall: datetime, n: int | None = None, from_: int | None = None,
                    to: int | None = None) -> Ranges:
    """相対的な期間を、期間の入力の基準の壁時計の時刻で求めます（7.3.1）。"""
    today = truncate(now_wall, DAY)
    if kind == "today":
        return [(today, today + timedelta(days=1))]
    if kind == "yesterday":
        return [(today - timedelta(days=1), today)]
    if kind == "last_days":
        n = int(n or 0)
        if n < 1:
            raise ValueError("日数は 1 以上にしてください。")
        return [(today - timedelta(days=n - 1), today + timedelta(days=1))]
    if kind == "last_hours":
        n = int(n or 0)
        if n < 1:
            raise ValueError("時間数は 1 以上にしてください。")
        hour = truncate(now_wall, HOUR)
        return [(hour - timedelta(hours=n - 1), hour + timedelta(hours=1))]
    if kind == "this_month":
        first = truncate(now_wall, MONTH)
        return [(first, add_months(first, 1))]
    if kind == "last_month":
        first = truncate(now_wall, MONTH)
        return [(add_months(first, -1), first)]
    if kind == "days_ago":
        a, b = int(from_ or 0), int(to or 0)
        if a < 0 or b < 0:
            raise ValueError("日数は 0 以上にしてください。")
        far, near = max(a, b), min(a, b)
        return [(today - timedelta(days=far), today - timedelta(days=near) + timedelta(days=1))]
    raise ValueError(f"知らない相対的な期間です: {kind}")


# ---------------------------------------------------------------- タイムゾーン


class Zone:
    """壁時計の時刻と UTC の変換。"""

    name = "UTC"

    def to_utc(self, wall: datetime) -> datetime:
        return wall

    def from_utc(self, utc_wall: datetime) -> datetime:
        return utc_wall


class FixedZone(Zone):
    """一定の時差（テスト用）。"""

    def __init__(self, hours: float, name: str = "ローカル") -> None:
        self.offset = timedelta(hours=hours)
        self.name = name

    def to_utc(self, wall: datetime) -> datetime:
        return wall - self.offset

    def from_utc(self, utc_wall: datetime) -> datetime:
        return utc_wall + self.offset


class LocalZone(Zone):
    """Windows のタイムゾーン（夏時間を含め、時刻ごとに変換します）。"""

    name = "ローカル"

    def to_utc(self, wall: datetime) -> datetime:
        return wall.astimezone(timezone.utc).replace(tzinfo=None)

    def from_utc(self, utc_wall: datetime) -> datetime:
        return utc_wall.replace(tzinfo=timezone.utc).astimezone().replace(tzinfo=None)


UTC_ZONE = Zone()


def convert_ranges(ranges: Ranges, src: Zone, dst: Zone) -> Ranges:
    """期間の各端を個別に変換します（3.2.4）。"""
    if src is dst or (type(src) is Zone and type(dst) is Zone):
        return list(ranges)
    out = []
    for s, e in ranges:
        out.append((dst.from_utc(src.to_utc(s)), dst.from_utc(src.to_utc(e))))
    return merge_ranges(out)


# ---------------------------------------------------------------- 値の指定


@dataclass
class UnitsSpec:
    """単位ごとの指定。None は「すべて」、集合は許す値。"""

    allowed: dict[int, set[int] | None] = field(default_factory=dict)

    def ok(self, unit: int, value: int) -> bool:
        s = self.allowed.get(unit)
        return s is None or value in s


def parse_units(spec: dict) -> UnitsSpec:
    """`units: {year: {mode, value, start, end, step}, …}` を解析します。"""
    out = UnitsSpec()
    for name, u in (spec or {}).items():
        if name not in UNITS or not isinstance(u, dict):
            continue
        unit = UNITS.index(name)
        mode = u.get("mode", "all")
        if mode == "all":
            out.allowed[unit] = None
        elif mode == "fixed":
            out.allowed[unit] = {int(u["value"])}
        elif mode == "range":
            start, end = int(u["start"]), int(u["end"])
            step = int(u.get("step") or 1)
            if end < start:
                raise ValueError(f"{UNIT_LABELS[unit]}の範囲の終了が開始より前です。")
            if step < 1:
                raise ValueError("間隔は 1 以上にしてください。")
            out.allowed[unit] = set(range(start, end + 1, step))
        else:
            raise ValueError(f"知らない指定です: {mode}")
    return out


@dataclass
class DateTimeValue:
    """日時の変数の値の指定を、照合に使う形にしたもの。"""

    mode: str  # any / ranges / units
    ranges: Ranges = field(default_factory=list)  # パス上の日時の基準の壁時計の時刻
    input_ranges: Ranges = field(default_factory=list)  # 期間の入力の基準の壁時計の時刻
    units: UnitsSpec | None = None


def resolve_value(value: dict, *, now_utc: datetime, input_zone: Zone, path_zone: Zone) -> DateTimeValue:
    """値の指定（5.5.3）を、パス上の日時の基準の区間の並びにします。"""
    mode = (value or {}).get("mode", "any")
    if mode == "any":
        return DateTimeValue("any")
    if mode == "units":
        return DateTimeValue("units", units=parse_units(value.get("units") or {}))
    if mode == "period":
        rs = period_ranges(value.get("start", ""), value.get("end", ""), value.get("step"), value.get("step_unit"))
    elif mode == "relative":
        now_wall = input_zone.from_utc(now_utc.astimezone(timezone.utc).replace(tzinfo=None))
        rs = relative_ranges(value.get("kind", ""), now_wall, value.get("n"), value.get("from"), value.get("to"))
    else:
        raise ValueError(f"知らない値の指定です: {mode}")
    return DateTimeValue("ranges", ranges=convert_ranges(rs, input_zone, path_zone), input_ranges=rs)


# ---------------------------------------------------------------- 一致の判定（追加の判定）


def match_fields(fields: dict[int, int], value: DateTimeValue, *, yy_years: list[int] | None = None) -> bool:
    """Blob 名から読み取った単位ごとの値が、値の指定に一致するか。

    上位の単位がない（年がないなど）場合は、期間の中のあり得る値を順に試します。
    """
    if not fields:
        return True
    gran = max(fields)
    if value.mode == "units":
        for u, v in fields.items():
            if not value.units.ok(u, v):
                return False
        return _calendar_possible(fields)
    if value.mode == "any":
        return _calendar_possible(fields)
    ranges = value.ranges
    if not ranges:
        return False
    lo, hi = ranges[0][0], ranges[-1][1]
    return _search(fields, gran, ranges, (), lo, hi)


def _calendar_possible(fields: dict[int, int]) -> bool:
    """暦の上であり得る値か（年がない場合は閏年を考えます）。"""
    y = fields.get(YEAR, 2000)
    m = fields.get(MONTH)
    d = fields.get(DAY)
    if m is not None and not 1 <= m <= 12:
        return False
    if d is not None:
        if m is None:
            return 1 <= d <= 31
        return 1 <= d <= calendar.monthrange(y, m)[1]
    return True


def _search(fields: dict[int, int], gran: int, ranges: Ranges, prefix: tuple[int, ...], lo: datetime,
            hi: datetime) -> bool:
    level = len(prefix)
    if level > gran:
        return overlaps(ranges, unit_start(prefix), unit_end(prefix))
    if level in fields:
        cands = [fields[level]]
    else:
        cands = list(unit_values(prefix, lo, hi)) if level != YEAR else list(range(lo.year, hi.year + 1))
    for v in cands:
        p = prefix + (v,)
        if not valid_fields(p):
            continue
        s, e = unit_start(p), unit_end(p)
        if not overlaps(ranges, s, e):
            continue
        if _search(fields, gran, ranges, p, max(lo, s), min(hi, e)):
            return True
    return False


def resolve_yy(yy: int, candidates: list[int]) -> list[int]:
    """下2桁の年から、あり得る年の一覧を求めます。"""
    out = [y for y in candidates if y % 100 == yy]
    return out or [2000 + yy]
