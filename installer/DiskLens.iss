; DiskLens installer (Inno Setup 6). Built by tools/build_installer.py, which passes:
;   /DAppVersion=x.y.z  /DStageDir=<build\stage>  /DOutDir=<dist>
; Installs per user (no admin) in %LOCALAPPDATA%\Programs\DiskLens, with its own Python in runtime\.

#ifndef AppVersion
  #define AppVersion "0.0.0"
#endif
#ifndef StageDir
  #define StageDir "..\build\stage"
#endif
#ifndef OutDir
  #define OutDir "..\dist"
#endif

[Setup]
AppId={{8E5B6E1C-4D7A-4C1E-9B6F-2D1A7C3E5F90}
AppName=DiskLens
AppVersion={#AppVersion}
AppVerName=DiskLens {#AppVersion}
AppPublisher=dionedde
AppPublisherURL=https://github.com/dionedde/disklens
AppSupportURL=https://github.com/dionedde/disklens/issues
AppUpdatesURL=https://github.com/dionedde/disklens/releases
DefaultDirName={autopf}\DiskLens
DisableProgramGroupPage=yes
PrivilegesRequired=lowest
OutputDir={#OutDir}
OutputBaseFilename=DiskLens-Setup-{#AppVersion}
SetupIconFile={#StageDir}\DiskLens.ico
UninstallDisplayIcon={app}\DiskLens.ico
UninstallDisplayName=DiskLens
Compression=lzma2/max
SolidCompression=yes
WizardStyle=modern
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
MinVersion=10.0
CloseApplications=yes
RestartApplications=no

[Languages]
Name: "en"; MessagesFile: "compiler:Default.isl"
Name: "pt"; MessagesFile: "compiler:Languages\BrazilianPortuguese.isl"
Name: "es"; MessagesFile: "compiler:Languages\Spanish.isl"

[CustomMessages]
en.AdminIcon=DiskLens (administrator)
pt.AdminIcon=DiskLens (administrador)
es.AdminIcon=DiskLens (administrador)
en.AppComment=See what fills your disk and why free space disappears
pt.AppComment=Veja o que ocupa o seu disco e por que o espaço livre some
es.AppComment=Mira qué ocupa tu disco y por qué desaparece el espacio libre
en.AdminComment=Open DiskLens as administrator (reads protected folders)
pt.AdminComment=Abre o DiskLens como administrador (lê pastas protegidas)
es.AdminComment=Abre DiskLens como administrador (lee carpetas protegidas)
en.LaunchNow=Open DiskLens
pt.LaunchNow=Abrir o DiskLens
es.LaunchNow=Abrir DiskLens
en.NoWebView2=DiskLens needs the Microsoft Edge WebView2 Runtime, which was not found on this computer.%n%nOpen the download page now?
pt.NoWebView2=O DiskLens precisa do Microsoft Edge WebView2 Runtime, que não foi encontrado neste computador.%n%nAbrir a página de download agora?
es.NoWebView2=DiskLens necesita Microsoft Edge WebView2 Runtime, que no se encontró en este equipo.%n%n¿Abrir la página de descarga ahora?
en.PurgeData=Also delete the DiskLens history and settings?%n%nChoose No to keep them for a future installation.
pt.PurgeData=Apagar também o histórico e as configurações do DiskLens?%n%nEscolha Não para mantê-los para uma instalação futura.
es.PurgeData=¿Borrar también el historial y la configuración de DiskLens?%n%nElige No para conservarlos para una instalación futura.

[Tasks]
Name: "desktopicon"; Description: "{cm:CreateDesktopIcon}"; GroupDescription: "{cm:AdditionalIcons}"; Flags: unchecked

[InstallDelete]
; an upgrade replaces the runtime and the app code entirely (settings and data stay)
Type: filesandordirs; Name: "{app}\runtime"
Type: filesandordirs; Name: "{app}\disklens"

[Files]
Source: "{#StageDir}\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
Name: "{autoprograms}\DiskLens"; Filename: "{app}\runtime\pythonw.exe"; Parameters: """{app}\launch.pyw"""; WorkingDir: "{app}"; IconFilename: "{app}\DiskLens.ico"; Comment: "{cm:AppComment}"; AppUserModelID: "DiskLens.Painel"
Name: "{autoprograms}\{cm:AdminIcon}"; Filename: "{app}\runtime\pythonw.exe"; Parameters: """{app}\launch.pyw"" --admin"; WorkingDir: "{app}"; IconFilename: "{app}\DiskLens.ico"; Comment: "{cm:AdminComment}"
Name: "{autodesktop}\DiskLens"; Filename: "{app}\runtime\pythonw.exe"; Parameters: """{app}\launch.pyw"""; WorkingDir: "{app}"; IconFilename: "{app}\DiskLens.ico"; Comment: "{cm:AppComment}"; AppUserModelID: "DiskLens.Painel"; Tasks: desktopicon

[Run]
Filename: "{app}\runtime\pythonw.exe"; Parameters: """{app}\launch.pyw"""; WorkingDir: "{app}"; Description: "{cm:LaunchNow}"; Flags: nowait postinstall skipifsilent

[UninstallDelete]
Type: filesandordirs; Name: "{app}\runtime"
Type: filesandordirs; Name: "{app}\disklens"
Type: files; Name: "{app}\scan.log"

[Code]
const
  WV2Key = '\Microsoft\EdgeUpdate\Clients\{F3017226-FE2A-4295-8BDF-00C3A9A7E4C5}';

function HasWebView2(Root: Integer; Key: String): Boolean;
var
  V: String;
begin
  Result := RegQueryStringValue(Root, Key, 'pv', V) and (V <> '') and (V <> '0.0.0.0');
end;

function WebView2Installed(): Boolean;
begin
  Result := HasWebView2(HKLM, 'SOFTWARE\WOW6432Node' + WV2Key) or HasWebView2(HKLM, 'SOFTWARE' + WV2Key)
            or HasWebView2(HKCU, 'Software' + WV2Key);
end;

procedure RunHelper(Args: String);
var
  Py: String;
  Rc: Integer;
begin
  Py := ExpandConstant('{app}\runtime\pythonw.exe');
  if FileExists(Py) then
    Exec(Py, '-m disklens.uninstall ' + Args, ExpandConstant('{app}'), SW_HIDE, ewWaitUntilTerminated, Rc);
end;

function PrepareToInstall(var NeedsRestart: Boolean): String;
begin
  { upgrading: close the tray icon and the panel of the installed version }
  RunHelper('--quit');
  Result := '';
end;

procedure CurStepChanged(CurStep: TSetupStep);
var
  Rc: Integer;
begin
  if (CurStep = ssPostInstall) and not WizardSilent() and not WebView2Installed() then
    if MsgBox(CustomMessage('NoWebView2'), mbConfirmation, MB_YESNO) = IDYES then
      ShellExec('open', 'https://developer.microsoft.com/microsoft-edge/webview2/', '', '', SW_SHOWNORMAL, ewNoWait, Rc);
end;

procedure CurUninstallStepChanged(CurUninstallStep: TUninstallStep);
begin
  if CurUninstallStep = usUninstall then
  begin
    if (not UninstallSilent()) and
       (MsgBox(CustomMessage('PurgeData'), mbConfirmation, MB_YESNO or MB_DEFBUTTON2) = IDYES) then
      RunHelper('--purge')
    else
      RunHelper('');
  end;
end;
