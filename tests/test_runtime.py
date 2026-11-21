"""全部入り版の実行環境の版の確認（6.4）。"""

from __future__ import annotations

import json
import unittest

from finfet_abs.core import runtime

from .helpers import TempDirMixin


class RuntimeCheckTest(TempDirMixin, unittest.TestCase):
    def write(self, data) -> None:
        (self.tmp / "installed.json").write_text(data if isinstance(data, str) else json.dumps(data), encoding="utf-8")

    def check(self, **kw):
        return runtime.check(self.tmp / "installed.json", version="1.2.3", python="3.14.10", **kw)

    def test_no_runtime_is_dev(self) -> None:
        self.assertIsNone(self.check())

    def test_match(self) -> None:
        self.write({"app_version": "1.2.3", "python": {"version": "3.14.10"}})
        self.assertIsNone(self.check())

    def test_mismatch(self) -> None:
        for data in ({"app_version": "1.2.2", "python": {"version": "3.14.10"}},
                     {"app_version": "1.2.3", "python": {"version": "3.14.9"}}, "{broken"):
            self.write(data)
            self.assertEqual(self.check(), runtime.MISMATCH)


if __name__ == "__main__":
    unittest.main()
