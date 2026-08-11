@echo off
REM ---------------------------------------------------------------------------
REM PYEXE 에 python 실행파일 '하나'만 담는다.
REM 여러 토큰("py -3" 같은)을 변수에 담으면 뒤 인수와 붙어 깨진다.
REM   예) %PY% getdata\build_f3.py  ->  py -3getdata\build_f3.py  ->  '3.py' 오류
REM 찾는 순서: HOLDWAITANAL_PYTHON  ->  PATH 의 python  ->  py 런처가 알려주는 경로
REM ---------------------------------------------------------------------------
set "PYEXE="

if defined HOLDWAITANAL_PYTHON (
    if exist "%HOLDWAITANAL_PYTHON%" set "PYEXE=%HOLDWAITANAL_PYTHON%"
)

if not defined PYEXE (
    for /f "usebackq delims=" %%p in (`where python 2^>nul`) do (
        if not defined PYEXE set "PYEXE=%%p"
    )
)

if not defined PYEXE (
    for /f "usebackq delims=" %%p in (`py -3 -c "import sys;print(sys.executable)" 2^>nul`) do (
        if not defined PYEXE set "PYEXE=%%p"
    )
)
