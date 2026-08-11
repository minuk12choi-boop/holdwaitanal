@echo off
REM Locate a single python executable and put it in PYEXE.
REM Do NOT put multi-token commands (like "py -3") in the variable:
REM cmd would join it with the next argument and break ("3.py" error).
REM Order: HOLDWAITANAL_PYTHON -> python on PATH -> py launcher's sys.executable
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
