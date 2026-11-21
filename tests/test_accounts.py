"""複数のユーザーアカウントでのサインイン（依頼者の意見による機能。v1.2）。"""

from __future__ import annotations

from datetime import datetime, timezone

from finfet_abs.azure.backend import AuthRef
from finfet_abs.azure.credentials import AuthService, NotSignedIn
from finfet_abs.azure.credstore import CredentialStore
from finfet_abs.azure.targets import TargetService
from finfet_abs.core import db as dbmod
from finfet_abs.core.clock import FakeClock
from finfet_abs.core.db import Database
from finfet_abs.demo.fake_auth import FakeAuthService
from finfet_abs.demo.memory import MemoryBackend
from finfet_abs.history.condition import clean_condition

from .helpers import TempDirMixin

SATO = {"home_account_id": "oid1.tid1", "username": "sato@contoso.com"}
SUZUKI = {"home_account_id": "oid2.tid2", "username": "Suzuki@Fabrikam.com"}


class Shared:
    """MSAL のトークンキャッシュの代わり（テナントの authority をまたいで共有します）。"""

    def __init__(self) -> None:
        self.accounts: list[dict] = []
        self.next_user: dict | None = None
        self.flows: list[dict] = []


class MultiApp:
    def __init__(self, shared: Shared) -> None:
        self.shared = shared

    def initiate_auth_code_flow(self, scopes, redirect_uri=None, **kw):
        n = len(self.shared.flows) + 1
        self.shared.flows.append(kw)
        return {"auth_uri": f"https://login.microsoftonline.com/x/authorize?state=s{n}", "state": f"s{n}"}

    def acquire_token_by_auth_code_flow(self, flow, query):
        u = self.shared.next_user
        if u not in self.shared.accounts:
            self.shared.accounts.append(u)
        return {"access_token": "at", "id_token_claims": {"name": u["username"].split("@")[0], "preferred_username": u["username"],
                                                          "tid": u["home_account_id"].split(".")[1], "oid": u["home_account_id"].split(".")[0]}}

    def get_accounts(self, username=None):
        return [a for a in self.shared.accounts if username is None or a["username"].lower() == username.lower()]

    def acquire_token_silent(self, scopes, account=None):
        return {"access_token": f"at-{account['username'].lower()}", "expires_in": 3600,
                "id_token_claims": {"preferred_username": account["username"]}}

    def remove_account(self, a):
        self.shared.accounts = [x for x in self.shared.accounts if x is not a]


class AccountsTest(TempDirMixin):
    def make(self, shared: Shared | None = None) -> AuthService:
        self.shared = shared or Shared()
        return AuthService(client_id=lambda: "cid", session=lambda: None, cache_path=self.tmp / "cache.bin", port=lambda: 50000,
                           app_factory=lambda cid, authority, cache, http: MultiApp(self.shared), open_browser=lambda u: None)

    def sign_in(self, a: AuthService, user: dict, **kw) -> None:
        self.shared.next_user = user
        a.start_sign_in(None, **kw)
        ok, msg = a.handle_return({"code": "c", "state": f"s{len(self.shared.flows)}"})
        self.assertTrue(ok, msg)

    def test_add_switch_and_sign_out(self) -> None:
        a = self.make()
        self.sign_in(a, SATO)
        self.sign_in(a, SUZUKI, add=True)
        self.assertEqual(self.shared.flows[-1].get("prompt"), "select_account")  # アカウントの選択の画面
        st = a.status()
        self.assertEqual([x["id"] for x in st["accounts"]], ["sato@contoso.com", "suzuki@fabrikam.com"])
        self.assertEqual(st["account"]["id"], "sato@contoso.com")  # 最初のアカウントが既定
        # アカウントを指定したトークン・既定のアカウントのトークン
        self.assertEqual(a.get_token(["s"], account="SUZUKI@fabrikam.com").token, "at-suzuki@fabrikam.com")
        self.assertEqual(a.get_token(["s"]).token, "at-sato@contoso.com")
        self.assertEqual(a.credential(None, "suzuki@fabrikam.com").get_token("s").token, "at-suzuki@fabrikam.com")
        # 既定のアカウントを変える
        a.set_default("suzuki@fabrikam.com")
        self.assertEqual(a.get_token(["s"]).token, "at-suzuki@fabrikam.com")
        self.assertEqual((self.tmp / "msal_default.txt").read_text(encoding="utf-8"), "suzuki@fabrikam.com")
        # 片方だけサインアウト
        a.sign_out("suzuki@fabrikam.com")
        self.assertTrue(a.signed_in)
        self.assertEqual(a.default_account, "sato@contoso.com")
        with self.assertRaises(NotSignedIn) as cm:
            a.get_token(["s"], account="suzuki@fabrikam.com")
        self.assertIn("suzuki@fabrikam.com", cm.exception.info.message)
        self.assertEqual([x["username"] for x in self.shared.accounts], ["sato@contoso.com"])
        # 再サインインはユーザー名を入力済みに
        a.start_sign_in(None, account="suzuki@fabrikam.com")
        self.assertEqual(self.shared.flows[-1].get("login_hint"), "suzuki@fabrikam.com")
        a.sign_out()
        self.assertFalse(a.signed_in)

    def test_restore_all_accounts_and_default(self) -> None:
        shared = Shared()
        shared.accounts = [dict(SATO), dict(SUZUKI)]
        (self.tmp / "msal_default.txt").write_text("suzuki@fabrikam.com", encoding="utf-8")
        a = self.make(shared)
        self.assertTrue(a.restore())
        self.assertEqual([x["id"] for x in a.status()["accounts"]], ["sato@contoso.com", "suzuki@fabrikam.com"])
        self.assertEqual(a.default_account, "suzuki@fabrikam.com")

    def test_migration_adds_account_columns(self) -> None:
        path = self.tmp / "old.db"
        d = Database(path, migrations={1: dbmod.MIGRATIONS[1]})
        d.start()
        d.close()
        d2 = Database(path)
        d2.start()
        try:
            self.assertEqual(d2.schema_version(), dbmod.LATEST_VERSION)
            for table in ("search_targets", "job_items"):
                cols = [r["name"] for r in d2.query(f"PRAGMA table_info({table})")]
                self.assertIn("account", cols, table)
        finally:
            d2.close()


class AccountTargetsTest(TempDirMixin):
    def setUp(self) -> None:
        super().setUp()
        self.db = Database(self.tmp / "c.db")
        self.db.start()
        self.auth = FakeAuthService(lambda: 50000)
        be = MemoryBackend()
        be.add_container("stexample01", "logs")
        self.svc = TargetService(backend=be, credstore=CredentialStore(self.db, FakeClock(datetime(2026, 11, 22, tzinfo=timezone.utc))),
                                 auth=self.auth)

    def tearDown(self) -> None:
        self.db.close()
        super().tearDown()

    def demo_sign_in(self, **kw) -> None:
        r = self.auth.start_sign_in(None, **kw)
        state = r["auth_uri"].split("state=")[1]
        self.assertTrue(self.auth.handle_return({"state": state})[0])

    def test_resolve_and_describe_with_account(self) -> None:
        self.demo_sign_in()
        t = {"storage_account": "stexample01", "endpoint_suffix": "core.windows.net", "container": "logs",
             "auth_kind": "entra_user", "account": "suzuki@fabrikam.com"}
        r = self.svc.resolve(t)
        self.assertFalse(r["resolved"])
        self.assertIn("suzuki@fabrikam.com", r["reason"])
        self.demo_sign_in(add=True)
        r = self.svc.resolve(t)
        self.assertTrue(r["resolved"])
        self.assertEqual(r["account_label"], "suzuki@fabrikam.com")
        # アカウントの指定がない接続先は、既定のアカウント
        r = self.svc.resolve({**t, "account": None})
        self.assertEqual(r["account_label"], "sato@contoso.com")

    def test_add_target_records_account(self) -> None:
        self.demo_sign_in()
        self.demo_sign_in(add=True)
        res = self.svc.add("stexample01", "core.windows.net", {"kind": "entra_user", "account": "Suzuki@Fabrikam.com"}, ["logs"])
        self.assertEqual(res["targets"][0]["account"], "suzuki@fabrikam.com")
        ref, _ = self.svc.auth_ref({"kind": "entra_user", "account": "suzuki@fabrikam.com"})
        self.assertEqual(ref, AuthRef("entra_user", account="suzuki@fabrikam.com"))
        self.auth.sign_out("suzuki@fabrikam.com")
        with self.assertRaises(Exception):
            self.svc.auth_ref({"kind": "entra_user", "account": "suzuki@fabrikam.com"})

    def test_condition_keeps_account_only_with_ids(self) -> None:
        cond = {"targets": [{"storage_account": "stexample01", "container": "logs", "auth_kind": "entra_user",
                             "account": "Suzuki@Fabrikam.com"}], "path_pattern": "a"}
        self.assertEqual(clean_condition(cond)["targets"][0]["account"], "suzuki@fabrikam.com")
        # ファイルに書き出すとき（保存した認証情報の ID を含めないとき）は、アカウントも含めません
        self.assertNotIn("account", clean_condition(cond, keep_credential_ids=False)["targets"][0])

