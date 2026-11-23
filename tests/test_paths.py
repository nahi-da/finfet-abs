"""T-PATH: 保存するパス（指定の解析と誤り、値、重複の検出・提案・番号、名前の直し、同名のファイル、30 万件の時間）。"""

from __future__ import annotations

import os
import time
import unittest

from finfet_abs.jobs import paths
from finfet_abs.jobs.paths import PathPlanner, Template, TemplateError, VarInfo

from .helpers import TempDirMixin

COND = {"targets": [{"storage_account": "stexample01", "container": "logs"}],
        "path_pattern": "{{dt=YYYY}}/{{dt=MM}}/{{dt=DD}}/{{app}}/{{dt=YYYYMMDD}}_{{n}}\\.log",
        "variables": {"dt": {"type": "datetime"}, "app": {"type": "string"}, "n": {"type": "number"}}}
VARS = paths.variables_of(COND)


def item(name: str, *, account: str = "stexample01", container: str = "logs", vars: dict | None = None) -> dict:
    return {"storage_account": account, "container": container, "blob_name": name, "vars": vars or {}}


def dt(y=2026, mo=11, d=13, h=None) -> dict:
    v = {"year": y, "month": mo, "day": d}
    if h is not None:
        v["hour"] = h
    return v


class TemplateTest(unittest.TestCase):
    def test_variables_of(self) -> None:
        self.assertEqual(VARS["dt"].units, {0, 1, 2})
        self.assertEqual(VARS["app"].kind, "string")
        # ジョブの作成の画面に渡す形（v1.5.2: 保存するパスのブロックで、日時の単位に合う書式だけを出します）
        info = {k: {"kind": v.kind, "units": sorted(v.units)} for k, v in sorted(VARS.items())}
        self.assertEqual(info["dt"], {"kind": "datetime", "units": [0, 1, 2]})
        self.assertEqual(info["n"], {"kind": "number", "units": []})

    def test_render(self) -> None:
        t = Template.parse("{{dt=YYYYMM}}/{{dt=YYYYMMDD}}.log", VARS)
        self.assertEqual(t.render(item("2026/11/13/web/20261113_01.log", vars={"dt": dt()})), "202611\\20261113.log")
        t = Template.parse("{{@account}}/{{@container}}/{{@dir}}/{{@stem}}-{{n}}{{@ext}}", VARS)
        self.assertEqual(t.render(item("a/b/c.tar.gz", vars={"n": "007"})), "stexample01\\logs\\a\\b\\c.tar-007.gz")
        # 値の / は階層の区切り、空の値の階層は _
        t = Template.parse("{{app}}/{{@name}}", VARS)
        self.assertEqual(t.render(item("x.log", vars={"app": "web/front"})), "web\\front\\x.log")
        self.assertEqual(t.render(item("x.log", vars={"app": ""})), "_\\x.log")
        # 名前の直し
        t = Template.parse("{{app}}/{{@name}}", VARS)
        self.assertEqual(t.render(item("a:b?.log", vars={"app": "CON"})), "_CON\\a_b_.log")
        self.assertEqual(t.render(item("x.log", vars={"app": ".."})), "__\\x.log")  # 保存先の外は指しません

    def test_errors(self) -> None:
        cases = {
            "": "入力",
            "C:/x/{{@name}}": "相対パス",
            "\\\\srv\\x": "相対パス",
            "/x": "相対パス",
            "{{@nope}}": "@account",
            "{{zzz}}": "検索条件にありません",
            "{{dt}}": "書式を付けて",
            "{{dt=YYYYMMDDhh}}": "時",
            "{{dt=yyyy}}": "YYYY",
            "{{app=YYYY}}": "書式は付けられません",
            "a//{{@name}}": "空の階層",
            "a/": "空の階層",
            "a/{{@name": "閉じていない",
        }
        for text, word in cases.items():
            with self.assertRaises(TemplateError, msg=text) as cm:
                Template.parse(text, VARS)
            self.assertIn(word, cm.exception.issues[0].message, text)
        with self.assertRaises(TemplateError) as cm:
            Template.parse("{{app}}/{{@name}}", VARS, url_mode=True)
        self.assertIn("URL", cm.exception.issues[0].message)
        Template.parse("{{@container}}/{{@name}}", {}, url_mode=True)

    def test_structures(self) -> None:
        it = item("a/b/c.log")
        self.assertEqual(PathPlanner("preserve").path(it), "logs\\a\\b\\c.log")
        self.assertEqual(PathPlanner("flatten").path(it), "logs_a_b_c.log")
        self.assertEqual(PathPlanner("name_only").path(it), "c.log")
        self.assertEqual(PathPlanner("preserve").path(item("a/ b./c<1>.log ")), "logs\\a\\ b\\c_1_.log")

    def test_sanitize(self) -> None:
        self.assertEqual(paths.sanitize_segment("nul.txt"), "_nul.txt")
        self.assertEqual(paths.sanitize_segment("COM1"), "_COM1")
        self.assertEqual(paths.sanitize_segment("COM10"), "COM10")
        # 1 つの階層の名前（ジョブのフォルダー名など）に区切りを含めません
        self.assertEqual(paths.sanitize_segment("20261121_stexample01/logs"), "20261121_stexample01_logs")
        self.assertEqual(paths.sanitize_segment("a\\b"), "a_b")
        self.assertEqual(paths.sanitize_segment("a\x01b"), "a_b")
        self.assertEqual(paths.sanitize_segment("..."), "_")
        self.assertEqual(paths.sanitize_segment("."), "_")


class DuplicateTest(unittest.TestCase):
    def test_detect_case_and_accounts(self) -> None:
        items = [item("x/A.log"), item("y/a.log"), item("z/b.log", account="stexample02"), item("z/b.log")]
        ps = [PathPlanner("name_only").path(i) for i in items]
        groups = paths.find_duplicates(ps)
        self.assertEqual(sorted(sorted(g) for g in groups.values()), [[0, 1], [2, 3]])
        ps = [PathPlanner("preserve").path(i) for i in items]
        self.assertEqual(len(paths.find_duplicates(ps)), 1)  # 別のアカウントの同じコンテナー・名前

    def test_suggest_datetime_unit(self) -> None:
        tmpl = "{{dt=YYYYMM}}/{{dt=YYYYMMDD}}.log"
        items = [item(f"h{h}", vars={"dt": dt(h=h)}) for h in (1, 2)]
        cond = dict(COND, path_pattern="{{dt=YYYY}}/{{dt=MM}}/{{dt=DD}}/{{dt=hh}}\\.log")
        vs = paths.variables_of(cond)
        t = Template.parse(tmpl, vs)
        ps = [t.render(i) for i in items]
        groups = list(paths.find_duplicates(ps).values())
        self.assertEqual(paths.suggest(items, groups, tmpl, vs), "{{dt=YYYYMM}}/{{dt=YYYYMMDDhh}}.log")

    def test_suggest_account(self) -> None:
        tmpl = "{{dt=YYYYMM}}/{{dt=YYYYMMDD}}.log"
        items = [item("a", account=a, vars={"dt": dt(d=d)}) for a in ("stexample01", "stexample02") for d in (13, 14)]
        t = Template.parse(tmpl, VARS)
        groups = list(paths.find_duplicates([t.render(i) for i in items]).values())
        self.assertEqual(len(groups), 2)
        s = paths.suggest(items, groups, tmpl, VARS)
        self.assertEqual(s, "{{dt=YYYYMM}}/{{dt=YYYYMMDD}}_{{@account}}.log")
        t2 = Template.parse(s, VARS)
        self.assertFalse(paths.find_duplicates([t2.render(i) for i in items]))

    def test_suggest_from_structure(self) -> None:
        items = [item("a/x.log", account="stexample01"), item("a/x.log", account="stexample02")]
        base = paths.base_template("preserve")
        groups = list(paths.find_duplicates([PathPlanner("preserve").path(i) for i in items]).values())
        s = paths.suggest(items, groups, base, {})
        self.assertEqual(s, "{{@container}}/{{@dir}}/{{@stem}}_{{@account}}{{@ext}}")
        self.assertEqual(Template.parse(s, {}).render(items[1]), "logs\\a\\x_stexample02.log")
        self.assertIsNone(paths.base_template("flatten"))

    def test_suggest_gives_up(self) -> None:
        # 4 つの要素がそろわないと区別できない組は、提案しません
        items = [item("same", vars={"app": a, "n": n}) for a in ("p", "q") for n in ("1", "2")]
        items += [item("same", account="stexample02", container="c2", vars={"app": "p", "n": "1"})]
        tmpl = "fixed.log"
        groups = list(paths.find_duplicates([Template.parse(tmpl, VARS).render(i) for i in items]).values())
        vs = {"app": VarInfo("string"), "n": VarInfo("string")}
        self.assertIsNotNone(paths.suggest(items, groups, tmpl, vs))  # app・n・@account の 3 個で解決
        items.append(item("same", account="stexample02", container="logs", vars={"app": "p", "n": "1"}))
        groups = list(paths.find_duplicates([Template.parse(tmpl, VARS).render(i) for i in items]).values())
        self.assertIsNone(paths.suggest(items, groups, tmpl, vs))

    def test_numbering_by_name_order(self) -> None:
        ps = ["d\\x.log", "d\\X.log", "d\\x(1).log", "d\\x.LOG"]
        order = [3, 1, 0, 2]  # Blob 名の順
        groups = list(paths.find_duplicates(ps).values())
        out = paths.number_duplicates(ps, order, groups)
        self.assertEqual(out[3], "d\\x.LOG")  # 最初のものはそのまま
        self.assertEqual(out[1], "d\\X(2).log")  # x(1) はほかの Blob が使っているため 2
        self.assertEqual(out[0], "d\\x(3).log")
        self.assertEqual(out[2], "d\\x(1).log")

    def test_300k_time(self) -> None:
        items = [item(f"2026/11/{d:02d}/{h:02d}/{i:04d}.json", vars={"dt": dt(d=d, h=h)})
                 for d in range(1, 21) for h in range(24) for i in range(625)]
        self.assertEqual(len(items), 300000)
        cond = dict(COND, path_pattern="{{dt=YYYY}}/{{dt=MM}}/{{dt=DD}}/{{dt=hh}}/.*")
        vs = paths.variables_of(cond)
        t0 = time.perf_counter()
        t = Template.parse("{{dt=YYYYMMDD}}/{{@name}}", vs)
        ps = [t.render(i) for i in items]
        groups = paths.find_duplicates(ps)
        s = paths.suggest(items, list(groups.values()), t.text, vs)
        el = time.perf_counter() - t0
        self.assertEqual(s, "{{dt=YYYYMMDDhh}}/{{@name}}")
        # 目安は 2 秒（開発の PC）。GitHub Actions の PC は数倍遅いため、上限を緩めます。
        # 開発の PC でも、全体のテストの途中ではほかの処理の影響で 4.5 秒ほどになることがあったため、上限は 6 秒にします
        self.assertLess(el, 20.0 if os.environ.get("CI") else 6.0)


class ReserveTest(TempDirMixin):
    def test_conflict_modes(self) -> None:
        p = self.tmp / "d" / "a.log"
        got, clash = paths.reserve(p, "number")
        self.assertEqual((got, clash), (str(p), False))
        got, clash = paths.reserve(p, "number")
        self.assertEqual((os.path.basename(got), clash), ("a(1).log", True))
        os.mkdir(self.tmp / "d" / "a(2).log")  # 同名のフォルダーも使用中
        got, _ = paths.reserve(p, "number")
        self.assertEqual(os.path.basename(got), "a(3).log")
        self.assertEqual(paths.reserve(p, "skip"), (None, True))
        self.assertEqual(paths.reserve(p, "overwrite"), (str(p), True))
        # 大文字・小文字だけが違う名前も、Windows では同じファイルです
        got, _ = paths.reserve(self.tmp / "d" / "A.LOG", "number")
        self.assertEqual(os.path.basename(got), "A(4).LOG")  # a(1)・a(2)（フォルダー）・a(3) は使用中

    def test_long_path_and_inside(self) -> None:
        deep = self.tmp.joinpath(*(["x" * 50] * 6), "file.log")
        self.assertGreater(len(str(deep)), 240)
        got, _ = paths.reserve(deep, "number")
        self.assertTrue(os.path.exists(paths.long_path(got)))
        self.assertTrue(paths.inside(self.tmp, "a\\b.log"))
        self.assertFalse(paths.inside(self.tmp, "..\\b.log"))
        self.assertFalse(paths.inside(self.tmp, "C:\\Windows\\x"))


if __name__ == "__main__":
    unittest.main()
