param()

$ErrorActionPreference = 'Stop'
$repo = (Resolve-Path -LiteralPath (Join-Path $PSScriptRoot '..')).Path
$python = Join-Path $repo '.venv\Scripts\python.exe'
$server = Join-Path $repo 'server.py'
$logs = Join-Path $repo 'data\service-logs'
$health = 'http://127.0.0.1:8000/api/health'

if (-not (Test-Path -LiteralPath $python) -or -not (Test-Path -LiteralPath $server)) {
    throw 'PanWatch Python runtime or server.py is missing'
}

function Test-Backend {
    try {
        $response = Invoke-WebRequest -Uri $health -UseBasicParsing -TimeoutSec 5
        return $response.StatusCode -eq 200
    } catch {
        return $false
    }
}

if (Test-Backend) {
    Write-Output 'PanWatch backend is already healthy'
    exit 0
}

$listener = Get-NetTCPConnection -LocalPort 8000 -State Listen -ErrorAction SilentlyContinue
if ($listener) {
    throw 'Port 8000 is occupied but PanWatch health check failed'
}

New-Item -ItemType Directory -Path $logs -Force | Out-Null
$stamp = Get-Date -Format 'yyyyMMdd-HHmmss'
$stdout = Join-Path $logs "backend-$stamp.out.log"
$stderr = Join-Path $logs "backend-$stamp.err.log"
$env:PLAYWRIGHT_SKIP_BROWSER_INSTALL = '1'
$env:PYTHONIOENCODING = 'utf-8'
$process = Start-Process -FilePath $python -ArgumentList @($server) -WorkingDirectory $repo -WindowStyle Hidden -RedirectStandardOutput $stdout -RedirectStandardError $stderr -PassThru

for ($attempt = 0; $attempt -lt 30; $attempt++) {
    if (Test-Backend) {
        Write-Output "PanWatch backend ready; pid=$($process.Id)"
        exit 0
    }
    if ($process.HasExited) {
        throw "PanWatch backend exited early; see $stderr"
    }
    Start-Sleep -Seconds 1
}
throw "PanWatch backend did not become healthy; see $stderr"
