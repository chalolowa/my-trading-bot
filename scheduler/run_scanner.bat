@echo off
cd /d "%~dp0.."
if not exist "logs" mkdir "logs"
set "PYTHON=%CD%\.venv\Scripts\python.exe"
if not exist "%PYTHON%" exit /b 1
for /L %%I in (1,1,32) do (
    "%PYTHON%" -c "import asyncio; from src.scanner import scanner; results = asyncio.run(scanner.scan_all()); print(results)" >> "%~dp0..\logs\scanner.log" 2>&1
    timeout /t 1800 /nobreak >nul
)