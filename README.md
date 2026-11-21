# FinFeT（Azure Blob Storage用検索・取得ツール）

> 本ツールは、Azure Blob Storage を利用するための非公式のツールです。Microsoft Corporation およびその関連会社が開発・提供するものではなく、Microsoft による提携、承認、後援を受けていません。Microsoft、Azure は、Microsoft グループの企業の商標です。
>
> This is an unofficial tool for Azure Blob Storage. It is not developed, provided, endorsed, or sponsored by Microsoft Corporation or its affiliates. Microsoft and Azure are trademarks of the Microsoft group of companies.

FinFeT（Finding & Fetching Tool for Azure Blob Storage）は、Azure ストレージアカウントの Blob を検索し、リハイドレートとダウンロードを行う Windows 向けのツールです。画面はローカルのWebサーバー（`127.0.0.1` だけ）で動き、既定のブラウザで表示します。

## できること

- パスパターン（正規表現と、日時などの変数）で Blob を検索します。診断設定のログなど、決まった形の Blob 名は「既知の形式」から作れます。一覧の取得は、パスパターンから求めた prefix で絞り込みます。
- 検索結果で Blob を選び、リハイドレートとダウンロードを「ジョブ」としてキューに追加します。リハイドレートの完了を待つ間は、次のジョブが進みます。ジョブは日をまたいで管理でき、リハイドレートだけを行って後日ダウンロードすることもできます。
- 帯域・同時接続数・要求の速度を制限できます。途中で止まったダウンロードは、取得済みの範囲から再開します。Content-MD5 があれば照合します。
- 認証は、Microsoft Entra ID のサインイン（ユーザーアカウント、またはアクセスキーの取得）、アクセスキー、SAS、接続文字列に対応します。保存した秘密情報は Windows の DPAPI で暗号化します。
- 検索履歴、ピン留め、検索条件のエクスポート・インポート。

Blob のアップロード・削除・書き換えは行いません（読み取りと層の変更だけです）。

## 動作環境

- Windows 11（x64）
- Python などの導入は不要です（全部入り版に Python 3.14 と依存パッケージを含みます）。

## 導入

1. [Releases](../../releases) から `finfet-abs-<版>-win-x64.zip` と `SHA256SUMS.txt` をダウンロードします。
2. ハッシュを確かめます: `certutil -hashfile finfet-abs-<版>-win-x64.zip SHA256` の値が `SHA256SUMS.txt` と同じであること。
   GitHub CLI があれば、このリポジトリの GitHub Actions で作られたことも確かめられます: `gh attestation verify finfet-abs-<版>-win-x64.zip --repo <所有者>/finfet-abs`
3. ZIP のプロパティで [許可する] にチェックを入れてから、任意のフォルダーに展開します。
4. `finfet-abs.bat` を実行します。

インターネットに接続できない環境では、接続できる環境でダウンロードとハッシュの確認を行い、ZIP を持ち込んで展開します。詳しくは、同梱の `docs\setup.html` を見てください。

### 更新

新しい版の ZIP を**新しいフォルダー**に展開し、古い版を終了してから、新しいフォルダーの `finfet-abs.bat` を起動します（古いフォルダーに上書きしません）。通常のモードのデータは `%LOCALAPPDATA%\finfet-abs\` にあり、そのまま引き継がれます。

### データの保存先（通常のモードとポータブルモード）

| モード | 条件 | データの保存先 |
|---|---|---|
| 通常 | 下のどちらでもない | `%LOCALAPPDATA%\finfet-abs\` |
| ポータブル | 本ツールのフォルダーに `data` フォルダーがある | `<本ツールのフォルダー>\data\<Windows ユーザー名>\` |

- ポータブルモードで使う場合は、起動の前に、本ツールのフォルダーに空の `data` フォルダーを作ります。
- モードを切り替えても、データは自動では移りません。移す場合は、本ツールを終了してから、`settings.json`・`finfet-abs.db`（と `finfet-abs.db-wal`・`finfet-abs.db-shm`）・`logs\` を、元の保存先から新しい保存先にコピーします。
- 保存した認証情報（DPAPI で暗号化したもの）は、保存した Windows ユーザーでしか復号できません。別のユーザー・別の PC では、接続先を追加し直してください。

## 使い方

同梱のドキュメント（`docs\index.html`。本ツールの画面の [ヘルプ] からも開けます）を見てください。デモモード（`finfet-abs.bat --demo`）では、Azure に接続せずに、架空のデータで操作を試せます。

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
- 実行環境の版: `python tools\update_runtime.py`（Python の embed 版と pip の URL・SHA-256 を `setup\runtime.json` に書きます）
- 全部入り版の作成: `python tools\build_release.py`（`dist\` に ZIP と `SHA256SUMS.txt` を作ります）。`v*` のタグを push すると、GitHub Actions が同じ手順で作り、Release の下書きを作ります。

## 貢献

- 不具合の報告と機能の要望は、Issue のテンプレートから送ってください。脆弱性は、公開の Issue ではなく [SECURITY.md](SECURITY.md) の方法で報告してください。
- プルリクエストは受け付けますが、マージするかどうかは保守者が判断します。お送りいただいた変更は、本リポジトリと同じ MIT License で提供されることに同意いただいたものとします。

## ライセンス

MIT License（[LICENSE](LICENSE)）。Copyright (c) 2026 Yu-gene。同梱するソフトウェアのライセンスは [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md) にあります。
