<# : batch part (Windows runs this first; PowerShell treats it as a comment)
@echo off
title Update Genesis
set "FIX_GENESIS_DIR=%~dp0"
set "FIX_GENESIS_BAT=%~f0"
powershell -NoProfile -ExecutionPolicy Bypass -Command "$s = [IO.File]::ReadAllText($env:FIX_GENESIS_BAT); & ([ScriptBlock]::Create($s))"
echo.
pause
exit /b
#>
# Update Genesis to 2.1 - run once after extracting the 2.1 zip over your Genesis folder.
#   1. finds the Genesis folder and removes Windows' "downloaded from the web" mark from its files
#   2. closes a running Genesis
#   3. installs the one new package (soundcard: records the computer's sound for meeting minutes)
#   4. turns on the image-generation fallbacks (GENESIS_IMAGE_PROVIDER=auto) - .env is backed up first
#   5. checks that Genesis 2.1 loads, points the desktop shortcut at it and starts it
$ErrorActionPreference = 'Continue'
function Say([string]$t, [string]$c = 'Gray') { Write-Host $t -ForegroundColor $c }
function Ok([string]$t) { Say ("  [OK]  " + $t) 'Green' }
function Warn([string]$t) { Say ("  [!]   " + $t) 'Yellow' }
function Bad([string]$t) { Say ("  [X]   " + $t) 'Red' }
Say ""
Say "==============================================================" 'Cyan'
Say "   UPDATE GENESIS" 'Cyan'
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
Ok "Genesis folder: $root"
Set-Location -LiteralPath $root
$py = Join-Path $root '.venv\Scripts\python.exe'
$pyw = Join-Path $root '.venv\Scripts\pythonw.exe'
if (-not (Test-Path -LiteralPath $py)) {
    Bad "Genesis isn't installed in this folder yet (.venv is missing). Double-click setup.bat first, then run this again."
    return
}

# 1. unblock
try {
    Get-ChildItem -LiteralPath $root -Recurse -File -Include *.bat, *.ps1, *.py, *.cmd -ErrorAction SilentlyContinue |
        Where-Object { $_.FullName -notlike '*\.venv\*' } | Unblock-File -ErrorAction SilentlyContinue
    Ok "Removed the 'downloaded from the web' mark from Genesis's files."
} catch { Warn "Couldn't unblock files ($($_.Exception.Message))." }

# 2. close Genesis
try {
    $running = @(Get-CimInstance Win32_Process -ErrorAction Stop | Where-Object {
        ($_.Name -in @('python.exe', 'pythonw.exe')) -and ($_.CommandLine -match '-m\s+genesis') })
} catch { $running = @() }
foreach ($p in $running) { try { Stop-Process -Id $p.ProcessId -Force -ErrorAction Stop } catch {} }
if ($running.Count -gt 0) { Start-Sleep -Milliseconds 800; Ok "Closed the running Genesis." }

# 3. new package
Say "  Installing the new part (soundcard, for meeting minutes)..."
& $py -m pip install --disable-pip-version-check -q "soundcard>=0.4.3" 2>&1 | Out-Null
$sc = & $py -c "import soundcard; print('ok')" 2>&1 | Out-String
if ($sc -match 'ok') { Ok "Meeting audio capture is ready (computer sound + microphone)." }
elseif ($sc -match 'blocked|Application Control') { Warn "Windows Smart App Control blocks 'soundcard'. Meeting notes will record your microphone only." }
else { Warn "Couldn't install 'soundcard' (no internet?). Meeting notes will record your microphone only." }

# 4. image providers
$envPath = Join-Path $root '.env'
if (Test-Path -LiteralPath $envPath) {
    $lines = @([IO.File]::ReadAllLines($envPath))
    $has = $lines | Where-Object { $_ -match '^\s*GENESIS_IMAGE_PROVIDER\s*=' }
    if (-not $has) {
        Copy-Item -LiteralPath $envPath -Destination (Join-Path $root (".env.before-2.1-" + (Get-Date -Format 'yyyyMMdd-HHmmss'))) -Force
        $lines += ''
        $lines += '# 2.1: image generation tries Gemini, then Hugging Face, then the free Pollinations service (it receives the prompt).'
        $lines += 'GENESIS_IMAGE_PROVIDER=auto'
        [IO.File]::WriteAllLines($envPath, [string[]]$lines, (New-Object Text.UTF8Encoding $false))
        Ok "Image generation now has a free fallback (GENESIS_IMAGE_PROVIDER=auto)."
    } else {
        Ok "Image generation setting already present - left as it is."
    }
} else {
    Warn "No .env settings file found - run Fix_Genesis.bat to connect the AI model."
}

# 4b. smarter AI model: Gemini Flash (thinking off) instead of Flash-Lite, only if it really answers on this PC
if (Test-Path -LiteralPath $envPath) {
    $lines = @([IO.File]::ReadAllLines($envPath))
    $prov = ($lines | Where-Object { $_ -match '^\s*GENESIS_LLM_PROVIDER\s*=\s*gemini\s*$' })
    $lite = ($lines | Where-Object { $_ -match '^\s*LLM_MODEL\s*=\s*\S*lite' })
    if ($prov -and $lite) {
        Say "  Checking whether the smarter Gemini model works with your key..."
        $probe = @'
import os, sys, json, logging
sys.path.insert(0, os.getcwd())
logging.disable(logging.CRITICAL)
from genesis.core.config import GenesisConfig
from genesis.ai.llm.openai_compat import GeminiProvider
cfg = GenesisConfig.from_env()
ok = False
try:
    p = GeminiProvider(cfg.gemini_api_key, "gemini-flash-latest", timeout=40, retries=1)
    a = p.chat([{"role": "user", "content": "Reply with exactly: ready"}], max_tokens=20)
    b = p.chat_json([{"role": "user", "content": 'Return {"ok": true} as JSON.'}], max_tokens=60)
    ok = bool(a.strip()) and isinstance(b, dict)
except Exception as exc:
    print("ERR", type(exc).__name__)
print("SMART " + json.dumps({"ok": ok}))
'@
        $probePath = Join-Path ([IO.Path]::GetTempPath()) ("genesis_probe_" + [Guid]::NewGuid().ToString('N') + ".py")
        [IO.File]::WriteAllText($probePath, $probe, (New-Object Text.UTF8Encoding $false))
        $out = & $py -I $probePath 2>&1 | ForEach-Object { "$_" }
        Remove-Item -LiteralPath $probePath -ErrorAction SilentlyContinue
        if (($out -join "`n") -match 'SMART \{"ok": true\}') {
            Copy-Item -LiteralPath $envPath -Destination (Join-Path $root (".env.before-smart-model-" + (Get-Date -Format 'yyyyMMdd-HHmmss'))) -Force
            $lines = $lines | ForEach-Object { if ($_ -match '^\s*LLM_MODEL\s*=') { 'LLM_MODEL=gemini-flash-latest' } else { $_ } }
            [IO.File]::WriteAllLines($envPath, [string[]]$lines, (New-Object Text.UTF8Encoding $false))
            Ok "Switched to the smarter Gemini Flash model (thinking off, so replies stay quick)."
        } else {
            Warn "The smarter model didn't answer with your key - keeping the current one."
        }
    }
}

# 5. check + start
$ver = & $py -c "import genesis, genesis.v2.meet_tools, genesis.v2.voice_ai, genesis.v2.coding_tools; print(genesis.__version__)" 2>&1 | Out-String
if ($ver -match '\b2\.[1-9]\.\d') { Ok ("Genesis " + $ver.Trim() + " loads correctly.") } else { Bad "Genesis didn't load:"; Say $ver; return }
try {
    $desktop = [Environment]::GetFolderPath('Desktop')
    if ($desktop) {
        $ws = New-Object -ComObject WScript.Shell
        $s = $ws.CreateShortcut((Join-Path $desktop 'Genesis.lnk'))
        $s.TargetPath = $pyw
        $s.Arguments = '-m genesis'
        $s.WorkingDirectory = $root
        $s.Description = 'Start Genesis'
        $s.Save()
        Ok "Desktop shortcut 'Genesis' updated."
    }
} catch {}
$a = Read-Host "  Start Genesis now? [Y/n]"
if ([string]::IsNullOrWhiteSpace($a) -or $a.Trim().ToLower().StartsWith('y')) {
    try {
        Start-Process -FilePath $pyw -ArgumentList '-m', 'genesis' -WorkingDirectory $root
        Ok "Genesis 2.1 is starting - the icon appears near the clock in a few seconds."
    } catch { Bad ("Windows blocked starting Genesis: " + $_.Exception.Message) }
}
Say ""
Say "  Try:  'Hey Genesis, show me an image of a flying dragon'" 'Green'
Say "        'generate an image of a cat on the moon'" 'Green'
Say "        'let's chat'   (voice chat - 'stop voice chat' to end)" 'Green'
Say "        'start a new google meet'  then  'take meeting notes'  ...  'stop meeting notes'" 'Green'
Say "        'open google colab'  (sign in to Google in that Chrome window once)" 'Green'
Say "        in an online compiler: 'write a program for bubble sort and run it'" 'Green'
