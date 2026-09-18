@echo off
cd /d "C:\mt5_tradebot"
call venv\Scripts\activate.bat
python -c "import asyncio; from src.scanner import scanner; results = asyncio.run(scanner.scan_all()); print(results)"`