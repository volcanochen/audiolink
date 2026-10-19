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

if "%~1"=="" (
  echo Usage: send.bat ^<receiver-ip^> ["capture device"]
  echo Example: send.bat 192.168.50.111
  pause
  exit /b 1
)

if "%~2"=="" (
  ".venv\Scripts\python.exe" -m audiolink send --peer %~1 --no-discover
) else (
  ".venv\Scripts\python.exe" -m audiolink send --peer %~1 --device "%~2" --no-discover
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
