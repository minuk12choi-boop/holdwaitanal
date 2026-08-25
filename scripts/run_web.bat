@echo off
REM Start the web server.
REM
REM Usage:  scripts\run_web.bat          (port 8090)
REM         scripts\run_web.bat 9090     (any free port)
REM
REM NOTE: Windows reserves 8000-8030 on this machine
REM       (netsh interface ipv4 show excludedportrange protocol=tcp).
REM       Pick a port outside that range.

setlocal
cd /d "%~dp0..\web"

set PORT=%1
if "%PORT%"=="" set PORT=8090

REM Free the port if a previous run is still holding it.
for /f "tokens=5" %%P in ('netstat -ano ^| findstr ":%PORT%" ^| findstr "LISTENING"') do (
  if not "%%P"=="4" (
    echo [KILL] pid %%P is holding port %PORT%
    taskkill /PID %%P /F >nul 2>&1
  )
)

REM Accept any host header. This is an internal tool on a closed network.
set DJANGO_ALLOWED_HOSTS=*

echo [RUN ] http://localhost:%PORT%/main/
waitress-serve --listen=0.0.0.0:%PORT% config.wsgi:application

endlocal
