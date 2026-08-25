@echo off
setlocal enableextensions enabledelayedexpansion

rem  Thin launcher. All settings live in config.ini (read by the .py).
rem  Reads the Blender path from config.ini (its bundled Python runs es3 for the
rem  NIF child expansion), resolves the input folder, and runs the script.

rem --- Console colours (ANSI, same palette as colors.py) -------
for /f %%a in ('echo prompt $E ^| cmd') do set "esc=%%a"
set "c_title=%esc%[38;2;202;165;96m"
set "c_head=%esc%[95m"
set "c_info=%esc%[94m"
set "c_ok=%esc%[92m"
set "c_err=%esc%[91m"
set "c_reset=%esc%[0m"

echo %c_title%========================================%c_reset%
echo %c_title%  Build Master Records%c_reset%
echo %c_title%========================================%c_reset%
echo.

rem --- Check prerequisites (see the README) ---
set "tes3conv=%~dp0..\Shared\tes3conv.exe"
if not exist "%tes3conv%" goto :no_tes3conv

rem --- Read the Blender path from Shared\paths.ini (first "blender" line) ---
set "blender="
for /f "usebackq tokens=1,* delims==" %%a in (`findstr /b /i "blender" "%~dp0..\Shared\paths.ini"`) do set "blender_raw=%%b"
for /f "tokens=* delims= " %%a in ("!blender_raw!") do set "blender=%%a"
if not defined blender goto :no_blender_cfg
if not exist "%blender%" goto :no_blender

rem --- Derive Blender's bundled python.exe (the app is never started) ---
rem  Several python.exe exist under the Blender folder (incl. a venv stub); take
rem  only the real interpreter at \python\bin\python.exe.
for %%I in ("%blender%") do set "blender_dir=%%~dpI"
set "blender_python="
for /f "delims=" %%p in ('dir /b /s "%blender_dir%python.exe" 2^>nul ^| findstr /i /l /e "\python\bin\python.exe"') do set "blender_python=%%p"
if not defined blender_python goto :no_python
if not exist "%blender_python%" goto :no_python

rem --- Resolve the input folder: dragged-on argument > prompt ---
if not "%~1"=="" set "source=%~1"
if not defined source (
    echo Enter an MO2 folder or a folder of plugins.
    echo.
    set /p "source=%c_info%Input folder: %c_reset%"
)

if not defined source goto :no_source_given
set "source=%source:"=%"
if "%source:~-1%"=="\" set "source=%source:~0,-1%"
if not defined source   goto :no_source_given
if not exist "%source%\" goto :no_source_exist

echo %c_info%Python:%c_reset% %blender_python%
if not "%~1"=="" echo %c_info%Source:%c_reset% %source%
echo.
echo %c_head%Building master records...%c_reset%
echo.

rem  Python spawns tes3conv per plugin, which leaves the console's stdin at EOF --
rem  so `pause` would be skipped and the window would close. Signal completion with
rem  a GUI MessageBox instead (inline, DPI-aware), then open the output folder.
rem  See the repo CLAUDE.md.
"%blender_python%" "%~dp0build_master_records.py" -- "%source%"
if errorlevel 1 goto :finished_err

echo.
echo %c_ok%Done.%c_reset%
powershell -NoProfile -Command "Add-Type -AssemblyName System.Windows.Forms; Add-Type -Namespace N -Name W -MemberDefinition ('[DllImport(' + [char]34 + 'user32.dll' + [char]34 + ')] public static extern bool SetProcessDPIAware();'); [void][N.W]::SetProcessDPIAware(); [System.Windows.Forms.Application]::EnableVisualStyles(); [void][System.Windows.Forms.MessageBox]::Show('Master records written to the output folder.','Build Master Records',[System.Windows.Forms.MessageBoxButtons]::OK,[System.Windows.Forms.MessageBoxIcon]::Information)"
if exist "%~dp0output\" start "" "%~dp0output"
exit /b 0

:finished_err
echo.
echo %c_err%Finished with errors (see above).%c_reset%
powershell -NoProfile -Command "Add-Type -AssemblyName System.Windows.Forms; Add-Type -Namespace N -Name W -MemberDefinition ('[DllImport(' + [char]34 + 'user32.dll' + [char]34 + ')] public static extern bool SetProcessDPIAware();'); [void][N.W]::SetProcessDPIAware(); [System.Windows.Forms.Application]::EnableVisualStyles(); [void][System.Windows.Forms.MessageBox]::Show('Finished with errors - see the console window.','Build Master Records',[System.Windows.Forms.MessageBoxButtons]::OK,[System.Windows.Forms.MessageBoxIcon]::Information)"
if exist "%~dp0output\" start "" "%~dp0output"
exit /b 1


:no_tes3conv
echo %c_err%Missing prerequisite: tes3conv.exe not found in the Shared folder:%c_reset%
echo   %tes3conv%
echo See the prerequisites in the README.
pause
exit /b 1

:no_blender_cfg
echo %c_err%Missing prerequisite: no 'blender' path found in Shared\paths.ini.%c_reset%
echo See the prerequisites in the README.
pause
exit /b 1

:no_blender
echo %c_err%Missing prerequisite: Blender not found at:%c_reset%
echo   %blender%
echo Edit the 'blender' path in Shared\paths.ini (see the README).
pause
exit /b 1

:no_python
echo %c_err%Missing prerequisite: Blender's bundled python.exe not found under:%c_reset%
echo   %blender_dir%
echo See the prerequisites in the README.
pause
exit /b 1

:no_source_given
echo %c_err%ERROR: No input folder provided.%c_reset%
pause
exit /b 1

:no_source_exist
echo %c_err%ERROR: Input folder does not exist:%c_reset%
echo   %source%
pause
exit /b 1
