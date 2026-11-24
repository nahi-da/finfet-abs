"""コンソールの表示（2.6.6）。print はこのモジュールだけで使います（8.5）。"""

from __future__ import annotations

import sys

from .core.secret import redact


def _out(text: str, stream=None) -> None:
    stream = stream or sys.stdout
    try:
        print(text, file=stream, flush=True)
    except (UnicodeEncodeError, OSError):
        pass


def info(text: str) -> None:
    """案内を表示します。"""
    _out(redact(text))


def error(text: str) -> None:
    """エラーの要約を表示します。"""
    _out(redact(text), sys.stderr)


def started_again(url: str) -> None:
    """2回目の起動で、ブラウザを開かない場合の案内。"""
    _out("FinFeT はすでに起動しています。次の URL をブラウザで開いてください（60 秒間有効です）。")
    _out(url)


def started(version: str, url: str, demo: bool) -> None:
    """起動の案内（2.6.1 の 7）。起動コード付きの URL は、ここでだけ表示します。"""
    _out(f"FinFeT {version} を起動しました。" + ("（デモモード）" if demo else ""))
    _out("画面が開かない場合は、次の URL をブラウザで開いてください（60 秒間・1 回だけ有効です）。")
    _out(url)
    _out("別のブラウザで開く場合は、画面の [設定] の「画面」の [別のブラウザで開く] で、新しい URL を表示できます。")
    _out("このウィンドウを閉じると、本ツールが終了します。")
