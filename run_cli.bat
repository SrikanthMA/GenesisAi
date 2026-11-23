@echo off
REM Text-only Genesis in this console
cd /d "%~dp0"
".venv\Scripts\python.exe" -m genesis --cli %*
