@echo off
setlocal
REM ---------------------------------------------------------------------------
REM 적재 실행 래퍼 (Windows 작업 스케줄러에 등록하는 파일)
REM   - build_f3.py : 매 실행마다
REM   - get_move.py : shift 기준시각(06 / 14 / 22시)에만 이어서
REM
REM 스케줄러는 로그온 사용자와 다른 계정/환경으로 돌 수 있어 PATH 에 python 이
REM 없을 수 있다. 실제로 "동작이 완료되었습니다 (2)" = 파일을 찾을 수 없음.
REM 아래 순서로 실행기를 찾고, 못 찾으면 로그에 남기고 종료한다.
REM   1) 환경변수 HOLDWAITANAL_PYTHON 에 지정된 경로
REM   2) PATH 의 python
REM   3) py 런처 (py -3)
REM ---------------------------------------------------------------------------
cd /d "%~dp0.."
if not exist logs mkdir logs
set LOG=logs\build_f3.log

echo.>> "%LOG%"
echo ===== %DATE% %TIME% =====>> "%LOG%"
echo [ENV] cwd=%CD%>> "%LOG%"

set PY=
if defined HOLDWAITANAL_PYTHON if exist "%HOLDWAITANAL_PYTHON%" set PY="%HOLDWAITANAL_PYTHON%"
if not defined PY (
    for /f "delims=" %%p in ('where python 2^>nul') do (
        if not defined PY set PY="%%p"
    )
)
if not defined PY (
    py -3 --version >nul 2>&1 && set PY=py -3
)
if not defined PY (
    echo [ERROR] python 실행기를 찾지 못했습니다.>> "%LOG%"
    echo         작업 스케줄러의 PATH 에 python 이 없습니다.>> "%LOG%"
    echo         해결: 시스템 환경변수 HOLDWAITANAL_PYTHON 에 python.exe 전체 경로를 지정하거나,>> "%LOG%"
    echo               작업의 [동작] 프로그램을 python.exe 로, 인수를 스크립트 경로로 지정하세요.>> "%LOG%"
    exit /b 2
)
echo [ENV] python=%PY%>> "%LOG%"

%PY% getdata\build_f3.py >> "%LOG%" 2>&1
echo [EXIT] build_f3 =%ERRORLEVEL%>> "%LOG%"

for /f %%h in ('powershell -NoProfile -Command "(Get-Date).Hour" 2^>nul') do set HH=%%h
set RUNMOVE=0
if "%HH%"=="6"  set RUNMOVE=1
if "%HH%"=="14" set RUNMOVE=1
if "%HH%"=="22" set RUNMOVE=1

if "%RUNMOVE%"=="1" (
    echo.>> logs\get_move.log
    echo ===== %DATE% %TIME% ^(shift 시점 자동 실행^) =====>> logs\get_move.log
    %PY% getdata\get_move.py >> logs\get_move.log 2>&1
    echo [EXIT] get_move =%ERRORLEVEL%>> logs\get_move.log
)
endlocal
