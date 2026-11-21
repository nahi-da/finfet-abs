"""秘密情報の型（5.6.2）と伏せ字（3.6.2）。"""

from __future__ import annotations

import re
import threading

MASK = "***"

_registry_lock = threading.Lock()
_registry: set[str] = set()


def register_secret(value: str) -> None:
    """伏せ字にする値を登録します（ログなどに値がそのまま現れた場合に備えます）。"""
    if value and len(value) >= 8:
        with _registry_lock:
            _registry.add(value)


class Secret:
    """秘密情報を包む型。`str()`・`repr()` は伏せ字になります。"""

    __slots__ = ("_value",)

    def __init__(self, value: str) -> None:
        if isinstance(value, Secret):
            value = value.reveal()
        self._value = str(value)
        register_secret(self._value)

    def reveal(self) -> str:
        """値を取り出します。使う直前にだけ呼びます。"""
        return self._value

    def __repr__(self) -> str:
        return "Secret(***)"

    def __str__(self) -> str:
        return MASK

    def __format__(self, spec: str) -> str:
        return MASK

    def __eq__(self, other: object) -> bool:
        return isinstance(other, Secret) and other._value == self._value

    def __hash__(self) -> int:
        return hash(("Secret", self._value))

    def __bool__(self) -> bool:
        return bool(self._value)

    def __reduce__(self):  # pickle などで値が出ないように
        raise TypeError("Secret は直列化できません")


# ---------------------------------------------------------------- 伏せ字の規則（3.6.2 の 1）

_STORAGE_HOST = r"[A-Za-z0-9-]+\.(?:blob|dfs|file|queue|table|web)\.[A-Za-z0-9.-]+"
_PATTERNS: list[tuple[re.Pattern, str]] = [
    # プロキシなどの URL の「ユーザー:パスワード@」
    (re.compile(r"([A-Za-z][A-Za-z0-9+.-]*://)[^/\s:@\"'<>]+:[^/\s@\"'<>]*@"), r"\1***:***@"),
    # ストレージ・サインインの URL のクエリ文字列の全体
    (re.compile(r"(https?://" + _STORAGE_HOST + r"(?::\d+)?[^\s?#\"'<>]*)\?[^\s\"'<>#]*"), r"\1?***"),
    (re.compile(r"(https?://login\.microsoftonline\.[A-Za-z.]+[^\s?#\"'<>]*)\?[^\s\"'<>#]*"), r"\1?***"),
    # 接続文字列の項目
    (re.compile(r"(AccountKey=)[^;\s\"']+", re.IGNORECASE), r"\1***"),
    (re.compile(r"(SharedAccessSignature=)[^;\s\"']+", re.IGNORECASE), r"\1***"),
    (re.compile(r"(SharedAccessKey=)[^;\s\"']+", re.IGNORECASE), r"\1***"),
    # クエリの値
    (re.compile(r"(?<![A-Za-z0-9_])(sig=)[^&\s\"'<>;]+", re.IGNORECASE), r"\1***"),
    (re.compile(r"(client_secret=)[^&\s\"'<>;]+", re.IGNORECASE), r"\1***"),
    (re.compile(r"(?<![A-Za-z0-9_])((?:code|state)=)[^&\s\"'<>;]+"), r"\1***"),
    (re.compile(r"(?<![A-Za-z0-9_])((?:password|passwd|pwd)=)[^&\s\"'<>;]+", re.IGNORECASE), r"\1***"),
    # Authorization ヘッダー
    (re.compile(r"(Bearer\s+)[A-Za-z0-9._~+/=-]+", re.IGNORECASE), r"\1***"),
    (re.compile(r"(Authorization\s*[:=]\s*)(?!Bearer)[^\s\"',]+", re.IGNORECASE), r"\1***"),
    # JWT の形式
    (re.compile(r"eyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]*"), MASK),
]


def redact(text: str) -> str:
    """文字列から秘密情報らしい部分を伏せ字にします。"""
    if not text:
        return text
    if not isinstance(text, str):
        text = str(text)
    with _registry_lock:
        values = sorted(_registry, key=len, reverse=True)
    for v in values:
        if v in text:
            text = text.replace(v, MASK)
    for pattern, repl in _PATTERNS:
        text = pattern.sub(repl, text)
    return text
