<# : batch part (Windows runs this first; PowerShell treats it as a comment)
@echo off
title Update Genesis to 3.3.0
set "FIX_GENESIS_DIR=%~dp0"
set "FIX_GENESIS_BAT=%~f0"
powershell -NoProfile -ExecutionPolicy Bypass -Command "$s = [IO.File]::ReadAllText($env:FIX_GENESIS_BAT); & ([ScriptBlock]::Create($s))"
echo.
pause
exit /b
#>
# Genesis 3.3.0 update: conversation modes and natural-conversation rules, project intelligence (Git),
# Mission Mode, emergency stop, specialist (TwinSim) model routing, Kokoro natural voice support, model benchmark.
# Also includes: Friday-style voice (female voice, On it / Done Boss, greeting, typed replies spoken),
# voice back on in local mode, digital twin (your preferences + answer-as-me), Wikipedia -> Word,
# documents and IEEE papers, Excel sheets, code in VS Code / Colab. Run after extracting the zip into the Genesis folder.
$ErrorActionPreference = 'Continue'
function Say([string]$t, [string]$c = 'Gray') { Write-Host $t -ForegroundColor $c }
function Ok([string]$t) { Say ("  [OK]  " + $t) 'Green' }
function Warn([string]$t) { Say ("  [!]   " + $t) 'Yellow' }
function Bad([string]$t) { Say ("  [X]   " + $t) 'Red' }
function YesNo([string]$q, [bool]$def = $true) {
    $hint = if ($def) { '[Y/n]' } else { '[y/N]' }
    $a = (Read-Host "  $q $hint").Trim().ToLower()
    if (-not $a) { return $def }
    return $a.StartsWith('y')
}
Say ""
Say "==============================================================" 'Cyan'
Say "   GENESIS 3.3 UPDATE" 'Cyan'
Say "==============================================================" 'Cyan'

# ---------------------------------------------------------------- Genesis folder
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
Get-ChildItem -LiteralPath $root -Filter *.bat -ErrorAction SilentlyContinue | Unblock-File -ErrorAction SilentlyContinue
Ok "Genesis folder: $root"
$py = Join-Path $root '.venv\Scripts\python.exe'
$pyw = Join-Path $root '.venv\Scripts\pythonw.exe'
if (-not (Test-Path -LiteralPath $py)) { Bad "Genesis isn't installed here (.venv missing). Run setup.bat first."; return }
if (-not (Test-Path -LiteralPath (Join-Path $root 'genesis\v3\router.py'))) { Bad "genesis\v3 is missing. Extract the Genesis 3.0 update zip into this folder first."; return }

# ---------------------------------------------------------------- 0. connect 3.0 to this Genesis
Say ""
Say "Connecting the 3.3.0 parts to your Genesis" 'Cyan'
& $py (Join-Path $root 'scripts\apply_v3_hooks.py')
$hookCode = $LASTEXITCODE
$verify = @'
from genesis.core.config import GenesisConfig, PROVIDER_ALIASES
from genesis.tools.registry import build_default_registry
from genesis.nlp import rules
assert PROVIDER_ALIASES.get("genesis") == "genesis", "provider alias"
assert build_default_registry().get("slm_build") is not None, "tools"
import genesis.core.container, genesis.core.assistant
print("VERIFY:OK")
r = rules.parse("check my local AI setup")
if r is None or r.slots.get("tool") != "local_ai_status":
    print("VERIFY:COMMANDS_SHADOWED")
'@
$vout = $verify | & $py - 2>&1
if (($hookCode -ne 0) -or -not (@($vout) -match 'VERIFY:OK')) {
    Bad "Genesis didn't load correctly with the 3.0 hooks. Undoing them so Genesis keeps working as before:"
    $vout | Select-Object -Last 6 | ForEach-Object { Say "      $_" }
    & $py (Join-Path $root 'scripts\apply_v3_hooks.py') --remove | Out-Null
    Warn "Nothing else was changed. Send me the lines above."
    return
}
Ok "3.3.0 is connected (backups of the changed files are in the backups folder)."
if (@($vout) -match 'COMMANDS_SHADOWED') { Warn "Another part of Genesis answers 'check my local AI setup' first; the AI itself still works." }
& $py -m unittest tests.test_v3 tests.test_v3_work tests.test_v3_friday tests.test_v3_jarvis tests.test_v3_comms tests.test_v3_jarvis_style 2>&1 | Select-Object -Last 3 | ForEach-Object { Say "      $_" }


# ---------------------------------------------------------------- settings
Say ""
Say "Settings" 'Cyan'
$envPath = Join-Path $root '.env'
if (Test-Path -LiteralPath $envPath) {
    Copy-Item -LiteralPath $envPath -Destination (Join-Path $root (".env.before-3.3.0-" + (Get-Date -Format 'yyyyMMdd-HHmmss'))) -Force
    $lines = @([IO.File]::ReadAllLines($envPath) | Where-Object { $_ -notmatch '^\s*(GENESIS_CLOUD_VOICE|GENESIS_FRIDAY|GENESIS_SPEAK_TYPED|GENESIS_STREAM_SPEECH|GENESIS_ASSEMBLE_SPEECH)\s*=' })
    $isLocal = @($lines | Where-Object { $_ -match '^\s*GENESIS_MODE\s*=\s*local' }).Count -gt 0
    if ($isLocal) {
        $lines = @($lines | ForEach-Object { if ($_ -match '^\s*GENESIS_EXTERNAL_AI_ENABLED\s*=') { 'GENESIS_EXTERNAL_AI_ENABLED=false' } else { $_ } })
    }
    $lines += 'GENESIS_CLOUD_VOICE=false'
    $lines += 'GENESIS_FRIDAY=true'
    $lines += 'GENESIS_SPEAK_TYPED=true'
    $lines += 'GENESIS_STREAM_SPEECH=true'
    $lines += 'GENESIS_ASSEMBLE_SPEECH=true'
    [IO.File]::WriteAllLines($envPath, [string[]]$lines, (New-Object Text.UTF8Encoding $false))
    Ok "Fully offline: chat and speech recognition run on this PC (GENESIS_CLOUD_VOICE=false). Friday mode on."
} else { Warn ".env not found - run Setup_Local_AI.bat first." }

# ---------------------------------------------------------------- report
$logDir = Join-Path $root 'logs'
New-Item -ItemType Directory -Force -Path $logDir | Out-Null
$report = Join-Path $logDir 'genesis3_diagnose.txt'
$env:GENESIS_CLOUD_VOICE = 'false'
& $py -m genesis.v3.diagnose 2>&1 | Out-File -FilePath $report -Encoding utf8
& $py -m unittest tests.test_v3 tests.test_v3_work tests.test_v3_friday tests.test_v3_jarvis tests.test_v3_comms tests.test_v3_jarvis_style 2>&1 | Select-Object -Last 60 | Out-File -FilePath $report -Append -Encoding utf8
Get-Content -LiteralPath $report | Select-Object -Last 4 | ForEach-Object { Say "      $_" }
Ok "Report saved to logs\genesis3_diagnose.txt (no keys or personal data)."

try {
    $running = @(Get-CimInstance Win32_Process -ErrorAction Stop | Where-Object {
        ($_.Name -in @('python.exe', 'pythonw.exe')) -and ($_.CommandLine -match '-m\s+genesis(\s|$)') })
} catch { $running = @() }
foreach ($p in $running) { try { Stop-Process -Id $p.ProcessId -Force -ErrorAction Stop } catch {} }
Start-Sleep -Milliseconds 800
Say ""
Say "Optional next steps" 'Cyan'
if (YesNo "Install the natural Kokoro voice now (one-time ~350 MB download)?") {
    & cmd /c (Join-Path $root 'Setup_Natural_Voice.bat')
}
if (YesNo "Upgrade speech recognition to Whisper small.en (more accurate; one-time ~480 MB download; runs on the CPU so the GPU stays free for the AI)?" $false) {
    $dl = @'
import sys
from pathlib import Path
from genesis.voice.stt import FasterWhisperSTT
FasterWhisperSTT("small.en", Path(sys.argv[1]) / "data" / "models" / "whisper")
print("WHISPER:OK")
'@
    $wout = $dl | & $py - $root 2>&1
    if (@($wout) -match 'WHISPER:OK') {
        $lines = @([IO.File]::ReadAllLines($envPath) | Where-Object { $_ -notmatch '^\s*GENESIS_WHISPER_MODEL\s*=' })
        $lines += 'GENESIS_WHISPER_MODEL=small.en'
        [IO.File]::WriteAllLines($envPath, [string[]]$lines, (New-Object Text.UTF8Encoding $false))
        Ok "Speech recognition upgraded to Whisper small.en."
    } else { Warn "The download didn't finish - speech recognition stays on Whisper base."; $wout | Select-Object -Last 3 | ForEach-Object { Say "      $_" } }
}
if (YesNo "Benchmark your local models now (about 2 minutes per model; saved in logs)?" $false) {
    $ollama = Join-Path $env:LOCALAPPDATA 'Programs\Ollama\ollama.exe'
    if ((Test-Path -LiteralPath $ollama) -and (YesNo "Also download gemma3:4b (3.3 GB) to compare it as Suzanne's model?" $false)) { & $ollama pull gemma3:4b }
    Push-Location $root
    & $py -m genesis.v3.bench --label after-3.1
    Pop-Location
}
try {
    Start-Process -FilePath $pyw -ArgumentList '-m', 'genesis' -WorkingDirectory $root
    Ok "Genesis restarted."
} catch { Warn "Start Genesis from the desktop icon." }
Say ""
Say "  Try:  'what did I work on yesterday'   'check the latest changes in genesis'   'summarize my genesis project'" 'Green'
Say "        'switch to coding mode'   'what should I work on today'   'stop everything'" 'Green'
Say "        'my genesis project is due tomorrow - check it and prepare a report'" 'Green'
Say "        'fix the failing tests in twinsim'   'undo the last fixes'" 'Green'
