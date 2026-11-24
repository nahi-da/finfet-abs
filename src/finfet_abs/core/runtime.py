"""全部入り版の実行環境（runtime\\installed.json）の版の確認（6.4）。"""

from __future__ import annotations

import json
import platform
from pathlib import Path

from ..version import __version__
from .paths import APP_DIR

INSTALLED = APP_DIR / "runtime" / "installed.json"
MISMATCH = "Python の実行環境の版が、本ツールの版と一致しません。全部入り版の ZIP を新しいフォルダーに展開してください。"


def check(installed: Path = INSTALLED, *, version: str = __version__, python: str | None = None) -> str | None:
    """実行環境の版が本体と合わなければ、表示する文を返します。全部入り版でない（開発の環境）なら None。"""
    if not installed.exists():
        return None
    try:
        data = json.loads(installed.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return MISMATCH
    if data.get("app_version") != version:
        return MISMATCH
    py = (data.get("python") or {}).get("version")
    if py and py != (python or platform.python_version()):
        return MISMATCH
    return None
