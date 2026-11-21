# FinFeT（Azure Blob Storage用検索・取得ツール）

> 本ツールは、Azure Blob Storage を利用するための非公式のツールです。Microsoft Corporation およびその関連会社が開発・提供するものではなく、Microsoft による提携、承認、後援を受けていません。Microsoft、Azure は、Microsoft グループの企業の商標です。
>
> This is an unofficial tool for Azure Blob Storage. It is not developed, provided, endorsed, or sponsored by Microsoft Corporation or its affiliates. Microsoft and Azure are trademarks of the Microsoft group of companies.

FinFeT（Finding & Fetching Tool for Azure Blob Storage）は、Azure ストレージアカウントの Blob を検索し、リハイドレートとダウンロードを行う Windows 向けのツールです。

現在、開発中です。

## 開発

- 必要なもの: Windows 11（x64）、Python 3.14、Git
- 仮想環境の作成と依存パッケージの導入

  ```
  python -m venv .venv
  .venv\Scripts\python.exe -m pip install --require-hashes -r setup\requirements.txt
  ```

- 起動（開発用）: `tools\dev-run.bat`（引数は `--demo`、`--version` など）
- テスト: `tools\run_tests.bat`（リポジトリの検査と単体テスト）
- 依存の固定: `python tools\lock_requirements.py`（`setup\requirements.in` から `setup\requirements.txt` を作ります）

## ライセンス

MIT License（[LICENSE](LICENSE)）。同梱するソフトウェアのライセンスは [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md) にあります。
