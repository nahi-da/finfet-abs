"""文字列の値の「*」と、除く値（v1.6.0）。"""

from __future__ import annotations

import unittest
from datetime import datetime, timezone

from finfet_abs.search import datetime_vars as dtv
from finfet_abs.search.pattern import CompileContext, PatternError, compile_path
from finfet_abs.search.prefix import compute as compute_prefix
from finfet_abs.search.variables import legacy_to_wildcard, string_values

CTX = CompileContext(now_utc=datetime(2026, 11, 24, tzinfo=timezone.utc), local_zone=dtv.FixedZone(9))


def one_of(*vals, exclude=(), syntax="wildcard"):
    v = {"mode": "one_of", "values": [{"value": x, "enabled": True} for x in vals], "exclude": [{"value": x, "enabled": True} for x in exclude]}
    if syntax:
        v["syntax"] = syntax
    return {"type": "string", "value": v}


def cp(pattern, var, **kw):
    return compile_path(pattern, {"v": var}, ctx=CTX, **kw)


class WildcardTest(unittest.TestCase):
    def test_exact_prefix_suffix_partial_middle(self) -> None:
        c = cp(r"logs/{{v}}/a\.log", one_of("web01", "tmp*", "*_bak", "*test*", "a*z"))
        ok = ["web01", "tmp", "tmp123", "x_bak", "_bak", "mytestx", "test", "az", "abcz"]
        ng = ["web011", "xtmp", "x_bak2", "tes", "za", "a/z"]
        for v in ok:
            self.assertIsNotNone(c.match(f"logs/{v}/a.log"), v)
        for v in ng:
            self.assertIsNone(c.match(f"logs/{v}/a.log"), v)

    def test_escaped_star_is_literal(self) -> None:
        c = cp(r"{{v}}\.txt", one_of(r"a\*b"))
        self.assertIsNotNone(c.match("a*b.txt"))
        self.assertIsNone(c.match("axb.txt"))

    def test_special_characters_escaped(self) -> None:
        c = cp(r"{{v}}\.txt", one_of("a.b*"))
        self.assertIsNotNone(c.match("a.bcd.txt"))
        self.assertIsNone(c.match("aXbcd.txt"))

    def test_star_stays_in_one_segment(self) -> None:
        c = cp(r"{{v}}/x", one_of("a*"))
        self.assertIsNone(c.match("ab/c/x"))

    def test_enumeration_and_prefix(self) -> None:
        self.assertEqual(string_values(one_of("web01", r"a\*b")["value"]), ["web01", "a*b"])
        self.assertIsNone(string_values(one_of("web*")["value"]))
        c = cp(r"logs/{{v}}/a\.log", one_of("web01", "db01"))
        self.assertEqual(sorted(compute_prefix(c.regex.pattern)), ["logs/db01/a.log", "logs/web01/a.log"])
        c = cp(r"logs/{{v}}/a\.log", one_of("web*"))
        self.assertTrue(all(p.startswith("logs/web") for p in compute_prefix(c.regex.pattern)))

    def test_legacy_conditions(self) -> None:
        for match, ok, ng in (("exact", "ab", "abc"), ("prefix", "abc", "xab"), ("partial", "xabx", "ba")):
            v = {"type": "string", "value": {"mode": "one_of", "match": match, "values": [{"value": "ab", "enabled": True}]}}
            c = cp(r"{{v}}/x", v)
            self.assertIsNotNone(c.match(f"{ok}/x"), match)
            self.assertIsNone(c.match(f"{ng}/x"), match)
        self.assertEqual(legacy_to_wildcard("a*b", "prefix"), r"a\*b*")
        self.assertEqual(legacy_to_wildcard("ab", "partial"), "*ab*")
        self.assertEqual(legacy_to_wildcard("ab", "exact"), "ab")


class ExcludeTest(unittest.TestCase):
    def test_exclude_with_any(self) -> None:
        v = {"type": "string", "value": {"mode": "any", "exclude": [{"value": "tmp*", "enabled": True}, {"value": "*_bak", "enabled": True},
                                                                   {"value": "skip", "enabled": False}]}}
        c = cp(r"logs/{{v}}/a\.log", v)
        self.assertIsNotNone(c.match("logs/web01/a.log"))
        self.assertIsNotNone(c.match("logs/skip/a.log"))  # チェックを外した除く値
        self.assertIsNone(c.match("logs/tmp1/a.log"))
        self.assertIsNone(c.match("logs/x_bak/a.log"))

    def test_exclude_with_one_of_and_enumeration(self) -> None:
        var = one_of("web01", "web02", "db01", exclude=["web02"])
        c = cp(r"{{v}}/a", var)
        self.assertIsNone(c.match("web02/a"))
        self.assertIsNotNone(c.match("web01/a"))
        self.assertEqual(string_values(var["value"]), ["web01", "db01"])

    def test_exclude_with_chars(self) -> None:
        v = {"type": "string", "value": {"mode": "chars", "classes": ["digit"], "exclude": [{"value": "0*", "enabled": True}]}}
        c = cp(r"n{{v}}", v)
        self.assertIsNotNone(c.match("n123"))
        self.assertIsNone(c.match("n0123"))

    def test_exclude_ignore_case(self) -> None:
        v = {"type": "string", "value": {"mode": "any", "exclude": [{"value": "tmp*", "enabled": True}]}}
        self.assertIsNotNone(cp(r"{{v}}/a", v).match("TMP1/a"))
        self.assertIsNone(cp(r"{{v}}/a", v, ignore_case=True).match("TMP1/a"))

    def test_number_exclude(self) -> None:
        v = {"type": "number", "padding": 2, "value": {"mode": "range", "start": 1, "end": 5, "exclude": [{"value": "3", "enabled": True}]}}
        c = cp(r"h{{v}}", v)
        self.assertIsNotNone(c.match("h02"))
        self.assertIsNone(c.match("h03"))
        v2 = {"type": "number", "value": {"mode": "any", "exclude": [{"value": "7", "enabled": True}]}}
        c2 = cp(r"h{{v}}", v2)
        self.assertIsNone(c2.match("h7"))
        self.assertIsNone(c2.match("h007"))
        self.assertIsNotNone(c2.match("h8"))

    def test_number_exclude_must_be_integer(self) -> None:
        v = {"type": "number", "value": {"mode": "any", "exclude": [{"value": "x", "enabled": True}]}}
        with self.assertRaises(PatternError):
            cp(r"h{{v}}", v)


if __name__ == "__main__":
    unittest.main()
