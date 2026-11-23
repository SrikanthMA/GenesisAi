"""Catalogue of well-known desktop and web applications.

Each entry may define:
    aliases   – spoken/typed names
    exe       – executable names looked up on PATH / App Paths registry
    paths     – candidate install paths (environment variables expanded)
    uri       – Windows protocol URI (e.g. ms-settings:)
    url       – web applications open in the default browser
    process   – process image names used by close_application
    shortcut  – Start-menu shortcut name fragment
Unknown apps are resolved by searching Start-menu shortcuts at runtime.
"""
from __future__ import annotations

APPS: dict[str, dict] = {
    # ------------------------------------------------------------- browsers
    "chrome": {"name": "Google Chrome", "aliases": ["chrome", "google chrome"], "exe": ["chrome.exe", "google-chrome", "chrome"],
               "paths": [r"%ProgramFiles%\Google\Chrome\Application\chrome.exe",
                         r"%ProgramFiles(x86)%\Google\Chrome\Application\chrome.exe",
                         r"%LOCALAPPDATA%\Google\Chrome\Application\chrome.exe"],
               "process": ["chrome.exe", "chrome"], "shortcut": "Google Chrome"},
    "edge": {"name": "Microsoft Edge", "aliases": ["edge", "microsoft edge"], "exe": ["msedge.exe", "microsoft-edge"],
             "paths": [r"%ProgramFiles(x86)%\Microsoft\Edge\Application\msedge.exe", r"%ProgramFiles%\Microsoft\Edge\Application\msedge.exe"],
             "process": ["msedge.exe"], "shortcut": "Microsoft Edge"},
    "firefox": {"name": "Firefox", "aliases": ["firefox", "mozilla firefox"], "exe": ["firefox.exe", "firefox"],
                "paths": [r"%ProgramFiles%\Mozilla Firefox\firefox.exe"], "process": ["firefox.exe", "firefox"], "shortcut": "Firefox"},
    "brave": {"name": "Brave", "aliases": ["brave", "brave browser"], "exe": ["brave.exe", "brave-browser"],
              "paths": [r"%ProgramFiles%\BraveSoftware\Brave-Browser\Application\brave.exe"], "process": ["brave.exe"], "shortcut": "Brave"},
    # ------------------------------------------------------------ dev tools
    "vscode": {"name": "Visual Studio Code", "aliases": ["vs code", "vscode", "visual studio code", "code editor", "v s code"],
               "exe": ["Code.exe", "code"],
               "paths": [r"%LOCALAPPDATA%\Programs\Microsoft VS Code\Code.exe", r"%ProgramFiles%\Microsoft VS Code\Code.exe"],
               "process": ["Code.exe", "code"], "shortcut": "Visual Studio Code"},
    "terminal": {"name": "Windows Terminal", "aliases": ["terminal", "windows terminal"], "exe": ["wt.exe", "gnome-terminal", "x-terminal-emulator"],
                 "process": ["WindowsTerminal.exe"], "shortcut": "Terminal"},
    "powershell": {"name": "PowerShell", "aliases": ["powershell"], "exe": ["pwsh.exe", "powershell.exe"], "process": ["powershell.exe", "pwsh.exe"]},
    "cmd": {"name": "Command Prompt", "aliases": ["command prompt", "cmd"], "exe": ["cmd.exe"], "process": ["cmd.exe"]},
    "pycharm": {"name": "PyCharm", "aliases": ["pycharm"], "exe": ["pycharm64.exe", "pycharm"], "process": ["pycharm64.exe"], "shortcut": "PyCharm"},
    "postman": {"name": "Postman", "aliases": ["postman"], "exe": ["Postman.exe"], "paths": [r"%LOCALAPPDATA%\Postman\Postman.exe"],
                "process": ["Postman.exe"], "shortcut": "Postman"},
    "git bash": {"name": "Git Bash", "aliases": ["git bash"], "paths": [r"%ProgramFiles%\Git\git-bash.exe"], "process": ["mintty.exe"]},
    # ------------------------------------------------------------ Windows
    "notepad": {"name": "Notepad", "aliases": ["notepad", "text editor"], "exe": ["notepad.exe", "gedit", "gnome-text-editor"], "process": ["notepad.exe"]},
    "calculator": {"name": "Calculator", "aliases": ["calculator", "calc"], "exe": ["calc.exe", "gnome-calculator"], "process": ["CalculatorApp.exe", "calc.exe"]},
    "explorer": {"name": "File Explorer", "aliases": ["file explorer", "explorer", "files", "file manager", "this pc", "my computer"],
                 "exe": ["explorer.exe", "nautilus", "xdg-open"], "process": []},
    "task manager": {"name": "Task Manager", "aliases": ["task manager"], "exe": ["taskmgr.exe"], "process": ["Taskmgr.exe"]},
    "control panel": {"name": "Control Panel", "aliases": ["control panel"], "exe": ["control.exe"], "process": []},
    "settings": {"name": "Windows Settings", "aliases": ["windows settings", "pc settings", "system settings"], "uri": "ms-settings:", "process": ["SystemSettings.exe"]},
    "paint": {"name": "Paint", "aliases": ["paint", "ms paint", "mspaint"], "exe": ["mspaint.exe"], "process": ["mspaint.exe"]},
    "snipping tool": {"name": "Snipping Tool", "aliases": ["snipping tool", "screenshot tool", "snip"], "uri": "ms-screenclip:", "exe": ["SnippingTool.exe"], "process": ["SnippingTool.exe"]},
    "camera": {"name": "Camera", "aliases": ["camera"], "uri": "microsoft.windows.camera:", "process": ["WindowsCamera.exe"]},
    "photos": {"name": "Photos", "aliases": ["photos"], "uri": "ms-photos:", "process": ["Microsoft.Photos.exe"]},
    "clock": {"name": "Clock", "aliases": ["clock", "alarms", "timer app"], "uri": "ms-clock:", "process": []},
    "store": {"name": "Microsoft Store", "aliases": ["microsoft store", "store", "app store"], "uri": "ms-windows-store:", "process": ["WinStore.App.exe"]},
    # --------------------------------------------------------------- Office
    "word": {"name": "Microsoft Word", "aliases": ["word", "microsoft word", "ms word"], "exe": ["WINWORD.EXE"], "process": ["WINWORD.EXE"], "shortcut": "Word"},
    "excel": {"name": "Microsoft Excel", "aliases": ["excel", "microsoft excel", "ms excel"], "exe": ["EXCEL.EXE"], "process": ["EXCEL.EXE"], "shortcut": "Excel"},
    "powerpoint": {"name": "PowerPoint", "aliases": ["powerpoint", "power point", "ppt"], "exe": ["POWERPNT.EXE"], "process": ["POWERPNT.EXE"], "shortcut": "PowerPoint"},
    "outlook": {"name": "Outlook", "aliases": ["outlook", "microsoft outlook"], "exe": ["OUTLOOK.EXE", "olk.exe"], "process": ["OUTLOOK.EXE", "olk.exe"], "shortcut": "Outlook"},
    "onenote": {"name": "OneNote", "aliases": ["onenote", "one note"], "exe": ["ONENOTE.EXE"], "process": ["ONENOTE.EXE"], "shortcut": "OneNote"},
    "teams": {"name": "Microsoft Teams", "aliases": ["teams", "microsoft teams", "ms teams"], "exe": ["ms-teams.exe", "Teams.exe"], "uri": "msteams:",
              "process": ["ms-teams.exe", "Teams.exe"], "shortcut": "Teams"},
    # ------------------------------------------------------ communication / media
    "spotify": {"name": "Spotify", "aliases": ["spotify", "music"], "exe": ["Spotify.exe", "spotify"], "paths": [r"%APPDATA%\Spotify\Spotify.exe"],
                "uri": "spotify:", "process": ["Spotify.exe"], "shortcut": "Spotify"},
    "discord": {"name": "Discord", "aliases": ["discord"], "paths": [r"%LOCALAPPDATA%\Discord\Update.exe"], "shortcut": "Discord",
                "process": ["Discord.exe"]},
    "slack": {"name": "Slack", "aliases": ["slack"], "exe": ["slack.exe"], "paths": [r"%LOCALAPPDATA%\slack\slack.exe"], "process": ["slack.exe"], "shortcut": "Slack"},
    "zoom": {"name": "Zoom", "aliases": ["zoom"], "paths": [r"%APPDATA%\Zoom\bin\Zoom.exe"], "process": ["Zoom.exe"], "shortcut": "Zoom"},
    "whatsapp": {"name": "WhatsApp", "aliases": ["whatsapp", "whats app"], "uri": "whatsapp:", "process": ["WhatsApp.exe"], "shortcut": "WhatsApp"},
    "telegram": {"name": "Telegram", "aliases": ["telegram"], "paths": [r"%APPDATA%\Telegram Desktop\Telegram.exe"], "process": ["Telegram.exe"], "shortcut": "Telegram"},
    "vlc": {"name": "VLC", "aliases": ["vlc", "vlc player", "media player"], "exe": ["vlc.exe", "vlc"], "paths": [r"%ProgramFiles%\VideoLAN\VLC\vlc.exe"],
            "process": ["vlc.exe"], "shortcut": "VLC"},
    "obs": {"name": "OBS Studio", "aliases": ["obs", "obs studio"], "paths": [r"%ProgramFiles%\obs-studio\bin\64bit\obs64.exe"], "process": ["obs64.exe"], "shortcut": "OBS"},
    "steam": {"name": "Steam", "aliases": ["steam"], "paths": [r"%ProgramFiles(x86)%\Steam\steam.exe"], "uri": "steam:", "process": ["steam.exe"], "shortcut": "Steam"},
    "notion": {"name": "Notion", "aliases": ["notion"], "paths": [r"%LOCALAPPDATA%\Programs\Notion\Notion.exe"], "process": ["Notion.exe"], "shortcut": "Notion"},
    "obsidian": {"name": "Obsidian", "aliases": ["obsidian"], "paths": [r"%LOCALAPPDATA%\Programs\Obsidian\Obsidian.exe"], "process": ["Obsidian.exe"], "shortcut": "Obsidian"},
    # ------------------------------------------------------------- web apps
    "gmail": {"name": "Gmail", "aliases": ["gmail", "g mail", "google mail", "my email", "my inbox", "email", "inbox"], "url": "https://mail.google.com/mail/u/0/#inbox"},
    "google calendar": {"name": "Google Calendar", "aliases": ["google calendar", "my calendar", "calendar"], "url": "https://calendar.google.com/"},
    "google drive": {"name": "Google Drive", "aliases": ["google drive", "drive", "my drive"], "url": "https://drive.google.com/"},
    "google docs": {"name": "Google Docs", "aliases": ["google docs", "docs"], "url": "https://docs.google.com/"},
    "youtube": {"name": "YouTube", "aliases": ["youtube", "you tube"], "url": "https://www.youtube.com/"},
    "github": {"name": "GitHub", "aliases": ["github", "git hub"], "url": "https://github.com/"},
    "linkedin": {"name": "LinkedIn", "aliases": ["linkedin", "linked in"], "url": "https://www.linkedin.com/"},
    "google maps": {"name": "Google Maps", "aliases": ["google maps", "maps"], "url": "https://maps.google.com/"},
    "stackoverflow": {"name": "Stack Overflow", "aliases": ["stack overflow", "stackoverflow"], "url": "https://stackoverflow.com/"},
    "chatgpt": {"name": "ChatGPT", "aliases": ["chatgpt", "chat gpt"], "url": "https://chatgpt.com/"},
    "claude": {"name": "Claude", "aliases": ["claude"], "url": "https://claude.ai/"},
    "whatsapp web": {"name": "WhatsApp Web", "aliases": ["whatsapp web"], "url": "https://web.whatsapp.com/"},
    "netflix": {"name": "Netflix", "aliases": ["netflix"], "url": "https://www.netflix.com/"},
    "outlook web": {"name": "Outlook on the web", "aliases": ["outlook web", "hotmail"], "url": "https://outlook.live.com/mail/"},
}


def alias_index() -> list[tuple[str, str]]:
    """(alias, app_key) pairs sorted longest alias first (for greedy matching)."""
    pairs = []
    for key, spec in APPS.items():
        for alias in {key, *spec.get("aliases", [])}:
            pairs.append((alias.lower(), key))
    pairs.sort(key=lambda p: -len(p[0]))
    return pairs


def lookup(name: str) -> tuple[str, dict] | None:
    n = (name or "").strip().lower()
    n = n[:-4] if n.endswith(".exe") else n
    for alias, key in alias_index():
        if n == alias:
            return key, APPS[key]
    return None
