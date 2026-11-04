@echo off
chcp 65001 >nul
cd /d "%~dp0"
if not exist ".venv\Scripts\python.exe" (
  echo Сначала установи зависимости по инструкции в README.md.
  pause
  exit /b 1
)
".venv\Scripts\python.exe" -u run_miniapp.py
pause
