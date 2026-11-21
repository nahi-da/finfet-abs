"""T-DT・T-TZ: 日時の変数と日時の基準（3.2.4、7.3.1）。"""

import itertools
import random
import unittest
from datetime import datetime, timedelta, timezone

from finfet_abs.search import datetime_vars as dtv
from finfet_abs.search.pattern import CompileContext, PatternError, compile_path

NOW = datetime(2026, 11, 20, 3, 30, tzinfo=timezone.utc)  # 日本時間 11/20 12:30
CTX = CompileContext(now_utc=NOW, local_zone=dtv.FixedZone(9))


def dtvar(value, tz="utc"):
    return {"dt": {"type": "datetime", "path_timezone": tz, "value": value}}


def brute(case, pattern, value, fmt_fn, gran, lo, hi, input_tz="utc", path_tz="utc", ctx=CTX):
    """lo〜hi の全区間の Blob 名を作り、一致と prefix の不変条件を確かめます。"""
    c = compile_path(pattern, dtvar(value, path_tz), input_timezone=input_tz, ctx=ctx)
    dv = dtv.resolve_value(value, now_utc=ctx.now_utc, input_zone=ctx.zone(input_tz), path_zone=ctx.zone(path_tz))
    t = lo
    n_match = 0
    while t < hi:
        end = dtv.add_unit(t, gran, 1)
        name = fmt_fn(t)
        expected = dtv.overlaps(dv.ranges, t, end) if dv.mode == "ranges" else True
        got = c.match(name)
        case.assertEqual(got is not None, expected, f"{name} {c.source}")
        if got is not None:
            n_match += 1
            case.assertTrue(any(name.startswith(p) for p in c.prefixes), f"{name} {c.prefixes}")
        t = end
    return c, n_match


class FormatTest(unittest.TestCase):
    def test_parse(self) -> None:
        items = dtv.parse_format("YYYY-MM-DDThh")
        self.assertEqual([i.symbol or i.text for i in items], ["YYYY", "-", "MM", "-", "DD", "T", "hh"])
        self.assertEqual([i.symbol for i in dtv.parse_format("year")], ["YYYY"])
        self.assertEqual([i.symbol or i.text for i in dtv.parse_format("hhmm")], ["hh", "mm"])
        self.assertEqual([i.symbol or i.text for i in dtv.parse_format("MDhms")], ["M", "D", "h", "m", "s"])
        for bad in ("yyyy", "YYYY-dd", "HH", "SS"):
            with self.assertRaises(dtv.DateTimeFormatError):
                dtv.parse_format(bad)
        with self.assertRaises(dtv.DateTimeFormatError):
            dtv.parse_format("abc")

    def test_period_points(self) -> None:
        self.assertEqual(dtv.parse_period_point("2026-01"), ((2026, 1), 1))
        self.assertEqual(dtv.parse_period_point("2026/1/5 9:05"), ((2026, 1, 5, 9, 5), 4))
        self.assertEqual(dtv.parse_period_point("2026-01-05T09:05:07"), ((2026, 1, 5, 9, 5, 7), 5))
        self.assertEqual(dtv.parse_period_point("2026112009"), ((2026, 11, 20, 9), 3))
        rs = dtv.period_ranges("2026-01", "2026-01")
        self.assertEqual(rs, [(datetime(2026, 1, 1), datetime(2026, 2, 1))])
        with self.assertRaises(ValueError):
            dtv.parse_period_point("2026-02-30")
        with self.assertRaises(ValueError):
            dtv.period_ranges("2026-02", "2026-01")

    def test_step(self) -> None:
        rs = dtv.period_ranges("2026-11-01 09", "2026-11-03", step=1, step_unit="day")
        self.assertEqual(rs, [(datetime(2026, 11, d, 9), datetime(2026, 11, d, 10)) for d in (1, 2, 3)])
        # 短い月にない日は月末に丸めます
        rs = dtv.period_ranges("2026-01-31", "2026-04-30", step=1, step_unit="month")
        self.assertEqual([r[0].date().isoformat() for r in rs], ["2026-01-31", "2026-02-28", "2026-03-31", "2026-04-30"])
        # 重なる回・隣り合う回は1つにまとめます
        rs = dtv.period_ranges("2026-11-01", "2026-11-03", step=1, step_unit="day")
        self.assertEqual(rs, [(datetime(2026, 11, 1), datetime(2026, 11, 4))])
        with self.assertRaises(ValueError):
            dtv.period_ranges("2026-01-01 00:00", "2026-12-31", step=1, step_unit="minute")


class GranularityTest(unittest.TestCase):
    def test_overlap_granularity(self) -> None:
        # 期間の開始が 10:30 でも h=10 は対象
        v = {"mode": "period", "start": "2026-11-20 10:30", "end": "2026-11-20 12:10"}
        c = compile_path("{{dt=YYYY}}/{{dt=MM}}/{{dt=DD}}/{{dt=hh}}", dtvar(v), input_timezone="utc", ctx=CTX)
        self.assertEqual([h for h in range(24) if c.match(f"2026/11/20/{h:02d}")], [10, 11, 12])

    def test_brute_ordered_hour(self) -> None:
        cases = [
            {"mode": "period", "start": "2026-11-28", "end": "2026-12-02"},
            {"mode": "period", "start": "2026-12-31 22", "end": "2027-01-01 03"},
            {"mode": "period", "start": "2028-02-28", "end": "2028-03-01"},
            {"mode": "period", "start": "2026-11-01 09", "end": "2026-11-05", "step": 1, "step_unit": "day"},
            {"mode": "relative", "kind": "last_days", "n": 3},
            {"mode": "relative", "kind": "last_hours", "n": 5},
        ]
        fmt = lambda t: f"y={t.year}/m={t.month:02d}/d={t.day:02d}/h={t.hour:02d}/m=00/PT1H.json"  # noqa: E731
        for v in cases:
            for tz in ("utc", "local"):
                c, n = brute(self, r"y={{dt=YYYY}}/m={{dt=MM}}/d={{dt=DD}}/h={{dt=hh}}/m=00/PT1H\.json", v, fmt,
                             dtv.HOUR, datetime(2026, 11, 20) - timedelta(days=25) if v["mode"] == "relative" else
                             datetime(int(v["start"][:4]), 1, 1) - timedelta(days=3),
                             datetime(2026, 11, 25) if v["mode"] == "relative" else
                             datetime(int(v["end"][:4]) + 1, 1, 10), input_tz=tz)
                self.assertGreater(n, 0)
                self.assertTrue(c.datetimes["dt"].ordered)

    def test_brute_unpadded_and_not_ordered(self) -> None:
        v = {"mode": "period", "start": "2026-01-30", "end": "2026-03-02"}
        brute(self, "{{dt=YYYY}}/{{dt=M}}/{{dt=D}}/x", v, lambda t: f"{t.year}/{t.month}/{t.day}/x", dtv.DAY,
              datetime(2025, 12, 1), datetime(2026, 4, 30))
        # 記号の順序が逆（追加の判定）
        c, _ = brute(self, "{{dt=DD}}-{{dt=MM}}-{{dt=YYYY}}", v, lambda t: f"{t.day:02d}-{t.month:02d}-{t.year}",
                     dtv.DAY, datetime(2025, 12, 1), datetime(2026, 4, 30))
        self.assertFalse(c.datetimes["dt"].ordered)
        # 同じ記号の繰り返し（後方参照）
        c, _ = brute(self, r"{{dt=YYYY}}/{{dt=MM}}/{{dt=DD}}/{{dt=YYYYMMDD}}_log\.txt", v,
                     lambda t: f"{t:%Y/%m/%d/%Y%m%d}_log.txt", dtv.DAY, datetime(2025, 12, 1), datetime(2026, 4, 30))
        self.assertIn("(?P=d_dt_YYYY)", c.source)
        self.assertIsNone(c.match("2026/02/01/20260202_log.txt"))
        # 年が2桁
        brute(self, "{{dt=YY}}{{dt=MM}}{{dt=DD}}", v, lambda t: f"{t:%y%m%d}", dtv.DAY, datetime(2025, 12, 1),
              datetime(2026, 4, 30))
        # 年がない（月・日だけ）
        c = compile_path("{{dt=MM}}/{{dt=DD}}", dtvar(v), input_timezone="utc", ctx=CTX)
        self.assertIsNotNone(c.match("02/28"))
        self.assertIsNone(c.match("02/29"))
        self.assertIsNone(c.match("03/03"))

    def test_minute_granularity_and_many_choices(self) -> None:
        v = {"mode": "period", "start": "2026-11-20 09:58", "end": "2026-11-20 10:03"}
        brute(self, "{{dt=YYYY}}{{dt=MM}}{{dt=DD}}/{{dt=hhmm}}", v, lambda t: f"{t:%Y%m%d/%H%M}", dtv.MINUTE,
              datetime(2026, 11, 20, 9, 0), datetime(2026, 11, 20, 11, 0))
        # 選択肢が 2,000 を超える場合は追加の判定に切り替えます
        v = {"mode": "period", "start": "2026-01-01 00:01", "end": "2026-04-01 00:00", "step": 1, "step_unit": "hour"}
        c, _ = brute(self, "{{dt=YYYY}}/{{dt=MM}}/{{dt=DD}}/{{dt=hh}}{{dt=mm}}", v,
                     lambda t: f"{t:%Y/%m/%d/%H%M}", dtv.MINUTE, datetime(2026, 3, 30), datetime(2026, 4, 2))
        self.assertFalse(c.datetimes["dt"].ordered)

    def test_units_and_any(self) -> None:
        v = {"mode": "units", "units": {"year": {"mode": "all"}, "month": {"mode": "fixed", "value": 2},
                                         "day": {"mode": "range", "start": 27, "end": 31, "step": 1},
                                         "hour": {"mode": "range", "start": 9, "end": 17, "step": 4}}}
        c = compile_path("{{dt=YYYY}}/{{dt=MM}}/{{dt=DD}}/{{dt=hh}}", dtvar(v), input_timezone="utc", ctx=CTX)
        self.assertIsNotNone(c.match("2024/02/29/13"))
        self.assertIsNone(c.match("2026/02/29/13"))  # 暦にない日
        self.assertIsNone(c.match("2026/02/28/10"))
        self.assertIsNone(c.match("2026/03/28/09"))
        self.assertEqual(c.match("2026/02/28/17"), {"dt": {"year": 2026, "month": 2, "day": 28, "hour": 17}})
        c = compile_path("{{dt=YYYY}}/{{dt=MM}}/{{dt=DD}}", dtvar({"mode": "any"}), input_timezone="utc", ctx=CTX)
        self.assertIsNotNone(c.match("2026/02/28"))
        self.assertIsNone(c.match("2026/02/30"))
        self.assertIsNone(c.match("2026/13/01"))

    def test_errors(self) -> None:
        with self.assertRaises(PatternError):
            compile_path("{{dt=YYYY}}", dtvar({"mode": "period", "start": "2026-13", "end": "2026-12"}),
                         input_timezone="utc", ctx=CTX)
        with self.assertRaises(PatternError):
            compile_path("{{dt}}", dtvar({"mode": "any"}), input_timezone="utc", ctx=CTX)


class TimezoneTest(unittest.TestCase):
    def rel(self, kind, **kw):
        now_wall = datetime(2026, 11, 20, 12, 30)
        return dtv.relative_ranges(kind, now_wall, kw.get("n"), kw.get("from_"), kw.get("to"))

    def test_relative(self) -> None:
        d = datetime
        self.assertEqual(self.rel("today"), [(d(2026, 11, 20), d(2026, 11, 21))])
        self.assertEqual(self.rel("yesterday"), [(d(2026, 11, 19), d(2026, 11, 20))])
        self.assertEqual(self.rel("last_days", n=7), [(d(2026, 11, 14), d(2026, 11, 21))])
        self.assertEqual(self.rel("last_hours", n=3), [(d(2026, 11, 20, 10), d(2026, 11, 20, 13))])
        self.assertEqual(self.rel("this_month"), [(d(2026, 11, 1), d(2026, 12, 1))])
        self.assertEqual(self.rel("last_month"), [(d(2026, 10, 1), d(2026, 11, 1))])
        self.assertEqual(self.rel("days_ago", from_=7, to=1), [(d(2026, 11, 13), d(2026, 11, 20))])
        self.assertEqual(self.rel("days_ago", from_=1, to=7), [(d(2026, 11, 13), d(2026, 11, 20))])
        r = dtv.relative_ranges("last_month", d(2027, 1, 5, 1), None)
        self.assertEqual(r, [(d(2026, 12, 1), d(2027, 1, 1))])

    def test_conversion(self) -> None:
        jst = dtv.FixedZone(9)
        d = datetime
        # 日付の境界
        self.assertEqual(dtv.convert_ranges([(d(2026, 11, 20), d(2026, 11, 21))], jst, dtv.UTC_ZONE),
                         [(d(2026, 11, 19, 15), d(2026, 11, 20, 15))])
        # 月末・年末
        self.assertEqual(dtv.convert_ranges([(d(2027, 1, 1), d(2027, 1, 2))], jst, dtv.UTC_ZONE),
                         [(d(2026, 12, 31, 15), d(2027, 1, 1, 15))])
        self.assertEqual(dtv.convert_ranges([(d(2026, 2, 28, 20), d(2026, 3, 1, 2))], dtv.UTC_ZONE, jst),
                         [(d(2026, 3, 1, 5), d(2026, 3, 1, 11))])
        # 相対的な期間はローカルの「今日」（UTC では前日 15:00〜）
        v = dtv.resolve_value({"mode": "relative", "kind": "today"}, now_utc=NOW, input_zone=jst, path_zone=dtv.UTC_ZONE)
        self.assertEqual(v.ranges, [(d(2026, 11, 19, 15), d(2026, 11, 20, 15))])
        # UTC の 11/19 20:00 は、ローカルではすでに 11/20
        now2 = datetime(2026, 11, 19, 20, 0, tzinfo=timezone.utc)
        v = dtv.resolve_value({"mode": "relative", "kind": "today"}, now_utc=now2, input_zone=jst, path_zone=jst)
        self.assertEqual(v.ranges, [(d(2026, 11, 20), d(2026, 11, 21))])
        # パス上の日時がローカル時刻、入力が UTC
        v = dtv.resolve_value({"mode": "period", "start": "2026-11-19 15", "end": "2026-11-20 14"}, now_utc=NOW,
                              input_zone=dtv.UTC_ZONE, path_zone=jst)
        self.assertEqual(v.ranges, [(d(2026, 11, 20), d(2026, 11, 21))])

    def test_local_zone_roundtrip(self) -> None:
        z = dtv.LocalZone()
        t = datetime(2026, 7, 1, 12, 0)
        self.assertEqual(z.from_utc(z.to_utc(t)), t)


class RandomBruteTest(unittest.TestCase):
    def test_random_periods(self) -> None:
        rnd = random.Random(1234)
        fmt = lambda t: f"{t:%Y/%m/%d/%H}"  # noqa: E731
        for _ in range(25):
            start = datetime(2026, 1, 1) + timedelta(hours=rnd.randint(0, 24 * 400))
            end = start + timedelta(hours=rnd.randint(0, 24 * 40))
            gs = rnd.choice(["%Y-%m-%d %H", "%Y-%m-%d", "%Y-%m"])
            v = {"mode": "period", "start": start.strftime(gs), "end": end.strftime(rnd.choice(["%Y-%m-%d %H", "%Y-%m-%d"]))}
            if dtv.period_ranges(v["start"], v["end"])[0][1] <= dtv.period_ranges(v["start"], v["end"])[0][0]:
                continue
            ctx = CompileContext(now_utc=NOW, local_zone=dtv.FixedZone(rnd.choice([9, -5, 5.5])),
                                 max_prefixes=rnd.choice([1, 4, 32, 64]))
            brute(self, "{{dt=YYYY}}/{{dt=MM}}/{{dt=DD}}/{{dt=hh}}", v, fmt, dtv.HOUR, start - timedelta(days=3),
                  end + timedelta(days=40), input_tz=rnd.choice(["utc", "local"]), ctx=ctx)


if __name__ == "__main__":
    unittest.main()
