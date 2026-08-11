@echo off
setlocal
REM get_move.py 수동 실행 래퍼 (백필/재적재용). 자동 실행은 run_build_f3.bat 이 처리.
REM   scripts\run_get_move.bat --full
cd /d "%~dp0.."
if not exist logs mkdir logs
set LOG=logs\get_move.log

set PY=
if defined HOLDWAITANAL_PYTHON if exist "%HOLDWAITANAL_PYTHON%" set PY="%HOLDWAITANAL_PYTHON%"
if not defined PY (
    for /f "delims=" %%p in ('where python 2^>nul') do (
        if not defined PY set PY="%%p"
    )
)
if not defined PY py -3 --version >nul 2>&1 && set PY=py -3
if not defined PY (
    echo [ERROR] python 실행기를 찾지 못했습니다.>> "%LOG%"
    exit /b 2
)

echo.>> "%LOG%"
echo ===== %DATE% %TIME% ^(수동 %*^) =====>> "%LOG%"
%PY% getdata\get_move.py %* >> "%LOG%" 2>&1
echo [EXIT] =%ERRORLEVEL%>> "%LOG%"
endlocal
