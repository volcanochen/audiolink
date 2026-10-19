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

set ROLE=%1
if "%ROLE%"=="" set ROLE=recv

".venv\Scripts\python.exe" -m audiolink ui --port 8770 --role %ROLE%
if errorlevel 1 pause
goto :eof

:nocreate
echo [AudioLink] failed to create venv. Install Python 3.10 or newer then retry.
pause
exit /b 1

:noinstall
echo [AudioLink] dependency install failed. Check network or proxy then re-run panel.bat.
pause
exit /b 1
