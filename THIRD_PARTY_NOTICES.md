# 同梱するソフトウェア

配布用の ZIP ファイルには、次のソフトウェアが含まれます。この一覧は、配布用の ZIP ファイルの作成時に、`tools/build_release.py` が実際に導入したパッケージから作成し直します。

| 名前 | ライセンス |
|---|---|
| Python | PSF-2.0 |
| azure-storage-blob | MIT |
| azure-core | MIT |
| msal | MIT |
| msal-extensions | MIT |
| waitress | ZPL-2.1 |
| jpholiday | MIT |
| cryptography | Apache-2.0 OR BSD-3-Clause |
| cffi | MIT |
| pycparser | BSD-3-Clause |
| PyJWT | MIT |
| requests | Apache-2.0 |
| urllib3 | MIT |
| idna | BSD-3-Clause |
| charset-normalizer | MIT |
| certifi | MPL-2.0 |
| isodate | BSD-3-Clause |
| typing-extensions | PSF-2.0 |
| pip | MIT |

## テストに使用するデータ

リポジトリの `tests/data/syukujitsu.csv` は、内閣府「国民の祝日について」（https://www8.cao.go.jp/chosei/shukujitsu/gaiyou.html）の syukujitsu.csv を加工して作成しました（文字コードを UTF-8 に変換）。配布用の ZIP ファイルには含まれません。
