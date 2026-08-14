@echo off
setlocal
REM Scheduler entry point. Runs every 30 minutes.
REM   build_f3.py : every run (f3_live always, f3_history at shift boundaries)
REM   get_move.py : every run (window = in-progress shift, max 8h)
REM Keep this file ASCII only.
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

echo.>> logs\get_move.log
echo ===== %DATE% %TIME% auto =====>> logs\get_move.log
"%PYEXE%" "getdata\get_move.py" >> logs\get_move.log 2>&1
echo [EXIT] get_move =%ERRORLEVEL%>> logs\get_move.log
endlocal
