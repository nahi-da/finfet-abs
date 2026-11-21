"""Release の本文（CHANGELOG.md のその版の部分）を出力します（6.11.5 の手順 4）。

使い方: python tools/release_notes.py vX.Y.Z
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def section(text: str, version: str) -> str | None:
    lines = text.splitlines()
    out: list[str] = []
    inside = False
    for line in lines:
        if line.startswith("## "):
            if inside:
                break
            inside = re.match(rf"^## \[?v?{re.escape(version)}\]?(\s|$|（|\()", line) is not None
            continue
        if inside:
            out.append(line)
    return "\n".join(out).strip() if inside or out else None


def main() -> int:
    if len(sys.argv) != 2:
        print(__doc__)
        return 2
    version = sys.argv[1].removeprefix("v")
    body = section((ROOT / "CHANGELOG.md").read_text(encoding="utf-8"), version)
    if not body:
        print(f"CHANGELOG.md に {version} の見出しがありません", file=sys.stderr)
        return 1
    sys.stdout.reconfigure(encoding="utf-8")
    print(body)
    print("\n---\n全部入り版の確かめ方は、同梱の docs/setup.html（ダウンロードとハッシュの確認）を見てください。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
