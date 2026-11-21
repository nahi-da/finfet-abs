"""実環境の確認用のリソースを作ります（8.4）。Azure CLI（ログイン済み）を使います。

作るもの（専用のリソースグループの中）:
  a: 汎用 v2（Hot と Archive の小さな Blob、診断設定のログの書き出し先）
  b: 共有キーによる認証を無効にしたもの
  c: ファイアウォールを有効にしたもの（許可する IP なし）
  d: 階層型名前空間を有効にしたもの
自分のアカウントに「ストレージ BLOB データ共同作成者」をリソースグループの範囲で割り当てます。
作ったリソースの名前と ID は、リポジトリのルートの .testenv.json（追跡しない）に書きます。

使い方: python tools/testenv/create.py [--location japaneast]
"""

from __future__ import annotations

import argparse
import json
import secrets
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
STATE = ROOT / ".testenv.json"


def az(*args: str, parse: bool = True):
    cmd = ["az", *args, "--only-show-errors"] + (["-o", "json"] if parse else [])
    r = subprocess.run(" ".join(f'"{a}"' if " " in a else a for a in cmd), shell=True, capture_output=True, text=True,
                       encoding="utf-8")
    if r.returncode != 0:
        print(r.stderr, file=sys.stderr)
        raise SystemExit(f"az の実行に失敗しました: {args[:3]}")
    return json.loads(r.stdout) if parse and r.stdout.strip() else r.stdout


def save(state: dict) -> None:
    STATE.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--location", default="japaneast")
    args = ap.parse_args()
    if STATE.exists():
        print(f"{STATE.name} があります。先に tools/testenv/delete.py で削除してください。")
        return 1
    rnd = secrets.token_hex(3)
    rg = f"rg-finfet-test-{rnd}"
    state = {"resource_group": rg, "location": args.location, "accounts": {}}
    account = az("account", "show")
    state["subscription_id"] = account["id"]
    state["tenant_id"] = account["tenantId"]
    me = az("ad", "signed-in-user", "show")
    state["user_object_id"] = me["id"]
    print(f"リソースグループ {rg} を作ります")
    rg_info = az("group", "create", "-n", rg, "-l", args.location, "--tags", "purpose=finfet-abs-test")
    state["resource_group_id"] = rg_info["id"]
    save(state)

    specs = {
        "a": ["--kind", "StorageV2", "--sku", "Standard_LRS", "--access-tier", "Hot"],
        "b": ["--kind", "StorageV2", "--sku", "Standard_LRS", "--allow-shared-key-access", "false"],
        "c": ["--kind", "StorageV2", "--sku", "Standard_LRS", "--default-action", "Deny"],
        "d": ["--kind", "StorageV2", "--sku", "Standard_LRS", "--hns", "true"],
    }
    for k, extra in specs.items():
        name = f"stfinfet{rnd}{k}"
        print(f"ストレージアカウント {name} を作ります")
        info = az("storage", "account", "create", "-n", name, "-g", rg, "-l", args.location, "--min-tls-version", "TLS1_2",
                  "--allow-blob-public-access", "false", *extra)
        state["accounts"][k] = {"name": name, "id": info["id"]}
        save(state)

    print("ストレージ BLOB データ共同作成者を割り当てます")
    az("role", "assignment", "create", "--assignee-object-id", state["user_object_id"], "--assignee-principal-type",
       "User", "--role", "Storage Blob Data Contributor", "--scope", rg_info["id"])

    # a: 小さな Blob（Hot と Archive）。アカウントキーで書き込みます（ロールの反映を待たないため）
    a = state["accounts"]["a"]["name"]
    key = az("storage", "account", "keys", "list", "-n", a, "-g", rg)[0]["value"]
    with tempfile.TemporaryDirectory() as tmp:
        names = []
        for i in range(6):
            p = Path(tmp) / f"f{i}.txt"
            p.write_text(f"finfet-abs test blob {i}\n" * 20, encoding="utf-8")
            names.append(p)
        for cont in ("logs", "archive", "日本語"):
            az("storage", "container", "create", "-n", cont, "--account-name", a, "--account-key", key, parse=False)
        blobs = [("logs", "app/2026/11/20/web.log", "Hot"), ("logs", "app/2026/11/21/web.log", "Cool"),
                 ("archive", "y=2026/m=11/d=20/h=09/a.json", "Archive"), ("archive", "y=2026/m=11/d=20/h=10/b.json", "Archive"),
                 ("archive", "y=2026/m=11/d=21/h=09/c.json", "Archive"), ("日本語", "報告書/2026年 #1 (確定).txt", "Hot")]
        for (cont, blob, tier), p in zip(blobs, names):
            az("storage", "blob", "upload", "--account-name", a, "--account-key", key, "-c", cont, "-n", blob, "-f", str(p),
               "--tier", tier, "--overwrite", parse=False)
    state["blobs"] = [{"container": c, "name": n, "tier": t} for c, n, t in blobs]
    save(state)

    # a: 診断設定（Blob のログをストレージアカウント a に書き出す。7.2 の確認用）
    try:
        az("monitor", "diagnostic-settings", "create", "--name", "finfet-test", "--resource",
           f"{state['accounts']['a']['id']}/blobServices/default", "--storage-account", state["accounts"]["a"]["id"],
           "--logs", '[{"category":"StorageRead","enabled":true},{"category":"StorageWrite","enabled":true}]')
        state["diagnostic_settings"] = True
    except SystemExit:
        state["diagnostic_settings"] = False
        print("診断設定を作れませんでした（7.2 の確認は省きます）")
    save(state)
    print(f"作りました。{STATE.name} に記録しました。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
