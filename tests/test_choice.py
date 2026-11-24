"""選択肢の変数（type: choice。依頼者の依頼による機能）。"""

from __future__ import annotations

import unittest
from datetime import datetime, timezone

from finfet_abs.history.condition import clean_condition, variables_summary
from finfet_abs.search import datetime_vars as dtv
from finfet_abs.search.pattern import CompileContext, PatternError, compile_path
from finfet_abs.search.prefix import compute as compute_prefix

CTX = CompileContext(now_utc=datetime(2026, 11, 22, tzinfo=timezone.utc), local_zone=dtv.FixedZone(9))


def choice(options, selected=None):
    return {"type": "choice", "value": {"options": list(options), "selected": list(options if selected is None else selected)}}


class ChoiceTest(unittest.TestCase):
    def test_matches_only_selected(self) -> None:
        cp = compile_path(r"vm-logs/{{vm}}/app\.log", {"vm": choice(["web01", "web02", "db01"], ["web01", "db01"])}, ctx=CTX)
        self.assertEqual(cp.match("vm-logs/web01/app.log"), {"vm": "web01"})
        self.assertIsNotNone(cp.match("vm-logs/db01/app.log"))
        self.assertIsNone(cp.match("vm-logs/web02/app.log"))  # 選んでいない値
        self.assertIsNone(cp.match("vm-logs/web011/app.log"))  # 完全一致

    def test_special_characters_are_literal(self) -> None:
        cp = compile_path(r"{{v1}}/x\.txt", {"v1": choice(["a.b", "c(d)"])}, ctx=CTX)
        self.assertIsNotNone(cp.match("a.b/x.txt"))
        self.assertIsNone(cp.match("aXb/x.txt"))
        self.assertIsNotNone(cp.match("c(d)/x.txt"))

    def test_nothing_selected_is_an_error(self) -> None:
        with self.assertRaises(PatternError) as cm:
            compile_path(r"{{vm}}/a\.log", {"vm": choice(["web01"], [])}, ctx=CTX)
        self.assertIn("1 つ以上選択してください", str(cm.exception.issues[0].message))

    def test_prefixes_from_selected_values(self) -> None:
        cp = compile_path(r"vm-logs/{{vm}}/{{dt=YYYY}}/app\.log", {"vm": choice(["web01", "db01"]),
                                                                 "dt": {"type": "datetime", "value": {"mode": "any"}}}, ctx=CTX)
        self.assertEqual(sorted(compute_prefix(cp.regex.pattern)), ["vm-logs/db01/", "vm-logs/web01/"])

    def test_condition_round_trip_and_summary(self) -> None:
        cond = {"targets": [{"storage_account": "stexample01", "container": "applogs", "auth_kind": "account_key"}],
                "path_pattern": r"vm-logs/{{vm}}/app\.log", "variables": {"vm": choice(["web01", "web02", "db01", "batch01"], ["web01", "db01", "batch01", "web02"])}}
        out = clean_condition(cond)
        self.assertEqual(out["variables"]["vm"]["type"], "choice")
        self.assertEqual(variables_summary(out), "vm: web01, db01, batch01 ほか1件")


if __name__ == "__main__":
    unittest.main()
