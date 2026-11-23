# Genesis - one-shot Windows setup.
#   powershell -ExecutionPolicy Bypass -File scripts\setup_windows.ps1
# Creates .venv, installs dependencies, creates .env, downloads the offline voice model and runs diagnostics.
$ErrorActionPreference = "Stop"
Set-Location (Split-Path -Parent $PSScriptRoot)
Write-Host "`n=== GENESIS setup ===`n" -ForegroundColor Red

# 1. find a suitable Python (3.10 - 3.12 recommended)
$py = $null
foreach ($v in @("3.12", "3.11", "3.10")) {
    try { & py -$v -c "import sys" 2>$null; if ($LASTEXITCODE -eq 0) { $py = @("py", "-$v"); break } } catch {}
}
if (-not $py) {
    try { & python -c "import sys; assert (3,10) <= sys.version_info[:2] < (3,13)" 2>$null; if ($LASTEXITCODE -eq 0) { $py = @("python") } } catch {}
}
if (-not $py) {
    Write-Host "Python 3.10-3.12 not found. Install Python 3.12 from https://www.python.org/downloads/ (tick 'Add to PATH')." -ForegroundColor Yellow
    exit 1
}
Write-Host "Using Python: $($py -join ' ')"

# 2. virtual environment
if (-not (Test-Path ".venv")) {
    Write-Host "Creating virtual environment .venv ..."
    if ($py.Length -gt 1) { & $py[0] $py[1] -m venv .venv } else { & $py[0] -m venv .venv }
}
$venvPy = ".\.venv\Scripts\python.exe"
& $venvPy -m pip install --upgrade pip wheel
Write-Host "Installing dependencies (this can take a few minutes the first time) ..."
& $venvPy -m pip install -r requirements.txt
& $venvPy -m pip install pytest ruff

# 3. .env
if (-not (Test-Path ".env")) {
    Copy-Item ".env.example" ".env"
    Write-Host "Created .env from .env.example - add your HF_TOKEN (and Google OAuth values for Gmail)." -ForegroundColor Yellow
}

# 4. models
Write-Host "Downloading the offline wake-word / speech model (~40 MB) ..."
& $venvPy scripts\download_models.py

# 5. checks
& $venvPy -m genesis --doctor
Write-Host "`nRunning the test suite ..."
& $venvPy -m pytest -q

Write-Host "`nDone! Start Genesis with:  .\.venv\Scripts\python.exe -m genesis   (or run_genesis.bat, or F5 in VS Code)" -ForegroundColor Green
