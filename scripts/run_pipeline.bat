@echo off
setlocal enabledelayedexpansion
REM ===========================================================================
REM run_pipeline.bat  -  the ONLY file registered in Windows Task Scheduler.
REM
REM   Task Scheduler
REM     Program : D:\PERSONAL_SPACE\SW\python\7_holdwaitanal\scripts\run_pipeline.bat
REM     Start in: D:\PERSONAL_SPACE\SW\python\7_holdwaitanal
REM     Trigger : daily 22:00, repeat every 30 minutes, for 1 day
REM
REM   Runs, in order:
REM     1) getdata\build_f3.py   f3_live / f3_history
REM     2) getdata\get_move.py   move_shift / move_daily / move_lot
REM
REM   NOTE: get_move runs FIRST. build_f3 computes W/T from f3_move_lot,
REM         so the move data must be current before f3 is built. Running
REM         f3 first leaves W/T one cycle behind.
REM
REM   Manual use (same file, arguments are passed through):
REM     scripts\run_pipeline.bat --force        force build_f3 even if no new cycle
REM     scripts\run_pipeline.bat --move-only --full
REM     scripts\run_pipeline.bat --move-only --hours 6
REM     scripts\run_pipeline.bat --f3-only
REM
REM   Keep this file ASCII + CRLF. UTF-8 or LF breaks cmd parsing.
REM ===========================================================================

cd /d "%~dp0.."
if not exist logs mkdir logs

REM --- pick python -----------------------------------------------------------
set "PYEXE="
if defined HOLDWAITANAL_PYTHON set "PYEXE=%HOLDWAITANAL_PYTHON%"
if not defined PYEXE for /f "delims=" %%P in ('where python 2^>nul') do (
    if not defined PYEXE set "PYEXE=%%P"
)
if not defined PYEXE for /f "delims=" %%P in ('py -3 -c "import sys;print(sys.executable)" 2^>nul') do (
    if not defined PYEXE set "PYEXE=%%P"
)
if not defined PYEXE (
    echo [ERROR] python not found. Set HOLDWAITANAL_PYTHON to python.exe.>> logs\pipeline.log
    exit /b 2
)

REM --- split arguments -------------------------------------------------------
set "RUN_F3=1"
set "RUN_MOVE=1"
set "ARGS="
for %%A in (%*) do (
    if /i "%%A"=="--f3-only"   ( set "RUN_MOVE=0" ) else (
    if /i "%%A"=="--move-only" ( set "RUN_F3=0" )   else (
        set "ARGS=!ARGS! %%A"
    ))
)

echo.>> logs\pipeline.log
echo ===== %DATE% %TIME% =====>> logs\pipeline.log
echo [ENV] python=%PYEXE%>> logs\pipeline.log
echo [ENV] args=%ARGS%>> logs\pipeline.log

REM  NOTE: inside an if-block, %ERRORLEVEL% expands once, before the
REM  command runs, so it always logs the OLD value. Call a label and
REM  read ERRORLEVEL right after the command instead.
set "RC=0"

if "%RUN_MOVE%"=="1" call :step get_move getdata\get_move.py
if "%RUN_F3%"=="1"   call :step build_f3 getdata\build_f3.py

if not "%RC%"=="0" (
    echo [FAIL] pipeline finished with errors. See logs\pipeline.log>> logs\pipeline.log
    echo [FAIL] pipeline finished with errors. See logs\pipeline.log
)

endlocal & exit /b %RC%

:step
echo --- %~1 --->> logs\pipeline.log
"%PYEXE%" %~2 %ARGS% >> logs\pipeline.log 2>&1
set "STEPRC=%ERRORLEVEL%"
echo [EXIT] %~1 =%STEPRC%>> logs\pipeline.log
if not "%STEPRC%"=="0" set "RC=%STEPRC%"
exit /b 0
