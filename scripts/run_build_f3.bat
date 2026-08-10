@echo off
REM ---------------------------------------------------------------------------
REM build_f3.py 실행 래퍼 (Windows 작업 스케줄러용)
REM
REM 작업 스케줄러는 작업 폴더를 지정하지 않으면 C:\Windows\System32 에서 실행한다.
REM .env 와 상대경로가 어긋나므로 반드시 이 래퍼를 쓴다.
REM
REM 22시대 실행에서는 get_move.py 도 이어서 돌린다.
REM   - 업무일이 22:00 에 끝나므로 그 직후가 MOVE 적재 시점이다.
REM   - build_f3(약 3~4분) 이 끝난 뒤 시작되며, get_move 는 교체 적재라
REM     여러 번 돌아도 결과가 같다(멱등).
REM ---------------------------------------------------------------------------
cd /d "%~dp0.."
if not exist logs mkdir logs

echo. >> logs\build_f3.log
echo ===== %DATE% %TIME% ===== >> logs\build_f3.log
python getdata\build_f3.py >> logs\build_f3.log 2>&1

for /f %%h in ('powershell -NoProfile -Command "(Get-Date).Hour"') do set HH=%%h
if "%HH%"=="22" (
    echo. >> logs\get_move.log
    echo ===== %DATE% %TIME% ^(build_f3 이후 연속 실행^) ===== >> logs\get_move.log
    python getdata\get_move.py >> logs\get_move.log 2>&1
)
