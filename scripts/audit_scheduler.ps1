# Read-only Windows checks. Does not register tasks or run trading commands.
$ErrorActionPreference = 'Stop'
$repo = Split-Path $PSScriptRoot -Parent
$setup = Join-Path $repo 'scheduler\setup_scheduler.ps1'
$tokens = $null
$parseErrors = $null
[void][System.Management.Automation.Language.Parser]::ParseFile($setup, [ref]$tokens, [ref]$parseErrors)
$results = [ordered]@{
    syntax_errors = $parseErrors.Count
    windows_timezone = (Get-TimeZone).Id
    configured_folder_exists = (Test-Path 'C:\mt5_tradebot')
}
try {
    New-ScheduledTaskTrigger -Daily -At '08:00' -RepetitionInterval (New-TimeSpan -Minutes 1) -RepetitionDuration (New-TimeSpan -Hours 12) | Out-Null
    $results.cycle_trigger = 'passed'
} catch {
    $results.cycle_trigger = $_.Exception.Message
}
$results.batch_files_with_trailing_backtick = @(
    Get-ChildItem (Join-Path $repo 'scheduler') -Filter '*.bat' | Where-Object {
        (Get-Content $_.FullName -Raw).TrimEnd().EndsWith('`')
    } | Select-Object -ExpandProperty Name
)
try {
    $results.registered_tasks = @(Get-ScheduledTask | Where-Object TaskName -Like 'MT5-TradeBot-*' | Select-Object TaskName, State)
} catch {
    $results.task_query_error = $_.Exception.Message
}
$results | ConvertTo-Json -Depth 4
