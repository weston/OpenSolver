; OpenSolver's Windows installer (#565). packaging/windows/build.sh compiles it with makensis:
;
;   makensis -DVERSION=0.1.0 -DVERSION4=0.1.0.0 -DSTAGE=<staged app> -DOUTFILE=<setup.exe> -DICON=<icon.ico> installer.nsi
;
; A per-user install, so no administrator rights: the app goes in %LOCALAPPDATA%\Programs\OpenSolver
; (as other per-user apps' do), with a Start menu shortcut, an optional desktop shortcut and an
; entry in Settings > Apps that runs the uninstaller. The user's data is not here: the viewer keeps
; its library and job queue in %USERPROFILE%\.wsolver and its log in %LOCALAPPDATA%\OpenSolver\Logs,
; so an upgrade touches neither, and the uninstaller removes only the log. (The solver's cache of
; the preflop table, in %LOCALAPPDATA%\wsolver, is shared with any solver built from a checkout, so
; it stays too.)
;
; Silent: OpenSolver-<version>-windows-x64-setup.exe /S [/D=<folder>] installs without a window
; (/D last, unquoted); "Uninstall OpenSolver.exe" /S uninstalls the same way.

Unicode true
ManifestDPIAware true
SetCompressor /SOLID lzma
RequestExecutionLevel user

!include "MUI2.nsh"
!include "LogicLib.nsh"
!include "FileFunc.nsh"
!include "WinVer.nsh"
!include "x64.nsh"

!define APP "OpenSolver"
!define WINDOW_CLASS "OpenSolverLauncher"
!define UNINSTALLER "Uninstall OpenSolver.exe"
!define UNINSTALL_KEY "Software\Microsoft\Windows\CurrentVersion\Uninstall\OpenSolver"
!define APP_KEY "Software\OpenSolver"

Name "${APP}"
OutFile "${OUTFILE}"
InstallDir "$LOCALAPPDATA\Programs\${APP}"
InstallDirRegKey HKCU "${APP_KEY}" "InstallDir"
BrandingText "${APP} ${VERSION}"

VIProductVersion "${VERSION4}"
VIAddVersionKey "ProductName" "${APP}"
VIAddVersionKey "ProductVersion" "${VERSION}"
VIAddVersionKey "FileVersion" "${VERSION4}"
VIAddVersionKey "FileDescription" "${APP} ${VERSION} setup"
VIAddVersionKey "CompanyName" "${APP}"
VIAddVersionKey "LegalCopyright" "${APP}"

!define MUI_ICON "${ICON}"
!define MUI_UNICON "${ICON}"
!define MUI_ABORTWARNING
!define MUI_WELCOMEPAGE_TEXT "This installs ${APP} ${VERSION}: the solver and its viewer, which runs in your browser, with everything they need.$\r$\n$\r$\nIt installs for you only, so it needs no administrator rights.$\r$\n$\r$\nClick Next to continue."
!define MUI_FINISHPAGE_RUN "$INSTDIR\OpenSolver.exe"
!define MUI_FINISHPAGE_RUN_TEXT "Start ${APP}"

!insertmacro MUI_PAGE_WELCOME
!define MUI_PAGE_CUSTOMFUNCTION_LEAVE CheckFolderPage
!insertmacro MUI_PAGE_DIRECTORY
!insertmacro MUI_PAGE_COMPONENTS
!insertmacro MUI_PAGE_INSTFILES
!insertmacro MUI_PAGE_FINISH

!insertmacro MUI_UNPAGE_CONFIRM
!insertmacro MUI_UNPAGE_INSTFILES

!insertmacro MUI_LANGUAGE "English"

; ----- checks ------------------------------------------------------------------------------------

; $0 = 1 when $INSTDIR is a folder this may install into: one that does not exist yet, an empty
; one, or one that holds OpenSolver already. Anything else would mix the app with other files, and
; the uninstaller, which removes the app's own folders by name, could take some of them.
Function CheckFolder
  StrCpy $0 1
  ${If} ${FileExists} "$INSTDIR\*.*"
  ${AndIfNot} ${FileExists} "$INSTDIR\OpenSolver.exe"
    FindFirst $1 $2 "$INSTDIR\*.*"
    ${DoWhile} $2 != ""
      ${If} $2 != "."
      ${AndIf} $2 != ".."
        StrCpy $0 0
        ${Break}
      ${EndIf}
      FindNext $1 $2
    ${Loop}
    FindClose $1
  ${EndIf}
FunctionEnd

Function CheckFolderPage
  Call CheckFolder
  ${If} $0 != 1
    MessageBox MB_OK|MB_ICONEXCLAMATION "$INSTDIR is not empty.$\r$\n$\r$\nChoose an empty folder, a new one, or the one OpenSolver is installed in."
    Abort
  ${EndIf}
FunctionEnd

; OpenSolver must not be running while its files are replaced or removed.
!macro CheckNotRunning UN
Function ${UN}CheckNotRunning
  ${Do}
    FindWindow $0 "${WINDOW_CLASS}"
    ${If} $0 == 0
      ${Break}
    ${EndIf}
    MessageBox MB_RETRYCANCEL|MB_ICONEXCLAMATION "OpenSolver is running. Quit it, then click Retry." /SD IDCANCEL IDRETRY +2
    Abort
  ${Loop}
FunctionEnd
!macroend
!insertmacro CheckNotRunning ""
!insertmacro CheckNotRunning "un."

Function .onInit
  ${IfNot} ${RunningX64}
  ${OrIfNot} ${AtLeastWin10}
    MessageBox MB_OK|MB_ICONSTOP "${APP} needs 64-bit Windows 10 or 11." /SD IDOK
    Abort
  ${EndIf}
  Call CheckNotRunning
FunctionEnd

Function un.onInit
  Call un.CheckNotRunning
FunctionEnd

; ----- install -----------------------------------------------------------------------------------

Section "${APP}" SecApp
  SectionIn RO
  Call CheckFolder   ; the folder page checks this, but a silent install has no pages
  ${If} $0 != 1
    SetErrorLevel 2
    MessageBox MB_OK|MB_ICONSTOP "$INSTDIR is not empty: choose an empty folder, a new one, or the one OpenSolver is installed in." /SD IDOK
    Quit
  ${EndIf}

  ; An upgrade: what the old version had and this one does not must not stay behind.
  RMDir /r "$INSTDIR\python"
  RMDir /r "$INSTDIR\viewer"
  RMDir /r "$INSTDIR\tools"
  RMDir /r "$INSTDIR\flops"

  SetOutPath "$INSTDIR"
  File /r "${STAGE}\*.*"
  WriteUninstaller "$INSTDIR\${UNINSTALLER}"

  CreateShortcut "$SMPROGRAMS\${APP}.lnk" "$INSTDIR\OpenSolver.exe" "" "$INSTDIR\OpenSolver.exe" 0
  WriteRegStr HKCU "${APP_KEY}" "StartMenuShortcut" "$SMPROGRAMS\${APP}.lnk"

  WriteRegStr HKCU "${APP_KEY}" "InstallDir" "$INSTDIR"
  WriteRegStr HKCU "${UNINSTALL_KEY}" "DisplayName" "${APP}"
  WriteRegStr HKCU "${UNINSTALL_KEY}" "DisplayVersion" "${VERSION}"
  WriteRegStr HKCU "${UNINSTALL_KEY}" "Publisher" "${APP}"
  WriteRegStr HKCU "${UNINSTALL_KEY}" "DisplayIcon" "$INSTDIR\OpenSolver.exe"
  WriteRegStr HKCU "${UNINSTALL_KEY}" "InstallLocation" "$INSTDIR"
  WriteRegStr HKCU "${UNINSTALL_KEY}" "UninstallString" '"$INSTDIR\${UNINSTALLER}"'
  WriteRegStr HKCU "${UNINSTALL_KEY}" "QuietUninstallString" '"$INSTDIR\${UNINSTALLER}" /S'
  WriteRegDWORD HKCU "${UNINSTALL_KEY}" "NoModify" 1
  WriteRegDWORD HKCU "${UNINSTALL_KEY}" "NoRepair" 1
  ${GetSize} "$INSTDIR" "/S=0K" $0 $1 $2
  WriteRegDWORD HKCU "${UNINSTALL_KEY}" "EstimatedSize" $0
SectionEnd

Section /o "Desktop shortcut" SecDesktop
  CreateShortcut "$DESKTOP\${APP}.lnk" "$INSTDIR\OpenSolver.exe" "" "$INSTDIR\OpenSolver.exe" 0
  WriteRegStr HKCU "${APP_KEY}" "DesktopShortcut" "$DESKTOP\${APP}.lnk"
SectionEnd

!insertmacro MUI_FUNCTION_DESCRIPTION_BEGIN
  !insertmacro MUI_DESCRIPTION_TEXT ${SecApp} "The solver, the viewer and the Python that runs it, and a Start menu shortcut."
  !insertmacro MUI_DESCRIPTION_TEXT ${SecDesktop} "A shortcut to OpenSolver on the desktop."
!insertmacro MUI_FUNCTION_DESCRIPTION_END

; ----- uninstall ---------------------------------------------------------------------------------

; The app's own files and folders by name, never the whole folder: whatever else is in it stays.
; The shortcuts are the ones the installer wrote down: a OpenSolver.lnk it did not make is not its own.
Section "Uninstall"
  ReadRegStr $0 HKCU "${APP_KEY}" "StartMenuShortcut"
  ${If} $0 != ""
    Delete "$0"
  ${EndIf}
  ReadRegStr $0 HKCU "${APP_KEY}" "DesktopShortcut"
  ${If} $0 != ""
    Delete "$0"
  ${EndIf}

  Delete "$INSTDIR\OpenSolver.exe"
  Delete "$INSTDIR\solver.exe"
  RMDir /r "$INSTDIR\python"
  RMDir /r "$INSTDIR\viewer"
  RMDir /r "$INSTDIR\tools"
  RMDir /r "$INSTDIR\flops"
  Delete "$INSTDIR\${UNINSTALLER}"
  RMDir "$INSTDIR"

  ; the launcher's log; the library and the job queue in %USERPROFILE%\.wsolver are the user's
  RMDir /r "$LOCALAPPDATA\OpenSolver\Logs"
  RMDir "$LOCALAPPDATA\OpenSolver"

  DeleteRegKey HKCU "${UNINSTALL_KEY}"
  DeleteRegKey HKCU "${APP_KEY}"
SectionEnd
