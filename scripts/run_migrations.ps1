# Apply pending app_db migrations.
#
# Run from anywhere in the project (PowerShell or VS Code terminal):
#   powershell -ExecutionPolicy Bypass -File scripts\run_migrations.ps1
#
# Safe to run more than once: each script skips work already done.
#
# NOTE: ASCII only. Windows PowerShell 5.1 reads a BOM-less UTF-8 file as
#       ANSI, which corrupts non-ASCII text and breaks parsing.

$ErrorActionPreference = "Stop"

$root = Split-Path -Parent $PSScriptRoot
$sqlDir = Join-Path $root "getdata"

$files = @(
  "migrate_holdtype_sort.sql",
  "migrate_virtual_step_status.sql",
  "migrate_virtual_eqp.sql"
)

$sec = Read-Host "MySQL root password" -AsSecureString
$bstr = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($sec)
$pw = [Runtime.InteropServices.Marshal]::PtrToStringAuto($bstr)

foreach ($f in $files) {
  $path = Join-Path $sqlDir $f
  if (-not (Test-Path $path)) {
    Write-Host "[SKIP] $f  (file not found)" -ForegroundColor DarkYellow
    continue
  }
  Write-Host ""
  Write-Host "[RUN ] $f" -ForegroundColor Cyan
  # Do NOT pipe the file: PowerShell re-encodes it as ANSI and Korean
  # column names such as the current-step flag get mangled.
  # Let the mysql client read the file itself, in utf8mb4.
  $src = ($path -replace '\\', '/')
  mysql --default-character-set=utf8mb4 -u root "-p$pw" app_db -e "source $src"
  if ($LASTEXITCODE -ne 0) {
    Write-Host "[FAIL] $f" -ForegroundColor Red
    exit 1
  }
  Write-Host "[DONE] $f" -ForegroundColor Green
}

Write-Host ""
Write-Host "All migrations finished. Restart the web server." -ForegroundColor Green
