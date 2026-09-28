@echo off
cd /d "%~dp0.."
if not exist "logs" mkdir "logs"
set "PYTHON=%CD%\.venv\Scripts\python.exe"
if not exist "%PYTHON%" exit /b 1
for /L %%I in (1,1,960) do (
    "%PYTHON%" -c "import asyncio; from src.trading_cycle import trading_cycle; asyncio.run(trading_cycle._tick())" >> "%~dp0..\logs\trading_cycle.log" 2>&1
    timeout /t 60 /nobreak >nul
)