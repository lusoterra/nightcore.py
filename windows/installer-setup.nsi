; installer-setup.nsi — nightcore installer (built by build.sh).
; Payload: all open tools; with --with-qaac also qaac + Apple DLLs.
; Installs to Program Files, optionally adds the install dir to machine
; PATH (on by default, untick to skip), writes an uninstaller.
; @VER@/@ROOT@/@PAYLOAD@/@OUT@/@TAG@ substituted at build time.
Unicode true
Name "nightcore @VER@@TAG@"
OutFile "@ROOT@/dist/@OUT@"
RequestExecutionLevel admin
ShowInstDetails show
Caption "nightcore @VER@@TAG@ setup"
BrandingText "nightcore setup@TAG@"

!include "FileFunc.nsh"
!include "StrFunc.nsh"
!include "WordFunc.nsh"
!include "x64.nsh"
${StrStr}
${UnStrStr}

InstallDir "$PROGRAMFILES64\nightcore"
Page components
Page directory
Page instfiles

Var ARGS

Function AddPath
    ; append $INSTDIR to HKLM PATH once (admin: machine-wide)
    ReadRegStr $0 HKLM "SYSTEM\CurrentControlSet\Control\Session Manager\Environment" "Path"
    ${StrStr} $1 $0 "$INSTDIR"
    ${If} $1 == ""
        WriteRegExpandStr HKLM "SYSTEM\CurrentControlSet\Control\Session Manager\Environment" "Path" "$0;$INSTDIR"
        System::Call 'user32::SendMessageTimeout(i 0xffff, i 0x1a, i 0, t "Environment", i 0, i 5000, *i .r0)'
    ${EndIf}
FunctionEnd

Function un.RemovePath
    ; drop every spelling of our entry, then broadcast
    ReadRegStr $0 HKLM "SYSTEM\CurrentControlSet\Control\Session Manager\Environment" "Path"
    ${WordReplace} $0 ";$INSTDIR" "" "+" $1
    ${WordReplace} $1 "$INSTDIR;" "" "+" $2
    ${WordReplace} $2 "$INSTDIR" "" "+" $3
    WriteRegExpandStr HKLM "SYSTEM\CurrentControlSet\Control\Session Manager\Environment" "Path" "$3"
    System::Call 'user32::SendMessageTimeout(i 0xffff, i 0x1a, i 0, t "Environment", i 0, i 5000, *i .r0)'
FunctionEnd

Section "install" SEC_INSTALL
    SetOutPath "$INSTDIR"
    File /r "@ROOT@/build/@PAYLOAD@/*"
    WriteUninstaller "$INSTDIR\uninstall.exe"
    WriteRegStr HKLM "Software\Microsoft\Windows\CurrentVersion\Uninstall\nightcore" \
        "DisplayName" "nightcore @VER@"
    WriteRegStr HKLM "Software\Microsoft\Windows\CurrentVersion\Uninstall\nightcore" \
        "DisplayVersion" "@VER@"
    WriteRegStr HKLM "Software\Microsoft\Windows\CurrentVersion\Uninstall\nightcore" \
        "UninstallString" "$INSTDIR\uninstall.exe"
    WriteRegStr HKLM "Software\Microsoft\Windows\CurrentVersion\Uninstall\nightcore" \
        "InstallLocation" "$INSTDIR"
    WriteRegDWORD HKLM "Software\Microsoft\Windows\CurrentVersion\Uninstall\nightcore" \
        "NoModify" 1
    WriteRegDWORD HKLM "Software\Microsoft\Windows\CurrentVersion\Uninstall\nightcore" \
        "NoRepair" 1
    DetailPrint "Installed. Type nightcore --version in a NEW terminal."
    DetailPrint "Run nightcore --check to see the bundled toolset."
    DetailPrint "For qaac: copy YOUR Apple DLLs (CoreAudioToolbox.dll, CoreFoundation.dll, icudt*.dll, libdispatch.dll, libicuin*.dll, libicuuc*.dll, objc.dll) into $INSTDIR\tools and run nightcore --check."
    DetailPrint "PowerShell completions: $INSTDIR\completions\nightcore.ps1 (dot-source it from your profile)."
SectionEnd

Section "Add install folder to machine PATH" SEC_PATH
    ; on by default (untick to skip); `nightcore` then works in new terminals
    Call AddPath
SectionEnd

Section "uninstall"
    Call un.RemovePath
    Delete "$INSTDIR\uninstall.exe"
    RMDir /r "$INSTDIR"
    DeleteRegKey HKLM "Software\Microsoft\Windows\CurrentVersion\Uninstall\nightcore"
SectionEnd
