# app_db 마이그레이션을 한 번에 적용한다.
#
# 실행 (PowerShell / VS Code 터미널, 프로젝트 어디서든):
#   powershell -ExecutionPolicy Bypass -File scripts\run_migrations.ps1
#
# 이미 적용된 것은 건너뛰므로 여러 번 돌려도 안전하다.

$ErrorActionPreference = "Stop"

# 이 스크립트가 있는 폴더의 상위 = 프로젝트 뿌리
$root = Split-Path -Parent $PSScriptRoot
$sqlDir = Join-Path $root "getdata"

$files = @(
  "migrate_holdtype_sort.sql",        # HOLD 유형 적용 순서(sort_no)
  "migrate_virtual_step_status.sql",  # 가상스텝 상태 보정
  "migrate_virtual_eqp.sql"           # NRDSEND / NRDMEAS 를 가상스텝으로
)

$pw = Read-Host "MySQL root 비밀번호" -AsSecureString
$plain = [Runtime.InteropServices.Marshal]::PtrToStringAuto(
  [Runtime.InteropServices.Marshal]::SecureStringToBSTR($pw))

foreach ($f in $files) {
  $path = Join-Path $sqlDir $f
  if (-not (Test-Path $path)) {
    Write-Host "[건너뜀] $f  (파일 없음)" -ForegroundColor DarkYellow
    continue
  }
  Write-Host ""
  Write-Host "[실행] $f" -ForegroundColor Cyan
  Get-Content $path -Encoding UTF8 | mysql --default-character-set=utf8mb4 -u root "-p$plain" app_db
  if ($LASTEXITCODE -ne 0) {
    Write-Host "[실패] $f  (위 오류를 확인하세요)" -ForegroundColor Red
    exit 1
  }
  Write-Host "[완료] $f" -ForegroundColor Green
}

Write-Host ""
Write-Host "모두 끝났습니다. 웹 서버를 다시 띄우세요." -ForegroundColor Green
