@echo off
setlocal enableextensions enabledelayedexpansion

rem  Settings live in config.ini; the record filter lives in build_gallery_data.py.
set "output_directory=%~dp0output"

rem --- Read settings from config.ini (key=value, # comments) ---
for /f "usebackq eol=# tokens=1,* delims== " %%a in ("%~dp0config.ini") do set "%%a=%%b"

rem --- Console colours (ANSI, same palette as colors.py) -------
for /f %%a in ('echo prompt $E ^| cmd') do set "esc=%%a"
set "c_title=%esc%[38;2;202;165;96m"
set "c_head=%esc%[95m"
set "c_info=%esc%[94m"
set "c_ok=%esc%[92m"
set "c_warn=%esc%[93m"
set "c_err=%esc%[91m"
set "c_dim=%esc%[90m"
set "c_reset=%esc%[0m"

echo %c_title%========================================%c_reset%
echo %c_title%  Build Gallery Data%c_reset%
echo %c_title%========================================%c_reset%
echo.

rem --- Check prerequisites (see the README) ---
where python >nul 2>nul
if errorlevel 1 goto :no_python
set "tes3conv=%~dp0..\Shared\tes3conv.exe"
if not exist "%tes3conv%" goto :no_tes3conv

rem --- Resolve the input folder: dragged-on argument > prompt ---
if not "%~1"=="" set "input_directory=%~1"
if not defined input_directory (
    echo Enter the folder containing .esp/.esm files.
    echo.
    set /p "input_directory=%c_info%Input folder: %c_reset%"
)

if not defined input_directory goto :no_input_given
set "input_directory=%input_directory:"=%"
if "%input_directory:~-1%"=="\" set "input_directory=%input_directory:~0,-1%"
if not defined input_directory     goto :no_input_given
if not exist "%input_directory%\"  goto :no_input_exist

if not exist "%output_directory%" mkdir "%output_directory%"
set /a size_threshold=min_plugin_size_kb*1024

if not "%~1"=="" echo %c_info%Input :%c_reset% %input_directory%
echo.
echo %c_head%Processing...%c_reset%
echo.

set /a filecount=0
for %%F in ("!input_directory!\*.esp" "!input_directory!\*.esm") do (
    if exist "%%F" set /a filecount+=1
)

for %%F in ("!input_directory!\*.esp" "!input_directory!\*.esm") do (
    if exist "%%F" (
        set "size=%%~zF"
        set "skip=0"
        if /i "!skip_small_plugins!"=="true" (
            if !filecount! GTR 1 (
                if !size! LEQ !size_threshold! set "skip=1"
            )
        )
        if !skip! EQU 1 (
            echo %c_warn%Skipping ^(under %min_plugin_size_kb% KB^): %%~nxF%c_reset%
        ) else (
            echo %c_dim%Processing: %%~nxF%c_reset%
            if exist "%output_directory%\%%~nF.json" del "%output_directory%\%%~nF.json"
            "%tes3conv%" -o "%%F" "%output_directory%\%%~nF.json"
            if !errorlevel! NEQ 0 (
                echo %c_err%  tes3conv failed on %%~nxF%c_reset%
            ) else (
                if exist "%output_directory%\%%~nF_filtered.json" del "%output_directory%\%%~nF_filtered.json"
                python "%~dp0build_gallery_data.py" "%output_directory%\%%~nF.json" "%output_directory%\%%~nF_filtered.json"
            )
        )
    )
)

rem  Running tes3conv per plugin over a full load order can take a while, so signal
rem  completion with a GUI MessageBox (inline, DPI-aware) that surfaces if you've
rem  tabbed away, then open the output folder once it's dismissed. See the repo CLAUDE.md.
echo.
echo %c_ok%Done.%c_reset%
powershell -NoProfile -Command "Add-Type -AssemblyName System.Windows.Forms; Add-Type -Namespace N -Name W -MemberDefinition ('[DllImport(' + [char]34 + 'user32.dll' + [char]34 + ')] public static extern bool SetProcessDPIAware();'); [void][N.W]::SetProcessDPIAware(); [System.Windows.Forms.Application]::EnableVisualStyles(); [void][System.Windows.Forms.MessageBox]::Show('Gallery data written to the output folder.','Build Gallery Data',[System.Windows.Forms.MessageBoxButtons]::OK,[System.Windows.Forms.MessageBoxIcon]::Information)"
if exist "%output_directory%\" start "" "%output_directory%"
exit /b 0


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

:no_input_given
echo %c_err%ERROR: No input folder provided.%c_reset%
pause
exit /b 1

:no_input_exist
echo %c_err%ERROR: Input folder does not exist:%c_reset%
echo   %input_directory%
pause
exit /b 1
