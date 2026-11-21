"""版と起動の引数の確認。"""

import contextlib
import io
import unittest

from finfet_abs import __main__ as entry
from finfet_abs.version import __version__


class VersionTest(unittest.TestCase):
    def test_semver(self) -> None:
        self.assertRegex(__version__, r"^\d+\.\d+\.\d+(?:-[0-9A-Za-z.]+)?$")

    def test_version_option(self) -> None:
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            code = entry.main(["--version"])
        self.assertEqual(code, 0)
        self.assertEqual(buf.getvalue().strip(), f"FinFeT {__version__}")


class CheckRepoTest(unittest.TestCase):
    """リポジトリの検査（tools/check_repo.py）の判定。"""

    def setUp(self) -> None:
        import importlib.util
        from pathlib import Path
        path = Path(__file__).resolve().parent.parent / "tools" / "check_repo.py"
        spec = importlib.util.spec_from_file_location("check_repo", path)
        self.mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.mod)

    def check(self, text: str) -> list[str]:
        return self.mod.check_text("x.txt", text, [], ["pc-test-name"])

    def test_dummy_sig_is_allowed(self) -> None:
        self.assertEqual(self.check("https://stexample01.blob.core.windows.net/c?sv=1&sig=REDACTED_TEST"), [])

    def test_real_like_sig(self) -> None:
        self.assertTrue(self.check("sig=" + "Ab1" * 10 + "%3D"))

    def test_account_key(self) -> None:
        self.assertTrue(self.check("AccountKey=" + "A" * 86 + "=="))

    def test_user_dir_and_words(self) -> None:
        self.assertTrue(self.check("C:" + r"\Users\someone\file"))
        self.assertTrue(self.check("built on PC-TEST-NAME"))

    def test_jwt(self) -> None:
        part = "eyJ" + "a" * 20
        self.assertTrue(self.check(f"{part}.{part}.{'b' * 20}"))
        self.assertFalse(self.check("ordinary text"))


if __name__ == "__main__":
    unittest.main()
