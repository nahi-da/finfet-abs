"""setup/runtime.json を作ります（6.9）。

Python の embed 版（Windows x64）と pip の wheel の URL・SHA-256 を記録します。
- Python: python.org のリリースの API で、3.14 系の最新のパッチ（または指定した版）の embed 版を探し、
  取得したファイルの SHA-256 を計算して、python.org が公開している SHA-256 と一致することを確かめます。
- pip: PyPI の JSON API で、最新（または指定した版）の py3-none-any の wheel を探し、
  取得したファイルの SHA-256 が PyPI の値と一致することを確かめます。
インターネットに接続できる環境で実行します。

使い方: python tools/update_runtime.py [--python 3.14.x] [--pip 25.x]
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
RUNTIME_JSON = ROOT / "setup" / "runtime.json"
SERIES = "3.14"
UA = {"User-Agent": "finfet-abs-tools"}


def get(url: str) -> bytes:
    req = urllib.request.Request(url, headers=UA)
    with urllib.request.urlopen(req, timeout=120) as res:  # noqa: S310（https の固定の配布元）
        return res.read()


def get_json(url: str):
    return json.loads(get(url).decode("utf-8"))


def version_key(v: str) -> tuple[int, ...]:
    return tuple(int(x) for x in v.split("."))


def find_python(version: str | None) -> dict:
    releases = get_json("https://www.python.org/api/v2/downloads/release/?is_published=true&pre_release=false")
    cands = []
    for r in releases:
        name = r["name"].removeprefix("Python ")
        parts = name.split(".")
        if len(parts) == 3 and all(p.isdigit() for p in parts) and name.startswith(SERIES + "."):
            cands.append((version_key(name), name, r["resource_uri"]))
    if not cands:
        raise SystemExit(f"Python {SERIES} の版が見つかりません")
    cands.sort()
    if version:
        cands = [c for c in cands if c[1] == version]
        if not cands:
            raise SystemExit(f"Python {version} が見つかりません")
    _key, name, uri = cands[-1]
    rid = uri.rstrip("/").rsplit("/", 1)[1]
    files = get_json(f"https://www.python.org/api/v2/downloads/release_file/?release={rid}")
    for f in files:
        if f["url"].endswith(f"python-{name}-embed-amd64.zip"):
            if not f.get("sha256_sum"):
                raise SystemExit("python.org が embed 版の SHA-256 を公開していません")
            return {"version": name, "url": f["url"], "sha256": f["sha256_sum"], "size": f.get("filesize"),
                    "sigstore_bundle": f.get("sigstore_bundle_file") or None}
    raise SystemExit(f"Python {name} の embed 版（amd64）が見つかりません")


def find_pip(version: str | None) -> dict:
    data = get_json(f"https://pypi.org/pypi/pip/{version}/json" if version else "https://pypi.org/pypi/pip/json")
    ver = data["info"]["version"]
    for u in data["urls"]:
        if u["packagetype"] == "bdist_wheel" and u["filename"].endswith("-py3-none-any.whl"):
            return {"version": ver, "url": u["url"], "sha256": u["digests"]["sha256"]}
    raise SystemExit(f"pip {ver} の wheel が見つかりません")


def verify(entry: dict, label: str) -> None:
    data = get(entry["url"])
    got = hashlib.sha256(data).hexdigest()
    if got != entry["sha256"]:
        raise SystemExit(f"{label} の SHA-256 が、配布元の公開している値と一致しません（{got}）")
    print(f"OK  {label} {entry['version']}: {got}（{len(data):,} バイト）")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--python", help="Python の版（既定は 3.14 系の最新）")
    ap.add_argument("--pip", help="pip の版（既定は最新）")
    args = ap.parse_args()
    py = find_python(args.python)
    pip = find_pip(args.pip)
    verify(py, "Python の embed 版")
    verify(pip, "pip")
    out = {
        "python": {"version": py["version"],
                   "embed_amd64": {"url": py["url"], "sha256": py["sha256"], "sigstore_bundle": py["sigstore_bundle"]}},
        "pip": {"version": pip["version"], "url": pip["url"], "sha256": pip["sha256"]},
        "verified_with": "python.org のリリースの API（sha256_sum）と PyPI の JSON API（digests.sha256）",
    }
    RUNTIME_JSON.write_text(json.dumps(out, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"書き出しました: {RUNTIME_JSON.relative_to(ROOT)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
