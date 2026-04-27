@echo off
REM Local dev launcher — assumes Python 3.12 + MT5 already installed on this machine.
REM Run this from the FaujiPackage folder. Builds a venv on first run.

setlocal
set ROOT=%~dp0
cd /d "%ROOT%"

if not exist "%ROOT%.venv\Scripts\python.exe" (
  echo [dev] Creating venv...
  py -3.12 -m venv .venv || python -m venv .venv
  call .venv\Scripts\activate.bat
  python -m pip install --upgrade pip
  python -m pip install -r requirements.txt
) else (
  call .venv\Scripts\activate.bat
)

echo [dev] Starting supervisor on https://localhost:8443
python -m supervisor.main
endlocal
