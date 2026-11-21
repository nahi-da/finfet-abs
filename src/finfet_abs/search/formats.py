"""既知の形式からの作成（3.2.6、7.2）。定義は data/known_formats.json です。"""

from __future__ import annotations

import json
import re
from functools import lru_cache

from ..core.paths import DATA_FILES_DIR

_PLACEHOLDER = re.compile(r"(?<!\{)\{([a-z_]+)\}(?!\})")
_META = re.compile(r"([.^$*+?{}\[\]\\|()])")


def escape_literal(text: str) -> str:
    """正規表現の記号だけをエスケープします（`PT1H.json` → `PT1H\\.json`）。"""
    return _META.sub(r"\\\1", text)


class FormatInputError(ValueError):
    """入力の誤り。field は入力の名前。"""

    def __init__(self, field: str, message: str) -> None:
        super().__init__(message)
        self.field = field


@lru_cache(maxsize=1)
def load() -> list[dict]:
    """定義を読み込みます。"""
    data = json.loads((DATA_FILES_DIR / "known_formats.json").read_text(encoding="utf-8"))
    return data["formats"]


def get(fid: str) -> dict | None:
    return next((f for f in load() if f["id"] == fid), None)


def public_list() -> list[dict]:
    """画面に返す一覧（例は返しません）。"""
    out = []
    for f in load():
        out.append({
            "id": f["id"], "group": f["group"], "name": f["name"], "description": f.get("description", ""),
            "verified": f.get("verified", False), "verified_note": f.get("verified_note", ""),
            "notes": f.get("notes", []),
            "inputs": [{k: v for k, v in i.items() if k in ("name", "label", "kind", "required", "suggest", "default",
                                                              "container_only")}
                       | ({"options": [{"id": o["id"], "label": o["label"]} for o in i["options"]]} if i.get("options") else {})
                       for i in f.get("inputs", [])],
        })
    return out


def build(fid: str, inputs: dict) -> dict:
    """入力した値から、コンテナー名・パスパターン・変数を作ります。"""
    f = get(fid)
    if f is None:
        raise FormatInputError("", "既知の形式が見つかりません。")
    defs = {i["name"]: i for i in f.get("inputs", [])}
    variables: dict[str, dict] = {}
    values: dict[str, str] = {}
    for name, d in defs.items():
        raw = str((inputs or {}).get(name, "") or "").strip()
        if d.get("kind") == "select":
            opt_id = raw if raw or "default" not in d else d["default"]
            opt = next((o for o in d["options"] if o["id"] == opt_id), None)
            if opt is None:
                raise FormatInputError(name, f"「{d['label']}」の選択が正しくありません。")
            values[name] = opt["value"]
            continue
        if raw and d.get("check") and not re.match(d["check"], raw):
            raise FormatInputError(name, f"「{d['label']}」の形式が正しくありません。")
        if not raw and d.get("required"):
            raise FormatInputError(name, f"「{d['label']}」を入力してください。")
        if d.get("upper"):
            raw = raw.upper()
        if d.get("lower"):
            raw = raw.lower()
        values[name] = raw

    def fill(template: str, *, path: bool) -> str:
        def repl(m: re.Match) -> str:
            name = m.group(1)
            d = defs.get(name)
            if d is None:
                return m.group(0)
            v = values.get(name, "")
            if d.get("kind") == "select" and v:
                return _PLACEHOLDER.sub(repl, v)
            if v:
                return escape_literal(v) if path else v
            var = d.get("var", name)
            if d.get("kind") == "select":
                vals = [o["value"] for o in d["options"] if o["value"]]
                variables[var] = {"type": "string", "label": d["label"], "value": {
                    "mode": "one_of", "match": "exact", "values": [{"value": x, "enabled": True} for x in vals]}}
            else:
                variables.setdefault(var, {"type": "string", "label": d["label"], "value": {"mode": "any"}})
            return "{{" + var + "}}"
        return _PLACEHOLDER.sub(repl, template)

    path = fill(f["path_template"], path=True)
    container = ""
    if f.get("container_template"):
        c = fill(f["container_template"], path=False)
        container = "" if "{{" in c else c
        # コンテナー名だけに使う入力から作った変数は、パスパターンには要りません
        for name, d in defs.items():
            if d.get("container_only"):
                variables.pop(d.get("var", name), None)
    for name, fmt, _pos in _vars_in(path):
        if fmt is not None:
            variables.setdefault(name, {"type": "datetime", "path_timezone": f.get("path_timezone", "utc"),
                                        "value": {"mode": "relative", "kind": "today"}})
        else:
            variables.setdefault(name, {"type": "string", "value": {"mode": "any"}})
    return {"format_id": fid, "name": f["name"], "container": container, "path_pattern": path,
            "variables": variables, "notes": f.get("notes", []), "verified": f.get("verified", False)}


def _vars_in(text: str):
    for m in re.finditer(r"\{\{([^{}]*?)\}\}", text):
        inner = m.group(1)
        if "=" in inner:
            n, fmt = inner.split("=", 1)
            yield n, fmt, m.start()
        else:
            yield inner, None, m.start()


def suggest(container: str) -> dict | None:
    """コンテナー名が既知の形式に一致すれば、その形式（ID と名前）を返します（3.2.6 の 2）。"""
    if not container or "{{" in container:
        return None
    for f in sorted(load(), key=lambda x: -len(x.get("container_regex") or "")):
        rx = f.get("container_regex")
        if rx and re.match(rx, container):
            return {"id": f["id"], "name": f["name"]}
    return None


def suggest_inputs(fid: str, container: str) -> dict:
    """コンテナー名から分かる入力（ログのカテゴリなど）。"""
    f = get(fid)
    out: dict = {}
    if not f:
        return out
    tpl = f.get("container_template") or ""
    m = re.fullmatch(re.escape(tpl).replace(r"\{category\}", "(?P<category>.+)").replace(r"\{table\}", "(?P<table>.+)"),
                     container)
    if m:
        out.update({k: v for k, v in m.groupdict().items() if v})
    if f.get("inputs") and any(i.get("container_only") for i in f["inputs"]):
        out["container"] = container
    return out
