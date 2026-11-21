"""T-HIST: 検索履歴 / T-IO: 検索条件のエクスポート・インポート。"""

from __future__ import annotations

import json
import unittest
from datetime import datetime, timedelta, timezone

from finfet_abs.azure.credstore import CredentialStore, new_from_input
from finfet_abs.azure.targets import TargetService
from finfet_abs.core.clock import FakeClock
from finfet_abs.core.db import Database
from finfet_abs.core.errors import AppError
from finfet_abs.core.settings import Settings
from finfet_abs.demo.memory import MemoryBackend
from finfet_abs.history import exchange
from finfet_abs.history.condition import clean_condition, condition_hash
from finfet_abs.history.service import MAX_PINNED, HistoryService
from finfet_abs.search import datetime_vars as dtv
from finfet_abs.search.pattern import CompileContext, compile_path

from .helpers import DUMMY_SIG, TempDirMixin, dummy_key

NOW = datetime(2026, 11, 20, 3, 0, tzinfo=timezone.utc)
ACC = "https://stexample01.blob.core.windows.net"


def sas_url(container: str = "logs") -> str:
    return f"{ACC}/{container}?sv=2024-11-04&sr=c&sp=rl&se=2027-01-01T00:00:00Z&sig={DUMMY_SIG}"


def cond(container: str = "logs", *, auth: str = "entra_user", cid: str | None = None, value: dict | None = None,
         name: str | None = None) -> dict:
    t = {"storage_account": "stexample01", "endpoint_suffix": "core.windows.net", "container": container, "auth_kind": auth}
    if cid:
        t["credential_id"] = cid
    c = {"targets": [t], "path_pattern": "y={{dt=YYYY}}/m={{dt=MM}}/d={{dt=DD}}/.*", "input_timezone": "utc",
         "variables": {"dt": {"type": "datetime", "path_timezone": "utc",
                              "value": value or {"mode": "relative", "kind": "last_days", "n": 7}}}}
    if name:
        c["name"] = name
    return clean_condition(c)


class Base(TempDirMixin):
    def setUp(self) -> None:
        super().setUp()
        self.db = Database(self.tmp / "h.db")
        self.db.start()
        self.settings = Settings(self.tmp / "settings.json")
        self.settings.load()
        self.clock = FakeClock(NOW)
        self.hist = HistoryService(self.db, self.settings, self.clock)
        self.store = CredentialStore(self.db, self.clock)

    def tearDown(self) -> None:
        self.db.close()
        super().tearDown()

    def add_sas(self, container: str = "logs") -> str:
        c, _ = new_from_input("sas_url", value=sas_url(container))
        return self.store.add(c)[0]

    def add_key(self) -> str:
        c, _ = new_from_input("account_key", account="stexample01", key=dummy_key())
        return self.store.add(c)[0]


class HistoryTest(Base):
    def test_same_condition_merged(self) -> None:
        a = self.hist.record(cond(auth="sas", cid="id-1"))
        self.clock.advance(60)
        b = self.hist.record(cond(auth="sas", cid="id-2", name="名前だけ違う"))
        self.assertEqual(a, b)  # 認証情報の ID と名前が違っても同じ条件
        row = self.hist.get(a)
        self.assertEqual(row["use_count"], 2)
        self.assertEqual(row["last_used_at"], "2026-11-20T03:01:00Z")
        self.assertEqual(row["condition"]["targets"][0]["credential_id"], "id-2")  # 最後に使った ID を残す
        # 変数の値が違えば別の条件
        c = self.hist.record(cond(value={"mode": "relative", "kind": "last_days", "n": 3}))
        self.assertNotEqual(a, c)
        self.assertEqual(self.hist.list()["total"], 2)

    def test_limit_keeps_pinned(self) -> None:
        self.settings.update({"history.max_entries": 10})
        first = self.hist.record(cond("c0"))
        self.hist.pin(first, True)
        ids = [first]
        for k in range(1, 15):
            self.clock.advance(60)
            ids.append(self.hist.record(cond(f"c{k}")))
        items = self.hist.list()["items"]
        got = {i["id"] for i in items}
        self.assertIn(first, got)  # ピン留めは残る
        self.assertEqual(len([i for i in items if not i["pinned"]]), 10)
        self.assertEqual(got - {first}, set(ids[-10:]))  # 古いものから削除
        self.assertTrue(items[0]["pinned"])  # ピン留めは先頭

    def test_pin_limit(self) -> None:
        ids = []
        for k in range(MAX_PINNED + 1):
            self.clock.advance(1)
            ids.append(self.hist.record(cond(f"p{k}")))
        for h in ids[:MAX_PINNED]:
            self.hist.pin(h, True)
        with self.assertRaises(AppError) as cm:
            self.hist.pin(ids[-1], True)
        self.assertEqual(cm.exception.code, "PIN_LIMIT")
        self.hist.pin(ids[0], True)  # もうピン留めしているものは、そのまま
        self.hist.pin(ids[0], False)
        self.assertTrue(self.hist.pin(ids[-1], True)["pinned"])
        menu = self.hist.list(pinned_and_recent=True)["items"]
        self.assertEqual(len([i for i in menu if i["pinned"]]), MAX_PINNED)

    def test_relative_period_recalculated(self) -> None:
        hid = self.hist.record(cond())
        saved = self.hist.get(hid)["condition"]
        self.assertEqual(saved["variables"]["dt"]["value"], {"mode": "relative", "kind": "last_days", "n": 7})

        def matches(now: datetime, name: str) -> bool:
            ctx = CompileContext(now_utc=now, local_zone=dtv.FixedZone(9))
            c = compile_path(saved["path_pattern"], saved["variables"], input_timezone="utc", ctx=ctx)
            return c.match(name) is not None

        self.assertTrue(matches(NOW, "y=2026/m=11/d=14/a.json"))
        self.assertFalse(matches(NOW, "y=2026/m=11/d=13/a.json"))
        later = NOW + timedelta(days=10)  # 呼び出した日を基準に計算し直します
        self.assertFalse(matches(later, "y=2026/m=11/d=14/a.json"))
        self.assertTrue(matches(later, "y=2026/m=11/d=30/a.json"))
        # 期間で入力した場合は、入力した日時のまま
        hid2 = self.hist.record(cond(value={"mode": "period", "start": "2026-11-01", "end": "2026-11-07"}))
        self.assertEqual(self.hist.get(hid2)["condition"]["variables"]["dt"]["value"]["start"], "2026-11-01")

    def test_not_recorded(self) -> None:
        raw = cond(auth="sas")
        raw["targets"][0]["credential"] = {"source": "sas_url", "value": sas_url()}
        raw["targets"][0]["token"] = "eyJhbGciOi.dummy.token"
        raw["targets"][0]["account_key"] = dummy_key()
        raw["save_dir"] = "C:\\example\\out"
        raw["options"] = {"merge_trial_pages": 2, "save_dir": "C:\\example"}
        cleaned = clean_condition(raw)
        hid = self.hist.record(cleaned)
        text = self.db.scalar("SELECT condition_json FROM search_history WHERE id=?", (hid,))
        for bad in (DUMMY_SIG, "eyJhbGciOi", dummy_key(), "save_dir", "C:\\\\example"):
            self.assertNotIn(bad, text)
        self.assertEqual(json.loads(text)["options"], {"merge_trial_pages": 2})

    def test_hash_ignores_credentials(self) -> None:
        a = cond(auth="sas", cid="x")
        b = cond(auth="sas")
        self.assertEqual(condition_hash(a), condition_hash(b))
        self.assertNotEqual(condition_hash(a), condition_hash(cond(auth="account_key")))

    def test_recall_auth(self) -> None:
        """呼び出したときの認証の決め方（3.1.9 の 1〜4）を、検索履歴の条件に当てはめます。"""
        be = MemoryBackend()
        be.add_container("stexample01", "logs")

        class Auth:
            signed_in = False

        auth = Auth()
        svc = TargetService(backend=be, credstore=self.store, clock=self.clock, auth=auth)
        sas_id = self.add_sas("logs")
        # 1. 記録した ID の認証情報が使える
        t = self.hist.get(self.hist.record(cond(auth="sas", cid=sas_id)))["condition"]["targets"][0]
        r = svc.resolve(t)
        self.assertEqual((r["resolved"], r["credential_id"]), (True, sas_id))
        # 2. Entra ID: サインインしていれば使う。していなければ決まらない
        t2 = self.hist.get(self.hist.record(cond("logs2")))["condition"]["targets"][0]
        self.assertFalse(svc.resolve(t2)["resolved"])
        auth.signed_in = True
        self.assertTrue(svc.resolve(t2)["resolved"])
        # 3. 記録した ID がない（削除した）→ 同じアカウントの使えるもの
        key_id = self.add_key()
        self.store.delete([sas_id], confirm=True)
        r = svc.resolve(t)
        self.assertEqual((r["resolved"], r["credential_id"]), (True, key_id))
        # 4. 決まらない
        self.store.delete([key_id], confirm=True)
        r = svc.resolve(t)
        self.assertFalse(r["resolved"])
        self.assertIsNone(r["credential_id"])


class ExchangeTest(Base):
    CTX = CompileContext(now_utc=NOW, local_zone=dtv.FixedZone(9))

    def roundtrip(self, data: dict) -> dict:
        return exchange.parse_import(json.dumps(data).encode("utf-8"), self.CTX)

    def test_includes_levels(self) -> None:
        sas_id = self.add_sas()
        c = cond(auth="sas", cid=sas_id, name="診断ログ")
        only, _ = exchange.export_file([c], "condition", self.store, clock=self.clock)
        self.assertEqual(only["format"], "finfet-abs-search")
        self.assertEqual(only["searches"][0]["targets"], [])
        tg, counts = exchange.export_file([c], "targets", self.store, clock=self.clock)
        self.assertEqual(tg["searches"][0]["targets"][0]["container"], "logs")
        self.assertEqual(counts["included"], 0)
        for d in (only, tg):
            text = json.dumps(d)
            self.assertNotIn(DUMMY_SIG, text)  # 秘密情報を含めない
            self.assertNotIn(sas_id, text)  # ID を含めない
            self.assertNotIn("credential", text)
        full, counts = exchange.export_file([c], "credentials", self.store, clock=self.clock)
        self.assertEqual(counts, {"included": 1, "not_included": 0})
        t = full["searches"][0]["targets"][0]
        self.assertEqual(t["credential"]["source"], "sas_url")
        self.assertIn(DUMMY_SIG, t["credential"]["value"])
        self.assertNotIn(sas_id, json.dumps(full))
        with self.assertRaises(AppError):
            exchange.export_file([c], "everything", self.store)

    def test_credential_counts(self) -> None:
        sas_id = self.add_sas()
        conds = [cond(auth="sas", cid=sas_id), cond(auth="sas", cid="deleted"), cond(auth="entra_user")]
        self.assertEqual(exchange.credential_counts(conds, self.store), {"included": 1, "not_included": 1, "entra": 1})

    def test_format_checks(self) -> None:
        cases = [
            (b"not json", "IMPORT_INVALID"),
            (json.dumps({"format": "other", "schema_version": 1}).encode(), "IMPORT_INVALID"),
            (json.dumps({"format": "finfet-abs-search"}).encode(), "IMPORT_INVALID"),
            (json.dumps({"format": "finfet-abs-search", "schema_version": 99, "searches": []}).encode(), "IMPORT_TOO_NEW"),
            (json.dumps({"format": "finfet-abs-search", "schema_version": 1, "searches": []}).encode(), "IMPORT_EMPTY"),
            (b"{" + b" " * (exchange.MAX_FILE + 1) + b"}", "IMPORT_TOO_LARGE"),
        ]
        for raw, code in cases:
            with self.assertRaises(AppError, msg=code) as cm:
                exchange.parse_import(raw, self.CTX)
            self.assertEqual(cm.exception.code, code)

    def test_unknown_fields_ignored_and_errors_per_item(self) -> None:
        data, _ = exchange.export_file([cond(name="a"), cond("other")], "targets", self.store, clock=self.clock)
        data["future_field"] = {"x": 1}
        data["searches"][0]["future_option"] = True
        data["searches"][0]["targets"][0]["future"] = "x"
        data["searches"].append({"targets": [{"storage_account": "Bad_Name"}], "path_pattern": "x"})
        data["searches"].append({"targets": [], "path_pattern": "a/{{x", "variables": {}})
        parsed = self.roundtrip(data)
        items = parsed["items"]
        self.assertEqual(len(items), 4)
        self.assertNotIn("future_option", items[0]["condition"])
        self.assertNotIn("future", items[0]["condition"]["targets"][0])
        self.assertIsNone(items[0]["error"])
        self.assertIn("ストレージアカウント名", items[2]["error"])
        self.assertTrue(items[3]["error"])  # パスパターンの誤り
        # utf-8 の BOM 付きでも読めます
        exchange.parse_import(b"\xef\xbb\xbf" + json.dumps(data).encode(), self.CTX)

    def test_secretish_values_removed(self) -> None:
        data, _ = exchange.export_file([cond(name="x")], "targets", self.store, clock=self.clock)
        s = data["searches"][0]
        s["name"] = f"メモ {ACC}/logs?sp=r&sig={DUMMY_SIG}"
        s["targets"][0]["container"] = "logs"
        s["targets"][0]["tenant"] = f"AccountKey={dummy_key()}"
        s["targets"][0]["credential_id"] = "should-be-ignored"
        parsed = self.roundtrip(data)
        text = json.dumps(parsed, ensure_ascii=False)
        self.assertNotIn(DUMMY_SIG, text)
        self.assertNotIn(dummy_key(), text)
        self.assertNotIn("should-be-ignored", text)
        self.assertTrue(parsed["warnings"])
        self.assertEqual(parsed["credentials"], 0)

    def test_import_credentials_and_replace(self) -> None:
        sas_id = self.add_sas()
        full, _ = exchange.export_file([cond(auth="sas", cid=sas_id)], "credentials", self.store, clock=self.clock)
        parsed = self.roundtrip(full)
        self.assertEqual(parsed["credentials"], 1)
        preview = exchange.preview_json(parsed)
        self.assertNotIn(DUMMY_SIG, json.dumps(preview))  # 画面には値を返しません
        self.assertEqual(preview["items"][0]["credentials"], 1)
        # 同じ値がすでにある → その ID を使う（確認なし）
        conds, conflicts, errors = exchange.commit_credentials(parsed, self.store, decisions={})
        self.assertEqual((conflicts, errors), ([], []))
        self.assertEqual(conds[0]["targets"][0]["credential_id"], sas_id)
        # 同じ範囲で値が違う → 置き換えるかの確認
        other = sas_url().replace("sp=rl", "sp=rlw")
        full["searches"][0]["targets"][0]["credential"]["value"] = other
        parsed = self.roundtrip(full)
        _conds, conflicts, _ = exchange.commit_credentials(parsed, self.store, decisions={})
        self.assertEqual(len(conflicts), 1)
        key = conflicts[0]["key"]
        self.assertNotIn(DUMMY_SIG, json.dumps(conflicts))
        # 置き換えない → 元の ID
        conds, _, _ = exchange.commit_credentials(parsed, self.store, decisions={key: False})
        self.assertEqual(conds[0]["targets"][0]["credential_id"], sas_id)
        self.assertIn("sp=rl&", self.store.secret(sas_id).reveal())
        # 置き換える → 同じ範囲の行の値が変わる
        conds, _, _ = exchange.commit_credentials(parsed, self.store, decisions={key: True})
        self.assertIn("sp=rlw", self.store.secret(conds[0]["targets"][0]["credential_id"]).reveal())
        self.assertEqual(self.db.scalar("SELECT COUNT(*) FROM credentials"), 1)

    def test_import_new_credential_and_errors(self) -> None:
        data, _ = exchange.export_file([cond(auth="sas"), cond("c2", auth="account_key")], "targets", self.store,
                                       clock=self.clock)
        data["searches"][0]["targets"][0]["credential"] = {"source": "sas_url", "value": sas_url()}
        data["searches"][1]["targets"][0]["credential"] = {"source": "account_key", "value": "not-a-key"}
        parsed = self.roundtrip(data)
        conds, conflicts, errors = exchange.commit_credentials(parsed, self.store, decisions={})
        self.assertEqual(conflicts, [])
        self.assertTrue(conds[0]["targets"][0]["credential_id"])
        self.assertNotIn("credential_id", conds[1]["targets"][0])
        self.assertEqual(len(errors), 1)
        self.assertNotIn("not-a-key", errors[0])

    def test_import_session(self) -> None:
        s = exchange.ImportSession()
        tok = s.put({"a": 1})
        self.assertEqual(s.get(tok), {"a": 1})
        s.drop(tok)
        with self.assertRaises(AppError):
            s.get(tok)


if __name__ == "__main__":
    unittest.main()
