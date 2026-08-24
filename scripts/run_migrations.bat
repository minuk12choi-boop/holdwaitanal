@echo off
REM Apply pending app_db migrations.
REM
REM Run by double-click, or from a terminal:
REM   scripts\run_migrations.bat
REM
REM Safe to run more than once.

setlocal
cd /d "%~dp0.."

set /p PW=MySQL root password:

for %%F in (
  migrate_holdtype_sort.sql
  migrate_virtual_step_status.sql
  migrate_virtual_eqp.sql
  migrate_fill_area.sql
) do (
  if exist "getdata\%%F" (
    echo.
    echo [RUN ] %%F
    REM Let mysql read the file so UTF-8 column names survive.
    mysql --default-character-set=utf8mb4 -u root -p%PW% app_db -e "source getdata/%%F"
    if errorlevel 1 (
      echo [FAIL] %%F
      goto :done
    )
    echo [DONE] %%F
  ) else (
    echo [SKIP] %%F  ^(file not found^)
  )
)

echo.
echo All migrations finished. Restart the web server.

:done
endlocal
pause
