@echo off
setlocal
REM ---------------------------------------------------------------------------
REM 적재 실행 래퍼 (Windows 작업 스케줄러에 등록하는 파일)
REM   - build_f3.py : 매 실행마다
REM   - get_move.py : shift 기준시각(06 / 14 / 22시)에만 이어서
REM ---------------------------------------------------------------------------
cd /d "%~dp0.."
if not exist logs mkdir logs
set "LOG=logs\build_f3.log"

echo.>> "%LOG%"
echo ===== %DATE% %TIME% =====>> "%LOG%"
echo [ENV] cwd=%CD%>> "%LOG%"

call "%~dp0_find_python.bat"
if not defined PYEXE (
    echo [ERROR] python 실행기를 찾지 못했습니다.>> "%LOG%"
    echo         시스템 환경변수 HOLDWAITANAL_PYTHON 에 python.exe 전체 경로를 지정하세요.>> "%LOG%"
    exit /b 2
)
echo [ENV] python=%PYEXE%>> "%LOG%"

"%PYEXE%" "getdata\build_f3.py" >> "%LOG%" 2>&1
echo [EXIT] build_f3 =%ERRORLEVEL%>> "%LOG%"

for /f "usebackq delims=" %%h in (`powershell -NoProfile -Command "(Get-Date).Hour" 2^>nul`) do set "HH=%%h"
set "RUNMOVE=0"
if "%HH%"=="6"  set "RUNMOVE=1"
if "%HH%"=="14" set "RUNMOVE=1"
if "%HH%"=="22" set "RUNMOVE=1"

if "%RUNMOVE%"=="1" (
    echo.>> logs\get_move.log
    echo ===== %DATE% %TIME% ^(shift 시점 자동 실행^) =====>> logs\get_move.log
    "%PYEXE%" "getdata\get_move.py" >> logs\get_move.log 2>&1
    echo [EXIT] get_move =%ERRORLEVEL%>> logs\get_move.log
)
endlocal
