@echo off
setlocal
REM ---------------------------------------------------------------------------
REM Open Spotfire Analyst so it refreshes data and uploads to S3.
REM For option B (scheduler opens and closes). Not needed if option A (resident).
REM
REM Prerequisite: register an IronPython script in the analysis that calls
REM   Application.Current.Close()  after the data function runs.
REM   See docs/spotfire_auto_refresh.md
REM Keep this file ASCII only.
REM ---------------------------------------------------------------------------
set DXP="C:\Program Files (x86)\TIBCO\Spotfire\12.0.4\Spotfire.Dxp.exe"
set FILE="D:\PERSONAL_SPACE\SW\python\7_holdwaitanal\MFM.dxp"

cd /d "%~dp0.."
if not exist logs mkdir logs
echo.>> logs\spotfire_refresh.log
echo ===== %DATE% %TIME% =====>> logs\spotfire_refresh.log

if not exist %DXP% (
    echo [ERROR] Spotfire.Dxp.exe not found: %DXP%>> logs\spotfire_refresh.log
    exit /b 2
)
if not exist %FILE% (
    echo [ERROR] analysis file not found: %FILE%>> logs\spotfire_refresh.log
    exit /b 2
)

start "" /wait %DXP% %FILE%
echo [EXIT] =%ERRORLEVEL%>> logs\spotfire_refresh.log
endlocal
