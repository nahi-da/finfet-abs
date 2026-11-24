"""日時の変数の曜日・祝日の絞り込み（v1.6.0）。"""

from __future__ import annotations

import unittest
from datetime import datetime, timezone

from finfet_abs.search import datetime_vars as dtv
from finfet_abs.search.holidays import HolidayCalendar
from finfet_abs.search.pattern import CompileContext, PatternError, compile_path

CTX = CompileContext(now_utc=datetime(2026, 11, 24, tzinfo=timezone.utc), local_zone=dtv.FixedZone(9))


def dt(value, days, tz="utc"):
    v = dict(value)
    v["days"] = days
    return {"dt": {"type": "datetime", "path_timezone": tz, "value": v}}


PERIOD = {"mode": "period", "start": "2026-11-16", "end": "2026-11-29"}  # 11/16（月）〜 11/29（日）。11/23（月）は祝日


class DaysTest(unittest.TestCase):
    def names(self, c, fmt="%Y/%m/%d"):
        out = []
        for d in range(14, 32):
            if d > 30:
                break
            name = datetime(2026, 11, d).strftime(fmt) + "/a.log"
            if c.match(name):
                out.append(d)
        return out

    def test_weekdays_only(self) -> None:
        c = compile_path(r"{{dt=YYYY}}/{{dt=MM}}/{{dt=DD}}/a\.log", dt(PERIOD, {"weekdays": [0, 1, 2, 3, 4]}),
                         input_timezone="utc", ctx=CTX)
        self.assertEqual(self.names(c), [16, 17, 18, 19, 20, 23, 24, 25, 26, 27])

    def test_exclude_holidays(self) -> None:
        c = compile_path(r"{{dt=YYYY}}/{{dt=MM}}/{{dt=DD}}/a\.log",
                         dt(PERIOD, {"weekdays": [0, 1, 2, 3, 4], "holidays": "exclude"}), input_timezone="utc", ctx=CTX)
        self.assertEqual(self.names(c), [16, 17, 18, 19, 20, 24, 25, 26, 27])

    def test_holidays_only_and_weekend(self) -> None:
        c = compile_path(r"{{dt=YYYY}}/{{dt=MM}}/{{dt=DD}}/a\.log",
                         dt(PERIOD, {"weekdays": list(range(7)), "holidays": "only"}), input_timezone="utc", ctx=CTX)
        self.assertEqual(self.names(c), [23])
        c = compile_path(r"{{dt=YYYY}}/{{dt=MM}}/{{dt=DD}}/a\.log", dt(PERIOD, {"weekdays": [5, 6]}),
                         input_timezone="utc", ctx=CTX)
        self.assertEqual(self.names(c), [21, 22, 28, 29])

    def test_company_holidays(self) -> None:
        ctx = CompileContext(now_utc=CTX.now_utc, local_zone=CTX.local_zone,
                             holidays=HolidayCalendar.from_settings("2026-11-24", "2026-11-23"))
        c = compile_path(r"{{dt=YYYY}}/{{dt=MM}}/{{dt=DD}}/a\.log",
                         dt(PERIOD, {"weekdays": [0, 1, 2, 3, 4], "holidays": "exclude"}), input_timezone="utc", ctx=ctx)
        self.assertEqual(self.names(c), [16, 17, 18, 19, 20, 23, 25, 26, 27])

    def test_input_timezone_decides_the_day(self) -> None:
        # パスは UTC の時。ローカル（+9）の月曜 11/16 は、UTC の 11/15 15:00〜11/16 14:59
        v = dt({"mode": "period", "start": "2026-11-15 00:00", "end": "2026-11-16 23:00"}, {"weekdays": [0]})
        c = compile_path(r"{{dt=YYYY}}/{{dt=MM}}/{{dt=DD}}/{{dt=hh}}\.log", v, input_timezone="local", ctx=CTX)
        self.assertIsNone(c.match("2026/11/15/14.log"))  # ローカル 11/15（日）23 時
        self.assertIsNotNone(c.match("2026/11/15/15.log"))  # ローカル 11/16（月）0 時
        self.assertIsNotNone(c.match("2026/11/16/14.log"))  # ローカル 11/16（月）23 時

    def test_any_and_compact_format(self) -> None:
        c = compile_path(r"app_{{dt=YYYYMMDD}}\.log", dt({"mode": "any"}, {"weekdays": [5, 6]}), input_timezone="utc", ctx=CTX)
        self.assertIsNotNone(c.match("app_20261121.log"))
        self.assertIsNone(c.match("app_20261120.log"))

    def test_requires_day_unit(self) -> None:
        with self.assertRaises(PatternError) as cm:
            compile_path(r"{{dt=YYYY}}/{{dt=MM}}/a\.log", dt({"mode": "any"}, {"weekdays": [0]}), input_timezone="utc", ctx=CTX)
        self.assertIn("日の単位", cm.exception.issues[0].message)

    def test_all_days_is_no_filter(self) -> None:
        c = compile_path(r"{{dt=YYYY}}/{{dt=MM}}/a\.log", dt({"mode": "any"}, {"weekdays": list(range(7)), "holidays": "include"}),
                         input_timezone="utc", ctx=CTX)
        self.assertIsNotNone(c.match("2026/11/a.log"))


if __name__ == "__main__":
    unittest.main()
