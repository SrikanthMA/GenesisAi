<# : batch part (Windows runs this first; PowerShell treats it as a comment)
@echo off
title Fix Genesis voice and local AI
set "FIX_GENESIS_DIR=%~dp0"
set "FIX_GENESIS_BAT=%~f0"
powershell -NoProfile -ExecutionPolicy Bypass -Command "$s = [IO.File]::ReadAllText($env:FIX_GENESIS_BAT); & ([ScriptBlock]::Create($s))"
echo.
pause
exit /b
#>
# Finds the sound output you can actually hear (laptop speakers / headphones instead of HDMI, NVIDIA or Nahimic
# virtual devices), unmutes it, sets the volume, makes it the Windows default, then restarts Genesis so it talks.
# It asks you "did you hear it?" for each output - nothing is changed without your answer.
$ErrorActionPreference = 'Continue'
function Say([string]$t, [string]$c = 'Gray') { Write-Host $t -ForegroundColor $c }
function Ok([string]$t) { Say ("  [OK]  " + $t) 'Green' }
function Warn([string]$t) { Say ("  [!]   " + $t) 'Yellow' }
function Bad([string]$t) { Say ("  [X]   " + $t) 'Red' }
function Heard([string]$q) {
    $a = (Read-Host "  $q [y/n]").Trim().ToLower()
    return $a.StartsWith('y')
}
Add-Type -AssemblyName System.Speech
function SpeakNow([string]$text) {
    $sp = New-Object System.Speech.Synthesis.SpeechSynthesizer
    try { $sp.SelectVoiceByHints([System.Speech.Synthesis.VoiceGender]::Female) } catch {}
    $sp.Volume = 100
    $sp.SetOutputToDefaultAudioDevice()
    $sp.Speak($text)
    $sp.Dispose()
}
Say ""
Say "==============================================================" 'Cyan'
Say "   FIX GENESIS VOICE + LOCAL AI" 'Cyan'
Say "==============================================================" 'Cyan'

# ---------------------------------------------------------------- 0. Ollama (the local AI) running + starting with Windows
Say ""
Say "0. Local AI (Ollama)" 'Cyan'
$ollamaDir = Join-Path $env:LOCALAPPDATA 'Programs\Ollama'
$app = Join-Path $ollamaDir 'ollama app.exe'
$cli = Join-Path $ollamaDir 'ollama.exe'
function Test-OllamaUp { try { (Invoke-RestMethod -Uri 'http://127.0.0.1:11434/api/version' -TimeoutSec 3).version } catch { $null } }
$ver = Test-OllamaUp
if (-not $ver) {
    if (Test-Path -LiteralPath $app) { Start-Process -FilePath $app }
    elseif (Test-Path -LiteralPath $cli) { Start-Process -FilePath $cli -ArgumentList 'serve' -WindowStyle Hidden }
    else { Bad "Ollama isn't installed - run Setup_Local_AI.bat." }
    for ($i = 0; $i -lt 30 -and -not $ver; $i++) { Start-Sleep -Seconds 1; $ver = Test-OllamaUp }
}
if ($ver) { Ok "Ollama $ver is running." } else { Bad "Ollama didn't start. Open 'Ollama' from the Start menu, then run this again." }
if (Test-Path -LiteralPath $app) {
    $startup = [Environment]::GetFolderPath('Startup')
    $lnk = Join-Path $startup 'Ollama.lnk'
    if (-not (Test-Path -LiteralPath $lnk)) {
        try {
            $sc = (New-Object -ComObject WScript.Shell).CreateShortcut($lnk)
            $sc.TargetPath = $app; $sc.WorkingDirectory = $ollamaDir; $sc.Save()
            Ok "Ollama will now start automatically when you sign in to Windows."
        } catch { Warn "Couldn't add Ollama to startup." }
    } else { Ok "Ollama already starts with Windows." }
}
Say "      (Genesis 3.0.3 also starts Ollama by itself if it ever finds it stopped.)"
Say ""
Say "  Turn your laptop volume up a bit and take off / connect your headphones as you normally use them."
Say ""

# ---------------------------------------------------------------- 1. unmute + volume up with the media keys
Say "1. Unmuting and raising the volume" 'Cyan'
try {
    $wsh = New-Object -ComObject WScript.Shell
    $wsh.SendKeys([char]173); Start-Sleep -Milliseconds 300   # mute toggle
    $wsh.SendKeys([char]175)                                  # volume up (also unmutes)
    for ($i = 0; $i -lt 10; $i++) { $wsh.SendKeys([char]175); Start-Sleep -Milliseconds 40 }
    Ok "Volume raised."
} catch { Warn "Couldn't press the volume keys - turn the volume up yourself." }
SpeakNow "Hello Boss. This is Genesis. Can you hear me?"
if (Heard "Did you hear 'Hello Boss, this is Genesis'?") {
    $fixed = $true
} else {
    $fixed = $false
    # ------------------------------------------------------------ 2. try each sound output
    Say ""
    Say "2. Trying each sound output on this PC" 'Cyan'
    $haveModule = $false
    try {
        if (-not (Get-Module -ListAvailable -Name AudioDeviceCmdlets)) {
            Say "      Installing the free 'AudioDeviceCmdlets' helper (one time, from the PowerShell Gallery)..."
            if (-not (Get-PackageProvider -ListAvailable -Name NuGet -ErrorAction SilentlyContinue)) {
                Install-PackageProvider -Name NuGet -MinimumVersion 2.8.5.201 -Scope CurrentUser -Force | Out-Null
            }
            Install-Module -Name AudioDeviceCmdlets -Scope CurrentUser -Force -AllowClobber -ErrorAction Stop
        }
        Import-Module AudioDeviceCmdlets -ErrorAction Stop
        $haveModule = $true
    } catch { Warn "Couldn't install the helper ($($_.Exception.Message))." }

    if ($haveModule) {
        $devices = @(Get-AudioDevice -List | Where-Object { $_.Type -eq 'Playback' })
        $original = $devices | Where-Object { $_.Default } | Select-Object -First 1
        # real speakers / headphones first, HDMI / virtual devices last
        $ordered = @($devices | Sort-Object { if ($_.Name -match 'Realtek|Speaker|Headphone|AMD') { 0 } elseif ($_.Name -match 'Nahimic|NVIDIA|HDMI|Virtual|Display') { 2 } else { 1 } })
        Say ("      Found " + $ordered.Count + " outputs:")
        $ordered | ForEach-Object { Say ("        - " + $_.Name + $(if ($_.Default) { '   (current default)' } else { '' })) }
        $n = 0
        foreach ($d in $ordered) {
            $n++
            Say ""
            Say ("   Trying " + $n + " of " + $ordered.Count + ": " + $d.Name) 'White'
            try {
                Set-AudioDevice -ID $d.ID | Out-Null
                Set-AudioDevice -PlaybackMute $false | Out-Null
                Set-AudioDevice -PlaybackVolume 70 | Out-Null
            } catch { Warn "Couldn't switch to it."; continue }
            SpeakNow ("Speaker number " + $n + ". Hello Boss, can you hear me?")
            if (Heard ("Did you hear 'Speaker number " + $n + "'?")) {
                Ok ("Using '" + $d.Name + "' as your speaker from now on.")
                $fixed = $true
                break
            }
        }
        if (-not $fixed -and $original) { Set-AudioDevice -ID $original.ID | Out-Null; Warn "Put your original sound output back." }
    } else {
        Say ""
        Say "   Opening Sound settings: click your Speakers / Headphones, press 'Set Default', then 'Test'." 'White'
        Start-Process mmsys.cpl
        [void](Read-Host "  Press Enter when you've done that")
        SpeakNow "Hello Boss. Can you hear me now?"
        $fixed = Heard "Did you hear 'Hello Boss, can you hear me now'?"
    }
}

if (-not $fixed) {
    Say ""
    Bad "No output played the voice. This is a Windows / driver sound problem, not Genesis:"
    Say "      - Open the Nahimic app (if installed) and make sure audio isn't muted or disabled there."
    Say "      - Does YouTube make sound in your browser? If not, update the Realtek audio driver from MSI's support site."
    Say "      - Try headphones in the headphone jack, then run this file again."
    return
}

# ---------------------------------------------------------------- 3. make sure Genesis will speak, restart it
Say ""
Say "3. Restarting Genesis with its voice on" 'Cyan'
$candidates = @()
if ($env:FIX_GENESIS_DIR) { $candidates += $env:FIX_GENESIS_DIR.TrimEnd('\', '/') }
if ($env:USERPROFILE) { $candidates += (Join-Path $env:USERPROFILE 'OneDrive\Documents\Genesis'); $candidates += (Join-Path $env:USERPROFILE 'Documents\Genesis') }
$root = $null
foreach ($c in $candidates) { if ($c -and (Test-Path -LiteralPath (Join-Path $c 'genesis\__main__.py'))) { $root = (Resolve-Path -LiteralPath $c).Path; break } }
if (-not $root) { Warn "Couldn't find the Genesis folder - start Genesis yourself."; return }
$py = Join-Path $root '.venv\Scripts\python.exe'
$pyw = Join-Path $root '.venv\Scripts\pythonw.exe'
$on = @'
from genesis.core.config import GenesisConfig
from genesis.database.db import Database
from genesis.database.repositories import Repositories
cfg = GenesisConfig.from_env()
db = Database(cfg.paths.db_path)
r = Repositories(db)
r.settings.set("voice.tts_enabled", True)
print("TTS:ON")
'@
$out = $on | & $py - 2>&1
if (@($out) -match 'TTS:ON') { Ok "Voice replies are switched on in Genesis." } else { Warn "Couldn't check Genesis's voice setting - say 'turn voice replies on' in Genesis." }
try {
    $running = @(Get-CimInstance Win32_Process -ErrorAction Stop | Where-Object {
        ($_.Name -in @('python.exe', 'pythonw.exe')) -and ($_.CommandLine -match '-m\s+genesis(\s|$)') })
} catch { $running = @() }
foreach ($p in $running) { try { Stop-Process -Id $p.ProcessId -Force -ErrorAction Stop } catch {} }
Start-Sleep -Milliseconds 800
Start-Process -FilePath $pyw -ArgumentList '-m', 'genesis' -WorkingDirectory $root
Ok "Genesis restarted - it should greet you in a few seconds."
Say ""
Say "  Then say or type:  'open notepad'   ->   'On it, Boss.' ... 'Done, Boss ...'" 'Green'
