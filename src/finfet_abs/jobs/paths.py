"""保存するパス（3.3.2 のパス構造、3.3.10.4 の名前の直し、3.3.15 の指定と重複の確認、7.3.6 の同名のファイル）。"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Iterable

from ..search import datetime_vars as dtv

STRUCTURES = ("preserve", "flatten", "name_only", "template")
BUILTINS = ("account", "container", "path", "dir", "name", "stem", "ext")
#: 提案に使う要素の上限（3.3.15.2）
MAX_SUGGEST_ELEMENTS = 3
LONG_PATH = 240

_BAD_CHARS = re.compile(r'[<>:"/|?*\\\x00-\x1f]')
_RESERVED = re.compile(r"^(?:CON|PRN|AUX|NUL|COM[1-9]|LPT[1-9])(?:\..*)?$", re.IGNORECASE)
_PLACE = re.compile(r"\{\{(.*?)\}\}")
UNIT_SYMBOL = {0: "YYYY", 1: "MM", 2: "DD", 3: "hh", 4: "mm", 5: "ss"}


# ---------------------------------------------------------------- Windows で使える名前（3.3.10.4）

def sanitize_segment(seg: str) -> str:
    if seg == ".":
        return "_"
    if seg == "..":
        return "__"
    seg = _BAD_CHARS.sub("_", seg)
    seg = seg.rstrip(". ")
    if not seg:
        return "_"
    if _RESERVED.match(seg):
        seg = "_" + seg
    return seg


def sanitize_path(rel: str) -> str:
    """相対パス（区切りは / または \\）の各階層を直し、\\ でつなぎます。空の階層は _ にします。"""
    parts = re.split(r"[/\\]", rel)
    return "\\".join(sanitize_segment(p) if p else "_" for p in parts)


def structure_path(structure: str, account: str, container: str, blob: str) -> str:
    """パス構造（指定する以外）での、保存先からの相対パス（直す前）。"""
    if structure == "flatten":
        return f"{container}_" + blob.replace("/", "_")
    if structure == "name_only":
        return blob.rsplit("/", 1)[-1]
    return f"{container}/{blob}"


def split_name(blob: str) -> dict[str, str]:
    """@path・@dir・@name・@stem・@ext の値。"""
    d, _, name = blob.rpartition("/")
    stem, dot, ext = name.rpartition(".")
    if not dot or not stem:
        stem, ext = name, ""
    else:
        ext = "." + ext
    return {"path": blob, "dir": d, "name": name, "stem": stem, "ext": ext}


# ---------------------------------------------------------------- 保存するパスの指定（3.3.15.1）

@dataclass
class TemplateIssue:
    pos: int
    message: str

    def to_json(self) -> dict:
        return {"pos": self.pos, "message": self.message}


class TemplateError(ValueError):
    def __init__(self, issues: list[TemplateIssue]) -> None:
        super().__init__(issues[0].message if issues else "")
        self.issues = issues


@dataclass
class VarInfo:
    """検索条件の変数（指定の検査と提案に使います）。"""

    kind: str  # string / number / datetime
    units: set[int] = field(default_factory=set)  # 日時の変数: パスパターンにある単位


def variables_of(cond: dict | None) -> dict[str, VarInfo]:
    """検索条件の変数と、日時の変数のパスパターン（コンテナー名を含む）にある単位。"""
    out: dict[str, VarInfo] = {}
    if not cond:
        return out
    for name, v in (cond.get("variables") or {}).items():
        out[name] = VarInfo(v.get("type", "string"))
    texts = [cond.get("path_pattern", "")] + [t.get("container", "") for t in cond.get("targets") or []]
    for text in texts:
        for m in _PLACE.finditer(text):
            inner = m.group(1)
            name, eq, fmt = inner.partition("=")
            if not eq:
                continue
            info = out.setdefault(name, VarInfo("datetime"))
            info.kind = "datetime"
            try:
                info.units.update(it.unit for it in dtv.parse_format(fmt) if it.symbol)
            except dtv.DateTimeFormatError:
                pass
    return out


Part = Callable[[dict], str]


class Template:
    """保存するパスの指定。parse で検査し、render で Blob ごとのパスを作ります。"""

    def __init__(self, text: str, parts: list[Part], used: list[tuple[str, str | None]]) -> None:
        self.text = text
        self._parts = parts
        self.used = used  # (名前, 書式) の一覧（@ で始まるものを含む）

    @classmethod
    def parse(cls, text: str, variables: dict[str, VarInfo], *, url_mode: bool = False) -> "Template":
        issues: list[TemplateIssue] = []
        parts: list[Part] = []
        used: list[tuple[str, str | None]] = []
        if not text.strip():
            raise TemplateError([TemplateIssue(0, "保存するパスを入力してください。")])
        if re.match(r"^(?:[A-Za-z]:|[/\\])", text):
            issues.append(TemplateIssue(0, "保存先からの相対パスで指定してください（ドライブ名や先頭の \\ は使えません）。"))
        pos = 0
        literal_check = []
        for m in _PLACE.finditer(text):
            if m.start() > pos:
                lit = text[pos:m.start()]
                parts.append(lambda _v, s=lit: s)
                literal_check.append((pos, lit))
            pos = m.end()
            inner = m.group(1).strip()
            if inner.startswith("@"):
                b = inner[1:]
                if b not in BUILTINS:
                    issues.append(TemplateIssue(m.start(), f"「{{{{{inner}}}}}」は使えません（@account、@container、@path、@dir、"
                                                           "@name、@stem、@ext を使えます）。"))
                    continue
                used.append((inner, None))
                parts.append(lambda v, b=b: v["builtin"][b])
                continue
            name, eq, fmt = inner.partition("=")
            if url_mode:
                issues.append(TemplateIssue(m.start(), "URL で指定したジョブでは、@ で始まるものだけを使えます。"))
                continue
            info = variables.get(name)
            if info is None:
                issues.append(TemplateIssue(m.start(), f"変数「{name}」は検索条件にありません。"))
                continue
            if info.kind == "datetime":
                if not eq:
                    issues.append(TemplateIssue(m.start(), f"日時の変数「{name}」には書式を付けてください（例: {{{{{name}=YYYYMMDD}}}}）。"))
                    continue
                try:
                    items = dtv.parse_format(fmt)
                except dtv.DateTimeFormatError as exc:
                    issues.append(TemplateIssue(m.start() + 2 + len(name) + 1 + exc.pos, str(exc)))
                    continue
                missing = sorted({it.unit for it in items if it.symbol} - info.units)
                if missing:
                    labels = "・".join(dtv.UNIT_LABELS[u] for u in missing)
                    issues.append(TemplateIssue(m.start(), f"日時の変数「{name}」のパスパターンに{labels}がないため、使えません。"))
                    continue
                used.append((name, fmt))
                parts.append(lambda v, n=name, items=items: _fmt_dt(v["vars"].get(n), items))
            else:
                if eq:
                    issues.append(TemplateIssue(m.start(), f"変数「{name}」は日時ではないため、書式は付けられません。"))
                    continue
                used.append((name, None))
                parts.append(lambda v, n=name: "" if v["vars"].get(n) is None else str(v["vars"].get(n)))
        if pos < len(text):
            lit = text[pos:]
            parts.append(lambda _v, s=lit: s)
            literal_check.append((pos, lit))
        if "{{" in "".join(lit for _p, lit in literal_check):
            issues.append(TemplateIssue(text.find("{{", 0), "閉じていない {{ があります。"))
        # 空の階層（指定の文字の中の // など）
        m2 = re.search(r"[/\\]{2,}|[/\\]$", text)
        if m2:
            issues.append(TemplateIssue(m2.start(), "空の階層があります。"))
        if issues:
            raise TemplateError(sorted(issues, key=lambda i: i.pos))
        return cls(text, parts, used)

    def render(self, item: dict) -> str:
        """Blob の保存先からの相対パス（直した後。区切りは \\）。item は account・container・blob_name・vars。"""
        v = {"builtin": {"account": item["storage_account"], "container": item["container"],
                         **split_name(item["blob_name"])}, "vars": item.get("vars") or {}}
        return sanitize_path("".join(p(v) for p in self._parts))


def _fmt_dt(value, items: list[dtv.FmtItem]) -> str:
    if not isinstance(value, dict):
        return ""
    fields = {i: value[u] for i, u in enumerate(dtv.UNITS) if u in value}
    try:
        return dtv.format_fields(items, fields)
    except KeyError:
        return ""


def base_template(structure: str) -> str | None:
    """パス構造に当たる指定（提案の元。3.3.15.2）。「階層を名前にする」は提案しません。"""
    return {"preserve": "{{@container}}/{{@path}}", "name_only": "{{@name}}"}.get(structure)


class PathPlanner:
    """ジョブの Blob ごとの保存するパス（保存先からの相対。直した後）を作ります。"""

    def __init__(self, structure: str, template: Template | None = None) -> None:
        if structure == "template" and template is None:
            raise ValueError("template")
        self.structure = structure
        self.template = template

    def path(self, item: dict) -> str:
        if self.structure == "template":
            return self.template.render(item)  # type: ignore[union-attr]
        return sanitize_path(structure_path(self.structure, item["storage_account"], item["container"], item["blob_name"]))


# ---------------------------------------------------------------- 重複の確認（3.3.15.2）

def path_key(p: str) -> str:
    """Windows は大文字・小文字を区別しないため、比べるときのキー。"""
    return p.lower()


def find_duplicates(paths: list[str]) -> dict[str, list[int]]:
    """パスのキー → 位置の一覧（2件以上のものだけ）。"""
    groups: dict[str, list[int]] = {}
    for i, p in enumerate(paths):
        groups.setdefault(path_key(p), []).append(i)
    return {k: v for k, v in groups.items() if len(v) > 1}


def element_values(item: dict, variables: dict[str, VarInfo]) -> dict[str, object]:
    """提案に使う要素ごとの値（変数、日時の単位、@account・@container・@dir・@name）。"""
    out: dict[str, object] = {}
    vals = item.get("vars") or {}
    for name, info in variables.items():
        v = vals.get(name)
        if info.kind == "datetime":
            if isinstance(v, dict):
                for i, u in enumerate(dtv.UNITS):
                    if i in info.units and u in v:
                        out[f"dt:{name}:{i}"] = v[u]
        else:
            out[f"var:{name}"] = v
    b = split_name(item["blob_name"])
    out["@account"] = item["storage_account"]
    out["@container"] = item["container"]
    out["@dir"] = b["dir"]
    out["@name"] = b["name"]
    return out


def suggest(items: list[dict], dup_groups: Iterable[list[int]], template_text: str | None,
            variables: dict[str, VarInfo]) -> str | None:
    """重複を解決する指定を提案します。3 個の要素で解決しなければ None。"""
    if template_text is None:
        return None
    groups = [g for g in dup_groups]
    if not groups:
        return None
    vals = {i: element_values(items[i], variables) for g in groups for i in g}
    # 指定の中で、すでに使っている要素は候補にしません
    present = _present_elements(template_text)
    candidates: set[str] = set()
    for g in groups:
        for key in vals[g[0]]:
            if key in present:
                continue
            if len({repr(vals[i].get(key)) for i in g}) > 1:
                candidates.add(key)
    chosen: list[str] = []
    unresolved = list(groups)

    def resolved(g: list[int], keys: list[str]) -> bool:
        seen = {tuple(repr(vals[i].get(k)) for k in keys) for i in g}
        return len(seen) == len(g)

    def score(keys: list[str]) -> tuple[int, int]:
        # 解決する組の数、同じなら、組の中で分けられる数（区別できる値の組み合わせの数）の合計
        n = sum(1 for g in unresolved if resolved(g, keys))
        parts = sum(len({tuple(repr(vals[i].get(k)) for k in keys) for i in g}) for g in unresolved)
        return n, parts

    while unresolved and len(chosen) < MAX_SUGGEST_ELEMENTS:
        best, best_score = None, score(chosen)
        for key in sorted(candidates - set(chosen), key=_element_order):
            sc = score(chosen + [key])
            if sc > best_score:
                best, best_score = key, sc
        if best is None:
            break
        chosen.append(best)
        unresolved = [g for g in unresolved if not resolved(g, chosen)]
    if unresolved:
        return None
    return add_elements(template_text, chosen)


def _element_order(key: str) -> tuple:
    # 同じ数を解決する場合は、日時の単位 → 変数 → @account → @container → @dir → @name の順に選びます
    order = {"dt": 0, "var": 1}
    if key.startswith("@"):
        return (2, ["@account", "@container", "@dir", "@name"].index(key), key)
    return (order[key.split(":")[0]], 0, key)


def _present_elements(text: str) -> set[str]:
    out: set[str] = set()
    for m in _PLACE.finditer(text):
        inner = m.group(1).strip()
        if inner.startswith("@"):
            out.add(inner)
            if inner == "@path":
                out.update({"@dir", "@name"})
            continue
        name, eq, fmt = inner.partition("=")
        if eq:
            try:
                for it in dtv.parse_format(fmt):
                    if it.symbol:
                        out.add(f"dt:{name}:{it.unit}")
            except dtv.DateTimeFormatError:
                pass
        else:
            out.add(f"var:{name}")
    return out


def add_elements(text: str, keys: list[str]) -> str:
    """要素を指定に加えます（日時の単位は同じ変数の書式の末尾、ほかは拡張子の前に _ でつなぐ）。"""
    others: list[str] = []
    for key in keys:
        if key.startswith("dt:"):
            _k, name, unit = key.split(":")
            sym = UNIT_SYMBOL[int(unit)]
            pat = re.compile(r"\{\{" + re.escape(name) + r"=([^}]*)\}\}")
            found = list(pat.finditer(text))
            if found:
                m = found[-1]  # 最後の箇所（ふつうはファイル名）の書式に加えます
                text = text[:m.start()] + "{{" + name + "=" + m.group(1) + sym + "}}" + text[m.end():]
            else:
                others.append("{{" + name + "=" + sym + "}}")
        elif key.startswith("var:"):
            others.append("{{" + key[4:] + "}}")
        else:
            others.append("{{" + key + "}}")
    if not others:
        return text
    ins = "".join("_" + o for o in others)
    # 最後の階層の拡張子の前に入れます（@name・@path は @stem と @ext に分けます）
    text = text.replace("{{@path}}", "{{@dir}}/{{@stem}}{{@ext}}") if "{{@path}}" in text else text
    text = text.replace("{{@name}}", "{{@stem}}{{@ext}}")
    if "{{@ext}}" in text:
        i = text.rfind("{{@ext}}")
        return text[:i] + ins + text[i:]
    seg_start = max(text.rfind("/"), text.rfind("\\")) + 1
    last = text[seg_start:]
    # {{…}} の外の最後の . を探します
    masked = _PLACE.sub(lambda m: "#" * len(m.group(0)), last)
    dot = masked.rfind(".")
    if dot > 0:
        return text[:seg_start + dot] + ins + text[seg_start + dot:]
    return text + ins


def number_duplicates(paths: list[str], order: list[int], dup_groups: Iterable[list[int]]) -> list[str]:
    """重なった Blob のうち、Blob 名の順（order の順）で 2 件目以降に (1)、(2) … を付けます。"""
    out = list(paths)
    used = {path_key(p) for p in paths}
    rank = {idx: n for n, idx in enumerate(order)}
    for g in dup_groups:
        members = sorted(g, key=lambda i: rank[i])
        for i in members[1:]:
            k = 1
            while True:
                cand = numbered(paths[i], k)
                if path_key(cand) not in used:
                    break
                k += 1
            used.add(path_key(cand))
            out[i] = cand
    return out


def numbered(path: str, k: int) -> str:
    """`名前(k).拡張子`。"""
    d, sep, name = path.rpartition("\\")
    stem, dot, ext = name.rpartition(".")
    if not dot or not stem:
        stem, ext, dot = name, "", ""
    return f"{d}{sep}{stem}({k}){dot}{ext}"


# ---------------------------------------------------------------- 実際の保存（7.3.6・3.3.10.4）

def long_path(p: Path | str) -> str:
    """240 文字を超えるパスは、\\\\?\\ を付けた長いパスにします。"""
    s = str(p)
    if len(s) > LONG_PATH and not s.startswith("\\\\?\\"):
        s = os.path.abspath(s)
        return "\\\\?\\UNC\\" + s[2:] if s.startswith("\\\\") else "\\\\?\\" + s
    return s


def inside(root: Path | str, rel: str) -> bool:
    """rel が保存先 root の中を指すか。"""
    r = os.path.abspath(str(root))
    p = os.path.abspath(os.path.join(r, rel))
    try:
        return os.path.commonpath([r.lower(), p.lower()]) == r.lower()
    except ValueError:
        return False


def reserve(path: Path | str, conflict: str) -> tuple[str | None, bool]:
    """同名のファイルの扱い（3.3.2）に従って保存するパスを決め、確保します。

    戻り値: (確保したパス（スキップなら None）, 既存のファイルに重なったか)。
    番号を付ける場合は、排他的な作成（open(…, 'x')）で空のファイルを作って名前を確保します（7.3.6）。
    """
    path = str(path)
    os.makedirs(long_path(os.path.dirname(path)), exist_ok=True)
    if conflict == "overwrite":
        return path, os.path.exists(long_path(path))
    k = 0
    while True:
        cand = path if k == 0 else _numbered_fs(path, k)
        try:
            with open(long_path(cand), "x"):
                pass
            return cand, k > 0
        except FileExistsError:
            if conflict == "skip":
                return None, True
        except PermissionError:
            # 同名のフォルダーがある場合（Windows では PermissionError になることがあります）
            if not os.path.isdir(long_path(cand)):
                raise
            if conflict == "skip":
                return None, True
        k += 1


def _numbered_fs(path: str, k: int) -> str:
    d, name = os.path.split(path)
    stem, ext = os.path.splitext(name)
    if not stem:
        stem, ext = name, ""
    return os.path.join(d, f"{stem}({k}){ext}")
