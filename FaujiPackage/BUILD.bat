@echo off
REM ============================================================
REM  FaujiBot — One-click installer builder
REM  Just double-click this file. It will:
REM    1. Auto-elevate to admin if needed
REM    2. Download embeddable Python 3.12
REM    3. Download MetaTrader 5 silent installer
REM    4. Download all Python packages (wheels)
REM    5. Auto-install Inno Setup if missing
REM    6. Compile FaujiSetup.exe
REM
REM  Output: installer\Output\FaujiSetup.exe (~250 MB)
REM  Copy that file to your AWS RDP and run as admin.
REM ============================================================

REM Auto-elevate to admin
net session >nul 2>&1
if %errorlevel% neq 0 (
  echo Requesting administrator privileges...
  powershell -Command "Start-Process -FilePath '%~f0' -Verb RunAs"
  exit /b
)

cd /d "%~dp0"

echo.
echo  ============================================
echo   FaujiBot Installer Builder
echo  ============================================
echo.
echo  This will take 3 to 5 minutes.
echo  It downloads Python, MT5, packages, and Inno Setup.
echo  Internet connection required.
echo.
pause

powershell -ExecutionPolicy Bypass -NoProfile -File "%~dp0installer\build.ps1"

if %errorlevel% neq 0 (
  echo.
  echo  ============================================
  echo   BUILD FAILED  ^(exit code %errorlevel%^)
  echo  ============================================
  echo  Scroll up to see the error.
  echo.
  pause
  exit /b %errorlevel%
)

echo.
echo  ============================================
echo   SUCCESS
echo  ============================================
echo.
echo   Your installer is ready:
echo   %~dp0installer\Output\FaujiSetup.exe
echo.
echo   Copy that file to your AWS RDP, then
echo   right-click - Run as administrator.
echo.

REM Open the Output folder so user can grab the file
start "" "%~dp0installer\Output"
pause
