@echo off
cd /d "%~dp0.."
set "PYTHON=%CD%\.venv\Scripts\python.exe"
if not exist "%PYTHON%" exit /b 1
for /L %%I in (1,1,32) do (
    "%PYTHON%" -c "import asyncio; from src.scanner import scanner; results = asyncio.run(scanner.scan_all()); print(results)"
    timeout /t 1800 /nobreak >nul
)