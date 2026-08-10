@echo off
REM ---------------------------------------------------------------------------
REM get_move.py 수동 실행 래퍼
REM
REM 자동 실행은 run_build_f3.bat 이 shift 시점(06/14/22시)에 이어서 처리한다.
REM 이 파일은 스케줄러에 등록하지 않는다. 백필이나 재적재가 필요할 때만 쓴다.
REM
REM   scripts\run_get_move.bat              최근 2일치 재적재
REM   scripts\run_get_move.bat --full       3개월치 백필
REM   scripts\run_get_move.bat --days 7     최근 7일
REM ---------------------------------------------------------------------------
cd /d "%~dp0.."
if not exist logs mkdir logs

echo. >> logs\get_move.log
echo ===== %DATE% %TIME% ^(수동 실행 %*^) ===== >> logs\get_move.log
python getdata\get_move.py %* >> logs\get_move.log 2>&1
