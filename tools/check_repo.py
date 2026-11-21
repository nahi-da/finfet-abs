"""リポジトリの検査（6.11.4）。

追跡するファイル（と、追跡の候補になる無視されていないファイル）の内容を調べ、
秘密情報らしい値・ローカルの情報・追跡してはいけないファイルがあれば失敗にします。

使い方: python tools/check_repo.py
例外にする文字列は tools/check_repo_allow.txt に1行に1つ書きます。
"""

from __future__ import annotations

import codecs
import os
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
ALLOW_FILE = ROOT / "tools" / "check_repo_allow.txt"
MAX_SIZE = 100 * 1024 * 1024

#: テストで使うダミーの値（sig= の後ろに置いてよい値）
DUMMY_SIG_VALUES = {"REDACTED_TEST", "***"}

RE_SIG = re.compile(r"sig=([A-Za-z0-9%+/=_-]{16,})")
RE_ACCOUNT_KEY = re.compile(r"AccountKey=([A-Za-z0-9+/]{40,}={0,2})")
RE_JWT = re.compile(r"eyJ[A-Za-z0-9_-]{8,}\.eyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}")
RE_USER_DIR = re.compile(r"[A-Za-z]:[\\/]+Users[\\/]+[^\\/\s\"'`<>]+", re.IGNORECASE)
RE_AUTHOR_TRAILER = re.compile(r"co-authored-by\s*:", re.IGNORECASE)

# 特定の製品名（0.6）。この検査のファイル自身に名前が現れないよう、ROT13 で書きます。
_PRODUCT_NAMES_ROT13 = ["pynhqr", "naguebcvp", "pungtcg", "bcranv", "pbcvybg", "trzvav"]
RE_PRODUCT = re.compile(
    r"\b(?:" + "|".join(codecs.decode(n, "rot13") for n in _PRODUCT_NAMES_ROT13) + r")\b",
    re.IGNORECASE,
)

FORBIDDEN_PREFIXES = ("runtime/", "data/")
FORBIDDEN_NAMES = {"prices.json", ".testenv.json"}


def load_allow() -> list[str]:
    """例外にする文字列の一覧を読み込みます。"""
    if not ALLOW_FILE.exists():
        return []
    items = []
    for line in ALLOW_FILE.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#"):
            items.append(line)
    return items


def list_files() -> list[str]:
    """追跡するファイルと、無視されていない未追跡のファイルの一覧を返します。"""
    out = subprocess.run(
        ["git", "ls-files", "--cached", "--others", "--exclude-standard", "-z"],
        cwd=ROOT, capture_output=True, check=True,
    ).stdout
    names = [n for n in out.decode("utf-8").split("\0") if n]
    return sorted(set(n for n in names if (ROOT / n).is_file()))


def local_words() -> list[str]:
    """この PC の情報（ユーザー名・コンピューター名）のうち、検査に使うもの。"""
    words = []
    for key in ("USERNAME", "COMPUTERNAME"):
        value = os.environ.get(key, "").strip()
        # 短すぎる値・CI の既定の名前は誤検知が多いため除きます
        if len(value) >= 4 and value.lower() not in {"runneradmin", "runner", "user", "admin"}:
            words.append(value)
    return words


def check_text(name: str, text: str, allow: list[str], words: list[str]) -> list[str]:
    """1つのファイルの内容を調べ、見つかった問題の一覧を返します。"""
    problems: list[str] = []

    def allowed(s: str) -> bool:
        return any(a in s for a in allow)

    for lineno, line in enumerate(text.splitlines(), 1):
        if allowed(line):
            continue
        for m in RE_SIG.finditer(line):
            if m.group(1) not in DUMMY_SIG_VALUES:
                problems.append(f"{name}:{lineno}: SAS の署名らしい値（sig=）")
        if RE_ACCOUNT_KEY.search(line):
            problems.append(f"{name}:{lineno}: アカウントキーらしい値（AccountKey=）")
        if RE_JWT.search(line):
            problems.append(f"{name}:{lineno}: JWT の形式の値")
        if RE_USER_DIR.search(line):
            problems.append(f"{name}:{lineno}: ユーザーのフォルダーのパス")
        if RE_AUTHOR_TRAILER.search(line):
            problems.append(f"{name}:{lineno}: 共同作成者の表記")
        if RE_PRODUCT.search(line):
            problems.append(f"{name}:{lineno}: 特定の製品名")
        low = line.lower()
        for w in words:
            if re.search(r"(?<![A-Za-z0-9])" + re.escape(w.lower()) + r"(?![A-Za-z0-9])", low):
                problems.append(f"{name}:{lineno}: この PC のユーザー名・コンピューター名")
    return problems


def main() -> int:
    """検査を実行します。問題がなければ 0 を返します。"""
    allow = load_allow()
    words = local_words()
    problems: list[str] = []
    files = list_files()
    for name in files:
        path = ROOT / name
        posix = name.replace("\\", "/")
        if posix.startswith(FORBIDDEN_PREFIXES) or Path(posix).name in FORBIDDEN_NAMES:
            problems.append(f"{name}: 追跡してはいけないファイル")
            continue
        size = path.stat().st_size
        if size >= MAX_SIZE:
            problems.append(f"{name}: 100 MB 以上のファイル")
            continue
        if posix == "tools/check_repo_allow.txt":
            continue
        data = path.read_bytes()
        if b"\0" in data[:8192]:
            continue  # バイナリのファイル
        try:
            text = data.decode("utf-8")
        except UnicodeDecodeError:
            continue
        problems.extend(check_text(name, text, allow, words))
    if problems:
        print("リポジトリの検査で問題が見つかりました:")
        for p in problems:
            print("  " + p)
        return 1
    print(f"リポジトリの検査: 問題はありません（{len(files)} ファイル）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
