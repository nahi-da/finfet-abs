"""文字列の変数の「条件に合う文字列」の文字クラス（3.2.3.1）。"""

from __future__ import annotations

#: 文字の種類 → 文字クラスの中身（正規表現の文字列）
CLASSES: dict[str, str] = {
    "digit": "0-9",
    "lower": "a-z",
    "upper": "A-Z",
    # ASCII の記号のうち / 以外（すべてエスケープします）
    "symbol": "".join("\\" + c for c in "!\"#$%&'()*+,-.:;<=>?@[\\]^_`{|}~"),
    "space": " ",
    "hiragana": "\\u3041-\\u309F\\u30FC",
    "katakana": "\\u30A1-\\u30FA\\u30FC-\\u30FF\\uFF66-\\uFF9F",
    "kanji": "\\u3400-\\u4DBF\\u4E00-\\u9FFF\\uF900-\\uFAFF\\U00020000-\\U0003134F\\u3005\\u3006\\u3007",
}

LABELS = {
    "digit": "数字", "lower": "英小文字", "upper": "英大文字", "symbol": "記号", "space": "空白",
    "hiragana": "ひらがな", "katakana": "カタカナ", "kanji": "漢字",
}


def escape_in_class(chars: str) -> str:
    """文字クラスの中に入れる文字をエスケープします（英数字以外はすべてエスケープ）。"""
    out = []
    for c in chars:
        if c.isascii() and not c.isalnum():
            out.append("\\" + c)
        elif c == "\n":
            out.append("\\n")
        else:
            out.append(c)
    return "".join(out)


def build_class(classes: list[str], extra: str = "", allow_slash: bool = False) -> str:
    """文字クラス（`[...]`）を作ります。種類が1つもなければ ValueError。"""
    parts = []
    for c in classes:
        if c not in CLASSES:
            raise ValueError(f"知らない文字の種類です: {c}")
        parts.append(CLASSES[c])
    extra_chars = "".join(dict.fromkeys(ch for ch in (extra or "") if ch != "/"))
    if extra_chars:
        parts.append(escape_in_class(extra_chars))
    if "/" in (extra or ""):
        allow_slash = True
    if not parts:
        raise ValueError("文字の種類を1つ以上選んでください。")
    if allow_slash:
        parts.append("/")
    return "[" + "".join(parts) + "]"


def length_quantifier(min_len: int | None, max_len: int | None) -> str:
    """長さの量指定子（`{1,}`、`{8,36}`、`{4}`）。"""
    lo = 1 if min_len is None else int(min_len)
    if lo < 1:
        raise ValueError("最小の長さは 1 以上にしてください。")
    if max_len is None or max_len == "":
        return "+" if lo == 1 else f"{{{lo},}}"
    hi = int(max_len)
    if hi < lo:
        raise ValueError("最大の長さは最小の長さ以上にしてください。")
    if hi == lo:
        return f"{{{lo}}}"
    return f"{{{lo},{hi}}}"
