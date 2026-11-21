"""Blob 名の並べ替えのキー（7.3.7）。数字の部分を数値として比べます（`h=2` が `h=10` より前）。"""

from __future__ import annotations

import re

_PARTS = re.compile(r"(\d+)")


def sort_key(name: str) -> bytes:
    """並べ替えのキー（SQLite の BLOB の比較で、数字の部分を数値として比べられる形）。"""
    out = []
    for i, part in enumerate(_PARTS.split(name)):
        if not part:
            continue
        if i % 2 == 1:
            digits = part.lstrip("0")
            n = min(len(digits), 99)
            out.append(f"{n:02d}".encode() + digits.encode())
        else:
            out.append(part.encode("utf-8"))
    # 先頭の 0 の数だけが違う同じ値（07 と 7）は、最後に元の名前で比べます
    return b"\x00".join(out) + b"\x00\x00" + name.encode("utf-8")
