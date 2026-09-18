"""
Risk Management: Position sizing, R-multiple calculation,
and trade journal tracking via SQLite.
"""
import sqlite3
import os
from datetime import datetime
from typing import Dict, Any, Optional, List
from dataclasses import dataclass
import pandas as pd
from config.settings import settings
from src.event_outbox import event_outbox


@dataclass
class Trade:
    trade_id: int
    instrument: str
    direction: str  # long / short
    entry_price: float
    exit_price: Optional[float] = None
    stop_loss: float = 0.0
    take_profit: float = 0.0
    position_size: float = 0.0  # units
    entry_time: datetime = None
    exit_time: Optional[datetime] = None
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

        if self.direction == "long":
            reward = self.exit_price - self.entry_price
        else:
            reward = self.entry_price - self.exit_price

        return reward / risk


class RiskManager:
    def __init__(self, db_path: str = None):
        self.db_path = db_path or settings.DB_PATH
        os.makedirs(os.path.dirname(self.db_path), exist_ok=True)
        self._init_db()

    def _init_db(self):
        """Initialize SQLite trade journal."""
        conn = sqlite3.connect(self.db_path)
        cursor = conn.cursor()
        cursor.execute("""
                       CREATE TABLE IF NOT EXISTS trades
                       (
                           trade_id
                           TEXT
                           PRIMARY
                           KEY,
                           instrument
                           TEXT,
                           direction
                           TEXT,
                           entry_price
                           REAL,
                           exit_price
                           REAL,
                           stop_loss
                           REAL,
                           take_profit
                           REAL,
                           position_size
                           REAL,
                           entry_time
                           TEXT,
                           exit_time
                           TEXT,
                           pnl
                           REAL,
                           status
                           TEXT,
                           r_multiple
                           REAL,
                           exit_reason
                           TEXT,
                           created_at
                           TEXT
                           DEFAULT
                           CURRENT_TIMESTAMP
                       )
                       """)

        cursor.execute("""
                       CREATE TABLE IF NOT EXISTS daily_summary
                       (
                           date
                           TEXT
                           PRIMARY
                           KEY,
                           total_trades
                           INTEGER,
                           winning_trades
                           INTEGER,
                           losing_trades
                           INTEGER,
                           total_r
                           REAL,
                           win_rate
                           REAL,
                           max_r_drawdown
                           REAL,
                           created_at
                           TEXT
                           DEFAULT
                           CURRENT_TIMESTAMP
                       )
                       """)
        conn.commit()
        conn.close()

    def calculate_position_size(
            self,
            account_balance: float,
            entry_price: float,
            stop_loss: float,
            instrument: str
    ) -> int:
        """
        Calculate position size in units based on 1% risk rule.
        Returns integer units (positive for long, negative for short).
        """
        risk_amount = account_balance * (settings.RISK_PER_TRADE_PCT / 100)
        risk_per_unit = abs(entry_price - stop_loss)

        if risk_per_unit == 0:
            return 0

        # For forex, 1 unit = 1 unit of base currency
        # Simplified: assume USD account and pair is XXX_USD or USD_XXX
        units = int(risk_amount / risk_per_unit)

        # Cap at reasonable limits
        max_units = int(account_balance / entry_price * 50)  # 50:1 leverage cap
        return min(units, max_units)

    def record_entry(self, trade: Trade):
        """Record trade entry in database."""
        conn = sqlite3.connect(self.db_path)
        cursor = conn.cursor()
        cursor.execute("""
            INSERT OR REPLACE INTO trades
            (trade_id, instrument, direction, entry_price, stop_loss, take_profit,
             position_size, entry_time, status, r_multiple)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            trade.trade_id, trade.instrument, trade.direction,
            trade.entry_price, trade.stop_loss, trade.take_profit,
            trade.position_size, trade.entry_time.isoformat(),
            trade.status, 0.0
        ))
        conn.commit()
        conn.close()
        event_outbox.append(
            "trade_entered",
            {
                "ticket": trade.trade_id,
                "instrument": trade.instrument,
                "direction": trade.direction,
                "entry_price": trade.entry_price,
                "stop_loss": trade.stop_loss,
                "take_profit": trade.take_profit,
                "position_size": trade.position_size,
                "entry_time": trade.entry_time.isoformat(),
            },
            aggregate_id=trade.trade_id,
        )

    def record_event(self, event_type: str, payload: Dict[str, Any],
                     aggregate_id: int | str | None = None) -> str:
        """Append a replayable decision or execution event locally."""
        return event_outbox.append(event_type, payload, aggregate_id)

    def record_exit(
            self,
            trade_id: int,
            exit_price: float,
            pnl: float,
            exit_reason: str = ""
    ) -> float:
        """Record trade exit and calculate R-multiple."""
        conn = sqlite3.connect(self.db_path)
        cursor = conn.cursor()

        # Get trade details
        cursor.execute("SELECT * FROM trades WHERE trade_id = ?", (trade_id,))
        row = cursor.fetchone()
        if not row:
            conn.close()
            return 0.0

        # Calculate R multiple
        entry_price = row[3]
        stop_loss = row[5]
        direction = row[2]

        risk = abs(entry_price - stop_loss)
        if risk > 0:
            if direction == "long":
                r_multiple = (exit_price - entry_price) / risk
            else:
                r_multiple = (entry_price - exit_price) / risk
        else:
            r_multiple = 0.0

        exit_time = datetime.now().isoformat()

        cursor.execute("""
                       UPDATE trades
                       SET exit_price  = ?,
                           pnl         = ?,
                           exit_time   = ?,
                           status      = 'closed',
                           r_multiple  = ?,
                           exit_reason = ?
                       WHERE trade_id = ?
                       """, (exit_price, pnl, exit_time, r_multiple, exit_reason, trade_id))

        conn.commit()
        conn.close()
        event_outbox.append(
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
        )

        return r_multiple

    def get_open_trades(self) -> List[Dict[str, Any]]:
        conn = sqlite3.connect(self.db_path)
        df = pd.read_sql_query(
            "SELECT * FROM trades WHERE status = 'open'", conn
        )
        conn.close()
        return df.to_dict('records') if not df.empty else []

    def get_trade_history(self, limit: int = 100) -> pd.DataFrame:
        conn = sqlite3.connect(self.db_path)
        df = pd.read_sql_query(
            f"SELECT * FROM trades WHERE status = 'closed' ORDER BY exit_time DESC LIMIT {limit}",
            conn
        )
        conn.close()
        return df

    def get_r_multiple_stats(self, days: int = 30) -> Dict[str, Any]:
        """Calculate R-multiple statistics for dashboard."""
        conn = sqlite3.connect(self.db_path)
        df = pd.read_sql_query(
            "SELECT r_multiple, pnl FROM trades WHERE status = 'closed'", conn
        )
        conn.close()

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
                "r_distribution": []
                ,"total_r": 0, "profit_factor": 0
            }

        wins = df[df["r_multiple"] > 0]
        losses = df[df["r_multiple"] <= 0]

        win_rate = len(wins) / len(df) * 100 if len(df) > 0 else 0

        return {
            "total_trades": len(df),
            "avg_r": round(df["r_multiple"].mean(), 2),
            "win_rate": round(win_rate, 1),
            "avg_win_r": round(wins["r_multiple"].mean(), 2) if len(wins) > 0 else 0,
            "avg_loss_r": round(losses["r_multiple"].mean(), 2) if len(losses) > 0 else 0,
            "max_r": round(df["r_multiple"].max(), 2),
            "min_r": round(df["r_multiple"].min(), 2),
            "expectancy": round(df["r_multiple"].mean(), 2),
            "r_distribution": df["r_multiple"].tolist(),
            "total_r": round(df["r_multiple"].sum(), 2),
            "profit_factor": round(
                wins["pnl"].sum() / abs(losses["pnl"].sum()), 2
            ) if not losses.empty and losses["pnl"].sum() else 0,
        }

    def save_daily_summary(self):
        """Save end-of-day summary for historical tracking."""
        stats = self.get_r_multiple_stats()
        today = datetime.now().strftime("%Y-%m-%d")

        conn = sqlite3.connect(self.db_path)
        cursor = conn.cursor()

        # Count today's trades
        cursor.execute("""
                       SELECT COUNT(*),
                              SUM(CASE WHEN r_multiple > 0 THEN 1 ELSE 0 END),
                              SUM(CASE WHEN r_multiple <= 0 THEN 1 ELSE 0 END),
                              SUM(r_multiple)
                       FROM trades
                       WHERE status = 'closed' AND date (exit_time) = date ('now')
                       """)
        row = cursor.fetchone()

        cursor.execute("""
            INSERT OR REPLACE INTO daily_summary
            (date, total_trades, winning_trades, losing_trades, total_r, win_rate, max_r_drawdown)
            VALUES (?, ?, ?, ?, ?, ?, ?)
        """, (
            today, row[0] or 0, row[1] or 0, row[2] or 0,
            row[3] or 0, stats["win_rate"], stats["min_r"]
        ))

        conn.commit()
        conn.close()

    def get_closed_trades(self, days: int = 30) -> List[Trade]:
        conn = sqlite3.connect(self.db_path)
        rows = conn.execute(
            "SELECT * FROM trades WHERE status = 'closed' "
            "AND exit_time >= datetime('now', ?) ORDER BY exit_time",
            (f"-{int(days)} days",),
        ).fetchall()
        conn.close()
        return [Trade(
            trade_id=int(row[0]), instrument=row[1], direction=row[2],
            entry_price=row[3], exit_price=row[4], stop_loss=row[5],
            take_profit=row[6], position_size=row[7],
            entry_time=datetime.fromisoformat(row[8]) if row[8] else None,
            exit_time=datetime.fromisoformat(row[9]) if row[9] else None,
            pnl=row[10] or 0.0, status=row[11], r_multiple=row[12] or 0.0,
            exit_reason=row[13] or "",
        ) for row in rows]

    def get_daily_summary(self) -> Dict[str, Any]:
        today = datetime.now().strftime("%Y-%m-%d")
        conn = sqlite3.connect(self.db_path)
        row = conn.execute(
            "SELECT COALESCE(SUM(pnl), 0), COUNT(*) FROM trades "
            "WHERE status = 'closed' AND date(exit_time) = ?", (today,)
        ).fetchone()
        open_count = conn.execute(
            "SELECT COUNT(*) FROM trades WHERE status = 'open'"
        ).fetchone()[0]
        conn.close()
        return {"daily_pnl": row[0], "closed_trades": row[1],
                "open_trades": open_count}


risk_manager = RiskManager()