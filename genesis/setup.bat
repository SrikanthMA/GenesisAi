@echo off
REM First-time setup (creates .venv, installs everything, downloads models, runs tests)
powershell -ExecutionPolicy Bypass -File "%~dp0scripts\setup_windows.ps1"
pause
