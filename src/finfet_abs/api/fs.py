"""フォルダーの選択の API（4.3.7、5.7.2）。

S9: API でフォルダーのパスを受け取る場合は、絶対パスに正規化し、データ保存先や本ツールのフォルダーへの書き込みは
拒否します。`open-folder` で開けるのはフォルダーだけです。
"""

from __future__ import annotations

import os
import secrets
import shutil
import string
from pathlib import Path

from ..core import paths, recent
from ..core.errors import AppError, bad_request
from ..server.http import Request, json_response

MAX_ENTRIES = 2000


def normalize_dir(text: str | None) -> Path:
    """入力されたフォルダーのパスを、絶対パスに正規化します。"""
    if not isinstance(text, str) or not text.strip():
        raise bad_request("フォルダーを指定してください。", "PATH_REQUIRED")
    s = os.path.expandvars(text.strip().strip('"'))
    if "\0" in s:
        raise bad_request("フォルダーのパスが正しくありません。", "PATH_INVALID")
    p = Path(s)
    if not (p.drive or s.startswith("\\\\")) or not p.is_absolute():
        raise bad_request("ドライブ名から始まるフォルダーのパスを入力してください。", "PATH_NOT_ABSOLUTE")
    return Path(os.path.abspath(s))


def forbidden_dirs(app) -> list[Path]:
    """書き込みを拒否するフォルダー（S9）。"""
    return [app.location.data_dir, app.location.local_dir, paths.APP_DIR]


def check_not_forbidden(app, p: Path) -> None:
    for d in forbidden_dirs(app):
        if paths.is_inside(p, d):
            raise AppError("PATH_FORBIDDEN", "このフォルダーには本ツールのデータまたは本体があるため、保存先に指定できません。")


def list_drives() -> list[str]:
    out = []
    for letter in string.ascii_uppercase:
        root = f"{letter}:\\"
        if os.path.exists(root):
            out.append(root)
    return out


def register(router, app) -> None:
    @router.get("/api/v1/fs/dirs")
    def dirs(req: Request):
        text = req.arg("path", "")
        if not text:
            home = Path.home()
            special = []
            for label, sub in (("ダウンロード", "Downloads"), ("ドキュメント", "Documents"), ("デスクトップ", "Desktop")):
                p = home / sub
                if p.is_dir():
                    special.append({"label": label, "path": str(p)})
            return json_response({"path": "", "parent": None, "dirs": [], "drives": list_drives(),
                                  "recent": recent.get(app.db, "directory", limit=10), "special": special})
        p = normalize_dir(text)
        if not p.is_dir():
            raise AppError("DIR_NOT_FOUND", "フォルダーが見つかりません。", status=404)
        names = []
        try:
            with os.scandir(p) as it:
                for e in it:
                    try:
                        if e.is_dir(follow_symlinks=False):
                            names.append(e.name)
                    except OSError:
                        continue
                    if len(names) >= MAX_ENTRIES:
                        break
        except PermissionError:
            raise AppError("DIR_ACCESS_DENIED", "このフォルダーの内容を表示する権限がありません。", status=403) from None
        names.sort(key=str.lower)
        parent = str(p.parent) if p.parent != p else None
        return json_response({"path": str(p), "parent": parent, "dirs": names})

    @router.post("/api/v1/fs/mkdir")
    def mkdir(req: Request):
        body = req.json()
        parent = normalize_dir(body.get("path"))
        name = str(body.get("name") or "").strip()
        if not name or paths.safe_component(name) != name:
            raise bad_request("フォルダー名に使用できない文字が含まれています。", "NAME_INVALID")
        target = parent / name
        check_not_forbidden(app, target)
        if target.exists():
            raise AppError("ALREADY_EXISTS", "同じ名前のフォルダーが存在します。", status=409)
        try:
            target.mkdir()
        except OSError as exc:
            raise AppError("MKDIR_FAILED", f"フォルダーを作成できませんでした（{exc.strerror or exc}）。") from None
        return json_response({"path": str(target)})

    @router.post("/api/v1/fs/check-writable")
    def check_writable(req: Request):
        body = req.json()
        p = normalize_dir(body.get("path"))
        check_not_forbidden(app, p)
        if not p.is_dir():
            raise AppError("DIR_NOT_FOUND", "フォルダーが見つかりません。", status=404)
        probe = p / f".finfet-abs-write-test-{secrets.token_hex(4)}"
        try:
            with open(probe, "xb"):
                pass
            probe.unlink()
        except OSError:
            raise AppError("NOT_WRITABLE", "このフォルダーには書き込む権限がありません。") from None
        free = shutil.disk_usage(p).free
        if body.get("remember"):
            recent.add(app.db, "directory", str(p))
        return json_response({"path": str(p), "writable": True, "free_bytes": free})

    @router.post("/api/v1/fs/open-folder")
    def open_folder(req: Request):
        body = req.json()
        p = normalize_dir(body.get("path"))
        if not p.is_dir():
            raise AppError("DIR_NOT_FOUND", "フォルダーが見つかりません。", status=404)
        os.startfile(str(p))  # noqa: S606（フォルダーだけを開く）
        return json_response({"ok": True})
