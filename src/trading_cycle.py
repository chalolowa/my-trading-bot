"""Autonomous MT5 trading cycle."""
import asyncio
from datetime import datetime

from config.mt5_client import mt5_client, normalize_symbol
from config.settings import settings
from src.market_hours import market_hours
from src.risk_manager import Trade, risk_manager
from src.scanner import scanner
from src.strategy_engine import strategy_engine
from src.telegram_bot import telegram


class TradingCycle:
    def __init__(self) -> None:
        self.running = False
        self.last_scan_time: datetime | None = None
        self.scan_results: list[dict] = []

    async def start(self) -> None:
        self.running = True
        telegram.send_message("MT5 trading bot started.")
        while self.running:
            try:
                await self._tick()
            except Exception as exc:
                telegram.send_message(f"Cycle error: {exc}")
            await asyncio.sleep(60)

    async def _tick(self) -> None:
        if market_hours.is_close_time():
            await self._close_all_positions("market_close")
            return
        if not market_hours.can_open_new_trade()["allowed"]:
            return

        now = datetime.now()
        if self.last_scan_time is None or (now - self.last_scan_time).total_seconds() >= 1800:
            self.scan_results = await scanner.scan_all()
            self.last_scan_time = now

        positions = mt5_client.get_open_positions()
        if len(positions) >= settings.MAX_OPEN_TRADES:
            return
        open_symbols = {normalize_symbol(position.symbol) for position in positions}
        for opportunity in self.scan_results[:3]:
            symbol = normalize_symbol(opportunity["instrument"])
            if symbol not in open_symbols:
                await self._evaluate_and_trade(symbol)

    async def _evaluate_and_trade(self, symbol: str) -> None:
        df = mt5_client.copy_rates_from_pos(
            symbol, strategy_engine.config.get("timeframe", "M15"), 0,
            strategy_engine.config.get("candle_count", 200),
        )
        if df.empty:
            return

        latest = strategy_engine.ta.analyze_instrument(df, strategy_engine.config).iloc[-1]
        direction = "long" if latest.get("SMA_9", 0) > latest.get("SMA_21", 0) else "short"
        should_enter, _ = strategy_engine.evaluate_entry(df, direction)
        risk_manager.record_event(
            "trade_decision",
            {
                "instrument": symbol,
                "direction": direction,
                "should_enter": should_enter,
                "timeframe": strategy_engine.config.get("timeframe", "M15"),
                "candle_count": len(df),
                "strategy": strategy_engine.config.get("name"),
            },
        )
        if not should_enter:
            return

        tick = mt5_client.get_tick(symbol)
        entry_price = float(tick.ask if direction == "long" else tick.bid)
        stop_loss = strategy_engine.calculate_stop_loss(entry_price, direction, df)
        take_profit = strategy_engine.calculate_take_profit(entry_price, stop_loss, direction)
        risk = abs(entry_price - stop_loss)
        if risk == 0 or abs(take_profit - entry_price) / risk < strategy_engine.config["risk_management"]["min_risk_reward"]:
            return

        account = mt5_client.get_account_info()
        volume = risk_manager.calculate_position_size(float(account.balance), entry_price, stop_loss, symbol)
        if volume < 1:
            return

        result = mt5_client.place_market_order(symbol, direction, volume, stop_loss, take_profit)
        ticket = int(result.order)
        risk_manager.record_entry(Trade(
            trade_id=ticket, instrument=symbol, direction=direction,
            entry_price=entry_price, stop_loss=stop_loss, take_profit=take_profit,
            position_size=volume, entry_time=datetime.now(),
        ))
        telegram.send_message(
            f"{'BUY' if direction == 'long' else 'SELL'} {symbol} "
            f"ticket={ticket} entry={entry_price:.5f} SL={stop_loss:.5f} TP={take_profit:.5f}"
        )

    async def _close_all_positions(self, reason: str) -> None:
        for position in mt5_client.get_open_positions():
            ticket = int(position.ticket)
            try:
                result = mt5_client.close_position(ticket)
                exit_price = float(getattr(result, "price", 0) or 0)
                risk_manager.record_exit(ticket, exit_price, float(position.profit), reason)
            except Exception as exc:
                telegram.send_message(f"Close failed for ticket {ticket}: {exc}")

    async def send_daily_summary(self) -> None:
        account = mt5_client.get_account_info()
        stats = risk_manager.get_r_multiple_stats()
        telegram.send_message(
            f"Daily summary: balance={float(account.balance):.2f}, "
            f"trades={stats['total_trades']}, total R={stats['total_r']:+.2f}"
        )
        risk_manager.save_daily_summary()

    def stop(self) -> None:
        self.running = False
        telegram.send_message("MT5 trading bot stopped.")


trading_cycle = TradingCycle()
