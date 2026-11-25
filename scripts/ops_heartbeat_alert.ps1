# Daily Task Scheduler job (09:00):
#  1. Read-only daily sync, so pin/social/listing results are measured daily
#     (weekly syncs can't see a 7-day post window). Read gates only: no writes.
#  2. Pop up a desktop alert if the weekly Etsy ops run hasn't happened
#     (`ops heartbeat` exits 1 when the last LIVE ops run is > 8 days old; it
#     dates ops runs, not syncs, so step 1 never hides a missed run).
$repo = Split-Path -Parent $PSScriptRoot
Set-Location $repo
$env:PYTHONIOENCODING = 'utf-8'
$env:SHOPSTEWARD_LIVE_ETSY_READ = '1'
$env:SHOPSTEWARD_LIVE_BUFFER = '1'
$sync = (& uv run shopsteward sync --live 2>&1 | Out-String).Trim()
$syncOk = $LASTEXITCODE -eq 0
$null = (& uv run shopsteward ops buffer-sync --live 2>&1 | Out-String)
$syncOk = $syncOk -and ($LASTEXITCODE -eq 0)
$out = (& uv run shopsteward ops heartbeat 2>&1 | Out-String).Trim()
$shell = New-Object -ComObject WScript.Shell
if ($LASTEXITCODE -ne 0) {
    $null = $shell.Popup("Weekly Etsy ops run missed.`n$out`nRun it per docs/runbooks/weekly-etsy-ops-run.md", 0, 'ShopSteward', 48)
}
if (-not $syncOk) {
    $tail = ($sync -split "`n" | Select-Object -Last 3) -join "`n"
    $null = $shell.Popup("Daily ShopSteward sync failed.`n$tail", 0, 'ShopSteward', 48)
}
