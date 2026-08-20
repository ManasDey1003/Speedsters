#define MyAppName "Persepolis Download Manager (Multi-Interface)"
#define MyAppVersion "5.2.0.1"
#define MyAppPublisher "Custom Build"
#define MyAppURL "https://github.com/persepolisdm/persepolis"
#define MyAppExeName "Persepolis Download Manager.exe"

[Setup]
; Generated a NEW AppId (different from the official installer's) so this
; custom build installs/upgrades independently and Windows doesn't confuse
; it with an official Persepolis release. Do not reuse the official AppId.
AppId={{b5be09d5-97fa-489e-bae8-040ff41ca205}
AppName={#MyAppName}
AppVersion={#MyAppVersion}
AppPublisher={#MyAppPublisher}
AppPublisherURL={#MyAppURL}
AppSupportURL={#MyAppURL}
AppUpdatesURL={#MyAppURL}
DefaultDirName={commonpf}\{#MyAppName}
DisableDirPage=auto
DisableProgramGroupPage=yes
UsedUserAreasWarning=no
OutputDir=Output
OutputBaseFilename=persepolis_multi_interface_{#MyAppVersion}_windows_64bit
Compression=lzma2/ultra64
SolidCompression=yes
CloseApplications=force
ArchitecturesInstallIn64BitMode=x64
ArchitecturesAllowed=x64
WizardStyle=modern

[Languages]
Name: "english"; MessagesFile: "compiler:Default.isl"

[Tasks]
Name: "desktopicon"; Description: "{cm:CreateDesktopIcon}"; GroupDescription: "{cm:AdditionalIcons}"; Flags: unchecked

[Files]
; Core app + your custom patched persepolis_lib_prime.py is already baked
; into this exe by PyInstaller at build time — nothing extra needed here
; for the patch itself.
Source: "..\Speedsters\dist\Persepolis Download Manager.exe"; DestDir: "{app}"; Flags: ignoreversion
Source: "..\Speedsters\dist\ffmpeg.exe"; DestDir: "{app}"; Flags: ignoreversion
Source: "..\Speedsters\dist\PersepolisBI.exe"; DestDir: "{app}"; Flags: ignoreversion
Source: "..\Speedsters\persepolis-windows-package-build\persepolis1.ico"; DestDir: "{app}"; Flags: ignoreversion

; --- Add any of your own extra files below this line ---
; Examples (uncomment / edit paths to match what you actually have):
; Source: "..\persepolis\dist\my_custom_icon.ico"; DestDir: "{app}"; Flags: ignoreversion
; Source: "..\persepolis\dist\my_readme.txt"; DestDir: "{app}"; Flags: ignoreversion
; Source: "..\persepolis\dist\extra_folder\*"; DestDir: "{app}\extra_folder"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
Name: "{autoprograms}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"
Name: "{autodesktop}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"; Tasks: desktopicon

[Run]
Filename: "{app}\{#MyAppExeName}"; Description: "{cm:LaunchProgram,{#StringChange(MyAppName, '&', '&&')}}"; Flags: nowait postinstall skipifsilent
