@echo off
setlocal enableextensions enabledelayedexpansion

rem  Thin launcher. All settings live in config.ini (read by the .ps1).
rem  This only resolves the input folder and runs the script.

rem --- Console colours (ANSI, same palette as colors.py) -------
for /f %%a in ('echo prompt $E ^| cmd') do set "esc=%%a"
set "c_title=%esc%[38;2;202;165;96m"
set "c_head=%esc%[95m"
set "c_info=%esc%[94m"
set "c_ok=%esc%[92m"
set "c_err=%esc%[91m"
set "c_reset=%esc%[0m"

echo %c_title%========================================%c_reset%
echo %c_title%  Render Blender Thumbnails%c_reset%
echo %c_title%========================================%c_reset%
echo.

rem --- Resolve the input folder: dragged-on argument > prompt ---
if not "%~1"=="" set "source=%~1"
if not defined source (
    echo Enter the folder containing meshes.
    echo.
    set /p "source=%c_info%Input folder: %c_reset%"
)

if not defined source goto :no_source_given
set "source=%source:"=%"
if "%source:~-1%"=="\" set "source=%source:~0,-1%"
if not defined source   goto :no_source_given
if not exist "%source%\" goto :no_source_exist

if not "%~1"=="" echo %c_info%Source :%c_reset% %source%
echo.
echo %c_head%Generating thumbnails...%c_reset%
echo.

powershell -ExecutionPolicy Bypass -File "%~dp0render_blender_thumbnails.ps1" -input_dir "%source%"
if errorlevel 1 (
    echo.
    echo %c_err%Thumbnail generation reported an error.%c_reset%
    pause
    exit /b 1
)
exit /b 0


:no_source_given
echo %c_err%ERROR: No input folder provided.%c_reset%
pause
exit /b 1

:no_source_exist
echo %c_err%ERROR: Input folder does not exist:%c_reset%
echo   %source%
pause
exit /b 1
