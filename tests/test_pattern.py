"""T-PAT・T-VAR: パスパターンと変数（3.2.2、3.2.3、7.3.2）。"""

import re
import unittest
from datetime import datetime, timezone

from finfet_abs.search import detect
from finfet_abs.search.datetime_vars import FixedZone
from finfet_abs.search.pattern import CompileContext, PatternError, compile_path, expand_containers, tokenize

CTX = CompileContext(now_utc=datetime(2026, 11, 20, 3, 0, tzinfo=timezone.utc), local_zone=FixedZone(9))


def comp(pattern, variables=None, **kw):
    return compile_path(pattern, variables or {}, ctx=kw.pop("ctx", CTX), **kw)


def one_of(*vals, match="exact", disabled=()):
    return {"type": "string", "value": {"mode": "one_of", "match": match,
                                        "values": [{"value": v, "enabled": v not in disabled} for v in vals]}}


class PatternTest(unittest.TestCase):
    def test_regex_fullmatch(self) -> None:
        c = comp(r"logs/[^/]+\.json")
        self.assertIsNotNone(c.match("logs/a.json"))
        self.assertIsNone(c.match("logs/a.json.bak"))
        self.assertIsNone(c.match("x/logs/a.json"))
        self.assertIsNone(c.match("logs/a/b.json"))
        self.assertIsNotNone(comp(r"logs/.*").match("logs/a/b/c"))
        # 先頭の / は取り除きます
        self.assertIsNotNone(comp(r"/logs/a\.json").match("logs/a.json"))

    def test_variables_and_braces(self) -> None:
        c = comp(r"{{app}}/{{level}}\.log")
        self.assertEqual(c.match("web/error.log"), {"app": "web", "level": "error"})
        # \{ は正規表現の { です
        c = comp(r"a\{x\}/{{v}}")
        self.assertEqual(c.match("a{x}/1"), {"v": "1"})
        c = comp(r"a{2}")
        self.assertIsNotNone(c.match("aa"))

    def test_error_positions(self) -> None:
        with self.assertRaises(PatternError) as cm:
            comp("abc/{{app")
        self.assertEqual(cm.exception.issues[0].pos, 4)
        with self.assertRaises(PatternError) as cm:
            comp("abc/{{1app}}")
        self.assertEqual(cm.exception.issues[0].pos, 6)
        with self.assertRaises(PatternError) as cm:
            comp("logs/{{app}}/(abc")
        self.assertEqual(cm.exception.issues[0].pos, 13)
        self.assertIn("「(」が閉じていません", cm.exception.issues[0].message)
        with self.assertRaises(PatternError) as cm:
            comp("/x/[abc")
        self.assertEqual(cm.exception.issues[0].pos, 3)
        with self.assertRaises(PatternError) as cm:
            comp("{{dt=YYYY}}/{{dt=yyyy}}")
        self.assertEqual(cm.exception.issues[0].pos, 17)
        self.assertEqual(cm.exception.issues[0].to_json()["pos"], 18)

    def test_named_groups_forbidden(self) -> None:
        with self.assertRaises(PatternError) as cm:
            comp(r"(?P<x>a)/b")
        self.assertIn("名前付きグループ", cm.exception.issues[0].message)
        with self.assertRaises(PatternError):
            comp(r"(?P=x)")

    def test_same_name_backreference(self) -> None:
        c = comp(r"{{rule}}/{{rule}}_\d+\.csv")
        self.assertEqual(c.match("daily/daily_1.csv"), {"rule": "daily"})
        self.assertIsNone(c.match("daily/weekly_1.csv"))

    def test_ignore_case(self) -> None:
        c = comp(r"Logs/{{app}}\.json", ignore_case=True)
        self.assertIsNotNone(c.match("logs/WEB.JSON"))
        self.assertEqual(c.prefixes, [""])
        c = comp(r"Logs/{{app}}\.json")
        self.assertIsNone(c.match("logs/WEB.json"))
        self.assertEqual(c.prefixes, ["Logs/"])

    def test_escape_literal_conversion(self) -> None:
        self.assertTrue(detect.looks_literal("logs/PT1H.json"))
        self.assertFalse(detect.looks_literal(r"logs/PT1H\.json"))
        self.assertFalse(detect.looks_literal("logs/abc"))
        s = detect.escape_non_variables("y={{dt=YYYY}}/a(1)+b.json")
        self.assertEqual(s, r"y={{dt=YYYY}}/a\(1\)\+b\.json")
        self.assertIsNotNone(comp(s, {"dt": {"type": "datetime", "value": {"mode": "any"}}}).match("y=2026/a(1)+b.json"))

    def test_mixed_datetime_and_plain(self) -> None:
        with self.assertRaises(PatternError) as cm:
            comp("{{x=YYYY}}/{{x}}")
        self.assertIn("混ざっています", cm.exception.issues[0].message)

    def test_tokenize_literal_container(self) -> None:
        toks, issues = tokenize("logs-{{n}}.x", literal=True)
        self.assertEqual(issues, [])
        self.assertEqual(toks[0].text, re.escape("logs-"))
        self.assertEqual(toks[2].text, re.escape(".x"))


class VariableTest(unittest.TestCase):
    def test_string_any_one_level(self) -> None:
        c = comp("a/{{v}}/b")
        self.assertIsNotNone(c.match("a/x y/b"))
        self.assertIsNone(c.match("a/x/y/b"))

    def test_one_of(self) -> None:
        c = comp("{{v}}/x", {"v": one_of("web", "api", "old", disabled=("old",))})
        self.assertIsNotNone(c.match("web/x"))
        self.assertIsNone(c.match("old/x"))
        self.assertIsNone(c.match("webx/x"))
        c = comp("{{v}}/x", {"v": one_of("web", match="prefix")})
        self.assertIsNotNone(c.match("web-01/x"))
        self.assertIsNone(c.match("a-web/x"))
        self.assertIsNone(c.match("web/1/x"))
        c = comp("{{v}}/x", {"v": one_of("web", match="partial")})
        self.assertIsNotNone(c.match("a-web-1/x"))
        self.assertIsNone(c.match("a/web/x"))
        # 記号はエスケープします
        c = comp("{{v}}", {"v": one_of("a.b", "c+d")})
        self.assertIsNotNone(c.match("a.b"))
        self.assertIsNone(c.match("axb"))
        with self.assertRaises(PatternError):
            comp("{{v}}", {"v": one_of("a/b")})
        with self.assertRaises(PatternError):
            comp("{{v}}", {"v": one_of("a", disabled=("a",))})

    def test_chars(self) -> None:
        v = {"type": "string", "value": {"mode": "chars", "classes": ["digit", "lower"], "extra": "-", "min": 8, "max": 36}}
        c = comp("{{v}}", {"v": v})
        self.assertIsNotNone(c.match("abc-1234"))
        self.assertIsNone(c.match("abc-123"))
        self.assertIsNone(c.match("ABC-1234"))
        self.assertIsNone(c.match("abc/1234"))
        v["value"]["allow_slash"] = True
        self.assertIsNotNone(comp("{{v}}", {"v": v}).match("abc/1234"))
        # 記号の文字クラスは / を含まず、] ^ - \ をエスケープします
        v = {"type": "string", "value": {"mode": "chars", "classes": ["symbol"], "min": 1}}
        c = comp("{{v}}", {"v": v})
        for s in ("]", "^", "-", "\\", "[", "!", "~", "{}"):
            self.assertIsNotNone(c.match(s), s)
        self.assertIsNone(c.match("/"))
        self.assertIsNone(c.match("a"))
        v = {"type": "string", "value": {"mode": "chars", "classes": ["hiragana", "katakana", "kanji"], "min": 1}}
        c = comp("{{v}}", {"v": v})
        for s in ("あいう", "カタカナー", "漢字々", "ｶﾀｶﾅ", "𠮷"):
            self.assertIsNotNone(c.match(s), s)
        self.assertIsNone(c.match("abc"))
        v = {"type": "string", "value": {"mode": "chars", "classes": [], "extra": "]^-\\", "min": 1, "max": 3}}
        c = comp("{{v}}", {"v": v})
        self.assertIsNotNone(c.match("]^-"))
        self.assertIsNone(c.match("a"))
        with self.assertRaises(PatternError):
            comp("{{v}}", {"v": {"type": "string", "value": {"mode": "chars", "classes": []}}})
        v = {"type": "string", "value": {"mode": "chars", "classes": ["digit"], "min": 4, "max": 4}}
        self.assertIsNone(comp("{{v}}", {"v": v}).match("123"))

    def test_numbers(self) -> None:
        c = comp("n={{n}}", {"n": {"type": "number", "value": {"mode": "any"}}})
        self.assertEqual(c.match("n=0042"), {"n": "0042"})
        self.assertIsNone(c.match("n=a"))
        c = comp("n={{n}}", {"n": {"type": "number", "padding": 2, "value": {"mode": "fixed", "value": 7}}})
        self.assertIsNotNone(c.match("n=07"))
        self.assertIsNone(c.match("n=7"))
        c = comp("n={{n}}", {"n": {"type": "number", "padding": 2, "value": {"mode": "range", "start": 1, "end": 12, "step": 3}}})
        self.assertEqual([x for x in range(0, 15) if c.match(f"n={x:02d}")], [1, 4, 7, 10])
        self.assertEqual(c.prefixes, ["n=01", "n=04", "n=07", "n=10"])
        # 列挙が 2,000 を超える場合は形と追加の判定
        c = comp("n={{n}}", {"n": {"type": "number", "value": {"mode": "range", "start": 0, "end": 99999, "step": 7}}})
        self.assertNotIn("|", c.source)
        self.assertIsNotNone(c.match("n=14"))
        self.assertIsNone(c.match("n=15"))
        self.assertIsNone(c.match("n=100002"))
        with self.assertRaises(PatternError):
            comp("{{n}}", {"n": {"type": "number", "value": {"mode": "range", "start": 5, "end": 1}}})


class ContainerTest(unittest.TestCase):
    def test_expand(self) -> None:
        v = {"dt": {"type": "datetime", "path_timezone": "utc", "value": {"mode": "period", "start": "2026-10", "end": "2026-12"}},
             "env": one_of("prod", "dev")}
        out = expand_containers("logs-{{env}}-{{dt=YYYYMM}}", v, input_timezone="utc", ctx=CTX)
        self.assertEqual(sorted(n for n, _ in out), ["logs-dev-202610", "logs-dev-202611", "logs-dev-202612",
                                                     "logs-prod-202610", "logs-prod-202611", "logs-prod-202612"])
        self.assertIsNone(expand_containers("logs-{{x}}", {}, input_timezone="utc", ctx=CTX))
        self.assertEqual(expand_containers("$logs", {}, input_timezone="utc", ctx=CTX), [("$logs", {})])
        v = {"dt": {"type": "datetime", "path_timezone": "utc", "value": {"mode": "period", "start": "2026-01-01", "end": "2026-12-31"}}}
        with self.assertRaises(PatternError):
            expand_containers("c{{dt=YYYYMMDD}}", v, input_timezone="utc", ctx=CTX)

    def test_container_fixed_values_restrict_path(self) -> None:
        v = {"dt": {"type": "datetime", "path_timezone": "utc", "value": {"mode": "period", "start": "2026-10-30", "end": "2026-11-02"}}}
        out = dict(expand_containers("logs-{{dt=YYYYMM}}", v, input_timezone="utc", ctx=CTX))
        fixed = out["logs-202611"]
        c = comp("{{dt=DD}}/x", v, input_timezone="utc", fixed={"dt": fixed["dt"]})
        self.assertIsNotNone(c.match("01/x"))
        self.assertIsNotNone(c.match("02/x"))
        self.assertIsNone(c.match("30/x"))
        self.assertEqual(c.match("02/x"), {"dt": {"year": 2026, "month": 11, "day": 2}})


if __name__ == "__main__":
    unittest.main()
