; Inno Setup script for FaujiBot turnkey installer.
; Build: powershell -ExecutionPolicy Bypass -File installer\build.ps1
; Output: installer\Output\FaujiSetup.exe

#define AppName       "FaujiBot"
#define AppVersion    "0.1.0"
#define AppPublisher  "FaujiBot"
#define InstallDir    "{autopf}\FaujiBot"
#define ProjectRoot   ".."

[Setup]
AppId={{A4F4D6FC-31E4-4D6C-9DA0-FAUJIBOT0001}}
AppName={#AppName}
AppVersion={#AppVersion}
AppPublisher={#AppPublisher}
DefaultDirName={#InstallDir}
DefaultGroupName={#AppName}
OutputBaseFilename=FaujiSetup
OutputDir=Output
Compression=lzma2/ultra
SolidCompression=yes
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
PrivilegesRequired=admin
DisableDirPage=auto
DisableProgramGroupPage=yes
WizardStyle=modern
SetupIconFile=
UninstallDisplayName=FaujiBot
UninstallDisplayIcon={app}\FaujiBot.ico

[Languages]
Name: "english"; MessagesFile: "compiler:Default.isl"

[Tasks]
Name: "desktopicon"; Description: "Create a desktop shortcut"; GroupDescription: "Additional shortcuts:"

[Files]
; Embeddable Python
Source: "python\*"; DestDir: "{app}\python"; Flags: recursesubdirs createallsubdirs ignoreversion

; Bot (untouched)
Source: "{#ProjectRoot}\bot\*"; DestDir: "{app}\bot"; Flags: recursesubdirs createallsubdirs ignoreversion

; Supervisor source
Source: "{#ProjectRoot}\supervisor\*"; DestDir: "{app}\supervisor"; Flags: recursesubdirs createallsubdirs ignoreversion
Source: "{#ProjectRoot}\requirements.txt"; DestDir: "{app}"; Flags: ignoreversion

; Wheels for offline pip install
Source: "wheels\*"; DestDir: "{app}\wheels"; Flags: recursesubdirs createallsubdirs ignoreversion

; MT5 silent installer (deleted after first run)
Source: "mt5setup.exe"; DestDir: "{app}"; Flags: ignoreversion deleteafterinstall

; Helper scripts
Source: "FaujiBot.cmd"; DestDir: "{app}"; Flags: ignoreversion
Source: "FaujiOpenMT5.cmd"; DestDir: "{app}"; Flags: ignoreversion
Source: "FaujiBot.ico"; DestDir: "{app}"; Flags: ignoreversion skipifsourcedoesntexist

[Dirs]
Name: "{app}\data"; Permissions: users-modify
Name: "{app}\supervisor\certs"; Permissions: users-modify

[Run]
; 1) Install MT5 silently
Filename: "{app}\mt5setup.exe"; Parameters: "/auto"; StatusMsg: "Installing MetaTrader 5..."; Flags: runhidden waituntilterminated

; 2) pip install all deps from bundled wheels (offline)
Filename: "{app}\python\python.exe"; Parameters: "-m pip install --no-index --find-links=""{app}\wheels"" -r ""{app}\requirements.txt"""; StatusMsg: "Installing Python packages..."; Flags: runhidden waituntilterminated

; 3) Register Scheduled Task (auto-start on logon)
Filename: "schtasks.exe"; Parameters: "/Create /F /SC ONLOGON /RL HIGHEST /TN ""FaujiBot"" /TR ""\""{app}\FaujiBot.cmd\"""""; StatusMsg: "Registering startup task..."; Flags: runhidden waituntilterminated

; 4) Open inbound firewall rule for the dashboard
Filename: "netsh.exe"; Parameters: "advfirewall firewall add rule name=""FaujiBot Dashboard"" dir=in action=allow protocol=TCP localport=8443"; StatusMsg: "Adding firewall rule..."; Flags: runhidden waituntilterminated

; 5) Launch supervisor + open browser at end of install
Filename: "{app}\FaujiBot.cmd"; Description: "Launch FaujiBot dashboard"; Flags: postinstall nowait skipifsilent

[Icons]
Name: "{group}\FaujiBot Dashboard"; Filename: "https://localhost:8443"; IconFilename: "{app}\FaujiBot.ico"
Name: "{group}\Open MT5";           Filename: "{app}\FaujiOpenMT5.cmd"
Name: "{group}\Uninstall FaujiBot"; Filename: "{uninstallexe}"
Name: "{commondesktop}\FaujiBot Dashboard"; Filename: "https://localhost:8443"; IconFilename: "{app}\FaujiBot.ico"; Tasks: desktopicon

[UninstallRun]
Filename: "schtasks.exe"; Parameters: "/Delete /F /TN ""FaujiBot"""; Flags: runhidden
Filename: "netsh.exe"; Parameters: "advfirewall firewall delete rule name=""FaujiBot Dashboard"""; Flags: runhidden
