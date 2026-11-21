"""Windows の API（ctypes）。ユーザーの情報、プロセスの優先度、スリープの抑止、ACL など。"""

from __future__ import annotations

import ctypes
import logging
import os
import subprocess
import sys
from ctypes import wintypes
from pathlib import Path

log = logging.getLogger(__name__)

IS_WINDOWS = sys.platform == "win32"

if IS_WINDOWS:
    _kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    _advapi32 = ctypes.WinDLL("advapi32", use_last_error=True)
    _secur32 = ctypes.WinDLL("secur32", use_last_error=True)

    _kernel32.GetCurrentProcess.restype = wintypes.HANDLE
    _kernel32.OpenProcess.restype = wintypes.HANDLE
    _kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    _kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    _kernel32.GetExitCodeProcess.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
    _kernel32.SetPriorityClass.argtypes = [wintypes.HANDLE, wintypes.DWORD]
    _kernel32.SetThreadExecutionState.restype = wintypes.DWORD
    _kernel32.SetThreadExecutionState.argtypes = [wintypes.DWORD]
    _kernel32.GetDriveTypeW.argtypes = [wintypes.LPCWSTR]
    _kernel32.GetDriveTypeW.restype = wintypes.UINT
    _kernel32.LocalFree.argtypes = [wintypes.HLOCAL]
    _kernel32.LocalFree.restype = wintypes.HLOCAL
    _advapi32.OpenProcessToken.argtypes = [wintypes.HANDLE, wintypes.DWORD, ctypes.POINTER(wintypes.HANDLE)]
    _advapi32.GetTokenInformation.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p,
                                              wintypes.DWORD, ctypes.POINTER(wintypes.DWORD)]
    _advapi32.ConvertSidToStringSidW.argtypes = [ctypes.c_void_p, ctypes.POINTER(wintypes.LPWSTR)]

TOKEN_QUERY = 0x0008
TOKEN_USER = 1
PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
STILL_ACTIVE = 259

NORMAL_PRIORITY_CLASS = 0x00000020
BELOW_NORMAL_PRIORITY_CLASS = 0x00004000
PROCESS_MODE_BACKGROUND_BEGIN = 0x00100000
PROCESS_MODE_BACKGROUND_END = 0x00200000

ES_CONTINUOUS = 0x80000000
ES_SYSTEM_REQUIRED = 0x00000001

DRIVE_REMOTE = 4


def get_user_sid() -> str:
    """現在のユーザーの SID（`S-1-5-21-…`）。"""
    if not IS_WINDOWS:
        return "S-0-0"
    token = wintypes.HANDLE()
    if not _advapi32.OpenProcessToken(_kernel32.GetCurrentProcess(), TOKEN_QUERY, ctypes.byref(token)):
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        size = wintypes.DWORD()
        _advapi32.GetTokenInformation(token, TOKEN_USER, None, 0, ctypes.byref(size))
        buf = ctypes.create_string_buffer(size.value)
        if not _advapi32.GetTokenInformation(token, TOKEN_USER, buf, size, ctypes.byref(size)):
            raise ctypes.WinError(ctypes.get_last_error())
        # TOKEN_USER の先頭は SID_AND_ATTRIBUTES（先頭が SID へのポインター）
        psid = ctypes.cast(buf, ctypes.POINTER(ctypes.c_void_p))[0]
        sid_str = wintypes.LPWSTR()
        if not _advapi32.ConvertSidToStringSidW(psid, ctypes.byref(sid_str)):
            raise ctypes.WinError(ctypes.get_last_error())
        try:
            return sid_str.value or ""
        finally:
            _kernel32.LocalFree(ctypes.cast(sid_str, ctypes.c_void_p))
    finally:
        _kernel32.CloseHandle(token)


def get_user_name_sam() -> str:
    """`GetUserNameExW(NameSamCompatible)` の値（`CONTOSO\\sato`）。"""
    if not IS_WINDOWS:
        return os.environ.get("USER", "user")
    size = wintypes.ULONG(256)
    buf = ctypes.create_unicode_buffer(size.value)
    if not _secur32.GetUserNameExW(2, buf, ctypes.byref(size)):
        buf = ctypes.create_unicode_buffer(size.value + 1)
        if not _secur32.GetUserNameExW(2, buf, ctypes.byref(size)):
            return os.environ.get("USERNAME", "user")
    return buf.value


def get_computer_name() -> str:
    """コンピューター名（NetBIOS 名）。"""
    if not IS_WINDOWS:
        return os.uname().nodename if hasattr(os, "uname") else "computer"
    size = wintypes.DWORD(256)
    buf = ctypes.create_unicode_buffer(size.value)
    if not _kernel32.GetComputerNameExW(0, buf, ctypes.byref(size)):
        return os.environ.get("COMPUTERNAME", "computer")
    return buf.value


def process_alive(pid: int) -> bool:
    """プロセスが生きているか。"""
    if pid <= 0:
        return False
    if not IS_WINDOWS:
        try:
            os.kill(pid, 0)
            return True
        except OSError:
            return False
    h = _kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not h:
        return False
    try:
        code = wintypes.DWORD()
        if not _kernel32.GetExitCodeProcess(h, ctypes.byref(code)):
            return False
        return code.value == STILL_ACTIVE
    finally:
        _kernel32.CloseHandle(h)


_background_mode = False


def set_process_priority(mode: str) -> bool:
    """処理の優先度（3.4.4 の L7）: `normal` / `below_normal` / `background`。"""
    global _background_mode
    if not IS_WINDOWS:
        return False
    h = _kernel32.GetCurrentProcess()
    ok = True
    if _background_mode and mode != "background":
        _kernel32.SetPriorityClass(h, PROCESS_MODE_BACKGROUND_END)
        _background_mode = False
    if mode == "background":
        if not _background_mode:
            _kernel32.SetPriorityClass(h, BELOW_NORMAL_PRIORITY_CLASS)
            ok = bool(_kernel32.SetPriorityClass(h, PROCESS_MODE_BACKGROUND_BEGIN))
            _background_mode = ok
    elif mode == "below_normal":
        ok = bool(_kernel32.SetPriorityClass(h, BELOW_NORMAL_PRIORITY_CLASS))
    else:
        ok = bool(_kernel32.SetPriorityClass(h, NORMAL_PRIORITY_CLASS))
    if not ok:
        log.warning("処理の優先度を変更できませんでした（%s）", mode)
    return ok


def set_thread_execution_state(prevent_sleep: bool) -> None:
    """呼び出したスレッドの、スリープの抑止を切り替えます（3.4.4 の L8）。"""
    if not IS_WINDOWS:
        return
    flags = ES_CONTINUOUS | (ES_SYSTEM_REQUIRED if prevent_sleep else 0)
    _kernel32.SetThreadExecutionState(flags)


def is_network_path(path: Path) -> bool:
    """ネットワーク上のパス（UNC パス、ネットワークドライブ）か。"""
    s = str(path)
    if s.startswith("\\\\") and not s.startswith("\\\\?\\"):
        return True
    if s.startswith("\\\\?\\UNC\\"):
        return True
    if not IS_WINDOWS:
        return False
    drive = os.path.splitdrive(os.path.abspath(s))[0]
    if not drive:
        return False
    return _kernel32.GetDriveTypeW(drive + "\\") == DRIVE_REMOTE


def restrict_to_current_user(path: Path, is_dir: bool) -> bool:
    """ファイル・フォルダーを、現在のユーザーだけが読み書きできる ACL にします（2.6.3、5.2）。

    `icacls` で継承を外し、現在のユーザーの SID にだけフルコントロールを与えます。
    設定できなければ False を返します。
    """
    if not IS_WINDOWS:
        return False
    try:
        sid = get_user_sid()
        icacls = Path(os.environ.get("SystemRoot", r"C:\Windows")) / "System32" / "icacls.exe"
        grant = f"*{sid}:(OI)(CI)F" if is_dir else f"*{sid}:F"
        result = subprocess.run(
            [str(icacls), str(path), "/inheritance:r", "/grant:r", grant],
            capture_output=True, timeout=30, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        if result.returncode != 0:
            log.warning("ACL を設定できませんでした（icacls の終了コード %s）", result.returncode)
            return False
        return True
    except Exception as exc:  # noqa: BLE001
        log.warning("ACL を設定できませんでした: %s", type(exc).__name__)
        return False


# ---------------------------------------------------------------- コンソールの終了の通知

if IS_WINDOWS:
    _HANDLER_ROUTINE = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.DWORD)
else:  # pragma: no cover
    _HANDLER_ROUTINE = None
_handler_ref = None

CTRL_C_EVENT = 0
CTRL_BREAK_EVENT = 1
CTRL_CLOSE_EVENT = 2
CTRL_LOGOFF_EVENT = 5
CTRL_SHUTDOWN_EVENT = 6


def set_console_ctrl_handler(callback) -> bool:
    """コンソールの終了の通知（Ctrl+C、ウィンドウを閉じる、シャットダウン）を受け取ります（2.6.4）。

    callback(event: int) -> None は、通知を受けたスレッドで同期的に呼ばれます。
    """
    global _handler_ref
    if not IS_WINDOWS:
        return False

    def _handler(event: int) -> bool:
        try:
            callback(event)
        except Exception:  # noqa: BLE001
            log.exception("終了の処理でエラーが発生しました")
        return True

    _handler_ref = _HANDLER_ROUTINE(_handler)
    return bool(_kernel32.SetConsoleCtrlHandler(_handler_ref, True))
