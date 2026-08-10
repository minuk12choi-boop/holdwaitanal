@echo off
REM build_f3.py 실행 래퍼 (Windows 작업 스케줄러용)
REM 작업 스케줄러는 작업 폴더를 지정하지 않으면 C:\Windows\System32 에서 실행한다.
REM .env 와 상대경로가 어긋나므로 반드시 이 래퍼를 쓴다.
cd /d "%~dp0.."
if not exist logs mkdir logs
echo. >> logs\build_f3.log
echo ===== %DATE% %TIME% ===== >> logs\build_f3.log
python getdata\build_f3.py >> logs\build_f3.log 2>&1
