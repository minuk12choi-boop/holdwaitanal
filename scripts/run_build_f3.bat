@echo off
setlocal
REM Scheduler entry point.
REM   build_f3.py : every run
REM   get_move.py : only at shift boundaries (06 / 14 / 22)
REM Keep this file ASCII only. Korean comments in CP949/UTF-8 can break cmd parsing.
cd /d "%~dp0.."
if not exist logs mkdir logs
set "LOG=logs\build_f3.log"

echo.>> "%LOG%"
echo ===== %DATE% %TIME% =====>> "%LOG%"
echo [ENV] cwd=%CD%>> "%LOG%"

call "%~dp0_find_python.bat"
if not defined PYEXE (
    echo [ERROR] python not found. Set system env HOLDWAITANAL_PYTHON to python.exe>> "%LOG%"
    exit /b 2
)
echo [ENV] python=%PYEXE%>> "%LOG%"

"%PYEXE%" "getdata\build_f3.py" >> "%LOG%" 2>&1
echo [EXIT] build_f3 =%ERRORLEVEL%>> "%LOG%"

set "HH="
for /f "usebackq delims=" %%h in (`powershell -NoProfile -Command "(Get-Date).Hour" 2^>nul`) do set "HH=%%h"
set "RUNMOVE=0"
if "%HH%"=="6"  set "RUNMOVE=1"
if "%HH%"=="14" set "RUNMOVE=1"
if "%HH%"=="22" set "RUNMOVE=1"

if "%RUNMOVE%"=="1" (
    echo.>> logs\get_move.log
    echo ===== %DATE% %TIME% shift-run =====>> logs\get_move.log
    "%PYEXE%" "getdata\get_move.py" >> logs\get_move.log 2>&1
    echo [EXIT] get_move =%ERRORLEVEL%>> logs\get_move.log
)
endlocal
