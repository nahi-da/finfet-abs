"""Blob 名の短縮表示（7.3.5）。名前が異なる Blob の表示は、必ず異なります。"""

from __future__ import annotations

from collections import defaultdict

ELLIPSIS = "…"


class CommonShortener:
    """共通する階層を省略します。すべての Blob で同じ位置に同じ値がある階層を `…` にまとめます。"""

    def __init__(self, names) -> None:
        common: dict[int, str] | None = None
        for n in names:
            parts = n.split("/")
            if common is None:
                common = dict(enumerate(parts))
                continue
            for i in list(common):
                if i >= len(parts) or parts[i] != common[i]:
                    del common[i]
            if not common:
                break
        self.common = set((common or {}).keys())

    def short(self, name: str) -> str:
        parts = name.split("/")
        out: list[str] = []
        for i, p in enumerate(parts):
            if i in self.common:
                if not out or out[-1] != ELLIPSIS:
                    out.append(ELLIPSIS)
            else:
                out.append(p)
        return "/".join(out)


def tail_names(names: list[str]) -> dict[str, str]:
    """末尾のみ: ファイル名だけを表示し、同じファイル名がある場合は区別できるところまで親の階層をさかのぼります。"""
    split = {n: n.split("/") for n in dict.fromkeys(names)}
    depth = {n: 1 for n in split}
    while True:
        groups: dict[str, list[str]] = defaultdict(list)
        for n, parts in split.items():
            groups["/".join(parts[-depth[n]:])].append(n)
        changed = False
        for key, members in groups.items():
            if len(members) > 1:
                for n in members:
                    if depth[n] < len(split[n]):
                        depth[n] += 1
                        changed = True
        if not changed:
            break
    out = {}
    for n, parts in split.items():
        d = depth[n]
        out[n] = "/".join(parts[-d:]) if d >= len(parts) else ELLIPSIS + "/" + "/".join(parts[-d:])
    return out
