@echo off
setlocal
set "PYTHONUTF8=1"
cd /d "%~dp0"
chcp 65001 >nul

set PY=python
where py >nul 2>nul && set PY=py

if not exist ".venv\Scripts\python.exe" (
  echo [1/3] creating venv ...
  %PY% -m venv .venv || goto :err
)
echo [2/3] upgrading pip ...
".venv\Scripts\python.exe" -m pip install -q --upgrade pip
echo [3/3] installing dependencies ...
".venv\Scripts\python.exe" -m pip install -q -r requirements.txt || goto :err

echo.
echo Done. Now double-click panel.bat
echo.
pause
exit /b 0

:err
echo.
echo Install failed. Check your network / proxy, then run this file again.
pause
exit /b 1
