@echo off
chcp 65001 >nul
setlocal
cd /d "%~dp0.."
set "PY=%~dp0..\.venv\Scripts\python.exe"
if not exist "%PY%" set "PY=python"
set "PYTHONPATH=%~dp0..\src"
"%PY%" -X utf8 tools\check_repo.py
if errorlevel 1 exit /b 1
"%PY%" -X utf8 -m unittest discover -s tests -t . %*
exit /b %ERRORLEVEL%
