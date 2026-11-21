"""静的ファイル（2.3）。

起動時にフォルダーの中のファイルの一覧（URL のパス → ファイル・内容の種類）を作り、一覧にあるものだけを返します。
要求のパスからファイルシステムのパスを組み立てません（ディレクトリ・トラバーサルを防ぐため）。
"""

from __future__ import annotations

from pathlib import Path

CONTENT_TYPES = {
    ".html": "text/html; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".js": "text/javascript; charset=utf-8",
    ".svg": "image/svg+xml",
    ".png": "image/png",
    ".ico": "image/x-icon",
    ".json": "application/json; charset=utf-8",
    ".txt": "text/plain; charset=utf-8",
}


class StaticFiles:
    """1つのフォルダーの静的ファイルの一覧。"""

    def __init__(self, root: Path, url_prefix: str = "/", exclude: set[str] | None = None) -> None:
        self.root = Path(root)
        self.prefix = url_prefix if url_prefix.endswith("/") else url_prefix + "/"
        self.files: dict[str, tuple[Path, str]] = {}
        exclude = exclude or set()
        if not self.root.is_dir():
            return
        for p in sorted(self.root.rglob("*")):
            if not p.is_file():
                continue
            ctype = CONTENT_TYPES.get(p.suffix.lower())
            if ctype is None:
                continue
            rel = p.relative_to(self.root).as_posix()
            if rel in exclude:
                continue
            self.files[self.prefix + rel] = (p, ctype)

    def lookup(self, path: str) -> tuple[Path, str] | None:
        """URL のパスに対応するファイル。一覧になければ None。"""
        return self.files.get(path)
