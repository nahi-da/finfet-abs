"""日本の祝日（v1.6.0）。内閣府の「国民の祝日」の CSV（tests/data/syukujitsu.csv）と全日を照合します。

CSV の出典: 内閣府「国民の祝日について」（https://www8.cao.go.jp/chosei/shukujitsu/gaiyou.html）の
syukujitsu.csv（2026-11-24 に取得。文字コードを UTF-8 に変換）。
"""

from __future__ import annotations

import unittest
from datetime import date, timedelta
from pathlib import Path

from finfet_abs.search.holidays import HolidayCalendar, national_holiday, parse_dates

CSV = Path(__file__).with_name("data") / "syukujitsu.csv"


def load() -> dict[date, str]:
    out = {}
    for line in CSV.read_text(encoding="utf-8").splitlines()[1:]:
        d, name = line.split(",")
        y, m, dd = map(int, d.split("/"))
        out[date(y, m, dd)] = name
    return out


class HolidayTest(unittest.TestCase):
    def test_matches_cabinet_office_csv(self) -> None:
        days = load()
        first, last = date(min(days).year, 1, 1), date(max(days).year, 12, 31)
        d = first
        bad = []
        while d <= last:
            if national_holiday(d) != (d in days):
                bad.append((d, days.get(d)))
            d += timedelta(days=1)
        self.assertEqual(bad, [])
        self.assertGreaterEqual(last.year, 2027)

    def test_settings(self) -> None:
        cal = HolidayCalendar.from_settings("2026-12-29, 2026/12/30", "2026-11-23")
        self.assertTrue(cal.is_holiday(date(2026, 12, 29)))
        self.assertTrue(cal.is_holiday(date(2026, 12, 30)))
        self.assertFalse(cal.is_holiday(date(2026, 11, 23)))  # 祝日として扱わない日
        self.assertTrue(cal.is_holiday(date(2026, 11, 3)))
        self.assertEqual(parse_dates("2026-02-30 x 2026-1-2")[1], ["2026-02-30", "x"])


if __name__ == "__main__":
    unittest.main()
