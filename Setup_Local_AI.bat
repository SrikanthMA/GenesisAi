<# : batch part (Windows runs this first; PowerShell treats it as a comment)
@echo off
title Set up local AI for Genesis (Ollama)
set "FIX_GENESIS_DIR=%~dp0"
set "FIX_GENESIS_BAT=%~f0"
powershell -NoProfile -ExecutionPolicy Bypass -Command "$s = [IO.File]::ReadAllText($env:FIX_GENESIS_BAT); & ([ScriptBlock]::Create($s))"
echo.
pause
exit /b
#>
# Genesis 3.0 - local-first intelligence. Runs Genesis's AI on this PC with Ollama: no API keys, no tokens, no cloud.
#  1. installs / starts Ollama (asks first)
#  2. measures RAM, GPU memory and disk, and recommends models for Suzanne, Andrew and complex tasks
#  3. downloads only the models you agree to (shows sizes; checks disk space first)
#  4. optionally builds Genesis's own model (genesis-slm) - instant, no training
#  5. switches .env to local mode (backed up first), runs a self-test, restarts Genesis
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
Say "   LOCAL AI FOR GENESIS  (Ollama - runs on this PC, free)" 'Cyan'
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
Say "0. Connecting the 3.0 local-AI parts to your Genesis" 'Cyan'
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
Ok "3.0 is connected (backups of the changed files are in the backups folder)."
if (@($vout) -match 'COMMANDS_SHADOWED') { Warn "Another part of Genesis answers 'check my local AI setup' first; the AI itself still works." }
& $py -m unittest tests.test_v3 tests.test_v3_work 2>&1 | Select-Object -Last 3 | ForEach-Object { Say "      $_" }

# ---------------------------------------------------------------- 1. Ollama
Say ""
Say "1. Ollama (the program that runs local AI models)" 'Cyan'
function Find-Ollama {
    $cmd = Get-Command ollama -ErrorAction SilentlyContinue
    if ($cmd) { return $cmd.Source }
    foreach ($p in @((Join-Path $env:LOCALAPPDATA 'Programs\Ollama\ollama.exe'), 'C:\Program Files\Ollama\ollama.exe')) {
        if (Test-Path -LiteralPath $p) { return $p }
    }
    return $null
}
$ollama = Find-Ollama
if (-not $ollama) {
    Warn "Ollama isn't installed."
    $winget = Get-Command winget -ErrorAction SilentlyContinue
    if ($winget -and (YesNo "Install Ollama now with winget (about 1 GB)?")) {
        winget install --id Ollama.Ollama -e --accept-source-agreements --accept-package-agreements
        $env:Path = [Environment]::GetEnvironmentVariable('Path', 'Machine') + ';' + [Environment]::GetEnvironmentVariable('Path', 'User')
        $ollama = Find-Ollama
    }
    if (-not $ollama) {
        Warn "Install Ollama from the page that opens, then run this file again."
        Start-Process 'https://ollama.com/download/windows'
        return
    }
}
Ok "Ollama: $ollama"

function Test-OllamaUp {
    try { $v = Invoke-RestMethod -Uri 'http://127.0.0.1:11434/api/version' -TimeoutSec 3; return $v.version } catch { return $null }
}
$ver = Test-OllamaUp
if (-not $ver) {
    Say "      Starting Ollama..."
    $app = Join-Path (Split-Path -Parent $ollama) 'ollama app.exe'
    if (Test-Path -LiteralPath $app) { Start-Process -FilePath $app }
    else { Start-Process -FilePath $ollama -ArgumentList 'serve' -WindowStyle Hidden }
    for ($i = 0; $i -lt 30 -and -not $ver; $i++) { Start-Sleep -Seconds 1; $ver = Test-OllamaUp }
}
if (-not $ver) { Bad "Ollama didn't start. Open the Ollama app from the Start menu, then run this file again."; return }
Ok "Ollama $ver is running."

# ---------------------------------------------------------------- 2. hardware + recommendation
Say ""
Say "2. Checking this PC" 'Cyan'
$probe = @'
import json
from genesis.v3 import catalog, hardware
from genesis.v3.ollama import OllamaClient
h = hardware.detect()
r = catalog.recommend(h)
r["summary"] = h.summary()
r["installed"] = sorted(OllamaClient().installed_names())
r["sizes"] = {m.name: m.download_gb for m in catalog.CATALOG}
print("RESULT:" + json.dumps(r))
'@
$out = $probe | & $py - 2>&1
$line = @($out | Where-Object { "$_".StartsWith('RESULT:') }) | Select-Object -Last 1
if (-not $line) { Bad "The hardware check failed:"; $out | Select-Object -Last 8 | ForEach-Object { Say "      $_" }; return }
$rec = "$line".Substring(7) | ConvertFrom-Json
Ok $rec.summary
Say ""
Say "   Recommended for this PC:" 'White'
Say ("     Suzanne (conversation) : " + $rec.suzanne)
Say ("     Andrew  (technical)    : " + $rec.andrew)
if ($rec.complex) { Say ("     Complex tasks (optional): " + $rec.complex) } else { Say "     Complex tasks          : Andrew's model (a 14B model won't fit)" }
foreach ($n in $rec.notes) { Warn $n }

$installed = @($rec.installed)
function Need([string]$m) { return -not (($installed -contains $m) -or ($installed -contains ($m + ':latest'))) }
$toPull = @()
foreach ($m in @($rec.suzanne, $rec.andrew) | Select-Object -Unique) { if (Need $m) { $toPull += $m } else { Ok "$m is already installed." } }
$complex = ''
if ($rec.complex) {
    if (-not (Need $rec.complex)) { $complex = $rec.complex; Ok "$($rec.complex) is already installed." }
    elseif (YesNo ("Also download " + $rec.complex + " for complex tasks (about " + $rec.sizes.($rec.complex) + " GB; slower, loaded only when needed)?") $false) {
        $toPull += $rec.complex; $complex = $rec.complex
    }
}

# ---------------------------------------------------------------- 3. downloads
Say ""
Say "3. Models" 'Cyan'
if ($toPull.Count -gt 0) {
    $total = 0.0; foreach ($m in $toPull) { $total += [double]$rec.sizes.$m }
    $drive = $null
    try {
        $mdir = if ($env:OLLAMA_MODELS) { $env:OLLAMA_MODELS } else { Join-Path $env:USERPROFILE '.ollama' }
        $drive = Get-PSDrive -Name ((Split-Path -Qualifier $mdir).TrimEnd(':')) -ErrorAction Stop
    } catch {}
    $freeGb = if ($drive) { [math]::Round($drive.Free / 1GB, 1) } else { $null }
    Say ("   To download: " + ($toPull -join ', ') + "  (about " + [math]::Round($total, 1) + " GB)")
    if ($freeGb -ne $null) {
        Say "   Free disk space: $freeGb GB"
        if ($freeGb -lt ($total + 5)) { Bad "Not enough free disk space. Free some space (or set OLLAMA_MODELS to another drive) and run this again."; return }
    }
    if (-not (YesNo "Download now?")) { Warn "Nothing downloaded. Run this file again when you're ready."; return }
    foreach ($m in $toPull) {
        Say ""
        Say "   Downloading $m ..." 'White'
        & $ollama pull $m
        if ($LASTEXITCODE -ne 0) { Bad "Downloading $m failed. Check your internet connection and run this again."; return }
        Ok "$m downloaded."
    }
} else { Ok "All recommended models are already installed." }

# ---------------------------------------------------------------- 4. Genesis SLM
Say ""
Say "4. Genesis's own model (genesis-slm)" 'Cyan'
Say "   Built from $($rec.suzanne) with Genesis's identity and your approved examples. Instant, no training."
$suzModel = $rec.suzanne
if (YesNo "Build genesis-slm and use it for Suzanne?") {
    $env:GENESIS_FIX_BASE = $rec.suzanne
    $build = @'
import json, os
from genesis.core.config import GenesisConfig
from genesis.v3.provider import runtime
from genesis.v3.slm import SLMManager
cfg = GenesisConfig.from_env()
rt = runtime(cfg)
mgr = SLMManager(rt["store"], rt["client"], cfg.paths.data_dir / "genesis-slm")
res = mgr.build(os.environ["GENESIS_FIX_BASE"])
if res["ok"]:
    rep = mgr.evaluate_version(res["version"]) or {}
    act = mgr.activate(res["version"])
    res.update(score=rep.get("score"), active=act["ok"], detail=act["detail"])
print("RESULT:" + json.dumps(res))
'@
    $out = $build | & $py - 2>&1
    $line = @($out | Where-Object { "$_".StartsWith('RESULT:') }) | Select-Object -Last 1
    Remove-Item Env:GENESIS_FIX_BASE -ErrorAction SilentlyContinue
    if ($line) {
        $res = "$line".Substring(7) | ConvertFrom-Json
        if ($res.ok -and $res.active) { Ok ("genesis-slm version " + $res.version + " built (self-check score " + $res.score + "%)."); $suzModel = 'genesis-slm' }
        else { Warn ("genesis-slm wasn't built: " + $res.detail + "  Suzanne will use " + $rec.suzanne + ".") }
    } else { Warn "genesis-slm wasn't built (see below). Suzanne will use $($rec.suzanne)."; $out | Select-Object -Last 5 | ForEach-Object { Say "      $_" } }
}

# ---------------------------------------------------------------- 5. .env
Say ""
Say "5. Switching Genesis to local mode" 'Cyan'
$envPath = Join-Path $root '.env'
$keys = 'GENESIS_MODE|GENESIS_LLM_PROVIDER|GENESIS_EXTERNAL_AI_ENABLED|GENESIS_MEMORY_MODE|GENESIS_CLOUD_VOICE|SUZANNE_MODEL|ANDREW_MODEL|GENESIS_COMPLEX_MODEL'
$lines = @()
if (Test-Path -LiteralPath $envPath) {
    $backup = Join-Path $root (".env.before-local-ai-" + (Get-Date -Format 'yyyyMMdd-HHmmss'))
    Copy-Item -LiteralPath $envPath -Destination $backup -Force
    Ok "Backed up .env to $(Split-Path -Leaf $backup)"
    $lines = @([IO.File]::ReadAllLines($envPath) | Where-Object { $_ -notmatch "^\s*($keys)\s*=" -and $_ -notmatch '^# Local AI \(Genesis 3\.0\)' })
}
$lines += ''
$lines += '# Local AI (Genesis 3.0) - set by Setup_Local_AI.bat. Set GENESIS_MODE=hybrid to go back to your cloud AI.'
$lines += 'GENESIS_MODE=local'
$lines += 'GENESIS_LLM_PROVIDER=genesis'
$lines += 'GENESIS_EXTERNAL_AI_ENABLED=false'
$lines += 'GENESIS_MEMORY_MODE=local'
$lines += 'GENESIS_CLOUD_VOICE=true'
$lines += "SUZANNE_MODEL=$suzModel"
$lines += "ANDREW_MODEL=$($rec.andrew)"
$lines += "GENESIS_COMPLEX_MODEL=$complex"
[IO.File]::WriteAllLines($envPath, [string[]]$lines, (New-Object Text.UTF8Encoding $false))
Ok "Local mode is on. Chat runs on this PC; voice keeps its usual engine (GENESIS_CLOUD_VOICE=true)."

# ---------------------------------------------------------------- 6. self-test + restart
Say ""
Say "6. Self-test (first answers are slower while models load)" 'Cyan'
$env:GENESIS_MODE = 'local'; $env:GENESIS_LLM_PROVIDER = 'genesis'; $env:SUZANNE_MODEL = $suzModel; $env:ANDREW_MODEL = $rec.andrew
& $py -m genesis.v3.selftest
if ($LASTEXITCODE -ne 0) { Warn "Some checks failed - see above. Genesis will still start; it shows a notice if a model can't answer." }

try {
    $running = @(Get-CimInstance Win32_Process -ErrorAction Stop | Where-Object {
        ($_.Name -in @('python.exe', 'pythonw.exe')) -and ($_.CommandLine -match '-m\s+genesis(\s|$)') })
} catch { $running = @() }
foreach ($p in $running) { try { Stop-Process -Id $p.ProcessId -Force -ErrorAction Stop } catch {} }
Start-Sleep -Milliseconds 800
try {
    Start-Process -FilePath $pyw -ArgumentList '-m', 'genesis' -WorkingDirectory $root
    Ok "Genesis restarted in local mode."
} catch { Warn "Start Genesis from the desktop icon." }
Say ""
Say "  Try:  'check my local AI setup'   'which model answered?'   'write a python function to sort a list'" 'Green'
