<# : batch part (Windows runs this first; PowerShell treats it as a comment)
@echo off
title Fix Genesis
set "FIX_GENESIS_DIR=%~dp0"
set "FIX_GENESIS_BAT=%~f0"
powershell -NoProfile -ExecutionPolicy Bypass -Command "$s = [IO.File]::ReadAllText($env:FIX_GENESIS_BAT); & ([ScriptBlock]::Create($s))"
echo.
pause
exit /b
#>
# =====================================================================================================================
#  Fix Genesis - switches Genesis's AI model to Google Gemini (free) and repairs the settings file.
#
#  What it does, in order:
#    1. finds your Genesis folder
#    2. repairs .env if an earlier attempt broke it (restores your backup)
#    3. makes a new backup of .env
#    4. asks for your Gemini key (typing is hidden) and TESTS it with Google using Genesis's own code
#    5. picks a Gemini model that actually answers
#    6. saves the settings, checks that Genesis reads them, offers a desktop shortcut and starts Genesis
#
#  Your key is only written to the .env file in your Genesis folder. It is never shown, logged or sent anywhere
#  except to Google (generativelanguage.googleapis.com) for the test.
# =====================================================================================================================
$ErrorActionPreference = 'Stop'
$TestMode = ($env:GENESIS_FIX_TEST -eq '1')   # used only for automated testing of this script

function Say([string]$text, [string]$color = 'Gray') { Write-Host $text -ForegroundColor $color }
function Ok([string]$text)   { Say ("  [OK]  " + $text) 'Green' }
function Warn([string]$text) { Say ("  [!]   " + $text) 'Yellow' }
function Bad([string]$text)  { Say ("  [X]   " + $text) 'Red' }
function Step([string]$text) { Say ""; Say $text 'Cyan' }

function Ask-YesNo([string]$question, [bool]$default = $true) {
    if ($TestMode) { return $default }
    $hint = if ($default) { '[Y/n]' } else { '[y/N]' }
    $a = Read-Host ("  " + $question + " " + $hint)
    if ([string]::IsNullOrWhiteSpace($a)) { return $default }
    return $a.Trim().ToLower().StartsWith('y')
}

function Read-Secret([string]$prompt) {
    if ($TestMode) { return $env:GENESIS_FIX_TEST_KEY }
    $sec = Read-Host -Prompt $prompt -AsSecureString
    $ptr = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($sec)
    try { return [Runtime.InteropServices.Marshal]::PtrToStringBSTR($ptr) }
    finally { [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($ptr) }
}

function Clean-Key([string]$k) {
    if ($null -eq $k) { return '' }
    $k = $k.Trim().Trim('"', "'").Trim()
    if ($k.ToLower().StartsWith('bearer ')) { $k = $k.Substring(7).Trim() }
    if ($k.StartsWith('GEMINI_API_KEY=')) { $k = $k.Substring(15).Trim() }
    return $k
}

function Key-Problem([string]$k) {
    if ([string]::IsNullOrWhiteSpace($k)) { return 'nothing was pasted' }
    if ($k.StartsWith('$') -or $k -match 'Get-Content|Where-Object|Read-Host|\.env') { return 'that is a PowerShell command, not a key' }
    if ($k -match '\s') { return 'it contains spaces - copy only the key itself' }
    if ($k.Length -lt 20) { return 'it is too short to be an API key' }
    return $null
}

function Run-Py([string[]]$argList) {
    $old = $ErrorActionPreference; $ErrorActionPreference = 'Continue'
    try { return ((& $py @argList 2>&1 | ForEach-Object { "$_" }) -join "`n") }
    finally { $ErrorActionPreference = $old }
}

function Mask([string]$k) { if ($k.Length -le 8) { return '****' } return $k.Substring(0, 4) + '...' + $k.Substring($k.Length - 4) }

function Read-Lines([string]$path) {
    if (-not (Test-Path -LiteralPath $path)) { return @() }
    return @([IO.File]::ReadAllLines($path))   # detects a BOM, defaults to UTF-8
}

function Write-Lines([string]$path, $lines) {
    [IO.File]::WriteAllLines($path, [string[]]$lines, (New-Object Text.UTF8Encoding $false))
}

function Get-EnvValue($lines, [string]$name) {
    foreach ($l in $lines) {
        if ($l -match ('^\s*(?:export\s+)?' + [regex]::Escape($name) + '\s*=\s*(.*)$')) {
            return (Clean-Key $Matches[1])
        }
    }
    return ''
}

function Test-EnvBroken($lines) {
    $real = @($lines | Where-Object { $_.Trim() -ne '' })
    if ($real.Count -lt 8) { return $true }   # the original file has ~100 lines; a 3-line file was overwritten
    foreach ($l in $lines) {
        if ($l -match '^\s*[A-Z_]+\s*=.*(\$lines|\$k\b|Get-Content|Where-Object|Read-Host)') { return $true }
    }
    return $false
}

Say ""
Say "==============================================================" 'Cyan'
Say "   FIX GENESIS  -  connect Genesis to Google Gemini (free)" 'Cyan'
Say "==============================================================" 'Cyan'

# ------------------------------------------------------------------------------------------------ 1. find Genesis
Step "1/6  Finding your Genesis folder"
$candidates = @()
if ($env:FIX_GENESIS_DIR) { $candidates += $env:FIX_GENESIS_DIR.TrimEnd('\', '/') }
$docs = [Environment]::GetFolderPath('MyDocuments')
if ($docs) { $candidates += (Join-Path $docs 'Genesis') }
if ($env:USERPROFILE) {
    $candidates += (Join-Path $env:USERPROFILE 'OneDrive\Documents\Genesis')
    $candidates += (Join-Path $env:USERPROFILE 'Documents\Genesis')
    $candidates += (Join-Path $env:USERPROFILE 'Desktop\Genesis')
    $candidates += (Join-Path $env:USERPROFILE 'Downloads\Genesis')
}
$root = $null
foreach ($c in $candidates) {
    if ($c -and (Test-Path -LiteralPath (Join-Path $c 'genesis\__main__.py'))) { $root = (Resolve-Path -LiteralPath $c).Path; break }
}
while (-not $root) {
    if ($TestMode) { Bad "Genesis folder not found."; return }
    Warn "I couldn't find the Genesis folder automatically."
    $p = Read-Host "  Paste the full path of your Genesis folder (the one with run_genesis.bat)"
    $p = $p.Trim().Trim('"')
    if ($p -and (Test-Path -LiteralPath (Join-Path $p 'genesis\__main__.py'))) { $root = (Resolve-Path -LiteralPath $p).Path }
    else { Bad "That folder doesn't contain Genesis. Try again." }
}
Ok "Genesis folder: $root"
try {
    Get-ChildItem -LiteralPath $root -Recurse -File -Include *.bat, *.ps1, *.py, *.cmd -ErrorAction SilentlyContinue |
        Where-Object { $_.FullName -notlike '*\.venv\*' } | Unblock-File -ErrorAction SilentlyContinue
} catch {}

$py = Join-Path $root '.venv\Scripts\python.exe'
if ($env:GENESIS_FIX_PYTHON) { $py = $env:GENESIS_FIX_PYTHON }
if (-not (Test-Path -LiteralPath $py)) {
    Warn "Genesis isn't installed in this folder yet (.venv is missing)."
    if ((-not $TestMode) -and (Ask-YesNo "Install it now? (needs internet, takes about 5-15 minutes)" $true)) {
        $old = $ErrorActionPreference; $ErrorActionPreference = 'Continue'
        try { & powershell -NoProfile -ExecutionPolicy Bypass -File (Join-Path $root 'scripts\setup_windows.ps1') }
        finally { $ErrorActionPreference = $old }
    }
    if (-not (Test-Path -LiteralPath $py)) {
        Bad "Genesis is still not installed. Install Python 3.12 from python.org (tick 'Add to PATH'), then run this file again."
        return
    }
    Ok "Genesis installed."
}
$ver = Run-Py @('-I', '-c', "import sys; sys.path.insert(0, r'$root'); import genesis; print(genesis.__version__)")
Ok ("Genesis version: " + (($ver -split "`n") | Select-Object -Last 1))

# Stop a running Genesis, otherwise it keeps the old settings
try {
    $running = @(Get-CimInstance Win32_Process -ErrorAction Stop | Where-Object {
        ($_.Name -in @('python.exe', 'pythonw.exe')) -and ($_.CommandLine -match '-m\s+genesis')
    })
} catch { $running = @() }
if ($running.Count -gt 0) {
    Warn "Genesis is running right now. It has to restart to use the new settings."
    if (Ask-YesNo "Close the running Genesis now?" $true) {
        foreach ($p in $running) { try { Stop-Process -Id $p.ProcessId -Force -ErrorAction Stop } catch {} }
        Start-Sleep -Milliseconds 800
        Ok "Closed Genesis."
    } else {
        Warn "OK - close Genesis yourself (tray icon > Quit) before starting it again."
    }
}

# ------------------------------------------------------------------------------------------------ 2. repair .env
Step "2/6  Checking your settings file (.env)"
$envPath = Join-Path $root '.env'
$stamp = Get-Date -Format 'yyyyMMdd-HHmmss'
if ((-not (Test-Path -LiteralPath $envPath)) -and (Test-Path -LiteralPath (Join-Path $root '.env.txt'))) {
    Move-Item -LiteralPath (Join-Path $root '.env.txt') -Destination $envPath
    Ok "Renamed .env.txt to .env (Windows had added .txt)."
}
$lines = Read-Lines $envPath
if ((Test-Path -LiteralPath $envPath) -and -not (Test-EnvBroken $lines)) {
    Ok "Settings file looks fine ($($lines.Count) lines)."
} else {
    if (Test-Path -LiteralPath $envPath) {
        Copy-Item -LiteralPath $envPath -Destination (Join-Path $root ".env.broken-$stamp") -Force
        Warn "Your settings file was damaged by the earlier attempt (saved a copy as .env.broken-$stamp)."
    } else {
        Warn "There is no settings file yet."
    }
    $good = $null
    $backups = @(Get-ChildItem -LiteralPath $root -Force -File | Where-Object { $_.Name -eq '.env.backup' -or $_.Name -like '.env.before-fix-*' } |
                 Sort-Object LastWriteTime -Descending)
    foreach ($b in $backups) {
        $bl = Read-Lines $b.FullName
        if (-not (Test-EnvBroken $bl)) { $good = $b; break }
    }
    if ($good) {
        Copy-Item -LiteralPath $good.FullName -Destination $envPath -Force
        $lines = Read-Lines $envPath
        Ok "Restored your settings from $($good.Name) ($($lines.Count) lines)."
    } elseif (Test-Path -LiteralPath (Join-Path $root '.env.example')) {
        Copy-Item -LiteralPath (Join-Path $root '.env.example') -Destination $envPath -Force
        $lines = Read-Lines $envPath
        Warn "No good backup found, so I started a fresh settings file from .env.example."
    } else {
        $lines = @()
        Warn "No backup or template found - creating a new settings file."
    }
}

# ------------------------------------------------------------------------------------------------ 3. backup
Step "3/6  Making a backup"
if (Test-Path -LiteralPath $envPath) {
    Copy-Item -LiteralPath $envPath -Destination (Join-Path $root ".env.before-fix-$stamp") -Force
    Ok "Backup saved as .env.before-fix-$stamp"
}

# Variables set in Windows itself win over .env - clear the ones that would block Gemini
$blocking = @('GENESIS_LLM_PROVIDER', 'LLM_PROVIDER', 'LLM_MODEL', 'LLM_BASE_URL', 'GEMINI_API_KEY', 'GOOGLE_API_KEY')
foreach ($n in $blocking) {
    $u = [Environment]::GetEnvironmentVariable($n, 'User')
    if ($u) {
        if (-not $TestMode) { [Environment]::SetEnvironmentVariable($n, $null, 'User') }
        Warn "Removed the Windows user setting $n (it was overriding your .env)."
    }
    $m = [Environment]::GetEnvironmentVariable($n, 'Machine')
    if ($m) { Warn "Windows has a system-wide setting $n that overrides .env. Remove it in 'Edit the system environment variables'." }
    Remove-Item -LiteralPath ("Env:" + $n) -ErrorAction SilentlyContinue   # for this window and the Genesis it starts
}

# ------------------------------------------------------------------------------------------------ 4/5. key + model test
Step "4/6  Your Gemini key"
Say "      Get a free key at  https://aistudio.google.com/apikey  (button: Create API key)."
Say "      When asked, paste it with RIGHT-CLICK (or Ctrl+V) and press Enter. The key stays hidden while you paste."

$existing = Get-EnvValue $lines 'GEMINI_API_KEY'
if (-not $existing) { $existing = Get-EnvValue $lines 'GOOGLE_API_KEY' }
if ($existing -and (Key-Problem $existing)) { $existing = '' }

$helper = @'
import json, os, sys
root = os.environ["GENESIS_FIX_ROOT"]
sys.path.insert(0, root)
os.chdir(root)
import logging
logging.disable(logging.CRITICAL)
import requests
from genesis.ai.llm.openai_compat import GeminiProvider
from genesis.ai.llm.base import LLMAuthError, LLMModelError, LLMUnavailable, LLMError

key = os.environ.get("GENESIS_FIX_KEY", "")
host = (os.environ.get("GENESIS_FIX_BASE") or "https://generativelanguage.googleapis.com").rstrip("/")
out = {"ok": False, "model": "", "kind": "", "message": "", "tried": []}

def done():
    print("RESULT " + json.dumps(out))
    sys.exit(0)

listed = []
try:
    r = requests.get(host + "/v1beta/models", params={"pageSize": 1000}, headers={"x-goog-api-key": key}, timeout=20)
    if r.status_code == 200:
        for m in r.json().get("models", []):
            name = m.get("name", "").split("/")[-1]
            if "generateContent" in m.get("supportedGenerationMethods", []):
                listed.append(name)
except requests.RequestException as exc:
    out.update(kind="network", message="Can't reach Google (%s). Check your internet connection." % type(exc).__name__)
    done()

BAD = ("image", "tts", "audio", "live", "embedding", "vision", "thinking", "exp", "learnlm", "gemma", "robotics", "computer")
# Genesis sometimes allows very short answers (5-120 tokens); "thinking" models spend that budget on thinking and
# return nothing, so the test above uses a small budget and only a model that really answers is chosen.
preferred = ["gemini-2.5-flash", "gemini-flash-latest", "gemini-2.0-flash", "gemini-2.5-flash-lite", "gemini-flash-lite-latest",
             "gemini-2.0-flash-lite"]
extra = sorted([n for n in listed if "flash" in n and not any(b in n for b in BAD) and n not in preferred], reverse=True)
if listed:
    cands = [n for n in preferred if n in listed] + extra
    if not cands:
        cands = [n for n in listed if n.startswith("gemini") and not any(b in n for b in BAD)][:4]
else:
    cands = preferred[:4]   # listing not allowed for this key type - try the usual names directly

last_kind, last_msg = "", ""
for model in cands[:8]:
    p = GeminiProvider(key, model, host + "/v1beta/openai", timeout=40, retries=1, backoff=2.0)
    try:
        text = p.chat([{"role": "user", "content": "Reply with exactly: Genesis is ready."}], max_tokens=40)
        data = p.chat_json([{"role": "system", "content": "Return only JSON."},
                            {"role": "user", "content": 'Return {"intent": "open_application", "app": "chrome"} as JSON.'}],
                           max_tokens=120)
        if not text or not isinstance(data, dict):
            raise LLMError("empty answer", kind="bad_response")
        out.update(ok=True, model=model, message=text[:80])
        done()
    except LLMAuthError as exc:
        out.update(kind="auth", message=str(exc)); done()
    except LLMModelError as exc:
        last_kind, last_msg = "model", str(exc)
    except LLMUnavailable as exc:
        k = getattr(exc, "kind", "")
        if k == "network" or k == "timeout":
            out.update(kind="network", message=str(exc)); done()
        last_kind, last_msg = (k or "unavailable"), str(exc)
    except LLMError as exc:
        low = str(exc).lower()
        if "api key not valid" in low or "api_key_invalid" in low or "invalid api key" in low or "unauthenticated" in low:
            out.update(kind="auth", message="Google says this API key is not valid."); done()
        last_kind, last_msg = "other", str(exc)
    out["tried"].append(model)
out.update(kind=last_kind or "other", message=last_msg or "No Gemini model answered.")
done()
'@
$helperPath = Join-Path ([IO.Path]::GetTempPath()) ("genesis_fix_" + [Guid]::NewGuid().ToString('N') + ".py")
Write-Lines $helperPath ($helper -split "`r?`n")

$key = $null; $model = $null; $attempt = 0
try {
    while (-not $model) {
        $attempt++
        if ($attempt -gt 4) { Bad "Stopping after several tries. Your settings were NOT changed."; return }
        if ($existing -and $attempt -eq 1) {
            Say ("      A Gemini key is already saved ({0}). Press Enter to use it, or paste a new one." -f (Mask $existing))
        }
        $k = Clean-Key (Read-Secret "  Paste your Gemini key")
        if (-not $k -and $existing -and $attempt -eq 1) { $k = $existing }
        $problem = Key-Problem $k
        if ($problem) { Bad "That doesn't look like a key: $problem."; continue }
        Ok ("Got a key ({0}). Testing it with Google - this takes a few seconds..." -f (Mask $k))

        $env:GENESIS_FIX_KEY = $k
        $env:GENESIS_FIX_ROOT = $root
        $raw = Run-Py @('-I', $helperPath)
        Remove-Item Env:GENESIS_FIX_KEY -ErrorAction SilentlyContinue
        $line = ($raw -split "`r?`n" | Where-Object { $_ -like 'RESULT *' } | Select-Object -Last 1)
        if (-not $line) {
            Bad "The test couldn't run. Details:"
            Say (($raw -replace [regex]::Escape($k), '****') -split "`r?`n" | Select-Object -Last 8 | Out-String)
            return
        }
        $res = $line.Substring(7) | ConvertFrom-Json
        if ($res.ok) {
            $key = $k; $model = $res.model
            Ok "Google answered! Using model: $model"
            break
        }
        switch ($res.kind) {
            'auth' {
                Bad "Google did not accept this key."
                Say "      Make a new one: https://aistudio.google.com/apikey -> 'Create API key' -> 'Copy key', then paste it here."
            }
            'quota' { Bad "This key works, but its free limit is used up right now. Wait a minute and try again, or use another Google account's key." }
            'rate_limited' { Bad "Google says 'too many requests' right now. Wait one minute, then paste the key again." }
            'network' { Bad $res.message; Say "      Check your internet (or VPN/firewall), then run this file again. Nothing was changed."; return }
            default { Bad ("Gemini didn't answer: " + $res.message) }
        }
        if ($TestMode) { return }
    }
} finally {
    Remove-Item -LiteralPath $helperPath -ErrorAction SilentlyContinue
    Remove-Item Env:GENESIS_FIX_KEY -ErrorAction SilentlyContinue
}

# ------------------------------------------------------------------------------------------------ 6. save + verify
Step "5/6  Saving the settings"
$turnOff = '^\s*(?:export\s+)?(GENESIS_LLM_PROVIDER|LLM_PROVIDER|LLM_MODEL|LLM_BASE_URL)\s*='
$dropKey = '^\s*(?:export\s+)?(GEMINI_API_KEY|GOOGLE_API_KEY)\s*='
$new = New-Object System.Collections.Generic.List[string]
$inOurBlock = $false
foreach ($l in $lines) {
    if ($l -match '^# --- Set by Fix_Genesis') { $inOurBlock = $true; continue }   # replaced below, not kept twice
    if ($inOurBlock -and $l -match $turnOff) { continue }
    if ($l.Trim() -eq '' -or $l.TrimStart().StartsWith('#')) { $inOurBlock = $false }
    if ($l -match $dropKey) { continue }
    if ($l -match $turnOff) { $new.Add('# ' + $l.TrimStart()); continue }
    $new.Add($l)
}
while ($new.Count -gt 0 -and $new[$new.Count - 1].Trim() -eq '') { $new.RemoveAt($new.Count - 1) }
$new.Add('')
$new.Add("# --- Set by Fix_Genesis on $(Get-Date -Format 'yyyy-MM-dd HH:mm'): Genesis uses Google Gemini ---")
$new.Add('GENESIS_LLM_PROVIDER=gemini')
$new.Add("LLM_MODEL=$model")
$new.Add("GEMINI_API_KEY=$key")
Write-Lines $envPath $new
Ok "Saved. (Your other settings, like the Hugging Face token, were kept.)"

$checkCode = @"
import json, os, sys, logging
logging.disable(logging.CRITICAL)
sys.path.insert(0, r'$root'); os.chdir(r'$root')
from genesis.core.config import GenesisConfig
c = GenesisConfig.from_env()
print('CHECK ' + json.dumps({'provider': c.llm_provider, 'model': c.llm_model_name(), 'key': c.llm_key_present, 'notes': c.llm_notes}))
"@
$check = Run-Py @('-I', '-c', $checkCode)
$cl = ($check -split "`r?`n" | Where-Object { $_ -like 'CHECK *' } | Select-Object -Last 1)
if ($cl) {
    $c = $cl.Substring(6) | ConvertFrom-Json
    if ($c.provider -eq 'gemini' -and $c.key -and $c.model -eq $model) { Ok "Genesis reads the new settings: Gemini, model $model." }
    else { Warn ("Genesis reads provider '{0}', model '{1}'. {2}" -f $c.provider, $c.model, ($c.notes -join ' ')) }
} else {
    Warn "Couldn't double-check the settings, but they are saved."
}

# ------------------------------------------------------------------------------------------------ 7. shortcut + start
Step "6/6  Starting Genesis"
$desktop = [Environment]::GetFolderPath('Desktop')
$lnk = if ($desktop) { Join-Path $desktop 'Genesis.lnk' } else { $null }
if ((-not $TestMode) -and $lnk -and -not (Test-Path -LiteralPath $lnk)) {
    if (Ask-YesNo "Put a 'Genesis' shortcut on your desktop so you can start it with a double-click?" $true) {
        try {
            $ws = New-Object -ComObject WScript.Shell
            $s = $ws.CreateShortcut($lnk)
            $s.TargetPath = Join-Path $root '.venv\Scripts\pythonw.exe'
            $s.Arguments = '-m genesis'
            $s.WorkingDirectory = $root
            $s.Description = 'Start Genesis'
            $s.Save()
            Ok "Desktop shortcut created: Genesis"
        } catch { Warn "Couldn't create the shortcut ($($_.Exception.Message))." }
    }
}
if ($TestMode) { Ok "Test mode: not starting Genesis."; return }
if (Ask-YesNo "Start Genesis now (tray icon + voice)?" $true) {
    try {
        Start-Process -FilePath (Join-Path $root '.venv\Scripts\pythonw.exe') -ArgumentList '-m', 'genesis' -WorkingDirectory $root
        Ok "Genesis is starting - look for its icon near the clock."
    } catch {
        Bad ("Windows blocked starting Genesis: " + $_.Exception.Message)
        Say "      Start it from PowerShell instead:  cd `"$root`"  then  .\.venv\Scripts\pythonw.exe -m genesis"
    }
}
Say ""
Say "  All done. Say  'Hey Genesis'  or press  Ctrl+Alt+G, then try:" 'Green'
Say "     - what is the capital of japan" 'Green'
Say "     - open my documents folder" 'Green'
Say "     - open google" 'Green'
Say ""
Say "  To undo: copy .env.before-fix-$stamp back to .env in the Genesis folder." 'DarkGray'
