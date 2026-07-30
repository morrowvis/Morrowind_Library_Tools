@echo off
setlocal enableextensions enabledelayedexpansion

rem  Thin launcher. All settings live in config.ini (read by the .py).
rem  This only resolves the two library folders and runs the script.

rem --- Console colours (ANSI, same palette as colors.py) -------
for /f %%a in ('echo prompt $E ^| cmd') do set "esc=%%a"
set "c_title=%esc%[38;2;202;165;96m"
set "c_info=%esc%[94m"
set "c_head=%esc%[95m"
set "c_ok=%esc%[92m"
set "c_err=%esc%[91m"
set "c_reset=%esc%[0m"

echo %c_title%========================================%c_reset%
echo %c_title%  Extract Library Changes%c_reset%
echo %c_title%========================================%c_reset%
echo.

rem --- Check prerequisites (see the README) ---
where python >nul 2>nul
if errorlevel 1 goto :no_python

rem --- Resolve NEW library: first dragged-on argument > prompt ---
set "new_lib="
if not "%~1"=="" set "new_lib=%~1"
if not defined new_lib (
    set /p "new_lib=%c_info%New library (Data Files): %c_reset%"
)
if not defined new_lib goto :no_input_given
set "new_lib=%new_lib:"=%"
if "%new_lib:~-1%"=="\" set "new_lib=%new_lib:~0,-1%"
if not exist "%new_lib%\" goto :no_new_exist

rem --- Resolve OLD library: second dragged-on argument > prompt ---
set "old_lib="
if not "%~2"=="" set "old_lib=%~2"
if not defined old_lib (
    set /p "old_lib=%c_info%Old library (Data Files): %c_reset%"
)
if not defined old_lib goto :no_input_given
set "old_lib=%old_lib:"=%"
if "%old_lib:~-1%"=="\" set "old_lib=%old_lib:~0,-1%"
if not exist "%old_lib%\" goto :no_old_exist

echo.
echo %c_info%New library:%c_reset% %new_lib%
echo %c_info%Old library:%c_reset% %old_lib%
echo.
echo %c_head%Comparing...%c_reset%
echo.

python "%~dp0extract_library_changes.py" "%new_lib%" "%old_lib%"
if errorlevel 1 goto :finished_err

rem  Hashing a full library can take a while, so signal completion with a GUI
rem  MessageBox (inline, DPI-aware) that surfaces if you've tabbed away, then open
rem  the output folder once it's dismissed. See the repo CLAUDE.md.
echo.
echo %c_ok%Done. Changes copied to the output folder.%c_reset%
powershell -NoProfile -Command "Add-Type -AssemblyName System.Windows.Forms; Add-Type -Namespace N -Name W -MemberDefinition ('[DllImport(' + [char]34 + 'user32.dll' + [char]34 + ')] public static extern bool SetProcessDPIAware();'); [void][N.W]::SetProcessDPIAware(); [System.Windows.Forms.Application]::EnableVisualStyles(); [void][System.Windows.Forms.MessageBox]::Show('Changes copied to the output folder.','Extract Library Changes',[System.Windows.Forms.MessageBoxButtons]::OK,[System.Windows.Forms.MessageBoxIcon]::Information)"
if exist "%~dp0output\" start "" "%~dp0output"
exit /b 0

:finished_err
echo.
echo %c_err%Comparison reported an error.%c_reset%
powershell -NoProfile -Command "Add-Type -AssemblyName System.Windows.Forms; Add-Type -Namespace N -Name W -MemberDefinition ('[DllImport(' + [char]34 + 'user32.dll' + [char]34 + ')] public static extern bool SetProcessDPIAware();'); [void][N.W]::SetProcessDPIAware(); [System.Windows.Forms.Application]::EnableVisualStyles(); [void][System.Windows.Forms.MessageBox]::Show('Finished with errors - see the console window.','Extract Library Changes',[System.Windows.Forms.MessageBoxButtons]::OK,[System.Windows.Forms.MessageBoxIcon]::Information)"
if exist "%~dp0output\" start "" "%~dp0output"
exit /b 1


:no_python
echo %c_err%Missing prerequisite: Python was not found on PATH.%c_reset%
echo See the prerequisites in the README.
pause
exit /b 1

:no_input_given
echo %c_err%ERROR: Both a new and an old library folder are required.%c_reset%
pause
exit /b 1

:no_new_exist
echo %c_err%ERROR: New library folder does not exist:%c_reset%
echo   %new_lib%
pause
exit /b 1

:no_old_exist
echo %c_err%ERROR: Old library folder does not exist:%c_reset%
echo   %old_lib%
pause
exit /b 1
