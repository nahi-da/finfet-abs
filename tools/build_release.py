"""全部入り版の作成（6.6）。

setup/runtime.json・setup/requirements.txt から、公式の配布元（python.org、PyPI）のファイルを取得し、
ハッシュを確かめてから、Python の embed 版・pip・パッケージ・本体・ドキュメント・ライセンスを組み立てて ZIP にします。
GitHub Actions（タグの push）と、開発者の PC（確認用）で同じように動きます。インターネットへの接続が必要です。

使い方: python tools/build_release.py [--out dist] [--tag vX.Y.Z] [--keep]
  --out   ZIP と SHA256SUMS.txt を置くフォルダー（既定: dist）
  --tag   タグの名前。version.py の版と違えば中止します（6.11.5）
  --keep  作業用のフォルダーを消さずに残します（中身の確認用）
"""

from __future__ import annotations

import argparse
import email.parser
import hashlib
import html
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import urllib.request
import zipfile
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SETUP = ROOT / "setup"
NAME = "finfet-abs"
UA = {"User-Agent": "finfet-abs-build"}
PLATFORM_ARGS = ["--platform", "win_amd64", "--python-version", "3.14", "--implementation", "cp",
                 "--abi", "cp314", "--abi", "abi3", "--abi", "none", "--only-binary=:all:"]
# 配布物に入れるリポジトリのファイル（6.3）
TOP_FILES = ("README.md", "LICENSE", "CHANGELOG.md", "SECURITY.md")
LICENSE_NAMES = re.compile(r"^(LICEN[CS]E|COPYING|NOTICE|AUTHORS|PATENTS)", re.I)


def log(msg: str) -> None:
    print(msg, flush=True)


def fail(msg: str) -> None:
    raise SystemExit(f"中止しました: {msg}")


def sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def download(url: str, dest: Path, expect: str) -> Path:
    """取得して SHA-256 を確かめます。違えば中止します（6.6 の手順 2）。"""
    if not url.startswith(("https://www.python.org/", "https://files.pythonhosted.org/")):
        fail(f"公式の配布元ではない URL です: {url}")
    if not dest.exists():
        req = urllib.request.Request(url, headers=UA)
        with urllib.request.urlopen(req, timeout=300) as res, dest.open("wb") as f:  # noqa: S310
            shutil.copyfileobj(res, f)
    got = sha256(dest)
    if got != expect:
        dest.unlink()
        fail(f"{dest.name} の SHA-256 が runtime.json と一致しません（{got}）")
    log(f"OK  {dest.name}  sha256={got}")
    return dest


def app_version() -> str:
    ns: dict = {}
    exec((ROOT / "src" / "finfet_abs" / "version.py").read_text(encoding="utf-8"), ns)  # noqa: S102（自分の version.py）
    return ns["__version__"]


def clean_env() -> dict:
    """Python のない状態に近い環境変数（開発の Python・仮想環境を見ないように）。"""
    env = {k: v for k, v in os.environ.items() if not k.upper().startswith(("PYTHON", "PIP_", "VIRTUAL_ENV", "CONDA"))}
    env["PYTHONUTF8"] = "1"
    return env


def run(args: list, **kw) -> None:
    log("> " + " ".join(str(a) for a in args))
    r = subprocess.run([str(a) for a in args], **kw)
    if r.returncode:
        fail(f"コマンドが失敗しました（終了コード {r.returncode}）")


def write_crlf(path: Path, text: str) -> None:
    path.write_bytes(text.replace("\r\n", "\n").replace("\n", "\r\n").encode("utf-8"))


# ---------------------------------------------------------------- 実行環境（6.5）

def build_runtime(stage: Path, work: Path, rt: dict) -> Path:
    cache = work / "cache"
    cache.mkdir(parents=True, exist_ok=True)
    py = rt["python"]
    embed = download(py["embed_amd64"]["url"], cache / py["embed_amd64"]["url"].rsplit("/", 1)[1], py["embed_amd64"]["sha256"])
    pip_whl = download(rt["pip"]["url"], cache / rt["pip"]["url"].rsplit("/", 1)[1], rt["pip"]["sha256"])

    # 3. パッケージの wheel（--require-hashes で requirements.txt のハッシュと照合）
    wheels = work / "wheels"
    wheels.mkdir(exist_ok=True)
    run([sys.executable, "-m", "pip", "download", "-r", SETUP / "requirements.txt", "--require-hashes", *PLATFORM_ARGS,
         "--disable-pip-version-check", "-d", wheels])

    # 4. embed 版を展開し、._pth を書き換える
    pydir = stage / "runtime" / "python"
    pydir.mkdir(parents=True)
    with zipfile.ZipFile(embed) as z:
        z.extractall(pydir)
    pths = list(pydir.glob("python3*._pth"))
    if len(pths) != 1:
        fail("embed 版に ._pth のファイルが見つかりません")
    stem = pths[0].stem  # python314
    write_crlf(pths[0], f"{stem}.zip\n.\nLib\\site-packages\n..\\..\\src\nimport site\n")
    exe = pydir / "python.exe"
    env = clean_env()

    # 5. pip を導入（wheel は ZIP のため、embed 版の Python でそのまま実行できる）。
    #    `python <wheel>\pip install …` は、Windows では pip 自身の更新を防ぐ確認で止まるため、
    #    -c で wheel を sys.path に加えて runpy で実行します（decisions.md）
    boot = ("import runpy, sys; sys.path.insert(0, sys.argv[1]); del sys.argv[1]; sys.argv[0] = 'pip-bootstrap'; "
            "runpy.run_module('pip', run_name='__main__', alter_sys=True)")
    run([exe, "-c", boot, pip_whl, "install", "--no-index", "--no-compile", "--disable-pip-version-check",
         "--no-warn-script-location", pip_whl], env=env, cwd=work)
    # 6. パッケージを導入
    run([exe, "-m", "pip", "install", "--no-index", "--find-links", wheels, "--require-hashes", "--only-binary=:all:",
         "--no-compile", "--disable-pip-version-check", "--no-warn-script-location", "-r", SETUP / "requirements.txt"],
        env=env, cwd=work)
    # 7. 起動用の exe を削除し、pip.bat を作る
    scripts = pydir / "Scripts"
    if scripts.exists():
        shutil.rmtree(scripts)
    write_crlf(stage / "runtime" / "pip.bat", '@echo off\nchcp 65001 >nul\n"%~dp0python\\python.exe" -X utf8 -m pip %*\n')
    for p in pydir.rglob("__pycache__"):
        shutil.rmtree(p, ignore_errors=True)
    return pydir


def installed_packages(pydir: Path) -> list[dict]:
    """導入したパッケージ（*.dist-info）の名前・版・ライセンス・ライセンスのファイル。"""
    out = []
    for d in sorted((pydir / "Lib" / "site-packages").glob("*.dist-info")):
        meta = email.parser.Parser().parsestr((d / "METADATA").read_text(encoding="utf-8", errors="replace"))
        lic = meta.get("License-Expression") or ""
        if not lic:
            raw = (meta.get("License") or "").strip()
            if raw and "\n" not in raw and len(raw) <= 60:
                lic = raw
        if not lic:
            cls = [c.split("::")[-1].strip() for c in meta.get_all("Classifier") or [] if c.startswith("License ::")]
            lic = " / ".join(cls)
        files = [p for p in d.rglob("*") if p.is_file() and LICENSE_NAMES.match(p.name)]
        out.append({"name": meta["Name"], "version": meta["Version"], "license": lic or "（METADATA に記載なし）",
                    "files": files, "dist_info": d.name})
    return out


def req_hashes() -> dict[str, list[str]]:
    out: dict[str, list[str]] = {}
    cur = None
    for line in (SETUP / "requirements.txt").read_text(encoding="utf-8").splitlines():
        m = re.match(r"^([A-Za-z0-9_.\-]+)==", line)
        if m:
            cur = re.sub(r"[-_.]+", "-", m.group(1)).lower()
            out[cur] = []
        for h in re.findall(r"--hash=sha256:([0-9a-f]{64})", line):
            if cur:
                out[cur].append(h)
    return out


# ---------------------------------------------------------------- 本体・ドキュメント・ライセンス（6.3、6.8、6.12）

def copy_app(stage: Path) -> None:
    ignore = shutil.ignore_patterns("__pycache__", "*.pyc", "prices.json")
    shutil.copytree(ROOT / "src" / "finfet_abs", stage / "src" / "finfet_abs", ignore=ignore)
    write_crlf(stage / "finfet-abs.bat", (ROOT / "finfet-abs.bat").read_text(encoding="utf-8"))
    for name in TOP_FILES:
        if (ROOT / name).exists():
            shutil.copy2(ROOT / name, stage / name)


def package_rows(rt: dict, pkgs: list[dict]) -> list[dict]:
    rows = [{"name": "Python（embed 版）", "version": rt["python"]["version"], "license": "PSF-2.0（同梱の第三者のものは LICENSE.txt）",
             "url": "https://www.python.org/"}]
    for p in pkgs:
        rows.append({"name": p["name"], "version": p["version"], "license": p["license"],
                     "url": f"https://pypi.org/project/{p['name']}/{p['version']}/"})
    return rows


def write_notices(stage: Path, rows: list[dict]) -> None:
    lines = ["# 同梱するソフトウェア", "",
             "全部入り版には、次のソフトウェアが含まれます。この一覧は `tools/build_release.py` が、実際に導入したパッケージから作りました。",
             "ライセンスの文は `licenses\\` のフォルダーにあります。", "",
             "| 名前 | 版 | ライセンス | 入手先 |", "|---|---|---|---|"]
    for r in rows:
        lines.append(f"| {r['name']} | {r['version']} | {r['license']} | {r['url']} |")
    lines += ["", "certifi（MPL-2.0）は改変せずに同梱しています。ソースは上の入手先（PyPI）から入手できます。",
              "waitress（ZPL-2.1）の名前・Zope の名前は、本ツールの宣伝に使いません。", ""]
    (stage / "THIRD_PARTY_NOTICES.md").write_text("\n".join(lines), encoding="utf-8")


def copy_licenses(stage: Path, pydir: Path, pkgs: list[dict]) -> None:
    lic = stage / "licenses"
    (lic / "python").mkdir(parents=True)
    shutil.copy2(pydir / "LICENSE.txt", lic / "python" / "LICENSE.txt")
    shutil.copy2(ROOT / "LICENSE", lic / "finfet-abs-LICENSE.txt")
    for p in pkgs:
        dest = lic / p["name"]
        dest.mkdir(exist_ok=True)
        if not p["files"]:
            log(f"注意: {p['name']} にライセンスのファイルがありません（METADATA: {p['license']}）")
        for f in p["files"]:
            shutil.copy2(f, dest / f.name if not (dest / f.name).exists() else dest / f"{f.parent.name}-{f.name}")


def fill_docs(stage: Path, values: dict[str, str]) -> None:
    """ドキュメントの版などの値を埋め込みます（`<span data-fill="名前">…</span>` の中身を置き換え）。"""
    shutil.copytree(ROOT / "docs", stage / "docs")
    for p in (stage / "docs").glob("*.html"):
        text = p.read_text(encoding="utf-8")

        def rep(m: re.Match) -> str:
            key = m.group(2)
            return m.group(1) + values[key] + m.group(3) if key in values else m.group(0)
        text = re.sub(r'(<(?:span|tbody) data-fill="([a-z_]+)"[^>]*>).*?(</(?:span|tbody)>)', rep, text, flags=re.S)
        p.write_text(text, encoding="utf-8")


def packages_tbody(rows: list[dict]) -> str:
    return "".join(f"<tr><td>{html.escape(r['name'])}</td><td>{html.escape(r['version'])}</td>"
                   f"<td>{html.escape(r['license'])}</td><td><a href=\"{html.escape(r['url'])}\">{html.escape(r['url'])}</a></td></tr>"
                   for r in rows)


# ---------------------------------------------------------------- 全体

def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--out", default=str(ROOT / "dist"))
    ap.add_argument("--tag")
    ap.add_argument("--keep", action="store_true")
    args = ap.parse_args()
    version = app_version()
    if args.tag and args.tag.removeprefix("v") != version:
        fail(f"タグ {args.tag} と version.py の版 {version} が違います")
    rt = json.loads((SETUP / "runtime.json").read_text(encoding="utf-8"))
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    work = Path(tempfile.mkdtemp(prefix="finfet-abs-build-"))
    stage = work / "stage" / NAME
    stage.mkdir(parents=True)
    try:
        log(f"FinFeT {version} の全部入り版を作ります（Python {rt['python']['version']}、pip {rt['pip']['version']}）")
        pydir = build_runtime(stage, work, rt)
        pkgs = installed_packages(pydir)
        hashes = req_hashes()
        installed = {
            "app_version": version,
            "python": {"version": rt["python"]["version"], "sha256": rt["python"]["embed_amd64"]["sha256"]},
            "pip": {"version": rt["pip"]["version"], "sha256": rt["pip"]["sha256"]},
            "packages": [{"name": p["name"], "version": p["version"],
                          "sha256": hashes.get(re.sub(r"[-_.]+", "-", p["name"]).lower(), [])} for p in pkgs
                         if p["name"].lower() != "pip"],
            "created_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        }
        (stage / "runtime" / "installed.json").write_text(json.dumps(installed, ensure_ascii=False, indent=2) + "\n",
                                                          encoding="utf-8")
        missing = set(hashes) - {re.sub(r"[-_.]+", "-", p["name"]).lower() for p in pkgs}
        if missing:
            fail(f"requirements.txt のパッケージが導入されていません: {sorted(missing)}")
        copy_app(stage)
        rows = package_rows(rt, pkgs)
        write_notices(stage, rows)
        copy_licenses(stage, pydir, pkgs)
        fill_docs(stage, {"version": version, "python_version": rt["python"]["version"], "packages": packages_tbody(rows)})
        # 10. 組み立てた結果で --self-check
        run([pydir / "python.exe", "-X", "utf8", "-m", "finfet_abs", "--self-check"], env=clean_env(), cwd=stage)
        # 11. ZIP と SHA256SUMS.txt
        zname = f"{NAME}-{version}-win-x64.zip"
        zpath = out / zname
        if zpath.exists():
            zpath.unlink()
        with zipfile.ZipFile(zpath, "w", zipfile.ZIP_DEFLATED, compresslevel=9) as z:
            for p in sorted(stage.rglob("*")):
                if p.is_file():
                    z.write(p, Path(NAME) / p.relative_to(stage))
        digest = sha256(zpath)
        (out / "SHA256SUMS.txt").write_text(f"{digest}  {zname}\n", encoding="utf-8", newline="\n")
        log(f"作成しました: {zpath}（{zpath.stat().st_size / 1024 / 1024:.1f} MiB）")
        log(f"SHA-256: {digest}")
        for r in rows:
            log(f"  {r['name']} {r['version']}  {r['license']}")
    finally:
        if args.keep:
            log(f"作業用のフォルダー: {work}")
        else:
            shutil.rmtree(work, ignore_errors=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
