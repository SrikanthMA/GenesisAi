# Build a standalone Windows executable (dist\Genesis\Genesis.exe) with PyInstaller.
#   powershell -ExecutionPolicy Bypass -File scripts\build_windows.ps1
# Optional: compile installer\genesis.iss with Inno Setup to get a Setup.exe.
$ErrorActionPreference = "Stop"
Set-Location (Split-Path -Parent $PSScriptRoot)
$venvPy = ".\.venv\Scripts\python.exe"
if (-not (Test-Path $venvPy)) { Write-Host "Run scripts\setup_windows.ps1 first." -ForegroundColor Yellow; exit 1 }
& $venvPy -m pip install "pyinstaller>=6.6"
& $venvPy scripts\make_icon.py
& $venvPy -m PyInstaller --noconfirm --clean scripts\genesis.spec
Write-Host "`nBuilt: dist\Genesis\Genesis.exe" -ForegroundColor Green
Write-Host "Copy your .env next to Genesis.exe (or into %LOCALAPPDATA%\Genesis) and copy data\models into %LOCALAPPDATA%\Genesis\data\models."
