# FinFeT（Azure Blob Storage用検索・取得ツール）

> 本ツールは、Azure Blob Storage を利用するための非公式のツールです。Microsoft Corporation およびその関連会社が開発・提供するものではなく、Microsoft による提携、承認、後援を受けていません。Microsoft、Azure は、Microsoft グループの企業の商標です。
>
> This is an unofficial tool for Azure Blob Storage. It is not developed, provided, endorsed, or sponsored by Microsoft Corporation or its affiliates. Microsoft and Azure are trademarks of the Microsoft group of companies.

Azure のストレージアカウントに保存したログやファイルの中から、「11 月 1 日から 7 日までの、web01 と web02 のログ」のように条件を指定して必要なファイルだけを探し、まとめてパソコンにダウンロードする Windows 用のツールです。

アーカイブ層に移したファイルも、そのままでは読めないので、オンライン層に戻す操作（リハイドレート）を行います。戻るまでの数時間を待ってから、自動でダウンロードします。

![検索の画面。VM の名前を選択肢から選び、期間を指定して検索する](docs/images/09-variables.png)

## こんなときに

- 診断設定で書き出したログが、`y=2026/m=11/d=21/h=09/...` のような日付のフォルダーに何万件も並んでいて、Azure Portal や Storage Explorer で 1 件ずつ探すのが大変
- 監査や障害の調査で、数か月前のアーカイブ済みのログを、決まった期間の分だけ取り出したい
- 取り出しに時間がかかるので、要求だけしておいて、翌日にダウンロードしたい
- 回線を使い切らないように、ダウンロードの速さを抑えたい

## 使い方の流れ

1. **接続先を追加する**: Microsoft のアカウントでサインインしてストレージアカウントとコンテナーを選びます。アクセスキー、SAS、接続文字列でも追加できます。
2. **探す**: ファイル名の決まり（パスパターン）と期間を指定して検索します。パスパターンが分からなくても、[パスパターンを自動生成] を押すと、コンテナーのファイル名を調べて候補を示します。診断設定のログなどのよくある形は、一覧から選ぶこともできます。
3. **選んでジョブにする**: 検索結果から必要なファイルを選び、保存先のフォルダーを指定します。アーカイブ層のファイルは、リハイドレートの要求から完了の確認、ダウンロードまでを、まとめて 1 つのジョブとして実行します。
4. **待つ**: ジョブは順番に進みます。途中でパソコンを止めても、次に起動したときに続きから再開します。

詳しい手順は、同梱の使い方（`docs\usage.html`。ツールの画面の [ヘルプ] からも開けます）にあります。Azure に接続せずに、架空のデータで操作を試すこともできます（`finfet-abs.bat --demo`）。

## 安心して使えるように

- ファイルの読み取りと、層の変更（リハイドレート）だけを行います。Blob のアップロード・削除・書き換えはしません。
- 画面はパソコンの中だけで動きます（`127.0.0.1` のローカルの Web サーバーを、既定のブラウザで表示します）。外部に公開しません。
- 保存したアクセスキーや SAS は、Windows の機能（DPAPI）で暗号化し、ほかの Windows ユーザーからは読めません。ログには秘密情報を書き出しません。
- リハイドレートには Azure の料金がかかり、取り消せません。ジョブを作るときに、対象の件数・サイズと注意を表示します。

## 動作環境

- Windows 11（x64）
- Python などのインストールは不要です（ZIP に Python と必要なパッケージを含めています）。

## 導入

1. [Releases](https://github.com/nahi-da/finfet-abs/releases) から `finfet-abs-<版>-win-x64.zip` と `SHA256SUMS.txt` をダウンロードします。
2. ダウンロードしたファイルが壊れていないかを確かめます。`certutil -hashfile finfet-abs-<版>-win-x64.zip SHA256` で表示される値が、`SHA256SUMS.txt` の値と同じであれば問題ありません。
   GitHub CLI があれば、このリポジトリの GitHub Actions で作られたことも確かめられます: `gh attestation verify finfet-abs-<版>-win-x64.zip --repo nahi-da/finfet-abs`
3. ZIP を右クリックして [プロパティ] を開き、[許可する] にチェックを入れて [OK] を押してから、好きなフォルダーに展開します（チェックを入れないと、起動のときに Windows の警告が表示されます）。
4. 展開したフォルダーの `finfet-abs.bat` をダブルクリックします。ブラウザに画面が表示されます。

インターネットに接続できないパソコンで使う場合は、接続できるパソコンでダウンロードとハッシュの確認を行い、ZIP を持ち込んで展開します。詳しくは、同梱の `docs\setup.html` を見てください。

### 新しい版への更新

新しい版の ZIP を**新しいフォルダー**に展開し、古い版を終了してから、新しいフォルダーの `finfet-abs.bat` を起動します（古いフォルダーに上書きしないでください）。設定や検索履歴、ジョブは `%LOCALAPPDATA%\finfet-abs\` にあり、そのまま引き継がれます。

### データの保存先（通常のモードとポータブルモード）

| モード | 条件 | データの保存先 |
|---|---|---|
| 通常 | 下のどちらでもない | `%LOCALAPPDATA%\finfet-abs\` |
| ポータブル | 本ツールのフォルダーに `data` フォルダーがある | `<本ツールのフォルダー>\data\<Windows ユーザー名>\` |

- USB メモリなどに入れて持ち運ぶ場合は、起動の前に、本ツールのフォルダーに空の `data` フォルダーを作ります（ポータブルモード）。
- モードを切り替えても、データは自動では移りません。移す場合は、本ツールを終了してから、`settings.json`・`finfet-abs.db`（と `finfet-abs.db-wal`・`finfet-abs.db-shm`）・`logs\` を、元の保存先から新しい保存先にコピーします。
- 保存した認証情報は、保存した Windows ユーザーでしか読めません。別のユーザー・別のパソコンでは、接続先を追加し直してください。

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
