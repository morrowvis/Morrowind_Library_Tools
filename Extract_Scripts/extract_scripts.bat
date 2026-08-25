@echo off
setlocal enableextensions enabledelayedexpansion

rem  Thin launcher. All settings live in config.ini (read by the .py).
rem  Extracts scripts + globals from plugins (MO2 load order, or a folder of
rem  plugins) into output\<PluginName>\, mirroring the ExportCells layout.

rem --- Console colours (ANSI, same palette as colors.py) -------
for /f %%a in ('echo prompt $E ^| cmd') do set "esc=%%a"
set "c_title=%esc%[38;2;202;165;96m"
set "c_head=%esc%[95m"
set "c_info=%esc%[94m"
set "c_ok=%esc%[92m"
set "c_err=%esc%[91m"
set "c_reset=%esc%[0m"

echo %c_title%========================================%c_reset%
echo %c_title%  Extract Scripts%c_reset%
echo %c_title%========================================%c_reset%
echo.

rem --- Check prerequisites (see the README) ---
where python >nul 2>nul
if errorlevel 1 goto :no_python
set "tes3conv=%~dp0..\Shared\tes3conv.exe"
if not exist "%tes3conv%" goto :no_tes3conv

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

if not "%~1"=="" echo %c_info%Source:%c_reset% %source%
echo.
echo %c_head%Extracting...%c_reset%
echo.

rem  Python spawns tes3conv per plugin, which leaves the console's stdin at EOF --
rem  so `pause` would be skipped and the window would close. Signal completion with
rem  a GUI MessageBox instead (inline, DPI-aware). See the repo CLAUDE.md.
python "%~dp0extract_scripts.py" -- "%source%"
if errorlevel 1 goto :finished_err

echo.
echo %c_ok%Done.%c_reset%
powershell -NoProfile -Command "Add-Type -AssemblyName System.Windows.Forms; Add-Type -Namespace N -Name W -MemberDefinition ('[DllImport(' + [char]34 + 'user32.dll' + [char]34 + ')] public static extern bool SetProcessDPIAware();'); [void][N.W]::SetProcessDPIAware(); [System.Windows.Forms.Application]::EnableVisualStyles(); [void][System.Windows.Forms.MessageBox]::Show('Scripts and globals written to the output folder.','Extract Scripts',[System.Windows.Forms.MessageBoxButtons]::OK,[System.Windows.Forms.MessageBoxIcon]::Information)"
rem  Open the output folder once the user dismisses the popup.
if exist "%~dp0output\" start "" "%~dp0output"
exit /b 0

:finished_err
echo.
echo %c_err%Finished with errors (see above).%c_reset%
powershell -NoProfile -Command "Add-Type -AssemblyName System.Windows.Forms; Add-Type -Namespace N -Name W -MemberDefinition ('[DllImport(' + [char]34 + 'user32.dll' + [char]34 + ')] public static extern bool SetProcessDPIAware();'); [void][N.W]::SetProcessDPIAware(); [System.Windows.Forms.Application]::EnableVisualStyles(); [void][System.Windows.Forms.MessageBox]::Show('Finished with errors - see the console window.','Extract Scripts',[System.Windows.Forms.MessageBoxButtons]::OK,[System.Windows.Forms.MessageBoxIcon]::Information)"
if exist "%~dp0output\" start "" "%~dp0output"
exit /b 1


:no_python
echo %c_err%Missing prerequisite: Python was not found on PATH.%c_reset%
echo See the prerequisites in the README.
pause
exit /b 1

:no_tes3conv
echo %c_err%Missing prerequisite: tes3conv.exe not found in the Shared folder:%c_reset%
echo   %tes3conv%
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
