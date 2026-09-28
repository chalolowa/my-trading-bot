"""
Risk Management: Position sizing, R-multiple calculation,
and trade journal tracking via SQLite.
"""
import logging
import os
import sqlite3
from collections.abc import Generator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

import numpy as np
import pandas as pd
import pytz

from config.mt5_client import mt5_client
from config.settings import settings
from src.database import create_connection
from src.event_outbox import EventOutbox, event_outbox
from src.logger import _console


@dataclass
class Trade:
    trade_id: int
    instrument: str
    direction: str  # long / short
    entry_price: float
    exit_price: float | None = None
    stop_loss: float = 0.0
    take_profit: float = 0.0
    position_size: float = 0.0  # units
    entry_time: datetime | None = None
    exit_time: datetime | None = None
    pnl: float = 0.0
    status: str = "open"  # open / closed
    r_multiple: float = 0.0
    exit_reason: str = ""

    def calculate_r_multiple(self) -> float:
        """Calculate R multiple for closed trades."""
        if self.exit_price is None or self.stop_loss == 0:
            return 0.0

        risk = abs(self.entry_price - self.stop_loss)
        if risk == 0:
            return 0.0

        if self.direction.lower() in {"long", "buy"}:
            reward = self.exit_price - self.entry_price
        else:
            reward = self.entry_price - self.exit_price

        return reward / risk


class RiskManager:
    def __init__(self, db_path: str | None = None, outbox: EventOutbox | None = None):
        self.db_path = db_path or settings.DB_PATH
        parent = os.path.dirname(self.db_path)
        if parent:
            os.makedirs(parent, exist_ok=True)
        if outbox is not None:
            self.outbox = outbox
        elif db_path is None or db_path == settings.DB_PATH:
            self.outbox = event_outbox
        else:
            self.outbox = EventOutbox(self.db_path)
        self._init_db()

    @contextmanager
    def _connection(self) -> Generator[sqlite3.Connection, None, None]:
        conn = create_connection(self.db_path, timeout=30.0)
        conn.row_factory = sqlite3.Row
        try:
            with conn:
                yield conn
        finally:
            conn.close()

    def _init_db(self):
        """Initialize SQLite trade journal with WAL mode and tables."""
        with self._connection() as conn:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS trades (
                    trade_id TEXT PRIMARY KEY,
                    instrument TEXT,
                    direction TEXT,
                    entry_price REAL,
                    exit_price REAL,
                    stop_loss REAL,
                    take_profit REAL,
                    position_size REAL,
                    entry_time TEXT,
                    exit_time TEXT,
                    pnl REAL,
                    status TEXT,
                    r_multiple REAL,
                    exit_reason TEXT,
                    created_at TEXT DEFAULT CURRENT_TIMESTAMP
                )
            """)

            conn.execute("""
                CREATE TABLE IF NOT EXISTS daily_summary (
                    date TEXT PRIMARY KEY,
                    total_trades INTEGER,
                    winning_trades INTEGER,
                    losing_trades INTEGER,
                    total_r REAL,
                    win_rate REAL,
                    max_r_drawdown REAL,
                    created_at TEXT DEFAULT CURRENT_TIMESTAMP
                )
            """)

    def calculate_position_size(
            self,
            account_balance: float,
            entry_price: float,
            stop_loss: float,
            instrument: str = "EURUSD"
    ) -> int:
        """
        Calculate position size in units based on risk percentage and currency conversion.
        Returns signed integer units (positive for long, negative for short).
        """
        risk_amount = account_balance * (settings.RISK_PER_TRADE_PCT / 100.0)
        stop_distance = abs(entry_price - stop_loss)

        if stop_distance == 0 or account_balance <= 0 or entry_price <= 0:
            _console(
                f"Position sizing rejected for {instrument}: stop_distance={stop_distance}, "
                f"balance={account_balance}, entry={entry_price}",
                level=logging.WARNING
            )
            return 0

        # Try to obtain broker tick size and tick value for currency-adjusted risk
        tick_size = None
        tick_value = None
        contract_size = 100000.0
        try:
            info = mt5_client.get_symbol_info(instrument)
            if info is not None:
                tick_size = getattr(info, "trade_tick_size", None)
                tick_value = getattr(info, "trade_tick_value", None)
                contract_size = float(getattr(info, "trade_contract_size", 100000.0) or 100000.0)
        except Exception:
            pass

        if tick_size and tick_value and tick_size > 0 and tick_value > 0 and contract_size > 0:
            risk_per_lot = (stop_distance / tick_size) * tick_value
            risk_per_unit = risk_per_lot / contract_size
        else:
            # Fallback when MT5 metadata is unavailable (e.g. offline unit testing)
            inst = instrument.replace("_", "").upper()
            if inst.startswith("USD") and not inst.endswith("USD"):
                # e.g. USDJPY, USDCAD: risk is in quote currency (JPY, CAD), convert to base (USD)
                risk_per_unit = stop_distance / entry_price
            else:
                risk_per_unit = stop_distance

        if risk_per_unit <= 0:
            _console(f"Position sizing failed: risk_per_unit <= 0 for {instrument}", level=logging.WARNING)
            return 0

        raw_units = round(risk_amount / risk_per_unit)
        inst = instrument.replace("_", "").upper()
        # Max leverage 50:1. If base currency is USD (e.g. USDJPY), max base units = balance * 50.
        # If quote is USD (e.g. EURUSD), max base units = (balance * 50) / entry_price.
        max_base_units = (account_balance * 50.0) / (entry_price if (not inst.startswith("USD") or inst == "USD") else 1.0)
        max_units = round(max_base_units)
        final_magnitude = min(raw_units, max_units)

        # Signed units: positive for long, negative for short
        is_short = entry_price < stop_loss
        final_units = -final_magnitude if is_short else final_magnitude

        # A15 logging
        _console(
            f"Position sizing for {instrument}: risk_amount={risk_amount:.2f}, "
            f"risk_per_unit={risk_per_unit:.5f}, raw_units={raw_units}, "
            f"max_units={max_units}, final_units={final_units}"
        )

        return final_units

    def record_entry(self, trade: Trade):
        """Record trade entry and outbox event atomically in a single SQLite transaction."""
        entry_iso = trade.entry_time.isoformat() if trade.entry_time else datetime.now(timezone.utc).isoformat()
        try:
            with self._connection() as conn:
                conn.execute("""
                    INSERT OR REPLACE INTO trades
                    (trade_id, instrument, direction, entry_price, stop_loss, take_profit,
                     position_size, entry_time, status, r_multiple)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """, (
                    str(trade.trade_id), trade.instrument, trade.direction,
                    trade.entry_price, trade.stop_loss, trade.take_profit,
                    trade.position_size, entry_iso,
                    trade.status, 0.0
                ))

                self.outbox.append(
                    "trade_entered",
                    {
                        "ticket": trade.trade_id,
                        "instrument": trade.instrument,
                        "direction": trade.direction,
                        "entry_price": trade.entry_price,
                        "stop_loss": trade.stop_loss,
                        "take_profit": trade.take_profit,
                        "position_size": trade.position_size,
                        "entry_time": entry_iso,
                    },
                    aggregate_id=trade.trade_id,
                    connection=conn,
                )
        except sqlite3.Error as exc:
            _console(f"SQLite trade entry failed (ticket={trade.trade_id}): {exc}", level=logging.ERROR)
            raise
        _console(f"SQLite trade entry succeeded (ticket={trade.trade_id})")

    def record_event(self, event_type: str, payload: dict[str, Any],
                     aggregate_id: int | str | None = None) -> str:
        """Append a replayable decision or execution event locally."""
        return self.outbox.append(event_type, payload, aggregate_id)

    def record_exit(
            self,
            trade_id: int,
            exit_price: float,
            pnl: float,
            exit_reason: str = ""
    ) -> float:
        """Record trade exit and outbox event atomically and calculate R-multiple."""
        exit_time = datetime.now(timezone.utc).isoformat()
        r_multiple = 0.0

        try:
            with self._connection() as conn:
                row = conn.execute(
                    "SELECT entry_price, stop_loss, direction FROM trades WHERE trade_id = ?",
                    (str(trade_id),)
                ).fetchone()

                if not row:
                    _console(f"SQLite trade exit skipped: ticket={trade_id} was not found", level=logging.WARNING)
                    return 0.0

                entry_price = float(row[0])
                stop_loss = float(row[1])
                direction = str(row[2]).lower()

                risk = abs(entry_price - stop_loss)
                if risk > 0:
                    if direction in {"long", "buy"}:
                        r_multiple = (exit_price - entry_price) / risk
                    else:
                        r_multiple = (entry_price - exit_price) / risk

                conn.execute("""
                    UPDATE trades
                    SET exit_price = ?,
                        pnl = ?,
                        exit_time = ?,
                        status = 'closed',
                        r_multiple = ?,
                        exit_reason = ?
                    WHERE trade_id = ?
                """, (exit_price, pnl, exit_time, r_multiple, exit_reason, str(trade_id)))

                self.outbox.append(
                    "trade_exited",
                    {
                        "ticket": trade_id,
                        "exit_price": exit_price,
                        "pnl": pnl,
                        "r_multiple": r_multiple,
                        "exit_reason": exit_reason,
                        "exit_time": exit_time,
                    },
                    aggregate_id=trade_id,
                    connection=conn,
                )
        except sqlite3.Error as exc:
            _console(f"SQLite trade exit failed (ticket={trade_id}): {exc}", level=logging.ERROR)
            raise

        _console(f"SQLite trade exit succeeded (ticket={trade_id}, R={r_multiple:+.2f})")
        return r_multiple

    def get_open_trades(self) -> list[dict[str, Any]]:
        with self._connection() as conn:
            rows = conn.execute("SELECT * FROM trades WHERE status = 'open'").fetchall()
            return [dict(row) for row in rows]

    def get_trade_history(self, limit: int = 100) -> pd.DataFrame:
        with self._connection() as conn:
            df = pd.read_sql_query(
                "SELECT * FROM trades WHERE status = 'closed' ORDER BY exit_time DESC LIMIT ?",
                conn,
                params=(limit,)
            )
            return df

    def get_r_multiple_stats(self, days: int = 30) -> dict[str, Any]:
        """Calculate R-multiple statistics for dashboard."""
        with self._connection() as conn:
            df = pd.read_sql_query(
                "SELECT r_multiple, pnl, exit_time FROM trades WHERE status = 'closed' "
                "AND exit_time >= datetime('now', ?)",
                conn,
                params=(f"-{int(days)} days",),
            )

        if df.empty:
            return {
                "total_trades": 0,
                "avg_r": 0,
                "win_rate": 0,
                "avg_win_r": 0,
                "avg_loss_r": 0,
                "max_r": 0,
                "min_r": 0,
                "expectancy": 0,
                "r_distribution": [],
                "total_r": 0,
                "profit_factor": 0,
                "max_r_drawdown": 0,
            }

        wins = df[df["r_multiple"] > 0]
        losses = df[df["r_multiple"] < 0]
        non_breakeven = len(wins) + len(losses)

        # Win rate excluding breakeven trades (or on decided trades)
        win_rate = (len(wins) / non_breakeven * 100) if non_breakeven > 0 else (100.0 if len(wins) > 0 else 0.0)

        # Calculate actual running drawdown on cumulative R
        cum_r = df["r_multiple"].cumsum()
        peak = cum_r.cummax()
        drawdown = peak - cum_r
        max_drawdown = float(drawdown.max()) if not drawdown.empty else 0.0

        loss_pnl_abs = abs(losses["pnl"].sum()) if not losses.empty else 0.0
        profit_factor = round(wins["pnl"].sum() / loss_pnl_abs, 2) if loss_pnl_abs > 0 else (
            round(wins["pnl"].sum(), 2) if not wins.empty else 0.0
        )

        return {
            "total_trades": len(df),
            "avg_r": round(float(df["r_multiple"].mean()), 2),
            "win_rate": round(win_rate, 1),
            "avg_win_r": round(float(wins["r_multiple"].mean()), 2) if len(wins) > 0 else 0,
            "avg_loss_r": round(float(losses["r_multiple"].mean()), 2) if len(losses) > 0 else 0,
            "max_r": round(float(df["r_multiple"].max()), 2),
            "min_r": round(float(df["r_multiple"].min()), 2),
            "expectancy": round(float(df["r_multiple"].mean()), 2),
            "r_distribution": df["r_multiple"].tolist(),
            "total_r": round(float(df["r_multiple"].sum()), 2),
            "profit_factor": profit_factor,
            "max_r_drawdown": round(max_drawdown, 2),
        }

    def save_daily_summary(self):
        """Save end-of-day summary for historical tracking using US/Eastern date."""
        est_tz = pytz.timezone("US/Eastern")
        today = datetime.now(est_tz).strftime("%Y-%m-%d")

        with self._connection() as conn:
            rows = conn.execute("""
                SELECT r_multiple FROM trades
                WHERE status = 'closed' AND date(exit_time) = ?
                ORDER BY exit_time
            """, (today,)).fetchall()

            if not rows:
                conn.execute("""
                    INSERT OR REPLACE INTO daily_summary
                    (date, total_trades, winning_trades, losing_trades, total_r, win_rate, max_r_drawdown)
                    VALUES (?, 0, 0, 0, 0.0, 0.0, 0.0)
                """, (today,))
                return

            r_vals = [float(r[0]) for r in rows]
            total_trades = len(r_vals)
            winning_trades = sum(1 for r in r_vals if r > 0)
            losing_trades = sum(1 for r in r_vals if r < 0)
            total_r = sum(r_vals)
            non_be = winning_trades + losing_trades
            win_rate = (winning_trades / non_be * 100.0) if non_be > 0 else 0.0

            # Running drawdown calculation
            cum = np.cumsum(r_vals)
            peak = np.maximum.accumulate(cum)
            dd = peak - cum
            max_dd = float(np.max(dd)) if len(dd) > 0 else 0.0

            conn.execute("""
                INSERT OR REPLACE INTO daily_summary
                (date, total_trades, winning_trades, losing_trades, total_r, win_rate, max_r_drawdown)
                VALUES (?, ?, ?, ?, ?, ?, ?)
            """, (today, total_trades, winning_trades, losing_trades, round(total_r, 2), round(win_rate, 1), round(max_dd, 2)))

    def get_closed_trades(self, days: int = 30) -> list[Trade]:
        with self._connection() as conn:
            rows = conn.execute(
                "SELECT trade_id, instrument, direction, entry_price, exit_price, stop_loss, "
                "take_profit, position_size, entry_time, exit_time, pnl, status, r_multiple, exit_reason "
                "FROM trades WHERE status = 'closed' "
                "AND exit_time >= datetime('now', ?) ORDER BY exit_time",
                (f"-{int(days)} days",),
            ).fetchall()

        trades = []
        for row in rows:
            entry_t = None
            if row["entry_time"]:
                try:
                    entry_t = datetime.fromisoformat(row["entry_time"])
                except Exception:
                    pass
            exit_t = None
            if row["exit_time"]:
                try:
                    exit_t = datetime.fromisoformat(row["exit_time"])
                except Exception:
                    pass
            trades.append(Trade(
                trade_id=int(row["trade_id"]),
                instrument=row["instrument"],
                direction=row["direction"],
                entry_price=float(row["entry_price"]),
                exit_price=float(row["exit_price"]) if row["exit_price"] is not None else None,
                stop_loss=float(row["stop_loss"]),
                take_profit=float(row["take_profit"]),
                position_size=float(row["position_size"]),
                entry_time=entry_t,
                exit_time=exit_t,
                pnl=float(row["pnl"]) if row["pnl"] is not None else 0.0,
                status=row["status"],
                r_multiple=float(row["r_multiple"]) if row["r_multiple"] is not None else 0.0,
                exit_reason=row["exit_reason"] or "",
            ))
        return trades

    def get_daily_summary(self) -> dict[str, Any]:
        """Get today's realized PnL and trade counts."""
        est_tz = pytz.timezone("US/Eastern")
        today = datetime.now(est_tz).strftime("%Y-%m-%d")
        with self._connection() as conn:
            row = conn.execute(
                "SELECT COALESCE(SUM(pnl), 0), COUNT(*) FROM trades "
                "WHERE status = 'closed' AND date(exit_time) = ?", (today,)
            ).fetchone()
            open_count = conn.execute(
                "SELECT COUNT(*) FROM trades WHERE status = 'open'"
            ).fetchone()[0]

            return {
                "daily_pnl": float(row[0]),
                "closed_trades": int(row[1]),
                "open_trades": int(open_count),
            }


risk_manager = RiskManager()