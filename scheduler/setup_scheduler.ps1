# Run as Administrator on the Windows MT5 host.
$BotPath = Split-Path $PSScriptRoot -Parent
$User = $env:USERNAME
$Python = "$BotPath\.venv\Scripts\python.exe"

# Confirm timezone configuration (B25)
$Tz = (Get-TimeZone).Id
Write-Host "Host Windows TimeZone: $Tz"
if ($Tz -notlike "*Eastern*") {
    Write-Warning "Host Windows TimeZone is '$Tz'. The scheduler daily triggers (08:00, 00:30) assume Eastern Time (US/Eastern)."
}

# Ensure logs directory exists (B2)
$LogsDir = Join-Path $BotPath "logs"
if (-not (Test-Path $LogsDir)) {
    New-Item -ItemType Directory -Path $LogsDir | Out-Null
}

# Task 1 settings: Server runs continuously with unlimited execution time limit (B7)
$ServerSettings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries -StartWhenAvailable `
    -ExecutionTimeLimit ([TimeSpan]::Zero)

# Batch tasks settings: 20-hour maximum execution limit per daily run
$BatchSettings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries -StartWhenAvailable `
    -ExecutionTimeLimit (New-TimeSpan -Hours 20)

# 1. MT5 TradeBot API Server (runs continuously, starts at system startup/logon - B24)
$Action = New-ScheduledTaskAction -Execute $Python -Argument "-m uvicorn main:app --host 0.0.0.0 --port 8001" -WorkingDirectory $BotPath
$Trigger = New-ScheduledTaskTrigger -AtLogOn
# Register task to run in interactive user session (B1)
Register-ScheduledTask -TaskName "MT5-TradeBot-Server" -Action $Action -Trigger $Trigger -Settings $ServerSettings -User $User -RunLevel Highest -Force

# The batch jobs run their own daily loop. This avoids the invalid
# -Daily/-RepetitionInterval parameter combination and repeats every day.
# 2. Scanner every 30 minutes while the daily loop is active
$Action2 = New-ScheduledTaskAction -Execute "$BotPath\scheduler\run_scanner.bat"
$Trigger2 = New-ScheduledTaskTrigger -Daily -At "08:00"
Register-ScheduledTask -TaskName "MT5-TradeBot-Scanner" -Action $Action2 -Trigger $Trigger2 -Settings $BatchSettings -User $User -Force

# 3. Trading cycle every minute while the daily loop is active
$Action3 = New-ScheduledTaskAction -Execute "$BotPath\scheduler\run_trading_cycle.bat"
$Trigger3 = New-ScheduledTaskTrigger -Daily -At "08:00"
Register-ScheduledTask -TaskName "MT5-TradeBot-Cycle" -Action $Action3 -Trigger $Trigger3 -Settings $BatchSettings -User $User -Force

# 4. Daily summary after both daylight/standard-time New York closes.
$Action4 = New-ScheduledTaskAction -Execute "$BotPath\scheduler\run_daily_summary.bat"
$Trigger4 = New-ScheduledTaskTrigger -Daily -At "00:30"
Register-ScheduledTask -TaskName "MT5-TradeBot-DailySummary" -Action $Action4 -Trigger $Trigger4 -Settings $BatchSettings -User $User -Force

Write-Host "All MT5 TradeBot tasks created successfully!"