"""T-PFX・T-LIST・T-DET・T-FMT・T-SHORT。"""

import itertools
import random
import re
import threading
import unittest
from datetime import datetime, timezone
from unittest import mock

from finfet_abs.azure.backend import AccountRef, AuthRef, BlobInfo, ContainerRef
from finfet_abs.demo.memory import MemoryBackend
from finfet_abs.search import detect, formats, listing, prefix, shortener
from finfet_abs.search.datetime_vars import FixedZone
from finfet_abs.search.pattern import CompileContext, compile_path
from finfet_abs.search.sortkey import sort_key

CTX = CompileContext(now_utc=datetime(2026, 11, 20, 3, 0, tzinfo=timezone.utc), local_zone=FixedZone(9))


def all_strings(alphabet: str, max_len: int):
    for n in range(max_len + 1):
        for t in itertools.product(alphabet, repeat=n):
            yield "".join(t)


class PrefixTest(unittest.TestCase):
    def check_invariant(self, regex: str, alphabet: str = "ab01/", max_len: int = 5, limit: int = 32) -> list[str]:
        ps = prefix.compute(regex, 0, limit)
        self.assertLessEqual(len(ps), max(limit, 1))
        for a, b in itertools.permutations(ps, 2):
            self.assertFalse(b.startswith(a), (a, b))
        rx = re.compile(regex)
        for s in all_strings(alphabet, max_len):
            if rx.fullmatch(s):
                self.assertTrue(any(s.startswith(p) for p in ps), (regex, s, ps))
        return ps

    def test_basic(self) -> None:
        self.assertEqual(prefix.compute("abc/[^/]+"), ["abc/"])
        self.assertEqual(prefix.compute("ab(?:0|1)/x"), ["ab0/x", "ab1/x"])
        self.assertEqual(prefix.compute("ab[01]/x"), ["ab0/x", "ab1/x"])
        self.assertEqual(prefix.compute("ab[0-9]/x"), ["ab"])
        self.assertEqual(prefix.compute("a+b"), ["a"])
        self.assertEqual(prefix.compute("ab*"), ["a"])
        self.assertEqual(prefix.compute(".*"), [""])
        self.assertEqual(prefix.compute("(?:a|ab)c"), ["ac", "abc"][:0] + sorted(["abc", "ac"]))
        # ほかの prefix で始まるものは除きます
        self.assertEqual(prefix.normalize(["ab", "a", "abc", "b"]), ["a", "b"])

    def test_invariants(self) -> None:
        for rx in ["a(?:b|0)/[01]", "(?:a|b)(?:0|1)(?:/|a)", "a?b", "(?:ab|a)b", "[ab]+0", "a(?:|b)0", "(?:a(?:0|1)|b)/",
                   "a(?=b)b", "(a)\\1", "(?:a|b|0|1|/){2}", "ab|a0|/"]:
            self.check_invariant(rx)

    def test_limit_and_nesting(self) -> None:
        rx = "y=2026/(?:11/(?:28|29|30)|12/(?:01|02))/x"
        self.assertEqual(len(prefix.compute(rx, 0, 32)), 5)
        ps = prefix.compute(rx, 0, 2)
        # 解析器は 01|02 を 0[12] にまとめるため、12 月は「12/0」までになります
        self.assertEqual(ps, ["y=2026/11/", "y=2026/12/0"])
        ps = prefix.compute(rx, 0, 1)
        self.assertEqual(ps, ["y=2026/1"])
        many = "x(?:" + "|".join(f"{i:02d}" for i in range(40)) + ")"
        self.assertEqual(prefix.compute(many, 0, 32), ["x"])

    def test_ignorecase_and_failure(self) -> None:
        self.assertEqual(prefix.compute("abc", re.IGNORECASE), [""])
        self.assertEqual(prefix.compute("(?i)abc"), [""])
        self.assertEqual(prefix.compute("a(?i:b)c"), ["a"])
        self.assertEqual(prefix.compute("(unbalanced"), [""])
        with mock.patch.object(prefix, "_p", None):
            self.assertEqual(prefix.compute("abc"), [""])


def blobs(names, tier="Archive"):
    return [BlobInfo(n, 10, tier=tier) for n in names]


class ListingTest(unittest.TestCase):
    def setUp(self) -> None:
        self.be = MemoryBackend()
        self.acct = AccountRef("stexample01", auth=AuthRef("account_key"))
        self.t = ContainerRef(self.acct, "logs")
        self.got = []

    def run_list(self, prefixes, **kw):
        self.got = []
        self.be.requests.clear()
        listing.list_target(self.be, self.t, prefixes, on_items=lambda items: self.got.extend(b.name for b in items), **kw)
        return [r for r in self.be.requests if r[0] == "list"]

    def test_single_page_merge(self) -> None:
        self.be.add_blobs("stexample01", "logs", blobs([f"d={d:02d}/h={h:02d}" for d in range(1, 4) for h in range(24)]))
        reqs = self.run_list(["d=01/", "d=02/"])
        self.assertEqual(len(reqs), 1)
        self.assertEqual(reqs[0][2], "d=0")
        self.assertEqual(len(self.got), 72)

    def test_split_after_trial(self) -> None:
        names = [f"d={d:02d}/{i:05d}" for d in range(1, 6) for i in range(4000)]
        self.be.add_blobs("stexample01", "logs", blobs(names))
        with mock.patch.object(listing, "PAGE_SIZE", 5000):
            reqs = self.run_list(["d=01/", "d=02/", "d=04/"], concurrency=1)
        # 1回目（d=0 の 5,000 件）で d=01 は取得済み。d=02 は途中のため最初から、d=04 は新しく取得
        self.assertEqual(reqs[0][2], "d=0")
        self.assertNotIn("d=01/", [r[2] for r in reqs])
        self.assertIn("d=02/", [r[2] for r in reqs])
        self.assertIn("d=04/", [r[2] for r in reqs])
        wanted = [n for n in names if n.startswith(("d=01/", "d=02/", "d=04/"))]
        # 重複を除いて、必要なものはすべて受け取ります（d=0 のページの d=03 などは呼び出し側でフィルターします）
        got_wanted = [n for n in self.got if n.startswith(("d=01/", "d=02/", "d=04/"))]
        self.assertEqual(sorted(got_wanted), sorted(wanted))
        self.assertEqual(len(got_wanted), len(set(got_wanted)))

    def test_covered_by_trial(self) -> None:
        self.assertTrue(listing.covered_by_trial("a/", "b/1"))
        self.assertFalse(listing.covered_by_trial("b/", "b/1"))
        self.assertFalse(listing.covered_by_trial("c/", "b/1"))
        # Azure の順（UTF-16 の符号単位）: 😀（U+1F600）は Ａ（U+FF21）より前。Python の文字列の比較では後
        self.assertTrue(listing.covered_by_trial("😀/", "Ａ/1"))
        self.assertFalse(listing.covered_by_trial("Ａ/", "😀/1"))

    def test_azure_order_split(self) -> None:
        """絵文字と全角の名前が混ざるとき、まとめて取得から切り替えても取りこぼさない（実環境で確認した順）。"""
        names = [f"x/😀/{i:05d}" for i in range(3000)] + [f"x/Ａ/{i:05d}" for i in range(3000)] + \
                [f"x/ﾆ/{i:05d}" for i in range(3000)]
        self.be.add_blobs("stexample01", "logs", blobs(names))
        listed = [b.name for b in self.be.list_blobs_page(self.t, "x/", page_size=10000).items]
        self.assertEqual(listed[0][:3], "x/😀")  # 架空の実装も Azure と同じ順
        self.assertEqual(listed, sorted(names, key=listing.azure_order))
        with mock.patch.object(listing, "PAGE_SIZE", 5000):
            self.run_list(["x/😀/", "x/Ａ/", "x/ﾆ/"], concurrency=1)
        self.assertEqual(set(self.got), set(names))

    def test_cache(self) -> None:
        self.be.add_blobs("stexample01", "logs", blobs([f"a/{i}" for i in range(10)] + [f"b/{i}" for i in range(10)]))
        cache = listing.ListingCache()
        self.assertEqual(len(self.run_list([""], cache=cache)), 1)
        self.assertEqual(len(self.run_list(["a/"], cache=cache)), 0)
        self.assertEqual(sorted(self.got), sorted(f"a/{i}" for i in range(10)))
        cache2 = listing.ListingCache()
        self.run_list(["a/"], cache=cache2)
        self.assertEqual(len(self.run_list([""], cache=cache2)), 1)  # より広い prefix は再利用しません

    def test_limit_and_cancel(self) -> None:
        self.be.add_blobs("stexample01", "logs", blobs([f"a/{i:05d}" for i in range(12000)]))
        count = [0]

        def on_items(items):
            count[0] += len(items)
            if count[0] >= 5000:
                raise listing.LimitReached()

        with self.assertRaises(listing.LimitReached):
            listing.list_target(self.be, self.t, ["a/"], on_items=on_items)
        self.assertEqual(count[0], 5000)
        ev = threading.Event()
        ev.set()
        with self.assertRaises(listing.Cancelled):
            listing.list_target(self.be, self.t, ["a/"], on_items=lambda i: None, cancel=ev)

    def test_containers_listing_excludes_logs(self) -> None:
        self.be.add_container("stexample01", "$logs")
        self.be.add_container("stexample01", "logs-202611")
        self.assertEqual(self.be.list_containers(self.acct, "logs-"), ["logs-202611"])
        self.assertEqual(self.be.list_containers(self.acct), ["logs-202611"])


class DetectTest(unittest.TestCase):
    def test_inference(self) -> None:
        r = detect.detect("y={{year}}/m={{month}}/{{seqNo}}/{{deviceId}}/{{app}}/{{dt=YYYY}}")
        defs = {v["name"]: v["definition"] for v in r["variables"]}
        self.assertEqual(defs["year"]["type"], "number")
        self.assertEqual(defs["year"]["padding"], 4)
        self.assertEqual(defs["month"]["padding"], 2)
        self.assertEqual(defs["seqNo"]["type"], "number")
        self.assertEqual(defs["deviceId"]["value"]["mode"], "chars")
        self.assertEqual(defs["app"]["value"]["mode"], "any")
        self.assertEqual(defs["dt"]["type"], "datetime")
        self.assertEqual(r["merge"]["pattern"], "y={{dt2=YYYY}}/m={{dt2=MM}}/{{seqNo}}/{{deviceId}}/{{app}}/{{dt=YYYY}}")

    def test_context(self) -> None:
        r = detect.detect("y={{a}}/m={{b}}/d={{c}}/h={{d}}/m={{e}}/x")
        units = {v["name"]: v["definition"].get("unit") for v in r["variables"]}
        self.assertEqual(units, {"a": "year", "b": "month", "c": "day", "d": "hour", "e": "minute"})
        self.assertEqual(r["merge"]["pattern"], "y={{dt=YYYY}}/m={{dt=MM}}/d={{dt=DD}}/h={{dt=hh}}/m={{dt=mm}}/x")

    def test_unused_and_conflict(self) -> None:
        existing = {"app": {"type": "string", "value": {"mode": "one_of", "values": [{"value": "x"}]}},
                    "old": {"type": "string", "value": {"mode": "any"}}}
        r = detect.detect("{{app}}/{{new}}", [], existing)
        by = {v["name"]: v for v in r["variables"]}
        self.assertFalse(by["app"]["new"])
        self.assertEqual(by["app"]["definition"], existing["app"])
        self.assertTrue(by["new"]["new"])
        self.assertFalse(by["old"]["in_pattern"])
        r = detect.detect("{{x=YYYY}}/{{x}}")
        self.assertTrue(r["issues"])
        # 日時と通常の名前の衝突（同じ名前を日時に変えた場合）
        r = detect.detect("{{app=YYYY}}", [], existing)
        self.assertEqual({v["name"]: v for v in r["variables"]}["app"]["definition"]["type"], "datetime")

    def test_container_variables(self) -> None:
        r = detect.detect("x", ["logs-{{env}}"])
        self.assertEqual(r["variables"][0]["where"], ["container"])


class FormatsTest(unittest.TestCase):
    def test_all_formats(self) -> None:
        self.assertTrue(formats.load())
        for f in formats.load():
            self.assertTrue(f.get("examples"), f["id"])
            for ex in f["examples"]:
                built = formats.build(f["id"], ex["inputs"])
                c = compile_path(built["path_pattern"], built["variables"] | {
                    n: {"type": "datetime", "path_timezone": "utc", "value": {"mode": "any"}}
                    for n, d in built["variables"].items() if d["type"] == "datetime"},
                    input_timezone="utc", ctx=CTX)
                self.assertIsNotNone(c.match(ex["blob"]), (f["id"], built["path_pattern"], ex["blob"]))
                self.assertEqual(built["container"], ex["container"], f["id"])
            # 入力を空にしても、変数としてコンパイルできます
            built = formats.build(f["id"], {i["name"]: (i["options"][0]["id"] if i.get("kind") == "select" else
                                                          "x" if i.get("required") else "")
                                            for i in f.get("inputs", [])})
            compile_path(built["path_pattern"], built["variables"], input_timezone="utc", ctx=CTX)

    def test_upper_and_errors(self) -> None:
        b = formats.build("resource-logs", {"category": "StorageRead", "sub": "00000000-0000-0000-0000-00000000000a",
                                            "rg": "rg-x", "resource": "stexample01"})
        self.assertIn("/SUBSCRIPTIONS/00000000-0000-0000-0000-00000000000A/RESOURCEGROUPS/RG-X/", b["path_pattern"])
        self.assertIn("STORAGEACCOUNTS/STEXAMPLE01/BLOBSERVICES", b["path_pattern"])
        self.assertEqual(b["container"], "insights-logs-storageread")
        with self.assertRaises(formats.FormatInputError):
            formats.build("resource-logs", {"category": ""})
        with self.assertRaises(formats.FormatInputError):
            formats.build("resource-logs", {"category": "x", "sub": "not-a-guid"})

    def test_suggest(self) -> None:
        self.assertEqual(formats.suggest("insights-logs-storageread")["id"], "resource-logs")
        self.assertEqual(formats.suggest("insights-logs-signinlogs")["id"], "entra-logs")
        self.assertEqual(formats.suggest("insights-logs-flowlogflowevent")["id"], "vnet-flow-logs")
        self.assertEqual(formats.suggest("$logs")["id"], "storage-analytics")
        self.assertEqual(formats.suggest("am-securityevent")["id"], "log-analytics-export")
        self.assertIsNone(formats.suggest("backup"))
        self.assertEqual(formats.suggest_inputs("resource-logs", "insights-logs-storageread"), {"category": "storageread"})
        ids = [f["id"] for f in formats.public_list()]
        self.assertNotIn("app-service-logs", ids)


class ShortTest(unittest.TestCase):
    def test_common_unique(self) -> None:
        rnd = random.Random(7)
        for _ in range(50):
            names = list({"/".join(rnd.choice(["a", "b", "x=1", "PT1H.json"]) for _ in range(rnd.randint(1, 5)))
                          for _ in range(rnd.randint(1, 30))})
            sh = shortener.CommonShortener(names)
            outs = [sh.short(n) for n in names]
            self.assertEqual(len(set(outs)), len(names), (names, outs))
            tails = shortener.tail_names(names)
            self.assertEqual(len(set(tails.values())), len(names), (names, tails))

    def test_examples(self) -> None:
        names = [f"resourceId=/SUBSCRIPTIONS/X/y=2026/m=11/d=13/h={h:02d}/m=00/PT1H.json" for h in (15, 16)]
        sh = shortener.CommonShortener(names)
        self.assertEqual(sh.short(names[0]), "…/h=15/…")
        t = shortener.tail_names(["a/x/PT1H.json", "b/x/PT1H.json", "c/y.json"])
        self.assertEqual(t["c/y.json"], "…/y.json")
        self.assertEqual(t["a/x/PT1H.json"], "a/x/PT1H.json")

    def test_sort_key(self) -> None:
        names = ["h=10", "h=2", "h=1", "a", "h=02", "h=10/a", "b2", "b10", "日本語1", "日本語10"]
        ordered = sorted(names, key=sort_key)
        self.assertLess(ordered.index("h=2"), ordered.index("h=10"))
        self.assertLess(ordered.index("b2"), ordered.index("b10"))
        self.assertLess(ordered.index("日本語1"), ordered.index("日本語10"))
        self.assertNotEqual(sort_key("h=02"), sort_key("h=2"))
        self.assertEqual(abs(ordered.index("h=02") - ordered.index("h=2")), 1)


if __name__ == "__main__":
    unittest.main()


class DemoDataTest(unittest.TestCase):
    def test_demo_names_match_known_formats(self) -> None:
        from finfet_abs.demo.generator import populate
        be = MemoryBackend()
        counts = populate(be, 2000, now=datetime(2026, 11, 20, 3, tzinfo=timezone.utc))
        self.assertGreater(sum(counts.values()), 1000)
        checks = [("stexample01", "insights-logs-storageread", "resource-logs", {"category": "StorageRead"}),
                  ("stexample01", "am-securityevent", "log-analytics-export", {"table": "SecurityEvent"}),
                  ("stexample02", "$logs", "storage-analytics", {}),
                  ("stexample03", "insights-logs-flowlogflowevent", "vnet-flow-logs", {}),
                  ("stexample02", "inventory", "inventory-data", {})]
        for acct, cont, fid, inputs in checks:
            built = formats.build(fid, inputs)
            for n, d in built["variables"].items():
                if d["type"] == "datetime":
                    d["value"] = {"mode": "any"}
            c = compile_path(built["path_pattern"], built["variables"], input_timezone="utc", ctx=CTX)
            page = be.list_blobs_page(ContainerRef(AccountRef(acct), cont), "")
            matched = [b.name for b in page.items if c.match(b.name)]
            self.assertTrue(matched, (cont, built["path_pattern"]))
