@echo off
setlocal
chcp 65001 >nul
cd /d "%~dp0"
if not exist ".venv\Scripts\python.exe" (
  echo Install the project dependencies from README.md first.
  pause
  exit /b 1
)
set "PYTHONUTF8=1"
".venv\Scripts\python.exe" -u "%~dp0run_miniapp.py" %*
set "MINIAPP_EXIT=%ERRORLEVEL%"
if not "%MINIAPP_EXIT%"=="0" pause
exit /b %MINIAPP_EXIT%
