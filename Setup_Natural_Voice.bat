<# : batch part (Windows runs this first; PowerShell treats it as a comment)
@echo off
title Genesis natural voice (Kokoro)
set "FIX_GENESIS_DIR=%~dp0"
set "FIX_GENESIS_BAT=%~f0"
powershell -NoProfile -ExecutionPolicy Bypass -Command "$s = [IO.File]::ReadAllText($env:FIX_GENESIS_BAT); & ([ScriptBlock]::Create($s))"
echo.
pause
exit /b
#>
# Installs Kokoro, a natural-sounding offline voice, for Genesis. One-time internet download (about 350 MB):
# the kokoro-onnx package and the Kokoro v1.0 model + voices from the kokoro-onnx GitHub release. After this, speech is
# generated entirely on this PC. Nothing is changed until you say yes; your TwinSim/other models are not touched.
$ErrorActionPreference = 'Continue'
$ProgressPreference = 'SilentlyContinue'
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
Say "   NATURAL VOICE FOR GENESIS  (Kokoro, offline)" 'Cyan'
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


$modelDir = Join-Path $root 'data\models\kokoro'
$files = @(
    @{ name = 'kokoro-v1.0.onnx'; url = 'https://github.com/thewh1teagle/kokoro-onnx/releases/download/model-files-v1.0/kokoro-v1.0.onnx'; sha = '7d5df8ecf7d4b1878015a32686053fd0eebe2bc377234608764cc0ef3636a6c5'; mb = 310 },
    @{ name = 'voices-v1.0.bin'; url = 'https://github.com/thewh1teagle/kokoro-onnx/releases/download/model-files-v1.0/voices-v1.0.bin'; sha = 'bca610b8308e8d99f32e6fe4197e7ec01679264efed0cac9140fe9c29f1fbf7d'; mb = 28 }
)
Say ""
Say "1. One-time download" 'Cyan'
Say "   - Python packages: kokoro-onnx, sounddevice (about 30 MB)"
Say "   - Kokoro v1.0 voice model and 26+ voices (about 340 MB) into data\models\kokoro"
Say "   After this, the voice works fully offline."
$drive = Get-PSDrive -Name ($root.Substring(0, 1)) -ErrorAction SilentlyContinue
if ($drive -and ($drive.Free / 1GB) -lt 1.5) { Bad ("Only {0:N1} GB free - free at least 1.5 GB first." -f ($drive.Free / 1GB)); return }
if (-not (YesNo "Download and install now?")) { Warn "Nothing changed."; return }

Say ""
Say "2. Installing packages" 'Cyan'
& $py -m pip install --disable-pip-version-check -q kokoro-onnx sounddevice
if ($LASTEXITCODE -ne 0) { Bad "pip couldn't install kokoro-onnx. Check your internet connection and run this again."; return }
Ok "kokoro-onnx installed."

Say ""
Say "3. Downloading the voice model" 'Cyan'
New-Item -ItemType Directory -Force -Path $modelDir | Out-Null
foreach ($f in $files) {
    $dest = Join-Path $modelDir $f.name
    if ((Test-Path -LiteralPath $dest) -and ((Get-Item -LiteralPath $dest).Length -gt ($f.mb * 0.9MB))) { Ok "$($f.name) already downloaded."; continue }
    Say ("   Downloading {0} ({1} MB)..." -f $f.name, $f.mb)
    try { Invoke-WebRequest -Uri $f.url -OutFile ($dest + '.part') -UseBasicParsing -ErrorAction Stop; Move-Item -Force ($dest + '.part') $dest }
    catch { Bad "Download failed: $($_.Exception.Message)"; return }
    $h = (Get-FileHash -LiteralPath $dest -Algorithm SHA256).Hash.ToLower()
    if ($h -eq $f.sha) { Ok "$($f.name) downloaded and verified." } else { Warn "$($f.name) downloaded, but its checksum differs from the published one ($h)." }
}

Say ""
Say "4. Voice test - listen" 'Cyan'
$test = @'
import sys, time
from genesis.v3.kokoro import KokoroTTS
from genesis.voice.tts import wait_until_quiet
voice = sys.argv[1] if len(sys.argv) > 1 else "friday"
t0 = time.perf_counter()
tts = KokoroTTS(voice=voice)
tts.speak(sys.argv[2] if len(sys.argv) > 2 else "Hey Boss. This is my new voice. Much better than the old robot, right?")
while not tts.speaking and time.perf_counter() - t0 < 30:
    time.sleep(0.02)
print("FIRST_AUDIO_S=%.2f" % (time.perf_counter() - t0))
wait_until_quiet(tts, 60)
tts.close()
'@
$testFile = Join-Path $env:TEMP 'genesis_kokoro_test.py'
[IO.File]::WriteAllText($testFile, $test)
Push-Location $root
$out = & $py $testFile friday 2>&1
Pop-Location
$first = @($out | Where-Object { "$_" -match 'FIRST_AUDIO_S=' }) | Select-Object -Last 1
if (-not $first) { Bad "The voice didn't start:"; $out | Select-Object -Last 6 | ForEach-Object { Say "      $_" }; return }
Ok ("Started speaking after " + ("$first".Split('=')[1]) + " s (includes loading the model).")
$voice = 'friday'
if (-not (YesNo "Did you hear a natural female voice (Genesis's own FRIDAY-style voice)?")) {
    Warn "If you heard nothing at all, run Fix_Voice_and_AI.bat to pick the right speaker, then run this again."
    return
}
if (YesNo "Hear the other voices and pick one?" $false) {
    $choices = @('friday', 'bf_isabella', 'bf_emma', 'af_heart', 'af_bella', 'bm_george')
    for ($i = 0; $i -lt $choices.Count; $i++) {
        Say ("   " + ($i + 1) + ". " + $choices[$i])
        Push-Location $root; & $py $testFile $choices[$i] ("Voice number " + ($i + 1) + ". Hello Boss, how does this sound?") *> $null; Pop-Location
    }
    $n = (Read-Host "  Which number do you like best? (Enter = 1)").Trim()
    if ($n -match '^[1-6]$') { $voice = $choices[[int]$n - 1] }
}

Say ""
Say "5. Switching Genesis to the natural voice" 'Cyan'
$envPath = Join-Path $root '.env'
$lines = @()
if (Test-Path -LiteralPath $envPath) {
    Copy-Item -LiteralPath $envPath -Destination (Join-Path $root (".env.before-voice-" + (Get-Date -Format 'yyyyMMdd-HHmmss'))) -Force
    $lines = @([IO.File]::ReadAllLines($envPath) | Where-Object { $_ -notmatch '^\s*(GENESIS_TTS|GENESIS_TTS_VOICE)\s*=' })
}
$lines += "GENESIS_TTS=kokoro"
$lines += "GENESIS_TTS_VOICE=$voice"
[IO.File]::WriteAllLines($envPath, [string[]]$lines, (New-Object Text.UTF8Encoding $false))
Ok "Voice set to $voice. (Set GENESIS_TTS=sapi in .env to go back to the Windows voice.)"
try {
    $running = @(Get-CimInstance Win32_Process -ErrorAction Stop | Where-Object {
        ($_.Name -in @('python.exe', 'pythonw.exe')) -and ($_.CommandLine -match '-m\s+genesis(\s|$)') })
} catch { $running = @() }
foreach ($p in $running) { try { Stop-Process -Id $p.ProcessId -Force -ErrorAction Stop } catch {} }
Start-Sleep -Milliseconds 800
Start-Process -FilePath $pyw -ArgumentList '-m', 'genesis' -WorkingDirectory $root
Ok "Genesis restarted with its new voice."
