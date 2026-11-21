"""データ保存先の判定（5.2）と、本ツールのフォルダー。"""

from __future__ import annotations

import logging
import os
import re
from dataclasses import dataclass
from pathlib import Path

from . import winapi

log = logging.getLogger(__name__)

APP_ID = "finfet-abs"

#: パッケージのフォルダー（src/finfet_abs）
PACKAGE_DIR = Path(__file__).resolve().parent.parent
#: 本ツールのフォルダー（src の親。配布物では finfet-abs\、開発中はリポジトリのルート）
APP_DIR = PACKAGE_DIR.parent.parent
#: 画面のファイル
WEB_DIR = PACKAGE_DIR / "web"
#: 同梱のデータ
DATA_FILES_DIR = PACKAGE_DIR / "data"
#: 同梱のドキュメント
DOCS_DIR = APP_DIR / "docs"

_INVALID = re.compile(r'[<>:"/\\|?*\x00-\x1f]')
_RESERVED = re.compile(r"^(?:CON|PRN|AUX|NUL|COM[1-9]|LPT[1-9])(?:\..*)?$", re.IGNORECASE)


def safe_component(name: str) -> str:
    """1つの階層の名前を、Windows で使える名前に直します（3.3.10.4）。"""
    if name == ".":
        return "_"
    if name == "..":
        return "__"
    s = _INVALID.sub("_", name)
    s = s.rstrip(". ")
    if not s:
        s = "_"
    if _RESERVED.match(s):
        s = "_" + s
    return s


def local_app_dir() -> Path:
    """`%LOCALAPPDATA%\\finfet-abs\\`（instance.json とトークンキャッシュの置き場所）。"""
    base = os.environ.get("LOCALAPPDATA")
    if not base:
        base = str(Path.home() / "AppData" / "Local")
    return Path(base) / APP_ID


def portable_user_folder_name() -> str:
    """ポータブルモードのユーザーごとのフォルダー名（`CONTOSO_sato`）。"""
    name = winapi.get_user_name_sam().replace("\\", "_")
    return safe_component(name)


@dataclass
class DataLocation:
    """データ保存先とモード。"""

    data_dir: Path
    #: `normal` / `portable` / `custom`（--data-dir）/ `demo`
    mode: str
    local_dir: Path
    is_network: bool = False
    #: ユーザーだけが読み書きできる ACL にできたか（ポータブルモードで新しく作ったとき。None は対象外）
    acl_ok: bool | None = None

    @property
    def db_path(self) -> Path:
        return self.data_dir / "finfet-abs.db"

    @property
    def settings_path(self) -> Path:
        return self.data_dir / "settings.json"

    @property
    def logs_dir(self) -> Path:
        return self.data_dir / "logs"

    @property
    def job_logs_dir(self) -> Path:
        return self.data_dir / "logs" / "jobs"

    @property
    def prices_path(self) -> Path:
        return self.data_dir / "prices.json"

    @property
    def mode_label(self) -> str:
        return {"normal": "通常", "portable": "ポータブルモード", "custom": "指定したフォルダー",
                "demo": "デモモード"}.get(self.mode, self.mode)


def resolve(data_dir_arg: str | None = None, *, app_dir: Path = APP_DIR, demo_dir: Path | None = None) -> DataLocation:
    """データ保存先を判定し、必要なフォルダーを作ります（起動のたびに判定します）。"""
    local = local_app_dir()
    if demo_dir is not None:
        loc = DataLocation(Path(demo_dir), "demo", local)
    elif data_dir_arg:
        loc = DataLocation(Path(data_dir_arg).resolve(), "custom", local)
    elif (app_dir / "data").is_dir():
        user_dir = app_dir / "data" / portable_user_folder_name()
        loc = DataLocation(user_dir, "portable", local)
        if not user_dir.exists():
            user_dir.mkdir(parents=True)
            loc.acl_ok = winapi.restrict_to_current_user(user_dir, is_dir=True)
        loc.is_network = winapi.is_network_path(user_dir)
    else:
        loc = DataLocation(local, "normal", local)
    local.mkdir(parents=True, exist_ok=True)
    loc.data_dir.mkdir(parents=True, exist_ok=True)
    loc.job_logs_dir.mkdir(parents=True, exist_ok=True)
    if loc.mode != "portable":
        loc.is_network = winapi.is_network_path(loc.data_dir)
    return loc


def is_inside(path: Path, parent: Path) -> bool:
    """path が parent の中（parent 自身を含む）にあるか。大文字・小文字を区別しません。"""
    try:
        p = os.path.normcase(os.path.abspath(path))
        q = os.path.normcase(os.path.abspath(parent))
        return os.path.commonpath([p, q]) == q
    except ValueError:
        return False
