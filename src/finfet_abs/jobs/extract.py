"""ダウンロードした圧縮ファイルの展開（依頼者の意見。v1.3.0）。

Python の標準のパッケージ（zipfile・tarfile・gzip・bz2・lzma）だけで、zip・tar・gz・bz2・xz を展開します。
`.tar.gz` のように、展開してさらに圧縮ファイルが現れる場合は、最後まで展開します（途中の圧縮ファイルは消します）。
ダウンロードした元のファイルは残します。

安全のため、次を守ります。
- 展開先のフォルダーの外を指す名前（絶対パス、`..`、ドライブ名）は展開しません（ZIP Slip）
- シンボリックリンク・ハードリンク・デバイスなど、通常のファイルとフォルダー以外は展開しません
- 展開した大きさの合計と、ファイルの数に上限を設けます（圧縮爆弾への備え）。上限を超えたら、展開したものをすべて消します
- 途中は一時的な名前のフォルダーに展開し、終わってから名前を付けます
"""

from __future__ import annotations

import bz2
import gzip
import lzma
import os
import re
import shutil
import stat
import tarfile
import threading
import uuid
import zipfile
from dataclasses import dataclass

from . import paths

#: ファイルの数の上限
MAX_FILES = 100_000
#: 入れ子の深さの上限
MAX_DEPTH = 8
_CHUNK = 1024 * 1024

# 名前の末尾 → (種類, 外す長さの末尾)
_SUFFIXES = [
    (".tar.gz", "tar"), (".tgz", "tar"), (".tar.bz2", "tar"), (".tbz2", "tar"), (".tbz", "tar"),
    (".tar.xz", "tar"), (".txz", "tar"), (".tar", "tar"), (".zip", "zip"),
    (".gz", "gz"), (".bz2", "bz2"), (".xz", "xz"),
]
_MAGIC = {"gz": b"\x1f\x8b", "bz2": b"BZh", "xz": b"\xfd7zXZ\x00", "zip": b"PK"}


class ExtractError(Exception):
    """展開できなかった理由（画面とログに出す文）。"""


class ExtractCancelled(Exception):
    """中止された。"""


@dataclass
class Result:
    files: int
    dest: str
    skipped: int = 0


def archive_kind(name: str) -> tuple[str, str] | None:
    """名前から圧縮ファイルの種類と、種類の末尾を外した名前を返します（圧縮ファイルでなければ None）。"""
    low = name.lower()
    for suf, kind in _SUFFIXES:
        if low.endswith(suf) and len(name) > len(suf):
            return kind, name[: -len(suf)]
    return None


def _head(path: str, n: int = 512) -> bytes:
    with open(paths.long_path(path), "rb") as f:
        return f.read(n)


def _looks_like(kind: str, path: str) -> bool:
    head = _head(path)
    if kind == "tar":
        # 圧縮した tar は、圧縮の形式で判定します（中身は展開のときに確かめます）
        return any(head.startswith(m) for m in (_MAGIC["gz"], _MAGIC["bz2"], _MAGIC["xz"])) or head[257:262] == b"ustar"
    return head.startswith(_MAGIC[kind])


def _is_plain_tar(path: str) -> bool:
    try:
        return _head(path)[257:262] == b"ustar"
    except OSError:
        return False


class _Budget:
    """展開した大きさとファイルの数の上限。"""

    def __init__(self, max_bytes: int, stop: threading.Event | None) -> None:
        self.max_bytes = max_bytes
        self.bytes = 0
        self.files = 0
        self.stop = stop

    def add_file(self) -> None:
        self.files += 1
        if self.files > MAX_FILES:
            raise ExtractError(f"ファイル数が上限（{MAX_FILES:,}）を超えたため、展開を中止しました。")

    def add_bytes(self, n: int) -> None:
        self.bytes += n
        if self.bytes > self.max_bytes:
            raise ExtractError(f"展開後のサイズが上限（{self.max_bytes / 1024 ** 3:g} GiB）を超えたため、展開を中止しました。")
        if self.stop is not None and self.stop.is_set():
            raise ExtractCancelled()


def _copy(src, dst_path: str, budget: _Budget) -> None:
    budget.add_file()
    os.makedirs(paths.long_path(os.path.dirname(dst_path)), exist_ok=True)
    with open(paths.long_path(dst_path), "xb") as out:
        while True:
            chunk = src.read(_CHUNK)
            if not chunk:
                break
            budget.add_bytes(len(chunk))
            out.write(chunk)


_DRIVE = re.compile(r"^[A-Za-z]:/")


def _safe_rel(name: str) -> str | None:
    """圧縮ファイルの中の名前を、展開先の中の相対パスにします。外を指す名前は None。"""
    n = name.replace("\\", "/")
    if n.startswith("/") or _DRIVE.match(n):
        return None
    parts = [p for p in n.split("/") if p not in ("", ".")]
    if not parts or any(p == ".." for p in parts):
        return None
    return "\\".join(paths.sanitize_segment(p) for p in parts)


def _inside(root: str, rel: str) -> str | None:
    dst = os.path.join(root, rel)
    if not paths.inside(root, rel):
        return None
    return dst


def _zip_name(info: zipfile.ZipInfo) -> str:
    # UTF-8 の印がない名前は、日本の Windows で作った ZIP（Shift_JIS）として読み直します
    if info.flag_bits & 0x800:
        return info.filename
    try:
        return info.filename.encode("cp437").decode("cp932")
    except (UnicodeEncodeError, UnicodeDecodeError):
        return info.filename


def _extract_zip(path: str, root: str, budget: _Budget, skipped: list[str]) -> None:
    try:
        zf = zipfile.ZipFile(paths.long_path(path))
    except zipfile.BadZipFile as exc:
        raise ExtractError(f"ZIP ファイルとして読み込めません（{exc}）。") from None
    with zf:
        for info in zf.infolist():
            name = _zip_name(info)
            rel = _safe_rel(name)
            dst = _inside(root, rel) if rel else None
            if dst is None:
                skipped.append(name)
                continue
            mode = info.external_attr >> 16
            if info.is_dir():
                os.makedirs(paths.long_path(dst), exist_ok=True)
                continue
            if stat.S_ISLNK(mode):
                skipped.append(name)
                continue
            if info.flag_bits & 0x1:
                raise ExtractError("パスワード付きの ZIP ファイルは展開できません。")
            try:
                with zf.open(info) as src:
                    _copy(src, dst, budget)
            except NotImplementedError:
                raise ExtractError("この ZIP ファイルの圧縮の方式には対応していません。") from None
            except FileExistsError:
                skipped.append(name)


def _extract_tar(path: str, root: str, budget: _Budget, skipped: list[str]) -> None:
    try:
        tf = tarfile.open(paths.long_path(path), mode="r:*")
    except (tarfile.TarError, EOFError, OSError, lzma.LZMAError) as exc:
        raise ExtractError(f"tar ファイルとして読み込めません（{exc}）。") from None
    with tf:
        try:
            for m in tf:
                rel = _safe_rel(m.name)
                dst = _inside(root, rel) if rel else None
                if dst is None or not (m.isfile() or m.isdir()):
                    skipped.append(m.name)
                    continue
                if m.isdir():
                    os.makedirs(paths.long_path(dst), exist_ok=True)
                    continue
                src = tf.extractfile(m)
                if src is None:
                    skipped.append(m.name)
                    continue
                with src:
                    try:
                        _copy(src, dst, budget)
                    except FileExistsError:
                        skipped.append(m.name)
        except (tarfile.TarError, EOFError, zlib_errors(), lzma.LZMAError) as exc:
            raise ExtractError(f"tar ファイルが破損しています（{exc}）。") from None


def zlib_errors():
    import zlib
    return zlib.error


_OPENERS = {"gz": gzip.open, "bz2": bz2.open, "xz": lzma.open}


def _extract_single(kind: str, path: str, dst: str, budget: _Budget) -> None:
    try:
        with _OPENERS[kind](paths.long_path(path), "rb") as src:
            _copy(src, dst, budget)
    except (OSError, EOFError, lzma.LZMAError, zlib_errors()) as exc:
        if isinstance(exc, OSError) and exc.errno is not None:
            raise
        raise ExtractError(f"{kind} ファイルとして読み込めません（{exc}）。") from None


def _expand_tree(root: str, budget: _Budget, skipped: list[str]) -> None:
    """展開したフォルダーの中の圧縮ファイルを、なくなるまで展開します（途中の圧縮ファイルは消します）。"""
    for _ in range(MAX_DEPTH):
        found = []
        for d, _dirs, files in os.walk(paths.long_path(root)):
            for f in files:
                fp = os.path.join(d, f)
                k = archive_kind(f)
                if k is not None and _looks_like(k[0], fp):
                    found.append((fp, k))
                elif k is None and _is_plain_tar(fp):
                    found.append((fp, ("tar", f)))
        if not found:
            return
        for fp, (kind, base) in found:
            want = os.path.join(os.path.dirname(fp), paths.sanitize_segment(base))
            # 名前に種類の末尾がない tar（archive.gz を展開した archive など）は、展開した後に元の名前を使います
            same = os.path.normcase(want) == os.path.normcase(fp)
            dest = _unique(want + ".extracting" if same else want)
            if kind in ("zip", "tar"):
                os.makedirs(dest)
                (_extract_zip if kind == "zip" else _extract_tar)(fp, dest, budget, skipped)
            else:
                _extract_single(kind, fp, dest, budget)
            os.remove(fp)
            budget.files -= 1
            if same:
                os.replace(dest, want)
    raise ExtractError(f"圧縮ファイルの多重の圧縮が深すぎます（{MAX_DEPTH} 段まで）。")


def _unique(path: str) -> str:
    if not os.path.exists(paths.long_path(path)):
        return path
    k = 2
    while True:
        cand = paths._numbered_fs(path, k)
        if not os.path.exists(paths.long_path(cand)):
            return cand
        k += 1


def extract(path: str, *, max_bytes: int, stop: threading.Event | None = None) -> Result | None:
    """ダウンロードしたファイル path を、同じフォルダーに展開します。

    圧縮ファイルでなければ None を返します。展開できなければ ExtractError（展開したものは残しません）。
    zip・tar は「名前から種類の末尾を外したフォルダー」に、gz・bz2・xz（tar 以外）は「末尾を外したファイル」にします。
    """
    name = os.path.basename(path)
    k = archive_kind(name)
    if k is None:
        return None
    kind, base = k
    if not _looks_like(kind, path):
        raise ExtractError("ファイル名は圧縮ファイルの形式ですが、内容が圧縮ファイルの形式ではありません。")
    parent = os.path.dirname(path)
    work = os.path.join(parent, f".{base}.extracting-{uuid.uuid4().hex[:8]}")
    os.makedirs(paths.long_path(work))
    budget = _Budget(max_bytes, stop)
    skipped: list[str] = []
    try:
        if kind in ("zip", "tar"):
            (_extract_zip if kind == "zip" else _extract_tar)(path, work, budget, skipped)
        else:
            _extract_single(kind, path, os.path.join(work, paths.sanitize_segment(base)), budget)
        _expand_tree(work, budget, skipped)
        if budget.files == 0:
            raise ExtractError("展開できるファイルがありませんでした（保存先の外を指す名前やリンクのみ、または空の圧縮ファイルです）。")
        entries = os.listdir(paths.long_path(work))
        if kind not in ("zip", "tar") and len(entries) == 1:
            # 1 つのファイルを圧縮したもの（app.log.gz → app.log）は、フォルダーを作らずに置きます
            src = os.path.join(work, entries[0])
            dest = _unique(os.path.join(parent, entries[0]))
            os.replace(paths.long_path(src), paths.long_path(dest))
            os.rmdir(paths.long_path(work))
        else:
            dest = _unique(os.path.join(parent, paths.sanitize_segment(base)))
            os.replace(paths.long_path(work), paths.long_path(dest))
    except BaseException:
        shutil.rmtree(paths.long_path(work), ignore_errors=True)
        raise
    return Result(files=budget.files, dest=dest, skipped=len(skipped))
