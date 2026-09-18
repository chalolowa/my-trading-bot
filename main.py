"""FastAPI entry point for the MT5 trading bot."""
from contextlib import asynccontextmanager
from datetime import datetime
from typing import Optional

from fastapi import BackgroundTasks, FastAPI, HTTPException
from pydantic import BaseModel, Field

from config.mt5_client import mt5_client, normalize_symbol
from config.settings import settings
from dashboard import dashboard_router
from src.risk_manager import Trade, risk_manager
from src.scanner import scanner
from src.strategy_engine import strategy_engine
from src.telegram_bot import telegram
from src.trading_cycle import trading_cycle
from src.event_outbox import mongo_synchronizer


class TradeRequest(BaseModel):
    instrument: str
    direction: str = Field(pattern="^(BUY|SELL|LONG|SHORT)$")
    volume: float = Field(gt=0)
    stop_loss: Optional[float] = None
    take_profit: Optional[float] = None


@asynccontextmanager
async def lifespan(app: FastAPI):
    print(f"MT5 TradeBot starting on {settings.API_HOST}:{settings.API_PORT}")
    await mongo_synchronizer.start()
    yield
    trading_cycle.stop()
    await mongo_synchronizer.stop()
    mt5_client.shutdown()


app = FastAPI(title="MT5 TradeBot API", version="3.0.0", lifespan=lifespan)
app.include_router(dashboard_router)


@app.get("/")
async def root():
    return {"message": "MT5 TradeBot API", "dashboard": "/dashboard/", "status": "online"}


@app.get("/api/v1/health")
async def health_check():
    try:
        account = mt5_client.get_account_info()
        positions = mt5_client.get_open_positions()
        return {"status": "healthy", "mt5_connected": True,
                "balance": float(account.balance), "open_positions": len(positions),
                "timestamp": datetime.utcnow().isoformat()}
    except Exception as exc:
        return {"status": "unhealthy", "mt5_connected": False, "error": str(exc),
                "timestamp": datetime.utcnow().isoformat()}


@app.get("/api/v1/account")
async def account_info():
    try:
        account = mt5_client.get_account_info()
        return {"status": "success", "data": {
            "balance": float(account.balance), "equity": float(account.equity),
            "currency": account.currency,
            "open_position_count": len(mt5_client.get_open_positions()),
        }}
    except Exception as exc:
        raise HTTPException(status_code=503, detail=str(exc))


@app.get("/api/v1/positions")
async def get_positions():
    try:
        positions = mt5_client.get_open_positions()
        return {"status": "success", "count": len(positions), "positions": [
            {"ticket": int(p.ticket), "symbol": normalize_symbol(p.symbol),
             "type": int(p.type), "volume": float(p.volume),
             "price_open": float(p.price_open), "profit": float(p.profit)}
            for p in positions
        ]}
    except Exception as exc:
        raise HTTPException(status_code=503, detail=str(exc))


@app.post("/api/v1/scan")
async def run_scanner():
    try:
        results = await scanner.scan_all()
        return {"status": "success", "data": results}
    except Exception as exc:
        raise HTTPException(status_code=503, detail=str(exc))


@app.post("/api/v1/trade/manual")
async def manual_trade(request: TradeRequest):
    try:
        direction = request.direction.lower()
        symbol = normalize_symbol(request.instrument)
        result = mt5_client.place_market_order(
            symbol, direction, request.volume,
            request.stop_loss, request.take_profit, "manual",
        )
        ticket = int(result.order)
        risk_manager.record_event(
            "manual_order_submitted",
            {
                "ticket": ticket,
                "instrument": symbol,
                "direction": direction,
                "volume": request.volume,
                "stop_loss": request.stop_loss,
                "take_profit": request.take_profit,
            },
            aggregate_id=ticket,
        )
        risk_manager.record_entry(Trade(
            trade_id=ticket, instrument=symbol, direction="long" if direction in {"buy", "long"} else "short",
            entry_price=float(result.price), stop_loss=request.stop_loss or 0.0,
            take_profit=request.take_profit or 0.0, position_size=request.volume,
            entry_time=datetime.now(),
        ))
        return {"status": "success", "ticket": ticket, "retcode": int(result.retcode)}
    except Exception as exc:
        raise HTTPException(status_code=503, detail=str(exc))


@app.post("/api/v1/trade/close/{ticket}")
async def close_trade(ticket: int):
    try:
        result = mt5_client.close_position(ticket)
        risk_manager.record_event(
            "manual_close_submitted",
            {"ticket": ticket, "retcode": int(result.retcode)},
            aggregate_id=ticket,
        )
        telegram.send_message(f"Closed MT5 ticket {ticket}")
        return {"status": "success", "ticket": ticket, "retcode": int(result.retcode)}
    except Exception as exc:
        raise HTTPException(status_code=503, detail=str(exc))


@app.post("/api/v1/trade/close-all")
async def close_all_trades():
    await trading_cycle._close_all_positions("manual")
    return {"status": "success"}


@app.post("/api/v1/auto/start")
async def start_autotrading(background_tasks: BackgroundTasks):
    if trading_cycle.running:
        return {"status": "already_running"}
    background_tasks.add_task(trading_cycle.start)
    return {"status": "started"}


@app.post("/api/v1/auto/stop")
async def stop_autotrading():
    trading_cycle.stop()
    return {"status": "stopped"}


@app.get("/api/v1/stats/r-multiple")
async def get_r_stats(days: int = 30):
    return risk_manager.get_r_multiple_stats(days)


@app.get("/api/v1/stats/daily")
async def get_daily_stats():
    return risk_manager.get_daily_summary()


@app.get("/api/v1/strategy/rules")
async def get_strategy_rules():
    return strategy_engine.config


@app.get("/api/v1/historical/{instrument}")
async def get_historical(instrument: str, timeframe: str = "M15", count: int = 100):
    if count < 1 or count > 5000:
        raise HTTPException(status_code=400, detail="count must be between 1 and 5000")
    try:
        df = mt5_client.copy_rates_from_pos(normalize_symbol(instrument), timeframe, 0, count)
        return {"status": "success", "instrument": normalize_symbol(instrument),
                "timeframe": timeframe, "count": len(df),
                "data": df.reset_index().to_dict("records")}
    except Exception as exc:
        raise HTTPException(status_code=503, detail=str(exc))


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("main:app", host=settings.API_HOST, port=settings.API_PORT)
