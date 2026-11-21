"""実環境の確認用のリソースを、リソースグループごと削除します（8.4）。

使い方: python tools/testenv/delete.py
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
STATE = ROOT / ".testenv.json"


def main() -> int:
    if not STATE.exists():
        print(f"{STATE.name} がありません。削除するリソースはありません。")
        return 0
    state = json.loads(STATE.read_text(encoding="utf-8"))
    rg = state["resource_group"]
    if not rg.startswith("rg-finfet-test-"):
        print("リソースグループの名前が想定と違うため、削除しません。")
        return 1
    print(f"リソースグループ {rg} を削除します")
    r = subprocess.run(f"az group delete -n {rg} --yes --no-wait --only-show-errors", shell=True)
    if r.returncode != 0:
        return 1
    STATE.rename(STATE.with_name(".testenv.deleted.json"))
    print("削除を開始しました（完了まで数分かかります）。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
