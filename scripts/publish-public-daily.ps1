param(
    [string]$TradeDate = (Get-Date -Format 'yyyy-MM-dd'),
    [switch]$RequirePaperNav
)

$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest

$sourceRoot = (Resolve-Path -LiteralPath (Join-Path $PSScriptRoot '..')).Path
$publicRoot = Join-Path (Split-Path $sourceRoot -Parent) 'PanWatch-public-archive'
$expectedRemote = 'https://github.com/qingyufmy/PanWatch-Ashare-Discipline.git'
$python = Join-Path $sourceRoot '.venv\Scripts\python.exe'
$exporter = Join-Path $sourceRoot 'scripts\export-public-daily.py'
$logs = Join-Path $sourceRoot 'data\public-upload-logs'

if ($TradeDate -notmatch '^\d{4}-\d{2}-\d{2}$') { throw 'Invalid trade date' }
New-Item -ItemType Directory -Path $logs -Force | Out-Null
Start-Transcript -Path (Join-Path $logs "publish-$TradeDate.log") -Append | Out-Null
try {
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
$branch = (& git -C $publicRoot branch --show-current).Trim()
if ($LASTEXITCODE -ne 0 -or $branch -ne 'main') {
    throw 'Public archive worktree must be on main'
}
$dirty = @(& git -C $publicRoot status --porcelain=v1 --untracked-files=all)
if ($LASTEXITCODE -ne 0) { throw 'Public archive worktree status failed' }
foreach ($entry in $dirty) {
    $path = $entry.Substring(3).Replace('\', '/')
    if (-not $path.StartsWith("daily_archive/$TradeDate/", [StringComparison]::Ordinal)) {
        throw 'Public archive worktree has unrelated or unpublished changes'
    }
}

    & git -C $publicRoot fetch origin main
    if ($LASTEXITCODE -ne 0) { throw 'Git fetch failed' }
    & git -C $publicRoot merge-base --is-ancestor origin/main HEAD
    if ($LASTEXITCODE -ne 0) { throw 'Public archive main is behind or diverged from origin/main' }
    $archiveRoot = Join-Path $publicRoot 'daily_archive'
    $output = & $python $exporter --trade-date $TradeDate --output-root $archiveRoot
    if ($LASTEXITCODE -ne 0) { throw 'Daily export failed' }
    $manifest = ($output -join "`n") | ConvertFrom-Json
    if ($manifest.counts.workflow_runs -lt 1) {
        Write-Output "No workflow runs for $TradeDate; nothing to publish"
        exit 0
    }
    if (-not $manifest.after_close_steps_present) {
        throw "Close review is incomplete: $($manifest.missing_close_steps -join ', ')"
    }
    if (-not $manifest.daily_review_complete) {
        throw 'Daily review model result is missing or invalid'
    }
    if ($RequirePaperNav -and $manifest.paper_nav_status -ne 'CAPTURED') {
        throw "Paper NAV is not source-qualified: $($manifest.paper_nav_status)"
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
    if ($LASTEXITCODE -eq 1) {
        & git -C $publicRoot -c user.name=Codex -c user.email=codex@localhost commit -m "data(portfolio): archive $TradeDate results"
        if ($LASTEXITCODE -ne 0) { throw 'Git commit failed' }
    } elseif ($LASTEXITCODE -ne 0) {
        throw 'Git staged diff failed'
    }
    $env:GIT_TERMINAL_PROMPT = '0'
    & git -C $publicRoot -c credential.interactive=never push origin HEAD:main
    if ($LASTEXITCODE -ne 0) { throw 'Git push failed' }
    $localHead = (& git -C $publicRoot rev-parse HEAD).Trim()
    $remoteLine = & git -C $publicRoot ls-remote origin refs/heads/main
    if ($LASTEXITCODE -ne 0 -or -not $remoteLine) { throw 'Git remote verification failed' }
    $remoteHead = ($remoteLine -split '\s+')[0]
    if ($localHead -ne $remoteHead) { throw 'GitHub main does not match the published archive commit' }
    Write-Output "Published $TradeDate to $expectedRemote at $localHead; gate=$($manifest.acceptance_gate); paper_nav=$($manifest.paper_nav_status)"
}
finally {
    Stop-Transcript | Out-Null
}
