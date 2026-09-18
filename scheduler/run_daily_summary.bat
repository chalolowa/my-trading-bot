@echo off
cd /d "C:\mt5_tradebot"
call venv\Scripts\activate.bat
python -c "import asyncio; from src.trading_cycle import trading_cycle; asyncio.run(trading_cycle.send_daily_summary())"`