@echo off
REM get_move.py 실행 래퍼 (Windows 작업 스케줄러용)
cd /d "%~dp0.."
if not exist logs mkdir logs
echo. >> logs\get_move.log
echo ===== %DATE% %TIME% ===== >> logs\get_move.log
python getdata\get_move.py >> logs\get_move.log 2>&1
