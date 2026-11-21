"""単一起動（2.6.3）。

`%LOCALAPPDATA%\\finfet-abs\\instance.json` に、起動中のプロセスの情報（ポート、プロセスID、2回目の起動の
ための秘密の値）を置きます。起動の重なりを防ぐため、`instance.lock` をプロセスの実行中ずっとロックします。
"""

from __future__ import annotations

import json
import logging
import msvcrt
import os
import time
import urllib.request
from pathlib import Path

from . import winapi

log = logging.getLogger(__name__)


def _names(demo: bool) -> tuple[str, str]:
    # デモモードは、通常の起動と同時に使えるよう、別の名前にします
    return ("instance-demo.json", "instance-demo.lock") if demo else ("instance.json", "instance.lock")


def _opener() -> urllib.request.OpenerDirector:
    # 自分自身への接続は、常にプロキシを使いません（3.1.11）
    return urllib.request.build_opener(urllib.request.ProxyHandler({}))


class InstanceLock:
    """プロセスの実行中ずっと保持するロック。"""

    def __init__(self, local_dir: Path, demo: bool = False) -> None:
        self.path = Path(local_dir) / _names(demo)[1]
        self._f = None

    def acquire(self) -> bool:
        """ロックを取ります。ほかのプロセスが持っていれば False。"""
        self.path.parent.mkdir(parents=True, exist_ok=True)
        f = open(self.path, "a+b")
        try:
            f.seek(0)
            msvcrt.locking(f.fileno(), msvcrt.LK_NBLCK, 1)
        except OSError:
            f.close()
            return False
        self._f = f
        return True

    def release(self) -> None:
        if self._f is not None:
            try:
                self._f.seek(0)
                msvcrt.locking(self._f.fileno(), msvcrt.LK_UNLCK, 1)
            except OSError:
                pass
            self._f.close()
            self._f = None


def instance_path(local_dir: Path, demo: bool = False) -> Path:
    return Path(local_dir) / _names(demo)[0]


def write(local_dir: Path, *, port: int, secret: str, version: str, demo: bool = False) -> Path:
    """instance.json を書き込み、現在のユーザーだけが読める ACL にします。"""
    path = instance_path(local_dir, demo)
    data = {"pid": os.getpid(), "port": port, "secret": secret, "version": version, "started_at": time.time()}
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(data), encoding="utf-8")
    winapi.restrict_to_current_user(tmp, is_dir=False)
    os.replace(tmp, path)
    return path


def remove(local_dir: Path, demo: bool = False) -> None:
    try:
        path = instance_path(local_dir, demo)
        data = json.loads(path.read_text(encoding="utf-8"))
        if data.get("pid") == os.getpid():
            path.unlink()
    except (OSError, ValueError):
        pass


def read(local_dir: Path, demo: bool = False) -> dict | None:
    try:
        return json.loads(instance_path(local_dir, demo).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def request_launch_url(info: dict, timeout: float = 3.0) -> str | None:
    """起動中のプロセスに起動コードを求め、ブラウザで開く URL を返します。応答がなければ None。"""
    try:
        pid = int(info.get("pid", 0))
        port = int(info.get("port", 0))
        secret = str(info.get("secret", ""))
    except (TypeError, ValueError):
        return None
    if not port or not winapi.process_alive(pid):
        return None
    base = f"http://127.0.0.1:{port}"
    opener = _opener()
    try:
        with opener.open(urllib.request.Request(base + "/api/v1/health", headers={"Host": f"127.0.0.1:{port}"}),
                         timeout=timeout) as r:
            if r.status != 200:
                return None
        req = urllib.request.Request(base + "/internal/launch-code", data=b"", method="POST",
                                     headers={"X-FinFeT-Instance": secret})
        with opener.open(req, timeout=timeout) as r:
            code = json.loads(r.read().decode("utf-8"))["code"]
        return f"{base}/auth/launch?code={code}"
    except Exception:  # noqa: BLE001
        return None


def wait_for_existing(local_dir: Path, demo: bool, timeout: float = 20.0) -> str | None:
    """起動中（または起動の途中）のプロセスが応答するまで待ち、起動コード付きの URL を返します。"""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        info = read(local_dir, demo)
        if info:
            url = request_launch_url(info)
            if url:
                return url
        time.sleep(0.5)
    return None
