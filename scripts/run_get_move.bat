@echo off
REM ---------------------------------------------------------------------------
REM get_move.py 단독 실행 래퍼
REM
REM 평상시에는 run_build_f3.bat 이 22시대에 알아서 이어 돌리므로 별도 스케줄
REM 등록이 필요 없다. 수동 재적재나 --full 백필용으로 쓴다.
REM   예)  scripts\run_get_move.bat --full
REM ---------------------------------------------------------------------------
cd /d "%~dp0.."
if not exist logs mkdir logs

echo. >> logs\get_move.log
echo ===== %DATE% %TIME% ^(수동 실행 %*^) ===== >> logs\get_move.log
python getdata\get_move.py %* >> logs\get_move.log 2>&1
