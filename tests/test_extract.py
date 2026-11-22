"""ダウンロードした圧縮ファイルの展開（v1.3.0）: 種類、入れ子、名前、安全（パスの外・リンク・大きさ）。"""

from __future__ import annotations

import bz2
import gzip
import io
import lzma
import os
import tarfile
import threading
import zipfile
from pathlib import Path

from unittest import mock

from finfet_abs.demo import memory
from finfet_abs.jobs import extract
from finfet_abs.jobs.service import clean_options

from .helpers import TempDirMixin
from .test_jobs import JobTestBase, blob

GIB = 1024 ** 3


def tar_bytes(files: dict[str, bytes], mode: str = "w") -> bytes:
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode=mode) as tf:
        for name, data in files.items():
            ti = tarfile.TarInfo(name)
            ti.size = len(data)
            tf.addfile(ti, io.BytesIO(data))
    return buf.getvalue()


class ExtractTest(TempDirMixin):
    def write(self, name: str, data: bytes) -> str:
        p = self.tmp / name
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(data)
        return str(p)

    def tree(self, root) -> dict[str, bytes]:
        out = {}
        for d, _dirs, files in os.walk(root):
            for f in files:
                p = os.path.join(d, f)
                out[os.path.relpath(p, root).replace("\\", "/")] = Path(p).read_bytes()
        return out

    def test_kind(self) -> None:
        self.assertEqual(extract.archive_kind("logs.tar.gz"), ("tar", "logs"))
        self.assertEqual(extract.archive_kind("a.TGZ"), ("tar", "a"))
        self.assertEqual(extract.archive_kind("app.log.gz"), ("gz", "app.log"))
        self.assertEqual(extract.archive_kind("x.zip"), ("zip", "x"))
        self.assertEqual(extract.archive_kind("d.tar.xz"), ("tar", "d"))
        self.assertEqual(extract.archive_kind("d.bz2"), ("bz2", "d"))
        self.assertIsNone(extract.archive_kind("app.log"))
        self.assertIsNone(extract.archive_kind(".zip"))

    def test_not_archive_returns_none(self) -> None:
        p = self.write("app.log", b"hello")
        self.assertIsNone(extract.extract(p, max_bytes=GIB))

    def test_zip_with_folders_and_japanese_names(self) -> None:
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as zf:
            zf.writestr("dir/a.txt", b"A")
            zf.writestr("監査/b.txt", b"B")  # UTF-8 の印付き
            zf.writestr("QQQQ.txt", b"C")
        # Shift_JIS の名前（UTF-8 の印なし）。日本の Windows で作った ZIP の形に、名前のバイトを置き換えます
        raw = buf.getvalue().replace(b"QQQQ.txt", "報告.txt".encode("cp932"))
        p = self.write("in/logs.zip", raw)
        res = extract.extract(p, max_bytes=GIB)
        self.assertEqual(res.files, 3)
        self.assertEqual(res.dest, str(self.tmp / "in" / "logs"))
        self.assertEqual(self.tree(res.dest), {"dir/a.txt": b"A", "監査/b.txt": b"B", "報告.txt": b"C"})
        self.assertTrue(os.path.exists(p), "元のファイルは残します")
        # もう一度展開すると、番号を付けた名前にします
        res2 = extract.extract(p, max_bytes=GIB)
        self.assertEqual(os.path.basename(res2.dest), "logs(2)")

    def test_single_gz_bz2_xz(self) -> None:
        for ext, comp in (("gz", gzip.compress), ("bz2", bz2.compress), ("xz", lzma.compress)):
            p = self.write(f"s/app.log.{ext}", comp(b"line\n" * 100))
            res = extract.extract(p, max_bytes=GIB)
            self.assertEqual(res.dest, str(self.tmp / "s" / "app.log"), ext)
            self.assertEqual(Path(res.dest).read_bytes(), b"line\n" * 100)
            os.remove(res.dest)
        self.assertEqual(sorted(os.listdir(self.tmp / "s")), ["app.log.bz2", "app.log.gz", "app.log.xz"])

    def test_nested_tar_gz_with_inner_archives(self) -> None:
        inner_zip = io.BytesIO()
        with zipfile.ZipFile(inner_zip, "w") as zf:
            zf.writestr("deep.txt", b"D")
        data = tar_bytes({"top/a.log.gz": gzip.compress(b"a"), "top/inner.zip": inner_zip.getvalue(),
                          "top/plain.txt": b"P", "top/more.tar": tar_bytes({"m.txt": b"M"})}, "w:gz")
        p = self.write("n/backup.tar.gz", data)
        res = extract.extract(p, max_bytes=GIB)
        t = self.tree(res.dest)
        self.assertEqual(t, {"top/a.log": b"a", "top/inner/deep.txt": b"D", "top/plain.txt": b"P", "top/more/m.txt": b"M"})
        self.assertEqual(res.files, 4)

    def test_gz_of_tar_without_tar_name(self) -> None:
        p = self.write("g/archive.gz", gzip.compress(tar_bytes({"x/1.txt": b"1", "x/2.txt": b"2"})))
        res = extract.extract(p, max_bytes=GIB)
        self.assertEqual(self.tree(res.dest), {"x/1.txt": b"1", "x/2.txt": b"2"})
        self.assertEqual(os.path.basename(res.dest), "archive")

    def test_zip_slip_and_absolute_are_skipped(self) -> None:
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as zf:
            zf.writestr("../evil.txt", b"E")
            zf.writestr("C:/Windows/evil.txt", b"E")
            zf.writestr("/abs.txt", b"E")
            zf.writestr("ok.txt", b"K")
        p = self.write("z/slip.zip", buf.getvalue())
        res = extract.extract(p, max_bytes=GIB)
        self.assertEqual(self.tree(res.dest), {"ok.txt": b"K"})
        self.assertEqual(res.skipped, 3)
        self.assertFalse((self.tmp / "evil.txt").exists())
        self.assertFalse((self.tmp / "z" / "evil.txt").exists())

    def test_tar_links_are_skipped(self) -> None:
        buf = io.BytesIO()
        with tarfile.open(fileobj=buf, mode="w") as tf:
            ln = tarfile.TarInfo("link")
            ln.type = tarfile.SYMTYPE
            ln.linkname = "../../outside"
            tf.addfile(ln)
            hl = tarfile.TarInfo("hard")
            hl.type = tarfile.LNKTYPE
            hl.linkname = "/etc/passwd"
            tf.addfile(hl)
            ti = tarfile.TarInfo("../up.txt")
            ti.size = 1
            tf.addfile(ti, io.BytesIO(b"U"))
            ti = tarfile.TarInfo("real.txt")
            ti.size = 1
            tf.addfile(ti, io.BytesIO(b"R"))
        p = self.write("t/l.tar", buf.getvalue())
        res = extract.extract(p, max_bytes=GIB)
        self.assertEqual(self.tree(res.dest), {"real.txt": b"R"})
        self.assertEqual(res.skipped, 3)

    def test_only_unsafe_entries_is_error(self) -> None:
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as zf:
            zf.writestr("../evil.txt", b"E")
        p = self.write("u/bad.zip", buf.getvalue())
        with self.assertRaises(extract.ExtractError):
            extract.extract(p, max_bytes=GIB)
        self.assertEqual(os.listdir(self.tmp / "u"), ["bad.zip"])

    def test_size_limit_removes_partial(self) -> None:
        # 小さく圧縮されて大きく展開されるもの（圧縮爆弾の形）
        p = self.write("b/bomb.gz", gzip.compress(b"\0" * (3 * 1024 * 1024)))
        with self.assertRaises(extract.ExtractError) as cm:
            extract.extract(p, max_bytes=2 * 1024 * 1024)
        self.assertIn("上限", str(cm.exception))
        self.assertEqual(os.listdir(self.tmp / "b"), ["bomb.gz"], "途中まで展開したものは残しません")

    def test_wrong_content(self) -> None:
        p = self.write("w/fake.zip", b"this is not a zip")
        with self.assertRaises(extract.ExtractError):
            extract.extract(p, max_bytes=GIB)
        p = self.write("w/broken.gz", b"\x1f\x8b\x08\x00garbage")
        with self.assertRaises(extract.ExtractError):
            extract.extract(p, max_bytes=GIB)
        self.assertEqual(sorted(os.listdir(self.tmp / "w")), ["broken.gz", "fake.zip"])

    def test_cancel(self) -> None:
        stop = threading.Event()
        stop.set()
        p = self.write("c/big.gz", gzip.compress(b"x" * (2 * 1024 * 1024)))
        with self.assertRaises(extract.ExtractCancelled):
            extract.extract(p, max_bytes=GIB, stop=stop)
        self.assertEqual(os.listdir(self.tmp / "c"), ["big.gz"])

    def test_windows_names_are_sanitized(self) -> None:
        data = tar_bytes({"a:b?.txt": b"1", "CON.txt": b"2"})
        p = self.write("x/names.tar", data)
        res = extract.extract(p, max_bytes=GIB)
        self.assertEqual(sorted(self.tree(res.dest)), ["_CON.txt", "a_b_.txt"])

    def test_options(self) -> None:
        class S:
            def get(self, k):
                return {"fetch.extract_archives": False, "fetch.extract_max_gib": 10}.get(k, _defaults(k))
        o = clean_options({"extract": True, "extract_max_gib": 5}, S())
        self.assertEqual((o["extract"], o["extract_max_gib"]), (True, 5))
        o = clean_options({}, S())
        self.assertEqual((o["extract"], o["extract_max_gib"]), (False, 10))


def _defaults(key):
    from finfet_abs.core.settings import SCHEMA
    return next(f.default for f in SCHEMA if f.key == key)


class EngineExtractTest(JobTestBase):
    """ジョブの実行で、ダウンロードの後に展開します（展開しないファイル・展開できないファイルも）。"""

    def test_job_extracts_after_download(self) -> None:
        zbuf = io.BytesIO()
        with zipfile.ZipFile(zbuf, "w") as zf:
            zf.writestr("in/a.txt", b"A" * 100)
        contents = {"x/logs.zip": zbuf.getvalue(), "x/bad.gz": b"not gzip at all", "x/plain.log": b"plain"}
        real = memory.blob_bytes

        def fake(name, offset, length):
            if name in contents:
                return contents[name][offset:offset + length]
            return real(name, offset, length)
        self.add("logs", [blob(n, size=len(d), md5=False) for n, d in contents.items()])
        with mock.patch.object(memory, "blob_bytes", fake):
            jid = self.create(self.make_search(), job_folder=False, extract=True)
            self.run_until(lambda: self.status(jid) == "completed")
        base = self.out / "logs" / "x"
        self.assertEqual((base / "logs" / "in" / "a.txt").read_bytes(), b"A" * 100)
        self.assertTrue((base / "logs.zip").exists())
        its = {r["blob_name"]: r for r in self.items(jid)}
        self.assertEqual(its["x/logs.zip"]["status_reason"], "展開しました（1 ファイル）")
        self.assertTrue(its["x/bad.gz"]["status_reason"].startswith("展開できませんでした"))
        self.assertEqual(its["x/bad.gz"]["status"], "completed")
        self.assertIsNone(its["x/plain.log"]["status_reason"])
        self.assertEqual(sorted(x.name for x in base.iterdir()), ["bad.gz", "logs", "logs.zip", "plain.log"])

    def test_job_without_extract(self) -> None:
        zbuf = io.BytesIO()
        with zipfile.ZipFile(zbuf, "w") as zf:
            zf.writestr("a.txt", b"A")
        data = zbuf.getvalue()
        self.add("logs", [blob("y/k.zip", size=len(data), md5=False)])
        with mock.patch.object(memory, "blob_bytes", lambda n, o, ln: data[o:o + ln]):
            jid = self.create(self.make_search(), job_folder=False)
            self.run_until(lambda: self.status(jid) == "completed")
        self.assertEqual([x.name for x in (self.out / "logs" / "y").iterdir()], ["k.zip"])
