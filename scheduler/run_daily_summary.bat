@echo off
cd /d "%~dp0.."
if not exist "logs" mkdir "logs"
set "PYTHON=%CD%\.venv\Scripts\python.exe"
if not exist "%PYTHON%" exit /b 1
"%PYTHON%" -c "import asyncio; from src.trading_cycle import trading_cycle; asyncio.run(trading_cycle.send_daily_summary())" >> "%~dp0..\logs\daily_summary.log" 2>&1