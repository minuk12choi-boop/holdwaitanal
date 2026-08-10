@echo off
REM ---------------------------------------------------------------------------
REM 적재 실행 래퍼 (Windows 작업 스케줄러에 등록하는 파일)
REM
REM  - build_f3.py  : 매 실행마다
REM  - get_move.py  : shift 기준시각(06 / 14 / 22시)에만 이어서
REM
REM 작업 스케줄러는 작업 폴더를 지정하지 않으면 C:\Windows\System32 에서
REM 실행한다. .env 와 상대경로가 어긋나므로 반드시 이 래퍼를 통해 실행한다.
REM ---------------------------------------------------------------------------
cd /d "%~dp0.."
if not exist logs mkdir logs

echo. >> logs\build_f3.log
echo ===== %DATE% %TIME% ===== >> logs\build_f3.log
python getdata\build_f3.py >> logs\build_f3.log 2>&1

REM PowerShell 은 시(hour)를 정수로 준다 (6, 14, 22)
for /f %%h in ('powershell -NoProfile -Command "(Get-Date).Hour"') do set HH=%%h

set RUNMOVE=0
if "%HH%"=="6"  set RUNMOVE=1
if "%HH%"=="14" set RUNMOVE=1
if "%HH%"=="22" set RUNMOVE=1

if "%RUNMOVE%"=="1" (
    echo. >> logs\get_move.log
    echo ===== %DATE% %TIME% ^(shift 시점 자동 실행^) ===== >> logs\get_move.log
    python getdata\get_move.py >> logs\get_move.log 2>&1
)
