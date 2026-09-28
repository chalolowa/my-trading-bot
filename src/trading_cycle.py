"""Autonomous MT5 trading cycle."""
import asyncio
import logging
from datetime import datetime, timezone

from config.mt5_client import mt5_client, normalize_symbol
from config.settings import settings
from src.logger import _console
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
        _console("MT5 trading bot loop started", level=logging.INFO)
        await telegram.send_message_async("MT5 trading bot started.")
        while self.running:
            try:
                await self._tick()
            except Exception as exc:
                _console(f"Trading cycle tick error: {exc}", level=logging.ERROR)
                await telegram.send_message_async(f"Cycle error: {exc}")
            await asyncio.sleep(60)

    async def _tick(self) -> None:
        if market_hours.is_close_time():
            _console("Close window active — flattening all positions", level=logging.INFO)
            await self._close_all_positions("market_close")
            return

        can_open = market_hours.can_open_new_trade()
        if not can_open.get("allowed", False):
            _console(f"Trading cycle: {can_open.get('reason', 'Market closed or cutoff active')}", level=logging.INFO)
            return

        # Daily loss limit check (B21)
        max_daily_loss_pct = strategy_engine.config.get("risk_management", {}).get("max_daily_loss_pct", 3.0)
        daily_summary = risk_manager.get_daily_summary()
        daily_pnl = float(daily_summary.get("daily_pnl", 0.0))
        try:
            account = mt5_client.get_account_info()
            balance = float(getattr(account, "balance", 0.0) or 0.0)
        except Exception as exc:
            _console(f"Unable to fetch account info for loss limit check: {exc}", level=logging.WARNING)
            balance = 0.0

        if balance > 0:
            max_loss_amount = balance * (max_daily_loss_pct / 100.0)
            if daily_pnl < -max_loss_amount:
                _console(
                    f"Daily loss limit reached (Realized PnL=${daily_pnl:.2f} <= -${max_loss_amount:.2f}, limit={max_daily_loss_pct}%). "
                    f"Skipping all new entries for the rest of the session.",
                    level=logging.WARNING
                )
                return

        # Scan cache evaluation across fresh process invocations (A4, B8)
        cached_scan = scanner.get_cached_scan(max_age_seconds=1800)
        now_utc = datetime.now(timezone.utc)
        last_scan = self.last_scan_time
        if last_scan is not None and last_scan.tzinfo is None:
            last_scan = last_scan.replace(tzinfo=timezone.utc)

        if self.scan_results and (last_scan is None or (now_utc - last_scan).total_seconds() < 1800):
            ts_str = last_scan.isoformat() if last_scan else now_utc.isoformat()
            _console(f"Reusing in-memory scan from {ts_str}, {len(self.scan_results)} results", level=logging.INFO)
        elif cached_scan:
            cached_ts, cached_results = cached_scan
            self.last_scan_time = cached_ts
            self.scan_results = cached_results
            _console(f"Reusing cached scan from {cached_ts.isoformat()}, {len(self.scan_results)} results", level=logging.INFO)
        else:
            _console("Running fresh scan", level=logging.INFO)
            self.scan_results = await scanner.scan_all()
            self.last_scan_time = now_utc

        positions = mt5_client.get_open_positions()
        max_open = strategy_engine.config.get("risk_management", {}).get("max_open_trades", settings.MAX_OPEN_TRADES)
        if len(positions) >= max_open:
            _console(f"Open position count {len(positions)} >= limit {max_open}", level=logging.INFO)
            return

        open_symbols = {normalize_symbol(position.symbol) for position in positions}
        for opportunity in self.scan_results[:3]:
            try:
                current_positions = mt5_client.get_open_positions()
                if len(current_positions) >= max_open:
                    _console(f"Position limit reached during scan evaluation ({len(current_positions)} >= {max_open})", level=logging.INFO)
                    break
                symbol = normalize_symbol(opportunity["instrument"])
                if symbol not in open_symbols:
                    await self._evaluate_and_trade(symbol)
                    open_symbols.add(symbol)
            except Exception as exc:
                _console(f"Error evaluating candidate {opportunity.get('instrument')}: {exc}", level=logging.ERROR)
                continue

    async def _evaluate_and_trade(self, symbol: str) -> None:
        timeframe = strategy_engine.config.get("timeframe", "M15")
        candle_count = strategy_engine.config.get("candle_count", 200)

        df = mt5_client.copy_rates_from_pos(symbol, timeframe, 1, candle_count)
        if df.empty or len(df) < 5:
            _console(f"Insufficient candle data for {symbol} (count={len(df)})", level=logging.INFO)
            return

        latest = strategy_engine.ta.analyze_instrument(df, strategy_engine.config).iloc[-1]
        direction = "long" if latest.get("SMA_9", 0) > latest.get("SMA_21", 0) else "short"

        # A10 logging: Symbol, guessed direction, timeframe, candle count fetched
        _console(
            f"Evaluating {symbol}: guessed_direction={direction}, timeframe={timeframe}, candles={len(df)}",
            level=logging.INFO
        )

        should_enter, metadata = strategy_engine.evaluate_entry(df, direction)
        risk_manager.record_event(
            "trade_decision",
            {
                "instrument": symbol,
                "direction": direction,
                "should_enter": should_enter,
                "timeframe": timeframe,
                "candle_count": len(df),
                "strategy": strategy_engine.config.get("name"),
            },
        )
        if not should_enter:
            return

        tick = mt5_client.get_tick(symbol)
        entry_price = float(tick.ask if direction == "long" else tick.bid)
        atr_val = metadata.get("atr")
        enriched_df = metadata.get("enriched_df", df)
        stop_loss = strategy_engine.calculate_stop_loss(entry_price, direction, df=enriched_df, atr=atr_val)
        take_profit = strategy_engine.calculate_take_profit(entry_price, stop_loss, direction)

        risk = abs(entry_price - stop_loss)
        min_rr = strategy_engine.config.get("risk_management", {}).get("min_risk_reward", 1.5)
        reward = abs(take_profit - entry_price)
        rr_ratio = (reward / risk) if risk > 0 else 0.0

        if risk == 0 or rr_ratio < min_rr:
            # A13 logging: Risk/reward check fails (< min_risk_reward)
            _console(
                f"Risk/reward check failed for {symbol}: computed R:R={rr_ratio:.2f} < min_risk_reward={min_rr:.2f}",
                level=logging.INFO
            )
            return

        account = mt5_client.get_account_info()
        balance = float(account.balance)
        units = risk_manager.calculate_position_size(
            balance, entry_price, stop_loss, symbol
        )

        if abs(units) <= 0:
            max_units = int((balance / entry_price) * 50) if entry_price > 0 else 0
            # A14 logging: units <= 0 after calculate_position_size()
            _console(
                f"Position sizing rejected (units={units} <= 0) for {symbol}: "
                f"balance={balance:.2f}, entry={entry_price:.5f}, stop_dist={risk:.5f}, "
                f"computed units={units}, max_units={max_units}",
                level=logging.WARNING
            )
            return

        volume = mt5_client.units_to_lots(symbol, abs(units))
        if volume <= 0.0:
            _console(
                f"Normalized lot volume is 0.0 for {symbol} (units={abs(units)}): order aborted",
                level=logging.WARNING
            )
            return

        # A16 logging: Immediately before place_market_order()
        _console(
            f"Placing market order: symbol={symbol}, direction={direction}, "
            f"volume={volume:.2f}, SL={stop_loss:.5f}, TP={take_profit:.5f}",
            level=logging.INFO
        )

        result = mt5_client.place_market_order(symbol, direction, volume, stop_loss, take_profit)
        ticket = int(result.order)
        # B19: Use actual fill price from result
        actual_fill_price = float(getattr(result, "price", 0.0) or entry_price)

        risk_manager.record_entry(Trade(
            trade_id=ticket, instrument=symbol, direction=direction,
            entry_price=actual_fill_price, stop_loss=stop_loss, take_profit=take_profit,
            position_size=volume, entry_time=datetime.now(timezone.utc),
        ))

        # A18 logging: Order placed successfully
        _console(
            f"Order placed successfully: ticket={ticket}, fill_price={actual_fill_price:.5f}, "
            f"SL={stop_loss:.5f}, TP={take_profit:.5f}",
            level=logging.INFO
        )

        await telegram.send_message_async(
            f"{'BUY' if direction == 'long' else 'SELL'} {symbol} "
            f"ticket={ticket} entry={actual_fill_price:.5f} SL={stop_loss:.5f} TP={take_profit:.5f}"
        )

    async def _close_all_positions(self, reason: str) -> None:
        for position in mt5_client.get_bot_positions():
            ticket = int(position.ticket)
            try:
                result = mt5_client.close_position(ticket)
                exit_price = float(getattr(result, "price", 0) or 0)
                risk_manager.record_exit(ticket, exit_price, float(position.profit), reason)
                _console(f"Closed position ticket={ticket} (reason={reason}, exit_price={exit_price:.5f})", level=logging.INFO)
            except Exception as exc:
                _console(f"Close failed for ticket {ticket}: {exc}", level=logging.ERROR)
                await telegram.send_message_async(f"Close failed for ticket {ticket}: {exc}")

    async def send_daily_summary(self) -> None:
        try:
            account = mt5_client.get_account_info()
            balance = float(account.balance)
        except Exception:
            balance = 0.0
        stats = risk_manager.get_r_multiple_stats()
        summary_text = (
            f"Daily summary: balance={balance:.2f}, "
            f"trades={stats['total_trades']}, total R={stats['total_r']:+.2f}, "
            f"win_rate={stats['win_rate']}%, max_drawdown={stats['max_r_drawdown']}R"
        )
        _console(summary_text, level=logging.INFO)
        await telegram.send_daily_summary_async(summary_text)
        risk_manager.save_daily_summary()

    def stop(self) -> None:
        self.running = False
        _console("MT5 trading bot loop stopped", level=logging.INFO)
        telegram.send_message("MT5 trading bot stopped.")


trading_cycle = TradingCycle()
