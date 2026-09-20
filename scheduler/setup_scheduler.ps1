# Run as Administrator on the Windows MT5 host.
$BotPath = Split-Path $PSScriptRoot -Parent
$User = $env:USERNAME
$Python = "$BotPath\.venv\Scripts\python.exe"
$Common = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries -StartWhenAvailable `
    -ExecutionTimeLimit (New-TimeSpan -Hours 20)

# 1. MT5 TradeBot API Server (runs continuously)
$Action = New-ScheduledTaskAction -Execute $Python -Argument "-m uvicorn main:app --host 0.0.0.0 --port 8001" -WorkingDirectory $BotPath
$Trigger = New-ScheduledTaskTrigger -AtLogOn
Register-ScheduledTask -TaskName "MT5-TradeBot-Server" -Action $Action -Trigger $Trigger -Settings $Common -User $User -RunLevel Highest -Force

# The batch jobs run their own daily loop. This avoids the invalid
# -Daily/-RepetitionInterval parameter combination and repeats every day.
# 2. Scanner every 30 minutes while the daily loop is active
$Action2 = New-ScheduledTaskAction -Execute "$BotPath\scheduler\run_scanner.bat"
$Trigger2 = New-ScheduledTaskTrigger -Daily -At "08:00"
Register-ScheduledTask -TaskName "MT5-TradeBot-Scanner" -Action $Action2 -Trigger $Trigger2 -Settings $Common -User $User -Force

# 3. Trading cycle every minute while the daily loop is active
$Action3 = New-ScheduledTaskAction -Execute "$BotPath\scheduler\run_trading_cycle.bat"
$Trigger3 = New-ScheduledTaskTrigger -Daily -At "08:00"
Register-ScheduledTask -TaskName "MT5-TradeBot-Cycle" -Action $Action3 -Trigger $Trigger3 -Settings $Common -User $User -Force

# 4. Daily summary after both daylight/standard-time New York closes.
$Action4 = New-ScheduledTaskAction -Execute "$BotPath\scheduler\run_daily_summary.bat"
$Trigger4 = New-ScheduledTaskTrigger -Daily -At "00:30"
Register-ScheduledTask -TaskName "MT5-TradeBot-DailySummary" -Action $Action4 -Trigger $Trigger4 -Settings $Common -User $User -Force

Write-Host "All MT5 TradeBot tasks created successfully!"