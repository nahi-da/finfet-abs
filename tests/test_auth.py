"""T-SAS・T-CONN・T-CRED・T-AUTH・T-PROXY（3.1）。"""

import json
import logging
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

from finfet_abs.azure import proxy as proxymod
from finfet_abs.azure.backend import AccountRef, BackendError, BlobInfo
from finfet_abs.azure.connstr import parse_connection_string
from finfet_abs.azure.credentials import AuthService, InteractionRequired
from finfet_abs.azure.credstore import CredentialStore, new_from_input
from finfet_abs.azure.sas import ParseError, parse_sas_url, validate_suffix
from finfet_abs.azure.targets import TargetService
from finfet_abs.core.clock import FakeClock
from finfet_abs.core.db import Database
from finfet_abs.core.errors import AppError
from finfet_abs.core.secret import Secret, redact
from finfet_abs.demo.memory import MemoryBackend

from .helpers import DUMMY_SIG, TempDirMixin, dummy_key

ACC = "https://stexample01.blob.core.windows.net"


class SasTest(unittest.TestCase):
    def test_container_sas(self) -> None:
        u = parse_sas_url(f"{ACC}/logs?sv=2024-11-04&sr=c&sp=rl&st=2026-11-01T00:00:00Z&se=2026-11-30T09:00:00Z&sig={DUMMY_SIG}")
        self.assertEqual((u.account, u.suffix, u.container, u.blob, u.level), ("stexample01", "core.windows.net", "logs", None, "container"))
        self.assertEqual(u.info.kind, "service")
        self.assertEqual(u.info.scope, "container")
        self.assertTrue(u.info.allows("list_blobs"))
        self.assertTrue(u.info.allows("read"))
        self.assertFalse(u.info.allows("set_tier"))
        self.assertFalse(u.info.allows("list_containers"))
        self.assertEqual(u.info.expiry, datetime(2026, 11, 30, 9, tzinfo=timezone.utc))
        self.assertNotIn(DUMMY_SIG, json.dumps(u.public_json()))

    def test_account_and_user_delegation(self) -> None:
        u = parse_sas_url(f"{ACC}/?sv=2024&ss=b&srt=sco&sp=rlw&se=2026-12-01&sig={DUMMY_SIG}")
        self.assertEqual((u.info.kind, u.level), ("account", "service"))
        for op in ("list_blobs", "list_containers", "read", "set_tier"):
            self.assertTrue(u.info.allows(op), op)
        u = parse_sas_url(f"{ACC}/?sv=2024&ss=b&srt=s&sp=l&se=2026-12-01&sig={DUMMY_SIG}")
        self.assertFalse(u.info.allows("list_blobs"))
        self.assertTrue(u.info.allows("list_containers"))
        self.assertFalse(u.info.allows("read"))
        u = parse_sas_url(f"{ACC}/logs?sv=2024&sr=c&sp=rl&skoid=x&sktid=y&se=2026-12-01&sig={DUMMY_SIG}")
        self.assertEqual(u.info.kind, "user_delegation")
        with self.assertRaises(ParseError):
            parse_sas_url(f"{ACC}/?sv=2024&ss=q&srt=sco&sp=rl&sig={DUMMY_SIG}")

    def test_policy_ip_and_blob(self) -> None:
        u = parse_sas_url(f"{ACC}/logs?sv=2024&sr=c&si=policy1&sip=10.0.0.1&spr=https&sig={DUMMY_SIG}")
        self.assertIsNone(u.info.expiry)
        self.assertIsNone(u.info.allows("list_blobs"))
        self.assertTrue(u.info.to_json()["policy"])
        self.assertTrue(u.info.to_json()["ip_restricted"])
        u = parse_sas_url(f"{ACC}/%E3%83%AD%E3%82%B0/%E5%A0%B1%E5%91%8A%20%231.pdf?sv=2024&sr=b&sp=r&sig={DUMMY_SIG}")
        self.assertEqual((u.container, u.blob, u.level), ("ログ", "報告 #1.pdf", "blob"))

    def test_rejects(self) -> None:
        for bad in (f"http://stexample01.blob.core.windows.net/c?sr=c&sig={DUMMY_SIG}",
                    f"https://files.contoso.com/c?sr=c&sig={DUMMY_SIG}",
                    f"{ACC}/c?sv=2024&sr=c&sp=rl",
                    f"https://stexample01.blob.core.windows.net:8443/c?sr=c&sig={DUMMY_SIG}",
                    f"https://ST_BAD.blob.core.windows.net/c?sr=c&sig={DUMMY_SIG}"):
            with self.assertRaises(ParseError, msg=bad):
                parse_sas_url(bad)
        u = parse_sas_url(f"https://stexample01.dfs.core.windows.net/c?sv=2024&sr=c&sp=rl&sig={DUMMY_SIG}")
        self.assertTrue(u.from_dfs)
        u = parse_sas_url(f"https://stexample01.blob.core.chinacloudapi.cn/c?sv=2024&sr=c&sp=rl&sig={DUMMY_SIG}")
        self.assertEqual(u.suffix, "core.chinacloudapi.cn")

    def test_expiry_warnings(self) -> None:
        u = parse_sas_url(f"{ACC}/logs?sv=2024&sr=c&sp=rl&se=2026-11-20T10:00:00Z&sig={DUMMY_SIG}")
        now = datetime(2026, 11, 20, 1, tzinfo=timezone.utc)
        self.assertFalse(u.info.expired(now))
        self.assertTrue(u.info.expires_within(now, 24))
        self.assertTrue(u.info.expired(now + timedelta(hours=10)))


class ConnTest(unittest.TestCase):
    def test_account_key(self) -> None:
        key = dummy_key()
        info = parse_connection_string(f"DefaultEndpointsProtocol=https;AccountName=stexample01;AccountKey={key};EndpointSuffix=core.windows.net")
        self.assertEqual((info.kind, info.account, info.suffix), ("account_key", "stexample01", "core.windows.net"))
        self.assertNotIn(key, json.dumps(info.public_json()))

    def test_sas_and_endpoint(self) -> None:
        info = parse_connection_string(f"BlobEndpoint=https://stexample02.blob.core.chinacloudapi.cn/;SharedAccessSignature=sv=2024&ss=b&srt=sco&sp=rl&se=2026-12-01&sig={DUMMY_SIG}")
        self.assertEqual((info.kind, info.account, info.suffix), ("sas", "stexample02", "core.chinacloudapi.cn"))
        self.assertEqual(info.sas.kind, "account")
        info = parse_connection_string(f"accountname=stexample03;accountkey={dummy_key()}")
        self.assertEqual(info.suffix, "core.windows.net")

    def test_rejects(self) -> None:
        for bad in (f"DefaultEndpointsProtocol=http;AccountName=stexample01;AccountKey={dummy_key()}",
                    f"BlobEndpoint=http://stexample01.blob.core.windows.net/;AccountKey={dummy_key()}",
                    "AccountName=stexample01",
                    "AccountName=stexample01;AccountKey=not base64!!",
                    f"AccountKey={dummy_key()}",
                    "UseDevelopmentStorage=true",
                    f"AccountName=stexample01;AccountKey={dummy_key()};EndpointSuffix=https://x.example/path",
                    "garbage"):
            with self.assertRaises(ParseError, msg=bad):
                parse_connection_string(bad)
        self.assertEqual(parse_connection_string("UseDevelopmentStorage=true", allow_development=True).kind, "development")

    def test_domain_other(self) -> None:
        self.assertEqual(validate_suffix("Core.ChinaCloudApi.cn"), "core.chinacloudapi.cn")
        for bad in ("https://core.windows.net", "core.windows.net/x", "core.windows.net:443", "localhost", "", "a..b"):
            with self.assertRaises(ParseError, msg=bad):
                validate_suffix(bad)


class CredTest(TempDirMixin):
    def setUp(self) -> None:
        super().setUp()
        self.db = Database(self.tmp / "c.db")
        self.db.start()
        self.clock = FakeClock(datetime(2026, 11, 20, tzinfo=timezone.utc))
        self.store = CredentialStore(self.db, self.clock)
        self.be = MemoryBackend()
        self.be.add_container("stexample01", "logs")
        self.be.add_container("stexample01", "other")
        self.svc = TargetService(backend=self.be, credstore=self.store, clock=self.clock)

    def tearDown(self) -> None:
        self.db.close()
        super().tearDown()

    def add_key(self, replace=False):
        c, _ = new_from_input("account_key", account="stexample01", key=dummy_key())
        return self.store.add(c, replace=replace)

    def test_add_and_replace(self) -> None:
        cid, replaced = self.add_key()
        self.assertFalse(replaced)
        with self.assertRaises(AppError) as cm:
            self.add_key()
        self.assertEqual(cm.exception.code, "CREDENTIAL_EXISTS")
        cid2, replaced = self.add_key(replace=True)
        self.assertEqual((cid2, replaced), (cid, True))
        raw = self.db.scalar("SELECT secret FROM credentials WHERE id=?", (cid,))
        self.assertNotIn(dummy_key(), raw)
        self.assertEqual(self.store.secret(cid).reveal(), dummy_key())
        self.assertNotIn(dummy_key(), json.dumps(self.store.list()))

    def test_auth_error_not_saved(self) -> None:
        self.be.denied[("stexample01", "logs", "list")] = "AuthenticationFailed"
        url = f"{ACC}/logs?sv=2024&sr=c&sp=rl&se=2027-01-01&sig={DUMMY_SIG}"
        with self.assertRaises(AppError) as cm:
            self.svc.list_containers("stexample01", "core.windows.net", {"input": {"source": "sas_url", "value": url}})
        self.assertEqual(cm.exception.code, "AuthenticationFailed")
        self.assertEqual(self.db.scalar("SELECT COUNT(*) FROM credentials"), 0)
        # 権限が足りないだけなら、名前を入力して追加でき、保存します
        del self.be.denied[("stexample01", "logs", "list")]
        self.be.denied[("stexample01", "", "list")] = "AuthorizationPermissionMismatch"
        res = self.svc.add("stexample01", "core.windows.net", {"input": {"source": "sas_url", "value": url}}, ["logs"])
        self.assertEqual(res["targets"][0]["container"], "logs")
        self.assertEqual(self.db.scalar("SELECT COUNT(*) FROM credentials"), 1)
        # 期限切れの SAS は保存しません
        old = f"{ACC}/other?sv=2024&sr=c&sp=rl&se=2026-01-01&sig={DUMMY_SIG}"
        with self.assertRaises(AppError):
            self.svc.add("stexample01", "core.windows.net", {"input": {"source": "sas_url", "value": old}}, ["other"])

    def test_resolve_rules(self) -> None:
        acc_id, _ = self.add_key()
        sas_c, _ = new_from_input("sas_url", value=f"{ACC}/logs?sv=2024&sr=c&sp=rl&se=2027-01-01&sig={DUMMY_SIG}")
        sas_id, _ = self.store.add(sas_c)
        t = {"storage_account": "stexample01", "endpoint_suffix": "core.windows.net", "container": "logs", "auth_kind": "sas",
             "credential_id": "deleted-id"}
        r = self.svc.resolve(t)
        self.assertEqual((r["resolved"], r["credential_id"]), (True, sas_id))  # 範囲がコンテナーのものを優先
        r = self.svc.resolve({**t, "container": "other"})
        self.assertEqual(r["credential_id"], acc_id)
        r = self.svc.resolve({**t, "endpoint_suffix": "core.chinacloudapi.cn"})
        self.assertFalse(r["resolved"])
        r = self.svc.resolve({**t, "credential_id": acc_id, "container": "logs"})
        self.assertEqual(r["credential_id"], acc_id)  # 記録した ID が使えればそれを使います

    def test_delete_with_usage(self) -> None:
        cid, _ = self.add_key()
        self.db.execute("INSERT INTO jobs(id, name, source, status, options_json) VALUES('j1','診断ログ','search','waiting_rehydrate','{}')")
        self.db.execute("INSERT INTO job_items(job_id, storage_account, container, blob_name, blob_name_lower, sort_key, status, credential_id) "
                        "VALUES('j1','stexample01','logs','a','a',x'00','rehydrating',?)", (cid,))
        self.db.execute("INSERT INTO search_history(id, condition_json, condition_hash) VALUES('h1', ?, 'x')",
                        (json.dumps({"targets": [{"credential_id": cid}]}),))
        res = self.store.delete([cid])
        self.assertTrue(res["confirm_required"])
        self.assertEqual(res["jobs"], [{"id": "j1", "name": "診断ログ"}])
        self.assertEqual(res["history_count"], 1)
        self.assertIsNotNone(self.store.get(cid))
        res = self.store.delete([cid], confirm=True)
        self.assertEqual(res["deleted"], 1)
        # 削除した後は、3.1.9 の規則で探し直します（ほかになければ決まりません）
        r = self.svc.resolve({"storage_account": "stexample01", "endpoint_suffix": "core.windows.net", "container": "logs",
                              "auth_kind": "account_key", "credential_id": cid})
        self.assertFalse(r["resolved"])


class FakeApp:
    """MSAL の PublicClientApplication の架空の実装。"""

    def __init__(self, authority, token_cache) -> None:
        self.authority = authority
        self.accounts = []
        self.silent_result = None
        self.flows = 0

    def initiate_auth_code_flow(self, scopes, redirect_uri=None, login_hint=None):
        self.flows += 1
        return {"auth_uri": f"https://login.microsoftonline.com/x/authorize?state=s{self.flows}", "state": f"s{self.flows}",
                "redirect_uri": redirect_uri}

    def acquire_token_by_auth_code_flow(self, flow, query):
        if query.get("state") != flow["state"]:
            return {"error": "state_mismatch"}
        self.accounts = [{"home_account_id": "oid.tid", "username": "sato@contoso.com"}]
        return {"access_token": "at", "id_token_claims": {"name": "佐藤", "preferred_username": "sato@contoso.com",
                                                          "tid": "00000000-0000-0000-0000-000000000001", "oid": "oid"}}

    def get_accounts(self):
        return self.accounts

    def acquire_token_silent(self, scopes, account=None):
        return self.silent_result

    def remove_account(self, a):
        self.accounts = []


class AuthTest(TempDirMixin):
    def make(self):
        self.apps = {}
        self.opened = []
        self.t = [1000.0]

        def factory(client_id, authority, token_cache, http_client):
            app = self.apps.get(authority) or FakeApp(authority, token_cache)
            self.apps[authority] = app
            return app

        return AuthService(client_id=lambda: "cid", session=lambda: None, cache_path=self.tmp / "cache.bin", port=lambda: 50000,
                           app_factory=factory, open_browser=self.opened.append, monotonic=lambda: self.t[0])

    def test_state_handling(self) -> None:
        a = self.make()
        r = a.start_sign_in()
        self.assertEqual(len(self.opened), 1)
        self.assertEqual(r["auth_uri"], self.opened[0])
        ok, msg = a.handle_return({"code": "c", "state": "wrong"})
        self.assertFalse(ok)
        self.assertIn("取り消された", msg)
        ok, _ = a.handle_return({"code": "c", "state": "s1"})
        self.assertTrue(ok)
        self.assertTrue(a.signed_in)
        self.assertEqual(a.account_info()["username"], "sato@contoso.com")
        # 同じ state の2回目は無視します
        ok, _ = a.handle_return({"code": "c", "state": "s1"})
        self.assertFalse(ok)

    def test_expiry_and_cancel(self) -> None:
        a = self.make()
        a.start_sign_in()
        self.t[0] += 601
        ok, msg = a.handle_return({"code": "c", "state": "s1"})
        self.assertFalse(ok)
        r = a.start_sign_in()
        a.cancel(r["flow_id"])
        ok, msg = a.handle_return({"code": "c", "state": "s2"})
        self.assertFalse(ok)
        self.assertIn("取り消されたか、期限が切れています", msg)

    def test_interaction_required_does_not_open_browser(self) -> None:
        a = self.make()
        a.start_sign_in()
        a.handle_return({"code": "c", "state": "s1"})
        n = len(self.opened)
        tid = "00000000-0000-0000-0000-000000000002"
        app = FakeApp("x", None)
        app.accounts = [{"home_account_id": "oid.tid", "username": "sato@contoso.com"}]
        app.silent_result = {"error": "invalid_grant", "error_codes": [50076]}
        self.apps[f"https://login.microsoftonline.com/{tid}"] = app
        with self.assertRaises(InteractionRequired) as cm:
            a.get_token(["https://storage.azure.com/.default"], tid)
        self.assertEqual(cm.exception.tenant_id, tid)
        self.assertIn("多要素認証", cm.exception.info.message)
        self.assertEqual(len(self.opened), n)
        self.assertEqual(a.tenant_status[tid]["status"], "mfa")
        app.silent_result = {"error": "x", "error_codes": [53003]}
        with self.assertRaises(InteractionRequired):
            a.get_token(["s"], tid)
        self.assertEqual(a.tenant_status[tid]["status"], "ca_blocked")
        app.silent_result = {"access_token": "ok", "expires_in": 3600}
        self.assertEqual(a.get_token(["s"], tid).token, "ok")
        self.assertEqual(len(self.opened), n)

    def test_restore_and_sign_out(self) -> None:
        a = self.make()
        org = "https://login.microsoftonline.com/organizations"
        a._app(None)
        self.apps[org].accounts = [{"home_account_id": "oid.tid", "username": "sato@contoso.com"}]
        self.apps[org].silent_result = {"access_token": "at", "id_token_claims": {"preferred_username": "sato@contoso.com"}}
        self.assertTrue(a.restore())
        self.assertTrue(a.signed_in)
        self.assertEqual(self.opened, [])
        a.sign_out()
        self.assertFalse(a.signed_in)


class AuthNetworkTest(AuthTest):
    def test_restore_with_network_error(self) -> None:
        a = self.make()
        org = "https://login.microsoftonline.com/organizations"
        a._app(None)
        app = self.apps[org]
        app.accounts = [{"home_account_id": "oid.tid", "username": "sato@contoso.com"}]

        def boom(*args, **kw):
            raise OSError("proxy down")
        app.acquire_token_silent = boom
        # 通信できなくても、キャッシュにアカウントがあればサインイン済みとして扱います
        self.assertTrue(a.restore())
        self.assertTrue(a.signed_in)
        with self.assertRaises(OSError):
            a.get_token(["https://storage.azure.com/.default"])


class ProxyTest(TempDirMixin):
    def cfg(self, **kw):
        kw.setdefault("env", {})
        kw.setdefault("registry", lambda: ({}, lambda h: False))
        return proxymod.ProxyConfig(**kw)

    def test_modes(self) -> None:
        u = "https://stexample01.blob.core.windows.net/x"
        self.assertIsNone(proxymod.resolve_proxy(self.cfg(mode="none", env={"HTTPS_PROXY": "http://p:1"}), u))
        self.assertEqual(proxymod.resolve_proxy(self.cfg(mode="manual", url="http://proxy.example:8080"), u), "http://proxy.example:8080")
        c = self.cfg(mode="manual", url="http://proxy.example:8080", bypass="*.blob.core.windows.net, 10.0.0.0/8")
        self.assertIsNone(proxymod.resolve_proxy(c, u))
        self.assertIsNone(proxymod.resolve_proxy(c, "https://10.1.2.3/"))
        self.assertEqual(proxymod.resolve_proxy(c, "https://login.microsoftonline.com/"), "http://proxy.example:8080")

    def test_env_and_registry(self) -> None:
        u = "https://management.azure.com/"
        self.assertEqual(proxymod.resolve_proxy(self.cfg(env={"HTTPS_PROXY": "http://up:1"}), u), "http://up:1")
        self.assertEqual(proxymod.resolve_proxy(self.cfg(env={"https_proxy": "http://low:1", "HTTPS_PROXY": "http://up:1"}), u),
                         "http://low:1")
        self.assertEqual(proxymod.resolve_proxy(self.cfg(env={"http_proxy": "http://h:1"}), u), "http://h:1")
        self.assertIsNone(proxymod.resolve_proxy(self.cfg(env={"HTTPS_PROXY": "http://up:1", "NO_PROXY": "azure.com"}), u))
        reg = lambda: ({"https": "http://reg:8080", "http": "http://reg:8080"}, lambda h: h.endswith(".contoso.com"))  # noqa: E731
        self.assertEqual(proxymod.resolve_proxy(self.cfg(registry=reg), u), "http://reg:8080")
        self.assertIsNone(proxymod.resolve_proxy(self.cfg(registry=reg), "https://x.contoso.com/"))
        self.assertIsNone(proxymod.resolve_proxy(self.cfg(), u))

    def test_localhost_always_direct(self) -> None:
        for mode in ("auto", "manual", "none"):
            c = self.cfg(mode=mode, url="http://p:1", env={"HTTP_PROXY": "http://p:1", "HTTPS_PROXY": "http://p:1"})
            for h in ("http://127.0.0.1:50000/api", "http://localhost:50000/", "http://[::1]:5/"):
                self.assertIsNone(proxymod.resolve_proxy(c, h), (mode, h))

    def test_basic_auth_not_logged(self) -> None:
        c = self.cfg(mode="manual", url="http://proxy.example:8080", username="user1", password=Secret("P@ss:word/1"))
        url = proxymod.resolve_proxy(c, "https://management.azure.com/")
        self.assertEqual(url, "http://user1:P%40ss%3Aword%2F1@proxy.example:8080")
        self.assertNotIn("P%40ss", redact(f"connect via {url}"))
        self.assertEqual(proxymod.proxy_label(url), "プロキシ proxy.example:8080")
        logs_dir = self.tmp / "logs"
        from finfet_abs.core import logs
        logs.setup(logs_dir, "DEBUG")
        try:
            logging.getLogger("finfet_abs.test").info("proxy %s", url)
        finally:
            logs.setup(None)
        text = "".join(p.read_text(encoding="utf-8") for p in logs_dir.glob("*.log"))
        self.assertNotIn("P%40ss", text)
        self.assertNotIn("P@ss", text)

    def test_ca_bundle(self) -> None:
        import certifi
        self.assertEqual(proxymod.build_ca_bundle("", self.tmp), certifi.where())
        pem = self.tmp / "extra.pem"
        pem.write_text("-----BEGIN CERTIFICATE-----\nMIIBdummy\n-----END CERTIFICATE-----\n", encoding="ascii")
        out = proxymod.build_ca_bundle(str(pem), self.tmp)
        text = Path(out).read_text(encoding="ascii")
        self.assertIn("MIIBdummy", text)
        self.assertGreater(len(text), len("MIIBdummy") + 1000)
        bad = self.tmp / "bad.pem"
        bad.write_text("nope", encoding="ascii")
        with self.assertRaises(ValueError):
            proxymod.build_ca_bundle(str(bad), self.tmp)

    def test_session_uses_config(self) -> None:
        c = self.cfg(mode="manual", url="http://proxy.example:8080")
        s = proxymod.ProxySession(c, True)
        with mock.patch.dict("os.environ", {"REQUESTS_CA_BUNDLE": "x.pem", "HTTPS_PROXY": "http://env:1"}):
            m = s.merge_environment_settings("https://management.azure.com/", {}, False, None, None)
        self.assertEqual(m["proxies"], {"http": "http://proxy.example:8080", "https": "http://proxy.example:8080"})
        self.assertTrue(m["verify"])
        m = s.merge_environment_settings("http://127.0.0.1:5/", {}, False, None, None)
        self.assertEqual(m["proxies"], {})


if __name__ == "__main__":
    unittest.main()
