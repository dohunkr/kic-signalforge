# 개발용 서버 실행 헬퍼 (백그라운드)
# 프로젝트 루트는 이 스크립트 위치에서 자동 계산합니다(경로 인코딩 문제 회피).
param(
    [int]$Port = 8000
)

$Root = Split-Path -Parent $PSScriptRoot
$Root = (Resolve-Path $Root).Path
$logDir = Join-Path $Root "data"
if (-not (Test-Path -LiteralPath $logDir)) { New-Item -ItemType Directory -Path $logDir | Out-Null }

$existing = Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue
if ($existing) {
    foreach ($conn in $existing) {
        Stop-Process -Id $conn.OwningProcess -Force -ErrorAction SilentlyContinue
    }
    Start-Sleep -Seconds 2
}

$out = Join-Path $logDir "server.out.log"
$err = Join-Path $logDir "server.err.log"
$proc = Start-Process -FilePath "python" -ArgumentList "run.py" -WorkingDirectory $Root `
    -WindowStyle Hidden -RedirectStandardOutput $out -RedirectStandardError $err -PassThru
$proc.Id | Out-File -FilePath (Join-Path $logDir "server.pid") -Encoding ascii
Write-Output "started pid=$($proc.Id) port=$Port"
Start-Sleep -Seconds 6
Write-Output "ready"
