# Daily Task Scheduler job: pop up a desktop alert if the weekly Etsy ops run
# hasn't happened (`ops heartbeat` exits 1 when the last live sync is > 8 days old).
$repo = Split-Path -Parent $PSScriptRoot
Set-Location $repo
$out = (& uv run shopsteward ops heartbeat 2>&1 | Out-String).Trim()
if ($LASTEXITCODE -ne 0) {
    $shell = New-Object -ComObject WScript.Shell
    $null = $shell.Popup("Weekly Etsy ops run missed.`n$out`nRun it per docs/runbooks/weekly-etsy-ops-run.md", 0, 'ShopSteward', 48)
}
