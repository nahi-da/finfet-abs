"""日本の祝日（曜日・祝日の絞り込み。v1.6.0）。

jpholiday を使い、次の補正をします（内閣府の「国民の祝日」の CSV との照合で確認。tests/test_holidays.py）。

- 「国民の休日」（祝日に挟まれた日）は、1985 年 12 月 27 日の法改正の施行より前には適用しません
- 「振替休日」は、1973 年 4 月 12 日の法改正の施行より前には適用しません

設定の「会社の休日」（祝日として扱う日）と「祝日として扱わない日」を加味します。
"""

from __future__ import annotations

import re
from datetime import date

import jpholiday

_KOKUMIN_FROM = date(1985, 12, 27)
_FURIKAE_FROM = date(1973, 4, 12)
_DATE = re.compile(r"^(\d{4})[-/](\d{1,2})[-/](\d{1,2})$")


def national_holiday(d: date) -> bool:
    """日本の祝日・休日か（設定の追加は含めません）。"""
    name = jpholiday.is_holiday_name(d)
    if not name:
        return False
    names = [n for n in re.split(r"[\s　]+", name) if n]
    if d < _KOKUMIN_FROM and all(n == "国民の休日" for n in names):
        return False
    if d < _FURIKAE_FROM and names and names[-1] == "振替休日":
        # 「建国記念の日 振替休日」のように、元の祝日の名前に「振替休日」が付いた形
        return False
    return True


def parse_dates(text: str) -> tuple[set[date], list[str]]:
    """設定の日の一覧（改行・「,」区切り。YYYY-MM-DD か YYYY/MM/DD）を解釈します。戻り値は (日の集合, 誤りの文字列)。"""
    out: set[date] = set()
    bad: list[str] = []
    for part in re.split(r"[\s,、]+", text or ""):
        if not part:
            continue
        m = _DATE.match(part)
        try:
            if not m:
                raise ValueError
            out.add(date(int(m.group(1)), int(m.group(2)), int(m.group(3))))
        except ValueError:
            bad.append(part)
    return out, bad


class HolidayCalendar:
    """祝日の判定（設定の追加を含む）。"""

    def __init__(self, extra: set[date] | None = None, not_holiday: set[date] | None = None) -> None:
        self.extra = set(extra or ())
        self.not_holiday = set(not_holiday or ())

    @classmethod
    def from_settings(cls, extra_text: str = "", not_holiday_text: str = "") -> "HolidayCalendar":
        return cls(parse_dates(extra_text)[0], parse_dates(not_holiday_text)[0])

    def is_holiday(self, d: date) -> bool:
        if d in self.not_holiday:
            return False
        return d in self.extra or national_holiday(d)
