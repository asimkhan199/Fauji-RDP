@echo off
REM Launcher invoked by the Scheduled Task and Start menu shortcut.
REM cd to install dir, set PYTHONPATH so `supervisor` package resolves, run.
setlocal
set APP=%~dp0
cd /d "%APP%"
set PYTHONPATH=%APP%
"%APP%python\python.exe" -m supervisor.main
