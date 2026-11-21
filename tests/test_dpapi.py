"""T-DPAPI: DPAPI による暗号化と、復号できない場合の原因の判定（5.6）。"""

import base64
import json
import unittest
from unittest import mock

from finfet_abs.core import dpapi
from finfet_abs.core.secret import Secret


class DpapiTest(unittest.TestCase):
    def test_round_trip(self) -> None:
        text = dpapi.seal(Secret("値-秘密-①"), "credential")
        obj = json.loads(text)
        self.assertEqual(obj["v"], 1)
        self.assertEqual(obj["alg"], "dpapi-user")
        self.assertNotIn("値-秘密", text)
        self.assertEqual(set(obj["env"]), {"sid", "user", "computer", "at"})
        self.assertTrue(obj["env"]["sid"].startswith("S-1-"))
        self.assertEqual(dpapi.unseal(text, "credential").reveal(), "値-秘密-①")

    def test_entropy_differs(self) -> None:
        text = dpapi.seal("abc-secret-value", "credential")
        with self.assertRaises(dpapi.DecryptError):
            dpapi.unseal(text, "job-secret")
        with self.assertRaises(dpapi.DecryptError):
            dpapi.unseal(text, "proxy")

    def _broken(self, **env) -> str:
        text = dpapi.seal("abc-secret-value", "credential")
        obj = json.loads(text)
        obj["data"] = base64.b64encode(b"broken-data").decode()
        obj["env"].update(env)
        return json.dumps(obj)

    def test_diagnose_other_user(self) -> None:
        text = self._broken(sid="S-1-5-21-0-0-0-1001", user="CONTOSO\\sato")
        with self.assertRaises(dpapi.DecryptError) as cm:
            dpapi.unseal(text, "credential")
        self.assertEqual(cm.exception.reason, 1)
        self.assertIn("CONTOSO\\sato", cm.exception.message)
        self.assertIsNotNone(cm.exception.winerror)

    def test_diagnose_other_pc(self) -> None:
        text = self._broken(computer="PC-0123")
        with self.assertRaises(dpapi.DecryptError) as cm:
            dpapi.unseal(text, "credential")
        self.assertEqual(cm.exception.reason, 2)
        self.assertIn("PC-0123", cm.exception.message)

    def test_diagnose_same_env(self) -> None:
        with self.assertRaises(dpapi.DecryptError) as cm:
            dpapi.unseal(self._broken(), "credential")
        self.assertEqual(cm.exception.reason, 3)

    def test_diagnose_with_swapped_current_env(self) -> None:
        text = dpapi.seal("abc-secret-value", "credential")
        obj = json.loads(text)
        obj["data"] = base64.b64encode(b"x").decode()
        fake = dpapi.Environment(sid="S-1-5-21-9-9-9-500", user="X\\y", computer=obj["env"]["computer"])
        with mock.patch.object(dpapi, "current_environment", return_value=fake):
            with self.assertRaises(dpapi.DecryptError) as cm:
                dpapi.unseal(json.dumps(obj), "credential")
        self.assertEqual(cm.exception.reason, 1)
        fake = dpapi.Environment(sid=obj["env"]["sid"], user="X\\y", computer="OTHER-PC")
        with mock.patch.object(dpapi, "current_environment", return_value=fake):
            with self.assertRaises(dpapi.DecryptError) as cm:
                dpapi.unseal(json.dumps(obj), "credential")
        self.assertEqual(cm.exception.reason, 2)

    def test_invalid_format(self) -> None:
        with self.assertRaises(dpapi.DecryptError) as cm:
            dpapi.unseal("not json", "credential")
        self.assertEqual(cm.exception.reason, 3)


if __name__ == "__main__":
    unittest.main()
