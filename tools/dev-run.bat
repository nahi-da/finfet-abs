@echo off
chcp 65001 >nul
setlocal
cd /d "%~dp0.."
set "PY=%~dp0..\.venv\Scripts\python.exe"
if not exist "%PY%" (
  echo 仮想環境 .venv が見つかりません。README の「開発」の手順で作成してください。
  exit /b 1
)
set "PYTHONPATH=%~dp0..\src"
"%PY%" -X utf8 -m finfet_abs %*
