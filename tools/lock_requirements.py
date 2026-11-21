"""依存の固定（6.9）。

setup/requirements.in から、Windows x64・Python 3.14 用の wheel を取得し、
すべての依存の版とハッシュを setup/requirements.txt に書き出します。
外部のツール（pip-tools など）は使いません。インターネットに接続できる環境で実行します。

使い方: python tools/lock_requirements.py
"""

from __future__ import annotations

import hashlib
import re
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
REQ_IN = ROOT / "setup" / "requirements.in"
REQ_TXT = ROOT / "setup" / "requirements.txt"

PLATFORM_ARGS = [
    "--platform", "win_amd64", "--python-version", "3.14", "--implementation", "cp",
    "--abi", "cp314", "--abi", "abi3", "--abi", "none", "--only-binary=:all:",
]


def normalize(name: str) -> str:
    """パッケージ名を正規化します（PEP 503）。"""
    return re.sub(r"[-_.]+", "-", name).lower()


def sha256(path: Path) -> str:
    """ファイルの SHA-256 を返します。"""
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def main() -> int:
    """依存を取得して requirements.txt を書き出します。"""
    with tempfile.TemporaryDirectory(prefix="finfet-lock-") as tmp:
        cmd = [sys.executable, "-m", "pip", "download", "-r", str(REQ_IN), *PLATFORM_ARGS,
               "-d", tmp, "--disable-pip-version-check", "-q"]
        subprocess.run(cmd, check=True)
        entries = []
        for whl in sorted(Path(tmp).glob("*.whl")):
            parts = whl.name.split("-")
            name, version = normalize(parts[0]), parts[1]
            entries.append((name, version, sha256(whl)))
        others = [p.name for p in Path(tmp).iterdir() if p.suffix != ".whl"]
        if others:
            print("wheel 以外のファイルがあります: " + ", ".join(others), file=sys.stderr)
            return 1
    lines = [
        "# このファイルは tools/lock_requirements.py で作ります。直接編集しないでください。",
        "# 対象: Windows x64、Python 3.14（6.9）",
    ]
    for name, version, digest in sorted(entries):
        lines.append(f"{name}=={version} \\")
        lines.append(f"    --hash=sha256:{digest}")
    REQ_TXT.write_text("\n".join(lines) + "\n", encoding="utf-8", newline="\n")
    print(f"{REQ_TXT.relative_to(ROOT)} を書き出しました（{len(entries)} パッケージ）")
    for name, version, _ in sorted(entries):
        print(f"  {name}=={version}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
