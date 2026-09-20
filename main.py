"""FastAPI entry point for the MT5 trading bot."""
from contextlib import asynccontextmanager
from datetime import datetime
from pathlib import Path
from typing import Optional

from fastapi import BackgroundTasks, FastAPI, HTTPException
from pydantic import BaseModel, Field
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles

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
    print(f"MT5 TradeBot starting on {settings.API_HOST}:{settings.API_PORT}", flush=True)
    try:
        mt5_client.connect()
        await mongo_synchronizer.start()
        await telegram.send_message_async(
            f"MT5 TradeBot started on {settings.API_HOST}:{settings.API_PORT}"
        )
        yield
    finally:
        trading_cycle.stop()
        await mongo_synchronizer.stop()
        mt5_client.shutdown()
        await telegram.send_message_async("MT5 TradeBot stopped.")


app = FastAPI(title="MT5 TradeBot API", version="3.0.0", lifespan=lifespan)
app.mount(
    "/assets",
    StaticFiles(directory=Path(__file__).resolve().parent / "assets"),
    name="assets",
)
app.include_router(dashboard_router)


@app.get("/", response_class=HTMLResponse)
async def root():
    return """
    <!DOCTYPE html>
    <html lang="en">
    <head>
        <meta charset="UTF-8">
        <meta name="viewport" content="width=device-width, initial-scale=1.0">
        <title>MT5 TradeBot</title>
        <style>
            :root {
                color-scheme: dark;
                font-family: "Segoe UI", Arial, sans-serif;
                background: #07111f;
                color: #e5eefb;
            }
            * { box-sizing: border-box; }
            body {
                margin: 0;
                min-height: 100vh;
                display: grid;
                place-items: center;
                background:
                    radial-gradient(circle at 15% 20%, #12375a 0, transparent 35%),
                    radial-gradient(circle at 85% 80%, #173c37 0, transparent 35%),
                    #07111f;
            }
            .card {
                width: min(920px, calc(100% - 32px));
                display: grid;
                grid-template-columns: 1fr 1fr;
                overflow: hidden;
                border: 1px solid #29415d;
                border-radius: 24px;
                background: rgba(14, 29, 48, 0.9);
                box-shadow: 0 24px 70px rgba(0, 0, 0, 0.35);
            }
            .content { padding: clamp(32px, 6vw, 72px); }
            .eyebrow {
                margin: 0 0 14px;
                color: #57d7ff;
                font-size: 0.8rem;
                font-weight: 700;
                letter-spacing: 0.16em;
                text-transform: uppercase;
            }
            h1 { margin: 0 0 18px; font-size: clamp(2.2rem, 5vw, 4rem); line-height: 1; }
            p { color: #a9bad0; line-height: 1.7; }
            .button {
                display: inline-block;
                margin-top: 18px;
                padding: 13px 22px;
                border-radius: 10px;
                background: #38bdf8;
                color: #062033;
                font-weight: 700;
                text-decoration: none;
                transition: transform 0.2s, background 0.2s;
            }
            .button:hover { transform: translateY(-2px); background: #7ddcff; }
            .art { min-height: 360px; background: #0b1b2e; }
            .art img { width: 100%; height: 100%; object-fit: cover; display: block; }
            @media (max-width: 700px) {
                .card { grid-template-columns: 1fr; }
                .art { min-height: 240px; order: -1; }
            }
        </style>
    </head>
    <body>
        <main class="card">
            <section class="content">
                <p class="eyebrow">MT5 TradeBot</p>
                <h1>Trade with clarity.</h1>
                <p>
                    Monitor market activity, review performance, and manage your
                    trading workflow from one focused dashboard.
                </p>
                <a class="button" href="/dashboard/">Open dashboard</a>
            </section>
            <section class="art">
                <img src="/assets/locha%20eng.jpg" alt="MT5 TradeBot">
            </section>
        </main>
    </body>
    </html>
    """


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
