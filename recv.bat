@echo off
setlocal
set "PYTHONUTF8=1"
cd /d "%~dp0"
chcp 65001 >nul

set PY=python
where py >nul 2>nul && set PY=py

if not exist ".venv\Scripts\python.exe" (
  echo [AudioLink] first run: creating virtual environment...
  %PY% -m venv .venv
  if errorlevel 1 goto :nocreate
  echo [AudioLink] installing dependencies now, this needs internet
  ".venv\Scripts\python.exe" -m pip install -q --upgrade pip
  ".venv\Scripts\python.exe" -m pip install -q -r requirements.txt
  if errorlevel 1 goto :noinstall
  echo [AudioLink] setup complete.
)

set DEV=%~1
set PORT=%~2
if "%PORT%"=="" set PORT=45800

if "%DEV%"=="" (
  ".venv\Scripts\python.exe" -m audiolink recv --port %PORT%
) else (
  ".venv\Scripts\python.exe" -m audiolink recv --device "%DEV%" --port %PORT%
)
pause
goto :eof

:nocreate
echo [AudioLink] failed to create venv. Install Python 3.10 or newer then retry.
pause
exit /b 1

:noinstall
echo [AudioLink] dependency install failed. Check network or proxy then re-run panel.bat.
pause
exit /b 1
