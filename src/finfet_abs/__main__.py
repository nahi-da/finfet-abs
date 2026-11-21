"""起動の入口（`python -m finfet_abs`）。引数の解析と起動（2.6）。"""

from __future__ import annotations

import argparse
import sys

from .version import APP_NAME, __version__


def build_parser() -> argparse.ArgumentParser:
    """コマンドライン引数（2.6.5）の解析器を作ります。"""
    parser = argparse.ArgumentParser(prog="finfet-abs", description=f"{APP_NAME} {__version__}")
    parser.add_argument("--no-browser", action="store_true", help="ブラウザを開かない")
    parser.add_argument("--port", type=int, default=None, help="ポートを固定する")
    parser.add_argument("--demo", action="store_true", help="デモモードで起動する")
    parser.add_argument("--data-dir", default=None, help="データ保存先を指定する（開発・テスト用）")
    parser.add_argument("--self-check", action="store_true", help="動作に必要なものを確認して終了する")
    parser.add_argument("--version", action="store_true", help="版を表示して終了する")
    return parser


def main(argv: list[str] | None = None) -> int:
    """本ツールを起動します。戻り値は終了コードです。"""
    args = build_parser().parse_args(argv)
    from . import console
    if args.version:
        console.info(f"{APP_NAME} {__version__}")
        return 0
    if args.self_check:
        from .app import self_check
        return self_check()
    from .app import run
    return run(args)


if __name__ == "__main__":
    sys.exit(main())
