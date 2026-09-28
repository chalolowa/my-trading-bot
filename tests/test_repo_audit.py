"""Offline audit: temporary SQLite only; no broker or Telegram calls.

Tests describe required behavior. Failures expose defects in the unchanged app.
Run from the repository root: python -m pytest tests/test_repo_audit.py -q
"""
import asyncio
import os
import sqlite3
import tempfile
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest

# Set before singleton imports so tests cannot write to the real journal.
_temporary = tempfile.TemporaryDirectory(prefix="tradebot-audit-")
os.environ["DB_PATH"] = str(Path(_temporary.name) / "trades.db")
os.environ["TELEGRAM_BOT_TOKEN"] = ""
os.environ["TELEGRAM_CHAT_ID"] = ""
os.environ["MONGO_URI"] = ""

from config.settings import Settings, settings
from src.event_outbox import EventOutbox, MongoEventSynchronizer
from src.market_hours import MarketHours
from src.risk_manager import RiskManager, Trade


def test_sqlite_journal_round_trip(tmp_path):
    manager = RiskManager(str(tmp_path / "journal.db"))
    manager.record_entry(Trade(123, "EURUSD", "long", 1.1,
                              stop_loss=1.09, take_profit=1.12,
                              position_size=0.01, entry_time=datetime.now()))
    assert len(manager.get_open_trades()) == 1
    assert manager.record_exit(123, 1.12, 20, "audit") == pytest.approx(2)
    assert manager.get_open_trades() == []
    assert manager.get_closed_trades()[0].pnl == 20
    with sqlite3.connect(manager.db_path) as connection:
        assert connection.execute("PRAGMA integrity_check").fetchone()[0] == "ok"


def test_sqlite_filename_without_directory(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    RiskManager("journal.db")


def test_outbox_persistence_retry_and_acknowledgement(tmp_path, monkeypatch):
    path = str(tmp_path / "outbox.db")
    outbox = EventOutbox(path)
    event_id = outbox.append("audit", {"value": 1}, 123)
    assert EventOutbox(path).pending_count() == 1
    collection = SimpleNamespace()
    documents = {}

    def replace(query, document, upsert):
        assert upsert
        documents[query["_id"]] = document
        return SimpleNamespace(acknowledged=True)

    collection.replace_one = replace
    from unittest.mock import MagicMock
    client = MagicMock()
    client.__getitem__.return_value.__getitem__.return_value = collection
    monkeypatch.setattr(settings, "MONGO_URI", "mongodb://localhost")
    synchronizer = MongoEventSynchronizer(outbox)
    with patch("pymongo.MongoClient", return_value=client):
        with patch.object(collection, "replace_one", side_effect=RuntimeError("offline")):
            assert synchronizer.sync_once() == 0
        assert outbox.pending(1)[0]["attempts"] == 1
        assert synchronizer.sync_once() == 1
        assert synchronizer.sync_once() == 0
        synchronizer.close()
    assert outbox.pending_count() == 0
    assert documents[event_id]["payload"] == {"value": 1}
    assert client.close.called


def test_mongodb_environment_alias(monkeypatch):
    monkeypatch.delenv("MONGO_URI")
    monkeypatch.setenv("MONGODB_URI", "mongodb://localhost:27017")
    assert Settings(_env_file=None).MONGO_URI == "mongodb://localhost:27017"


@pytest.mark.parametrize("day,allowed", [(18, True), (19, False), (20, False)])
def test_market_weekday_gate(day, allowed):
    hours = MarketHours()
    now = hours.est.localize(datetime(2026, 9, day, 10))
    with patch.object(hours, "now_est", return_value=now):
        assert hours.can_open_new_trade()["allowed"] is allowed


@pytest.mark.parametrize(
    "when,market_open",
    [
        (datetime(2026, 9, 20, 16, 59), False),  # Sunday before weekly open
        (datetime(2026, 9, 20, 17, 0), True),  # Sunday weekly open
        (datetime(2026, 9, 21, 2, 0), True),  # Overnight weekday session
        (datetime(2026, 9, 25, 16, 59), True),  # Friday before weekly close
        (datetime(2026, 9, 25, 17, 0), False),  # Friday weekly close
        (datetime(2026, 9, 26, 12, 0), False),  # Saturday
    ],
)
def test_forex_weekly_session_boundaries(when, market_open):
    hours = MarketHours()
    now = hours.est.localize(when)
    with patch.object(hours, "now_est", return_value=now):
        assert hours.is_market_open() is market_open


def test_sunday_trade_cutoff_uses_next_weekday_flattening_time():
    hours = MarketHours()
    sunday_evening = hours.est.localize(datetime(2026, 9, 20, 18, 0))
    with patch.object(hours, "now_est", return_value=sunday_evening):
        assert hours.can_open_new_trade()["allowed"] is True
        assert hours.time_until_new_trade_cutoff() == timedelta(hours=21, minutes=30)

    friday_after_daily_cutoff = hours.est.localize(datetime(2026, 9, 25, 15, 31))
    with patch.object(hours, "now_est", return_value=friday_after_daily_cutoff):
        assert hours.can_open_new_trade()["allowed"] is False


def test_dashboard_with_closed_trade():
    from dashboard.multiple_tracker import generate_r_multiple_chart
    trade = Trade(1, "EURUSD", "long", 1.1, exit_price=1.12,
                  exit_time=datetime.now(), r_multiple=2)
    with patch("dashboard.multiple_tracker.risk_manager.get_closed_trades", return_value=[trade]):
        assert generate_r_multiple_chart()["data"]


def test_dashboard_with_open_trade():
    from dashboard.multiple_tracker import api_stats
    with patch("dashboard.multiple_tracker.risk_manager.get_open_trades", return_value=[{"trade_id": 1}]):
        assert asyncio.run(api_stats())["open_trades"] == [{"trade_id": 1}]


def test_statistics_respect_requested_days(tmp_path):
    manager = RiskManager(str(tmp_path / "history.db"))
    manager.record_entry(Trade(456, "EURUSD", "long", 1.1,
                              stop_loss=1.09, entry_time=datetime(2020, 1, 1)))
    manager.record_exit(456, 1.12, 20, "audit")
    with sqlite3.connect(manager.db_path) as connection:
        connection.execute("UPDATE trades SET exit_time = '2020-01-01T12:00:00'")
    assert manager.get_r_multiple_stats(days=1)["total_trades"] == 0


def test_cycle_respects_remaining_position_slots(monkeypatch):
    from unittest.mock import AsyncMock

    from src.trading_cycle import TradingCycle
    cycle = TradingCycle()
    cycle.last_scan_time = datetime.now()
    cycle.scan_results = [{"instrument": symbol} for symbol in ["EURUSD", "GBPUSD", "AUDUSD"]]
    positions = [SimpleNamespace(symbol=s) for s in ["USDJPY", "USDCAD"]]

    async def enter(symbol):
        positions.append(SimpleNamespace(symbol=symbol))

    monkeypatch.setattr(settings, "MAX_OPEN_TRADES", 3)
    with patch("src.trading_cycle.market_hours.is_close_time", return_value=False), \
         patch("src.trading_cycle.market_hours.can_open_new_trade", return_value={"allowed": True}), \
         patch("src.trading_cycle.mt5_client.get_open_positions", side_effect=lambda: list(positions)), \
         patch.object(cycle, "_evaluate_and_trade", new=AsyncMock(side_effect=enter)):
        asyncio.run(cycle._tick())
    assert len(positions) <= settings.MAX_OPEN_TRADES


def test_cycle_outside_hours_never_contacts_broker():
    from src.trading_cycle import TradingCycle
    with patch("src.trading_cycle.market_hours.is_close_time", return_value=False), \
         patch("src.trading_cycle.market_hours.can_open_new_trade", return_value={"allowed": False}), \
         patch("src.trading_cycle.mt5_client.get_open_positions") as broker:
        asyncio.run(TradingCycle()._tick())
        broker.assert_not_called()


def test_cycle_force_close_path():
    from unittest.mock import AsyncMock

    from src.trading_cycle import TradingCycle
    cycle = TradingCycle()
    with patch("src.trading_cycle.market_hours.is_close_time", return_value=True), \
         patch.object(cycle, "_close_all_positions", new_callable=AsyncMock) as close:
        asyncio.run(cycle._tick())
        close.assert_awaited_once_with("market_close")
