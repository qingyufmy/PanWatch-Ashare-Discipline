param(
    [string]$TradeDate = (Get-Date -Format 'yyyy-MM-dd')
)

$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest

$sourceRoot = (Resolve-Path -LiteralPath (Join-Path $PSScriptRoot '..')).Path
$publicRoot = 'D:\盯盘\PanWatch-public'
$expectedRemote = 'https://github.com/qingyufmy/PanWatch-Ashare-Discipline.git'
$python = Join-Path $sourceRoot '.venv\Scripts\python.exe'
$exporter = Join-Path $sourceRoot 'scripts\export-public-daily.py'
$logs = Join-Path $sourceRoot 'data\public-upload-logs'

if ($TradeDate -notmatch '^\d{4}-\d{2}-\d{2}$') { throw 'Invalid trade date' }
$day = [datetime]::ParseExact($TradeDate, 'yyyy-MM-dd', [cultureinfo]::InvariantCulture)
if ($day.Date -gt (Get-Date).Date) { throw 'Future trade date refused' }
if ($day.Date -eq (Get-Date).Date -and (Get-Date).TimeOfDay -lt [timespan]::Parse('15:40:00')) {
    throw 'Current trading day is not past the 15:40 archive cutoff'
}
if (-not (Test-Path -LiteralPath $python) -or -not (Test-Path -LiteralPath $exporter)) {
    throw 'Local Python runtime or exporter is missing'
}
if (-not (Test-Path -LiteralPath (Join-Path $publicRoot '.git'))) {
    throw 'Public repository checkout is missing'
}

$remote = (& git -C $publicRoot remote get-url origin).Trim()
if ($LASTEXITCODE -ne 0 -or $remote -ne $expectedRemote) {
    throw 'Public repository remote differs from the configured destination'
}

New-Item -ItemType Directory -Path $logs -Force | Out-Null
Start-Transcript -Path (Join-Path $logs "publish-$TradeDate.log") -Append | Out-Null
try {
    $archiveRoot = Join-Path $publicRoot 'daily_archive'
    $output = & $python $exporter --trade-date $TradeDate --output-root $archiveRoot
    if ($LASTEXITCODE -ne 0) { throw 'Daily export failed' }
    $manifest = ($output -join "`n") | ConvertFrom-Json
    if ($manifest.counts.workflow_runs -lt 1) {
        Write-Output "No workflow runs for $TradeDate; nothing to publish"
        exit 0
    }
    $dayPath = Join-Path $archiveRoot $TradeDate
    foreach ($file in (Get-ChildItem -LiteralPath $dayPath -Filter '*.json' -File)) {
        $content = [IO.File]::ReadAllText($file.FullName)
        if ($content -match 'open-apis/bot/v2/hook/' -or
            $content -match 'github_pat_[A-Za-z0-9_]{20,}' -or
            $content -match 'sk-[A-Za-z0-9_-]{16,}') {
            throw "Possible credential in public archive file $($file.Name)"
        }
    }
    & git -C $publicRoot add -- "daily_archive/$TradeDate"
    if ($LASTEXITCODE -ne 0) { throw 'Git staging failed' }
    & git -C $publicRoot diff --cached --quiet
    if ($LASTEXITCODE -eq 0) {
        Write-Output "Archive unchanged for $TradeDate"
        exit 0
    }
    & git -C $publicRoot -c user.name=Codex -c user.email=codex@localhost commit -m "data(portfolio): 归档 $TradeDate 交易日结果"
    if ($LASTEXITCODE -ne 0) { throw 'Git commit failed' }
    $env:GIT_TERMINAL_PROMPT = '0'
    & git -C $publicRoot -c credential.interactive=never push origin main
    if ($LASTEXITCODE -ne 0) { throw 'Git push failed' }
    Write-Output "Published $TradeDate to $expectedRemote; gate=$($manifest.acceptance_gate); close_steps=$($manifest.after_close_steps_present)"
}
finally {
    Stop-Transcript | Out-Null
}
