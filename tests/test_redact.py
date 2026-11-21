"""T-RED: 秘密情報がログ・例外・API の応答に出ないこと（3.6.2）。"""

import json
import logging
import unittest

from finfet_abs.core import logs
from finfet_abs.core.secret import Secret, redact

from .helpers import DUMMY_SIG, Client, TempDirMixin, dummy_jwt, dummy_key, make_app, quiet_logs


#: 署名の形のダミー（ファイルに sig= の後ろの値として書かないよう、組み立てて使います）
FAKE_SIG = "AbCdEf" + "0123456789"


def secrets_list() -> list[str]:
    return [dummy_key(), dummy_jwt(), "SuperSecretPassw0rd", "AuthCodeValue123456", "StateValue98765432"]


def samples() -> list[str]:
    key = dummy_key()
    jwt = dummy_jwt()
    return [
        f"https://stexample01.blob.core.windows.net/logs?sv=2024-01-01&sr=c&sp=rl&sig={DUMMY_SIG}x&se=2026",
        "https://stexample01.blob.core.windows.net/?sv=2024&ss=b&srt=sco&sp=rl&sig=" + FAKE_SIG + "xyz%3D",
        f"DefaultEndpointsProtocol=https;AccountName=stexample01;AccountKey={key};EndpointSuffix=core.windows.net",
        "BlobEndpoint=https://stexample01.blob.core.windows.net/;SharedAccessSignature=sv=2024&sig=" + FAKE_SIG,
        "Endpoint=sb://x/;SharedAccessKeyName=a;SharedAccessKey=SuperSecretPassw0rd",
        "client_secret=SuperSecretPassw0rd&grant_type=x",
        "GET /?code=AuthCodeValue123456&state=StateValue98765432",
        f"Authorization: Bearer {jwt}",
        f"token {jwt}",
        "proxy http://user:SuperSecretPassw0rd@proxy.example:8080",
        "https://login.microsoftonline.com/organizations/oauth2/v2.0/authorize?client_id=x&code_challenge=AuthCodeValue123456",
    ]


class RedactTest(TempDirMixin):
    def test_patterns(self) -> None:
        for s in samples():
            out = redact(s)
            for secret in secrets_list() + [FAKE_SIG, DUMMY_SIG + "x"]:
                self.assertNotIn(secret, out, s)
        # 秘密情報でない部分は残ります
        out = redact("https://stexample01.blob.core.windows.net/logs/a.json?sv=1&sig=abc")
        self.assertIn("stexample01.blob.core.windows.net/logs/a.json", out)

    def test_registered_secret_value(self) -> None:
        s = Secret("PlainLookingValue-XYZ-12345")
        self.assertEqual(str(s), "***")
        self.assertEqual(repr(s), "Secret(***)")
        self.assertEqual(f"{s}", "***")
        self.assertNotIn("PlainLookingValue", redact("value is PlainLookingValue-XYZ-12345 here"))

    def test_log_file(self) -> None:
        logs.setup(self.tmp / "logs", "DEBUG")
        try:
            log = logging.getLogger("finfet_abs.test")
            for s in samples():
                log.info("message %s", s)
            try:
                raise ValueError(f"failed with {samples()[2]} and {samples()[7]}")
            except ValueError:
                log.exception("error occurred")
        finally:
            logs.setup(None)
        text = "".join(p.read_text(encoding="utf-8") for p in (self.tmp / "logs").glob("*.log"))
        self.assertIn("message", text)
        self.assertIn("ValueError", text)
        for secret in secrets_list() + [FAKE_SIG]:
            self.assertNotIn(secret, text)

    def test_job_log(self) -> None:
        jl = logs.JobLog(self.tmp / "jobs")
        jl.write("job1", logging.INFO, f"stexample01/logs/a.json {samples()[0]} {samples()[2]}")
        text = (self.tmp / "jobs" / "job1.log").read_text(encoding="utf-8")
        self.assertIn("stexample01/logs/a.json", text)
        self.assertNotIn(dummy_key(), text)
        self.assertNotIn(DUMMY_SIG + "x", text)


class ApiRedactTest(TempDirMixin):
    def test_settings_api_hides_proxy_password(self) -> None:
        quiet_logs()
        app = make_app(self.tmp)
        try:
            c = Client(app.build_wsgi())
            c.login(app.security)
            r = c.put("/api/v1/settings", {"changes": {"network.proxy.password": "SuperSecretPassw0rd",
                                                       "network.proxy.mode": "manual"}})
            self.assertEqual(r.status, 200)
            self.assertNotIn(b"SuperSecretPassw0rd", r.body)
            r = c.get("/api/v1/settings")
            self.assertNotIn(b"SuperSecretPassw0rd", r.body)
            self.assertTrue(r.json()["values"]["network.proxy.password_set"])
            raw = app.location.settings_path.read_text(encoding="utf-8")
            self.assertNotIn("SuperSecretPassw0rd", raw)
            self.assertIn("dpapi-user", raw)
            self.assertEqual(app.settings.get_secret("network.proxy.password").reveal(), "SuperSecretPassw0rd")
            session = json.dumps(c.get("/api/v1/session").json())
            self.assertNotIn("SuperSecretPassw0rd", session)
        finally:
            app.stop()


if __name__ == "__main__":
    unittest.main()
