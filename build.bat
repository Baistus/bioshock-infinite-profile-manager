@echo off
rem Erzeugt mit PyInstaller eine einzelne Datei dist\BioShockProfileManager.exe
rem Aufruf:  build.bat            (wartet am Ende auf eine Taste)
rem          build.bat /nopause   (ohne Warten, z. B. fuer automatische Aufrufe)
setlocal
cd /d "%~dp0"

set "PY=python"
where py >nul 2>nul && set "PY=py -3"

rem Eigene Build-Umgebung und Zwischendateien ausserhalb des Projekts (nicht in OneDrive),
rem damit das normale Python unveraendert bleibt.
set "BASE=%LOCALAPPDATA%\BioShockProfileManager"
set "VENV=%BASE%\build-venv"
set "WORK=%BASE%\build-work"
set "VPY=%VENV%\Scripts\python.exe"

if not exist "%VPY%" (
    echo [1/4] Build-Umgebung anlegen ...
    %PY% -m venv "%VENV%"
    if errorlevel 1 goto :fehler
) else (
    echo [1/4] Build-Umgebung vorhanden.
)

echo [2/4] PyInstaller installieren bzw. aktualisieren ...
"%VPY%" -m pip install --disable-pip-version-check --upgrade pyinstaller
if errorlevel 1 goto :fehler

echo [3/4] Programmsymbol und Versionsinfo erzeugen ...
"%VPY%" -c "import bioshock_profiles as b; b.write_icon_file(r'%WORK%\emblem.ico'); b.write_version_file(r'%WORK%\version.txt')"
if errorlevel 1 goto :fehler

echo [4/4] EXE bauen ...
"%VPY%" -m PyInstaller --noconfirm --clean --onefile --windowed ^
    --name BioShockProfileManager --icon "%WORK%\emblem.ico" --version-file "%WORK%\version.txt" ^
    --workpath "%WORK%" --specpath "%WORK%" --distpath "%~dp0dist" ^
    "%~dp0bioshock_profiles.py"
if errorlevel 1 goto :fehler

echo.
echo Fertig: "%~dp0dist\BioShockProfileManager.exe"
if /i not "%~1"=="/nopause" pause
exit /b 0

:fehler
echo.
echo FEHLER beim Bauen. Ist Python 3 installiert und im PATH? Besteht eine Internetverbindung (fuer PyInstaller)?
if /i not "%~1"=="/nopause" pause
exit /b 1
