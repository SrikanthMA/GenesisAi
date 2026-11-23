<# : batch part (Windows runs this first; PowerShell treats it as a comment)
@echo off
title Set up free image generation for Genesis
set "FIX_GENESIS_DIR=%~dp0"
set "FIX_GENESIS_BAT=%~f0"
powershell -NoProfile -ExecutionPolicy Bypass -Command "$s = [IO.File]::ReadAllText($env:FIX_GENESIS_BAT); & ([ScriptBlock]::Create($s))"
echo.
pause
exit /b
#>
# Set up free image generation: Cloudflare Workers AI (FLUX.1-schnell), free daily allowance (~170 images a day).
# Asks for your Cloudflare Account ID and an API token (typing hidden), makes one test image with them, and only then
# saves them to .env (backed up first). The token is only sent to api.cloudflare.com.
$ErrorActionPreference = 'Continue'
function Say([string]$t, [string]$c = 'Gray') { Write-Host $t -ForegroundColor $c }
function Ok([string]$t) { Say ("  [OK]  " + $t) 'Green' }
function Warn([string]$t) { Say ("  [!]   " + $t) 'Yellow' }
function Bad([string]$t) { Say ("  [X]   " + $t) 'Red' }
Say ""
Say "==============================================================" 'Cyan'
Say "   FREE IMAGE GENERATION FOR GENESIS  (Cloudflare)" 'Cyan'
Say "==============================================================" 'Cyan'

$candidates = @()
if ($env:FIX_GENESIS_DIR) { $candidates += $env:FIX_GENESIS_DIR.TrimEnd('\', '/') }
$docs = [Environment]::GetFolderPath('MyDocuments')
if ($docs) { $candidates += (Join-Path $docs 'Genesis') }
if ($env:USERPROFILE) {
    $candidates += (Join-Path $env:USERPROFILE 'OneDrive\Documents\Genesis')
    $candidates += (Join-Path $env:USERPROFILE 'Documents\Genesis')
}
$root = $null
foreach ($c in $candidates) { if ($c -and (Test-Path -LiteralPath (Join-Path $c 'genesis\__main__.py'))) { $root = (Resolve-Path -LiteralPath $c).Path; break } }
while (-not $root) {
    $p = (Read-Host "  Paste the full path of your Genesis folder").Trim().Trim('"')
    if ($p -and (Test-Path -LiteralPath (Join-Path $p 'genesis\__main__.py'))) { $root = (Resolve-Path -LiteralPath $p).Path }
    else { Bad "That folder doesn't contain Genesis." }
}
Set-Location -LiteralPath $root
Ok "Genesis folder: $root"
$py = Join-Path $root '.venv\Scripts\python.exe'
$pyw = Join-Path $root '.venv\Scripts\pythonw.exe'
if (-not (Test-Path -LiteralPath $py)) { Bad "Genesis isn't installed here (.venv missing). Run setup.bat first."; return }

Say ""
Say "  You need a free Cloudflare account (2 minutes):" 'Cyan'
Say "   1. Sign up / log in:  https://dash.cloudflare.com/sign-up"
Say "   2. In the left menu open  AI  >  Workers AI  and click  'Use REST API'."
Say "   3. Click 'Create a Workers AI API Token' > 'Create API Token' > copy the token."
Say "   4. On the same page copy your 'Account ID'."
Say ""
if ((Read-Host "  Open the Cloudflare page in your browser now? [Y/n]") -notmatch '^\s*[nN]') {
    Start-Process 'https://dash.cloudflare.com/?to=/:account/ai/workers-ai'
}

function Read-Secret([string]$prompt) {
    $sec = Read-Host -Prompt $prompt -AsSecureString
    $ptr = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($sec)
    try { return [Runtime.InteropServices.Marshal]::PtrToStringBSTR($ptr) } finally { [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($ptr) }
}

$helper = @'
import os, sys, json
root = os.environ["GENESIS_FIX_ROOT"]
sys.path.insert(0, root)
import logging
logging.disable(logging.CRITICAL)
from genesis.v2 import images
class Svc:
    session = None
try:
    data, ctype = images._cloudflare(Svc(), "a small red dragon flying over mountains, digital art")
    print("RESULT " + json.dumps({"ok": True, "bytes": len(data)}))
except Exception as exc:
    print("RESULT " + json.dumps({"ok": False, "kind": getattr(exc, "kind", ""), "message": str(exc)}))
'@
$helperPath = Join-Path ([IO.Path]::GetTempPath()) ("genesis_img_" + [Guid]::NewGuid().ToString('N') + ".py")
[IO.File]::WriteAllText($helperPath, $helper, (New-Object Text.UTF8Encoding $false))

$saved = $false
try {
    for ($try = 1; $try -le 3 -and -not $saved; $try++) {
        $acct = (Read-Host "  Paste your Cloudflare Account ID").Trim().Trim('"')
        if ($acct -notmatch '^[0-9a-fA-F]{32}$') { Bad "An Account ID is 32 letters/digits (0-9, a-f). Copy it again."; continue }
        $tok = (Read-Secret "  Paste your Cloudflare API token (hidden)").Trim().Trim('"')
        if ($tok.Length -lt 20 -or $tok -match '\s') { Bad "That doesn't look like an API token. Copy it again."; continue }
        Say "  Testing - making one small image with Cloudflare..."
        $env:GENESIS_FIX_ROOT = $root
        $env:CLOUDFLARE_ACCOUNT_ID = $acct
        $env:CLOUDFLARE_API_TOKEN = $tok
        $out = & $py -I $helperPath 2>&1 | ForEach-Object { "$_" }
        Remove-Item Env:CLOUDFLARE_API_TOKEN -ErrorAction SilentlyContinue
        $line = ($out | Where-Object { $_ -like 'RESULT *' } | Select-Object -Last 1)
        if (-not $line) { Bad "The test couldn't run."; Say (($out -join "`n") -replace [regex]::Escape($tok), '****'); break }
        $res = $line.Substring(7) | ConvertFrom-Json
        if ($res.ok) {
            Ok "Cloudflare made a test image. Saving the settings..."
            $envPath = Join-Path $root '.env'
            $lines = @()
            if (Test-Path -LiteralPath $envPath) {
                Copy-Item -LiteralPath $envPath -Destination (Join-Path $root (".env.before-images-" + (Get-Date -Format 'yyyyMMdd-HHmmss'))) -Force
                $lines = @([IO.File]::ReadAllLines($envPath) | Where-Object { $_ -notmatch '^\s*(CLOUDFLARE_ACCOUNT_ID|CLOUDFLARE_API_TOKEN)\s*=' })
            }
            $lines += ''
            $lines += '# Free image generation (Cloudflare Workers AI, ~170 images a day) - set by Setup_Images.bat'
            $lines += "CLOUDFLARE_ACCOUNT_ID=$acct"
            $lines += "CLOUDFLARE_API_TOKEN=$tok"
            [IO.File]::WriteAllLines($envPath, [string[]]$lines, (New-Object Text.UTF8Encoding $false))
            $saved = $true
            Ok "Saved."
        } else {
            Bad $res.message
            if ($res.kind -eq 'auth') { Say "      Make sure the token was created from the Workers AI page (it needs 'Workers AI' permission) and the Account ID is from the same account." }
        }
    }
} finally {
    Remove-Item -LiteralPath $helperPath -ErrorAction SilentlyContinue
    Remove-Item Env:CLOUDFLARE_API_TOKEN -ErrorAction SilentlyContinue
}
if (-not $saved) { Warn "Nothing was changed. Run this file again when you have the Account ID and token."; return }

try {
    $running = @(Get-CimInstance Win32_Process -ErrorAction Stop | Where-Object {
        ($_.Name -in @('python.exe', 'pythonw.exe')) -and ($_.CommandLine -match '-m\s+genesis') })
} catch { $running = @() }
foreach ($p in $running) { try { Stop-Process -Id $p.ProcessId -Force -ErrorAction Stop } catch {} }
Start-Sleep -Milliseconds 800
try {
    Start-Process -FilePath $pyw -ArgumentList '-m', 'genesis' -WorkingDirectory $root
    Ok "Genesis restarted with free image generation."
} catch { Warn "Start Genesis from the desktop icon." }
Say ""
Say "  Try:  'generate an image of a flying dragon'" 'Green'
