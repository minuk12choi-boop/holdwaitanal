@echo off
REM Start the web server.
REM   - frees port 8020 if a previous run is still holding it
REM   - allows access by IP / hostname, not just localhost
REM
REM Usage:  scripts\run_web.bat

setlocal
cd /d "%~dp0..\web"

REM Free the port if something is still listening.
for /f "tokens=5" %%P in ('netstat -ano ^| findstr ":8020" ^| findstr "LISTENING"') do (
  echo [KILL] pid %%P is holding port 8020
  taskkill /PID %%P /F >nul 2>&1
)

REM Accept any host header. This is an internal tool on a closed network.
set DJANGO_ALLOWED_HOSTS=*

echo [RUN ] http://localhost:8020/main/
waitress-serve --listen=0.0.0.0:8020 config.wsgi:application

endlocal
