# Run as Administrator on the Windows MT5 host
$BotPath = "C:\mt5_tradebot"
$User = $env:USERNAME
$Python = "$BotPath\venv\Scripts\python.exe"

# 1. MT5 TradeBot API Server (runs continuously)
$Action = New-ScheduledTaskAction -Execute $Python -Argument "-m uvicorn main:app --host 0.0.0.0 --port 8001" -WorkingDirectory $BotPath
$Trigger = New-ScheduledTaskTrigger -AtLogOn
$Settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -StartWhenAvailable
Register-ScheduledTask -TaskName "MT5-TradeBot-Server" -Action $Action -Trigger $Trigger -Settings $Settings -User $User -RunLevel Highest -Force

# 2. Scanner every 30 minutes during market hours
$Action2 = New-ScheduledTaskAction -Execute "$BotPath\scheduler\run_scanner.bat"
$Trigger2 = New-ScheduledTaskTrigger -Once -At "08:00" -RepetitionInterval (New-TimeSpan -Minutes 30) -RepetitionDuration (New-TimeSpan -Hours 13)
Register-ScheduledTask -TaskName "MT5-TradeBot-Scanner" -Action $Action2 -Trigger $Trigger2 -Settings $Settings -User $User -Force

# 3. Trading Cycle every 1 minute during market hours
$Action3 = New-ScheduledTaskAction -Execute "$BotPath\scheduler\run_trading_cycle.bat"
$Trigger3 = New-ScheduledTaskTrigger -Daily -At "08:00" -RepetitionInterval (New-TimeSpan -Minutes 1) -RepetitionDuration (New-TimeSpan -Hours 12)
Register-ScheduledTask -TaskName "MT5-TradeBot-Cycle" -Action $Action3 -Trigger $Trigger3 -Settings $Settings -User $User -Force

# 4. Daily Summary at market close
$Action4 = New-ScheduledTaskAction -Execute "$BotPath\scheduler\run_daily_summary.bat"
$Trigger4 = New-ScheduledTaskTrigger -Daily -At "21:00"
Register-ScheduledTask -TaskName "MT5-TradeBot-DailySummary" -Action $Action4 -Trigger $Trigger4 -Settings $Settings -User $User -Force

Write-Host "All MT5 TradeBot tasks created successfully!"