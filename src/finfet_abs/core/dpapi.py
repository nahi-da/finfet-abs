"""DPAPI による暗号化（5.6）。

秘密情報は `seal()` で 5.6.1 の JSON の文字列にして保存し、`unseal()` で `Secret` に戻します。
"""

from __future__ import annotations

import base64
import ctypes
import json
import logging
from ctypes import wintypes
from dataclasses import dataclass

from . import winapi
from .clock import SYSTEM_CLOCK, Clock, to_iso
from .secret import Secret

log = logging.getLogger(__name__)

CRYPTPROTECT_UI_FORBIDDEN = 0x01

#: 用途ごとの追加のエントロピー（5.6.1）
PURPOSES = {
    "credential": b"finfet-abs/v1/credential",
    "job-secret": b"finfet-abs/v1/job-secret",
    "proxy": b"finfet-abs/v1/proxy",
    "instance": b"finfet-abs/v1/instance",
    "selfcheck": b"finfet-abs/v1/selfcheck",
}


class DATA_BLOB(ctypes.Structure):
    _fields_ = [("cbData", wintypes.DWORD), ("pbData", ctypes.POINTER(ctypes.c_char))]


if winapi.IS_WINDOWS:
    _crypt32 = ctypes.WinDLL("crypt32", use_last_error=True)
    _kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    _crypt32.CryptProtectData.argtypes = [
        ctypes.POINTER(DATA_BLOB), wintypes.LPCWSTR, ctypes.POINTER(DATA_BLOB),
        ctypes.c_void_p, ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(DATA_BLOB)]
    _crypt32.CryptUnprotectData.argtypes = [
        ctypes.POINTER(DATA_BLOB), ctypes.POINTER(wintypes.LPWSTR), ctypes.POINTER(DATA_BLOB),
        ctypes.c_void_p, ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(DATA_BLOB)]
    _kernel32.LocalFree.argtypes = [ctypes.c_void_p]
    _kernel32.LocalFree.restype = ctypes.c_void_p


class DpapiError(Exception):
    """DPAPI の呼び出しの失敗。`winerror` は Windows のエラーコード。"""

    def __init__(self, winerror: int) -> None:
        super().__init__(f"DPAPI error {winerror}")
        self.winerror = winerror


def _blob(data: bytes) -> tuple[DATA_BLOB, ctypes.Array]:
    buf = ctypes.create_string_buffer(data, len(data))
    return DATA_BLOB(len(data), ctypes.cast(buf, ctypes.POINTER(ctypes.c_char))), buf


def protect(data: bytes, entropy: bytes) -> bytes:
    """`CryptProtectData` で暗号化します（現在の Windows ユーザーだけが復号できます）。"""
    if not winapi.IS_WINDOWS:
        raise DpapiError(-1)
    din, _b1 = _blob(data)
    dent, _b2 = _blob(entropy)
    out = DATA_BLOB()
    if not _crypt32.CryptProtectData(ctypes.byref(din), None, ctypes.byref(dent), None, None,
                                     CRYPTPROTECT_UI_FORBIDDEN, ctypes.byref(out)):
        raise DpapiError(ctypes.get_last_error())
    try:
        return ctypes.string_at(out.pbData, out.cbData)
    finally:
        _kernel32.LocalFree(ctypes.cast(out.pbData, ctypes.c_void_p))


def unprotect(data: bytes, entropy: bytes) -> bytes:
    """`CryptUnprotectData` で復号します。"""
    if not winapi.IS_WINDOWS:
        raise DpapiError(-1)
    din, _b1 = _blob(data)
    dent, _b2 = _blob(entropy)
    out = DATA_BLOB()
    if not _crypt32.CryptUnprotectData(ctypes.byref(din), None, ctypes.byref(dent), None, None,
                                       CRYPTPROTECT_UI_FORBIDDEN, ctypes.byref(out)):
        raise DpapiError(ctypes.get_last_error())
    try:
        return ctypes.string_at(out.pbData, out.cbData)
    finally:
        _kernel32.LocalFree(ctypes.cast(out.pbData, ctypes.c_void_p))


@dataclass
class Environment:
    """保存した環境（5.6.1 の env）。秘密情報ではありません。"""

    sid: str
    user: str
    computer: str


_env_cache: Environment | None = None


def current_environment() -> Environment:
    """現在の Windows ユーザーの SID・名前と、コンピューター名。"""
    global _env_cache
    if _env_cache is None:
        _env_cache = Environment(winapi.get_user_sid(), winapi.get_user_name_sam(), winapi.get_computer_name())
    return _env_cache


class DecryptError(Exception):
    """復号できない（5.6.3）。`reason` は判定の番号（1〜3）、`message` は画面に表示する文。"""

    def __init__(self, reason: int, message: str, winerror: int | None) -> None:
        super().__init__(f"decrypt failed (reason {reason}, winerror {winerror})")
        self.reason = reason
        self.message = message
        self.winerror = winerror


def seal(secret: Secret | str, purpose: str, *, clock: Clock = SYSTEM_CLOCK) -> str:
    """秘密情報を暗号化し、保存の形式（5.6.1）の JSON の文字列にします。"""
    value = secret.reveal() if isinstance(secret, Secret) else str(secret)
    data = protect(value.encode("utf-8"), PURPOSES[purpose])
    env = current_environment()
    return json.dumps({
        "v": 1,
        "alg": "dpapi-user",
        "data": base64.b64encode(data).decode("ascii"),
        "env": {"sid": env.sid, "user": env.user, "computer": env.computer, "at": to_iso(clock.now())},
    }, ensure_ascii=False)


def diagnose(stored_env: dict, current: Environment, winerror: int | None) -> DecryptError:
    """復号できない原因を判定します（5.6.3）。"""
    sid = stored_env.get("sid")
    user = stored_env.get("user") or "不明"
    computer = stored_env.get("computer") or "不明"
    if sid and sid != current.sid:
        return DecryptError(1, f"別の Windows ユーザー（{user}）が保存したため、暗号化を解除できません。"
                               "現在の Windows ユーザーで接続先を追加し直すか、保存したユーザーで本ツールを使用してください。",
                            winerror)
    if computer and computer != "不明" and computer.lower() != current.computer.lower():
        return DecryptError(2, f"別の PC（{computer}）で保存されたため、暗号化を解除できません"
                               "（ポータブルモードのフォルダーを別の PC にコピーした場合など）。"
                               "この PC で接続先を追加し直してください。", winerror)
    return DecryptError(3, "暗号化を解除できませんでした。ファイルの破損、管理者による Windows のパスワードのリセット、"
                           "ユーザープロファイルの作り直しなどが考えられます。接続先を追加し直してください。", winerror)


def unseal(text: str, purpose: str) -> Secret:
    """保存の形式の文字列を復号して `Secret` を返します。失敗したら `DecryptError`。"""
    try:
        obj = json.loads(text)
        if obj.get("v") != 1 or obj.get("alg") != "dpapi-user":
            raise ValueError("unknown format")
        data = base64.b64decode(obj["data"])
        env = obj.get("env") or {}
    except Exception:  # noqa: BLE001
        raise DecryptError(3, "暗号化を解除できませんでした。保存した形式が正しくありません。接続先を追加し直してください。",
                           None) from None
    try:
        return Secret(unprotect(data, PURPOSES[purpose]).decode("utf-8"))
    except DpapiError as exc:
        err = diagnose(env, current_environment(), exc.winerror)
        log.warning("復号できませんでした（判定 %s、エラーコード %s）", err.reason, exc.winerror)
        raise err from None


def stored_environment(text: str) -> dict:
    """保存の形式の文字列から env を取り出します（表示用）。"""
    try:
        return dict(json.loads(text).get("env") or {})
    except Exception:  # noqa: BLE001
        return {}
