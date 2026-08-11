@echo off
setlocal
REM Manual run / backfill. Automatic runs are chained from run_build_f3.bat.
REM   scripts\run_get_move.bat --full
cd /d "%~dp0.."
if not exist logs mkdir logs
set "LOG=logs\get_move.log"

call "%~dp0_find_python.bat"
if not defined PYEXE (
    echo [ERROR] python not found. Set system env HOLDWAITANAL_PYTHON to python.exe>> "%LOG%"
    exit /b 2
)

echo.>> "%LOG%"
echo ===== %DATE% %TIME% manual %*=====>> "%LOG%"
echo [ENV] python=%PYEXE%>> "%LOG%"
"%PYEXE%" "getdata\get_move.py" %* >> "%LOG%" 2>&1
echo [EXIT] =%ERRORLEVEL%>> "%LOG%"
endlocal
