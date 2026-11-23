; Inno Setup script — builds GenesisSetup.exe from the PyInstaller output (dist\Genesis).
; 1. powershell -ExecutionPolicy Bypass -File scripts\build_windows.ps1
; 2. Open this file in Inno Setup (https://jrsoftware.org/isinfo.php) and press Compile.
#define AppName "Genesis"
#define AppVersion "1.0.0"

[Setup]
AppId={{6B1E9E57-4A5C-4F7C-9A77-5E0C1D6A2B19}
AppName={#AppName}
AppVersion={#AppVersion}
AppPublisher=Genesis Team
DefaultDirName={localappdata}\Programs\Genesis
DefaultGroupName=Genesis
PrivilegesRequired=lowest
OutputDir=..\dist
OutputBaseFilename=GenesisSetup
SetupIconFile=..\assets\genesis.ico
Compression=lzma2
SolidCompression=yes
WizardStyle=modern

[Tasks]
Name: "startup"; Description: "Start Genesis when I sign in to Windows"; Flags: unchecked
Name: "desktopicon"; Description: "Create a desktop shortcut"; Flags: unchecked

[Files]
Source: "..\dist\Genesis\*"; DestDir: "{app}"; Flags: recursesubdirs ignoreversion

[Icons]
Name: "{group}\Genesis"; Filename: "{app}\Genesis.exe"
Name: "{autodesktop}\Genesis"; Filename: "{app}\Genesis.exe"; Tasks: desktopicon

[Registry]
Root: HKCU; Subkey: "Software\Microsoft\Windows\CurrentVersion\Run"; ValueType: string; ValueName: "Genesis"; \
  ValueData: """{app}\Genesis.exe"" --minimized"; Flags: uninsdeletevalue; Tasks: startup

[Run]
Filename: "{app}\Genesis.exe"; Description: "Launch Genesis"; Flags: nowait postinstall skipifsilent
