@echo off
chcp 65001 >nul
setlocal
cd /d "%~dp0"
set "PY=%~dp0runtime\python\python.exe"
if not exist "%PY%" (
  echo Python の実行環境（runtime フォルダー）が見つかりません。全部入り版の ZIP を展開し直してください。
  pause
  exit /b 1
)
"%PY%" -X utf8 -m finfet_abs %*
