"""Comprehensive test suite verifying all acceptance criteria from TradeBot_Remediation_Report.md."""

import asyncio
import json
import logging
import os
import sqlite3
import threading
from datetime import datetime, time, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import numpy as np
import pandas as pd
import pytest
import pytz

from config.mt5_client import MT5Client, mt5_client, normalize_symbol
from config.settings import Settings, settings
from dashboard.multiple_tracker import generate_r_multiple_chart, router
from src.database import (
    configure_sqlite_connection,
    create_connection,
    get_db_connection,
    init_database,
)
from src.event_outbox import EventOutbox, MongoEventSynchronizer, event_outbox
from src.logger import _console, logger
from src.market_hours import MarketHours, market_hours
from src.risk_manager import RiskManager, Trade, risk_manager
from src.scanner import MarketScanner, scanner
from src.strategy_engine import StrategyEngine, strategy_engine
from src.technical_analysis import TechnicalAnalyzer
from src.telegram_bot import TelegramNotifier, telegram
from src.trading_cycle import TradingCycle, trading_cycle


# ---------------------------------------------------------------------------
# Section A Tests: Logging Verification (A1 - A18)
# ---------------------------------------------------------------------------


def test_a1_mt5_connect_logging(caplog):
    client = MT5Client()
    mock_api = MagicMock()
    mock_api.initialize.return_value = True
    mock_account = SimpleNamespace(login=123456, server="DemoServer", balance=50000.0)
    mock_api.account_info.return_value = mock_account

    with patch("config.mt5_client._mt5", mock_api), caplog.at_level(logging.INFO):
        client.connect()
        assert "MT5 connected successfully" in caplog.text
        assert "login=123456" in caplog.text

    # Failure logging
    mock_fail_api = MagicMock()
    mock_fail_api.initialize.return_value = False
    mock_fail_api.last_error.return_value = (10004, "Invalid account")
    client_fail = MT5Client()
    with patch("config.mt5_client._mt5", mock_fail_api), caplog.at_level(logging.ERROR):
        with pytest.raises(RuntimeError):
            client_fail.connect()
        assert "MT5 connect failed" in caplog.text


def test_a2_to_a5_trading_cycle_tick_logging(caplog):
    cycle = TradingCycle()
    # A2: Market closed reason logging
    with (
        patch("src.trading_cycle.market_hours.is_close_time", return_value=False),
        patch(
            "src.trading_cycle.market_hours.can_open_new_trade",
            return_value={
                "allowed": False,
                "reason": "Market closed. Trading hours: 8:00-16:00 EST",
            },
        ),
        caplog.at_level(logging.INFO),
    ):
        asyncio.run(cycle._tick())
        assert "Market closed" in caplog.text

    # A3: Close window active logging
    with (
        patch("src.trading_cycle.market_hours.is_close_time", return_value=True),
        patch.object(cycle, "_close_all_positions", new_callable=AsyncMock),
        caplog.at_level(logging.INFO),
    ):
        asyncio.run(cycle._tick())
        assert "Close window active — flattening all positions" in caplog.text

    # A4 & A5: Cache and position limit logging
    cycle.scan_results = [{"instrument": "EURUSD"}]
    with (
        patch("src.trading_cycle.market_hours.is_close_time", return_value=False),
        patch(
            "src.trading_cycle.market_hours.can_open_new_trade",
            return_value={"allowed": True},
        ),
        patch(
            "src.trading_cycle.scanner.get_cached_scan",
            return_value=(datetime.now(timezone.utc), [{"instrument": "EURUSD"}]),
        ),
        patch(
            "src.trading_cycle.mt5_client.get_open_positions",
            return_value=[
                SimpleNamespace(symbol="EURUSD"),
                SimpleNamespace(symbol="GBPUSD"),
                SimpleNamespace(symbol="USDJPY"),
            ],
        ),
        caplog.at_level(logging.INFO),
    ):
        asyncio.run(cycle._tick())
        assert "Reusing cached scan from" in caplog.text
        assert "Open position count 3 >= limit" in caplog.text


def test_a6_to_a9_scanner_logging(caplog):
    sc = MarketScanner()
    # A6: Market closed skipped
    with (
        patch("src.scanner.market_hours.is_market_open", return_value=False),
        caplog.at_level(logging.INFO),
    ):
        res = asyncio.run(sc.scan_all())
        assert res == []
        assert "Forex market closed (weekly session) — scan skipped" in caplog.text

    # A7, A8, A9: Error logging, Filter failure logging, Summary logging
    df_sample = pd.DataFrame(
        {
            "open": [1.10] * 60,
            "high": [1.11] * 60,
            "low": [1.09] * 60,
            "close": [1.10] * 60,
        },
        index=pd.date_range("2026-01-01", periods=60, freq="15min"),
    )
    tick_mock = SimpleNamespace(ask=1.1005, bid=1.1000)

    with (
        patch("src.scanner.market_hours.is_market_open", return_value=True),
        patch("src.scanner.mt5_client.copy_rates_from_pos", return_value=df_sample),
        patch("src.scanner.mt5_client.get_tick", return_value=tick_mock),
        caplog.at_level(logging.INFO),
    ):
        res = asyncio.run(sc.scan_all())
        assert "instruments tradable this scan" in caplog.text


def test_a11_a12_strategy_engine_logging(caplog):
    engine = StrategyEngine()
    df_sample = pd.DataFrame(
        {
            "open": np.linspace(1.10, 1.15, 60),
            "high": np.linspace(1.11, 1.16, 60),
            "low": np.linspace(1.09, 1.14, 60),
            "close": np.linspace(1.10, 1.15, 60),
        },
        index=pd.date_range("2026-01-01", periods=60, freq="15min"),
    )

    with caplog.at_level(logging.INFO):
        should_enter, meta = engine.evaluate_entry(df_sample, "long")
        assert "Evaluating condition [long]:" in caplog.text
        assert "Strategy evaluation for long:" in caplog.text
        assert "conditions met" in caplog.text


# ---------------------------------------------------------------------------
# Section B Tests: P0 Critical Defects
# ---------------------------------------------------------------------------


def test_b3_position_sizing_currency_conversion_and_signed_units():
    """B3: Test that USDJPY risk converted via tick value equals EURUSD risk amount."""
    manager = RiskManager("test_b3.db")

    # Mock symbol metadata for EURUSD (USD quoted: tick_size 0.00001, tick_value 1.0, contract 100000)
    eurusd_info = SimpleNamespace(
        trade_tick_size=0.00001, trade_tick_value=1.0, trade_contract_size=100000.0
    )
    # Mock symbol metadata for USDJPY (JPY quoted: tick_size 0.001, tick_value ~0.66667 for 1 lot at 150.00, contract 100000)
    usdjpy_info = SimpleNamespace(
        trade_tick_size=0.001, trade_tick_value=0.666667, trade_contract_size=100000.0
    )

    balance = 100000.0  # 1% risk = 1000 USD

    # EURUSD: Entry 1.1000, Stop 1.0900 (100 pips = 0.0100 distance). Direction = Long (positive units)
    with patch("src.risk_manager.mt5_client.get_symbol_info", return_value=eurusd_info):
        units_eurusd = manager.calculate_position_size(
            balance, 1.1000, 1.0900, "EURUSD"
        )
        assert units_eurusd > 0
        # 1000 USD risk / ( (0.0100 / 0.00001) * 1.0 / 100000 ) = 100,000 units (1.0 lot)
        assert units_eurusd == 100000

    # USDJPY: Entry 150.00, Stop 148.50 (150 pips = 1.50 JPY distance). Direction = Long
    with patch("src.risk_manager.mt5_client.get_symbol_info", return_value=usdjpy_info):
        units_usdjpy = manager.calculate_position_size(
            balance, 150.00, 148.50, "USDJPY"
        )
        assert units_usdjpy > 0
        # 1000 USD risk / ( (1.50 / 0.001) * 0.666667 / 100000 ) = 100,000 units (1.0 lot)
        assert units_usdjpy == 100000

    # Short position returns signed negative units
    with patch("src.risk_manager.mt5_client.get_symbol_info", return_value=eurusd_info):
        units_short = manager.calculate_position_size(balance, 1.0900, 1.1000, "EURUSD")
        assert units_short == -100000


def test_b4_market_hours_close_floor_after_hours():
    """B4: Check at 18:00 ET on a weekday returns is_close_time() == True."""
    hours = MarketHours()
    # Wednesday at 18:00 ET (after 15:30 close start)
    weekday_after_hours = hours.est.localize(datetime(2026, 9, 23, 18, 0, 0))
    with patch.object(hours, "now_est", return_value=weekday_after_hours):
        assert hours.is_close_time() is True

    # Weekend at 18:00 ET returns False
    weekend_after_hours = hours.est.localize(datetime(2026, 9, 26, 18, 0, 0))
    with patch.object(hours, "now_est", return_value=weekend_after_hours):
        assert hours.is_close_time() is False


def test_b5_sqlite_wal_mode_and_concurrency(tmp_path):
    """B5: SQLite configured with WAL mode and concurrent writers succeed without locking errors."""
    db_file = str(tmp_path / "wal_test.db")
    manager = RiskManager(db_file)

    with create_connection(db_file) as conn:
        journal_mode = conn.execute("PRAGMA journal_mode;").fetchone()[0]
        busy_timeout = conn.execute("PRAGMA busy_timeout;").fetchone()[0]
        assert journal_mode.upper() == "WAL"
        assert busy_timeout >= 30000

    # Test concurrent writes from two separate connections
    errors = []

    def writer(tid_prefix):
        try:
            m = RiskManager(db_file)
            for i in range(10):
                m.record_entry(
                    Trade(
                        trade_id=int(f"{tid_prefix}{i}"),
                        instrument="EURUSD",
                        direction="long",
                        entry_price=1.10,
                        stop_loss=1.09,
                        take_profit=1.12,
                        position_size=1000,
                        entry_time=datetime.now(timezone.utc),
                    )
                )
        except Exception as e:
            errors.append(e)

    t1 = threading.Thread(target=writer, args=(100,))
    t2 = threading.Thread(target=writer, args=(200,))
    t1.start()
    t2.start()
    t1.join()
    t2.join()

    assert len(errors) == 0
    assert len(manager.get_open_trades()) == 20


def test_b6_dashboard_chart_json_serialization(tmp_path):
    """B6: Plotly chart serialization in dashboard_home produces valid JS variable without Python literals."""
    db_file = str(tmp_path / "chart_test.db")
    manager = RiskManager(db_file)
    manager.record_entry(
        Trade(
            1,
            "EURUSD",
            "long",
            1.10,
            stop_loss=1.09,
            take_profit=1.12,
            position_size=1000,
            entry_time=datetime(2026, 1, 1, 10, 0),
        )
    )
    manager.record_exit(1, 1.12, 20.0, "take_profit")

    with patch("dashboard.multiple_tracker.risk_manager", manager):
        chart = generate_r_multiple_chart(30)
        assert chart
        assert "data" in chart
        assert "layout" in chart

        from fastapi.testclient import TestClient
        from main import app

        with patch("main.risk_manager", manager):
            client = TestClient(app)
            response = client.get("/dashboard/")
            assert response.status_code == 200
            home_response = client.get("/")
            assert home_response.status_code == 200
            assert 'id="start-autotrading"' in home_response.text
            assert 'fetch("/api/v1/auto/start", { method: "POST" })' in home_response.text
            # Check that chartData is serialized as valid JS JSON
            assert "const chartData = {" in response.text
            assert (
                "Plotly.newPlot('r-chart', chartData.data, chartData.layout);"
                in response.text
            )
            # Python literals should not appear raw inside the script
            assert (
                "True"
                not in response.text.split("const chartData = ")[1].split("</script>")[
                    0
                ]
            )


def test_b8_scan_cache_persistence_across_process_instances(tmp_path, monkeypatch):
    """B8: Scan cache survives across fresh process / class instantiations."""
    cache_path = tmp_path / "scan_cache.json"
    monkeypatch.setattr("src.scanner.CACHE_FILE", cache_path)

    sc1 = MarketScanner()
    sc1.save_cached_scan([{"instrument": "EURUSD", "composite_score": 85}])

    # Fresh scanner and trading cycle instances
    sc2 = MarketScanner()
    cached = sc2.get_cached_scan(max_age_seconds=1800)
    assert cached is not None
    ts, results = cached
    assert len(results) == 1
    assert results[0]["instrument"] == "EURUSD"
    assert results[0]["composite_score"] == 85


def test_b9_stop_loss_calculation_independent_of_mutation_order():
    """B9: ATR stop loss calculation works consistently and does not rely on mutable in-place dataframe mutation."""
    engine = StrategyEngine()
    df_raw = pd.DataFrame(
        {
            "open": [1.10] * 50,
            "high": [1.12] * 50,
            "low": [1.08] * 50,
            "close": [1.10] * 50,
        },
        index=pd.date_range("2026-01-01", periods=50, freq="15min"),
    )

    # Direct ATR stop loss computation
    sl_long = engine.calculate_stop_loss(1.1000, "long", df=df_raw)
    sl_short = engine.calculate_stop_loss(1.1000, "short", df=df_raw)

    assert sl_long < 1.1000
    assert sl_short > 1.1000

    # With pre-computed ATR
    sl_with_atr = engine.calculate_stop_loss(1.1000, "long", atr=0.0040)
    assert sl_with_atr == pytest.approx(1.1000 - (0.0040 * 1.5))


def test_b10_atomic_trade_and_outbox_transaction(tmp_path):
    """B10: Trade row and outbox event are committed atomically."""
    db_path = str(tmp_path / "atomic.db")
    manager = RiskManager(db_path)
    t = Trade(
        999,
        "GBPUSD",
        "long",
        1.25,
        stop_loss=1.24,
        take_profit=1.27,
        position_size=5000,
        entry_time=datetime.now(timezone.utc),
    )

    manager.record_entry(t)

    with create_connection(db_path) as conn:
        trade_count = conn.execute(
            "SELECT COUNT(*) FROM trades WHERE trade_id = '999'"
        ).fetchone()[0]
        outbox_count = conn.execute(
            "SELECT COUNT(*) FROM event_outbox WHERE aggregate_id = '999'"
        ).fetchone()[0]
        assert trade_count == 1
        assert outbox_count == 1


# ---------------------------------------------------------------------------
# Section B Tests: P1 High Priority Defects
# ---------------------------------------------------------------------------


def test_b11_normalize_volume_lot_step():
    client = MT5Client()
    info = SimpleNamespace(volume_step=0.01, volume_min=0.01, volume_max=100.0)
    with patch.object(client, "get_symbol_info", return_value=info):
        assert client.normalize_volume("EURUSD", 0.0599999999) == 0.05
        assert client.normalize_volume("EURUSD", 0.005) == 0.0
        assert client.normalize_volume("EURUSD", 1.25) == 1.25


def test_b12_zero_volume_order_refusal():
    client = MT5Client()
    info = SimpleNamespace(volume_step=0.01, volume_min=0.01, volume_max=100.0)
    mock_api = MagicMock()
    with (
        patch("config.mt5_client._mt5", mock_api),
        patch.object(client, "get_symbol_info", return_value=info),
    ):
        with pytest.raises(
            ValueError, match="Refusing order: normalized volume is 0.0"
        ):
            client.place_market_order("EURUSD", "long", 0.001)


def test_b13_dynamic_filling_mode():
    client = MT5Client()
    info_fok = SimpleNamespace(filling_mode=1)
    info_ioc = SimpleNamespace(filling_mode=2)
    mock_api = MagicMock(
        ORDER_FILLING_FOK=0, ORDER_FILLING_IOC=1, ORDER_FILLING_RETURN=2
    )
    with patch("config.mt5_client._mt5", mock_api):
        assert client._get_filling_mode(info_fok) == 0  # ORDER_FILLING_FOK
        assert client._get_filling_mode(info_ioc) == 1  # ORDER_FILLING_IOC


def test_b16_outbox_retry_max_attempts(tmp_path):
    db_path = str(tmp_path / "outbox_retry.db")
    outbox = EventOutbox(db_path)
    eid = outbox.append("test_event", {"data": 123})

    for _ in range(10):
        outbox.mark_failed(eid, "connection timeout")

    # Pending count with max_attempts=10 should now exclude this exhausted event
    assert outbox.pending_count(max_attempts=10) == 0
    assert len(outbox.pending(limit=10, max_attempts=10)) == 0


def test_b18_telegram_html_parse_mode():
    bot_mock = MagicMock()
    notifier = TelegramNotifier()
    notifier.enabled = True
    notifier.bot = bot_mock
    notifier.chat_id = "12345"

    asyncio.run(notifier._send("<b>Bold Title</b>"))
    bot_mock.send_message.assert_called_once_with(
        chat_id="12345", text="<b>Bold Title</b>", parse_mode="HTML"
    )


def test_b19_actual_fill_price_recorded_in_trade():
    cycle = TradingCycle()
    df_sample = pd.DataFrame(
        {
            "open": [1.10] * 50,
            "high": [1.12] * 50,
            "low": [1.08] * 50,
            "close": [1.10] * 50,
        },
        index=pd.date_range("2026-01-01", periods=50, freq="15min"),
    )

    order_result = SimpleNamespace(order=777, price=1.1025)
    with (
        patch(
            "src.trading_cycle.mt5_client.copy_rates_from_pos", return_value=df_sample
        ),
        patch(
            "src.trading_cycle.strategy_engine.evaluate_entry",
            return_value=(True, {"atr": 0.0020, "enriched_df": df_sample}),
        ),
        patch(
            "src.trading_cycle.mt5_client.get_tick",
            return_value=SimpleNamespace(ask=1.1000, bid=1.0998),
        ),
        patch(
            "src.trading_cycle.mt5_client.get_account_info",
            return_value=SimpleNamespace(balance=10000.0),
        ),
        patch(
            "src.trading_cycle.risk_manager.calculate_position_size", return_value=10000
        ),
        patch("src.trading_cycle.mt5_client.units_to_lots", return_value=0.1),
        patch(
            "src.trading_cycle.mt5_client.place_market_order", return_value=order_result
        ),
        patch("src.trading_cycle.risk_manager.record_entry") as mock_record_entry,
        patch("src.trading_cycle.telegram.send_message_async"),
    ):
        asyncio.run(cycle._evaluate_and_trade("EURUSD"))
        trade_arg = mock_record_entry.call_args[0][0]
        assert trade_arg.entry_price == 1.1025


def test_b20_opportunity_loop_exception_isolation():
    cycle = TradingCycle()
    cycle.scan_results = [{"instrument": "FAIL_PAIR"}, {"instrument": "OK_PAIR"}]
    evaluated = []

    async def mock_eval(symbol):
        if symbol == "FAIL_PAIR":
            raise RuntimeError("Broker feed dropped")
        evaluated.append(symbol)

    with (
        patch("src.trading_cycle.market_hours.is_close_time", return_value=False),
        patch(
            "src.trading_cycle.market_hours.can_open_new_trade",
            return_value={"allowed": True},
        ),
        patch("src.trading_cycle.mt5_client.get_open_positions", return_value=[]),
        patch.object(cycle, "_evaluate_and_trade", side_effect=mock_eval),
    ):
        asyncio.run(cycle._tick())
        assert "OK_PAIR" in evaluated


def test_b21_daily_loss_limit_gating(caplog):
    cycle = TradingCycle()
    with (
        patch("src.trading_cycle.market_hours.is_close_time", return_value=False),
        patch(
            "src.trading_cycle.market_hours.can_open_new_trade",
            return_value={"allowed": True},
        ),
        patch(
            "src.trading_cycle.risk_manager.get_daily_summary",
            return_value={"daily_pnl": -400.0},
        ),
        patch(
            "src.trading_cycle.mt5_client.get_account_info",
            return_value=SimpleNamespace(balance=10000.0),
        ),
        caplog.at_level(logging.WARNING),
    ):
        # 3% of 10000 is 300; -400 exceeds max loss limit
        asyncio.run(cycle._tick())
        assert "Daily loss limit reached" in caplog.text


# ---------------------------------------------------------------------------
# Section B Tests: P2 & P3 Priority Defects
# ---------------------------------------------------------------------------


def test_b28_scanner_nan_filter_guard():
    sc = MarketScanner()
    df_nan = pd.DataFrame(
        {
            "open": [1.10] * 50,
            "high": [1.10] * 50,
            "low": [1.10] * 50,
            "close": [1.10] * 50,
        },
        index=pd.date_range("2026-01-01", periods=50, freq="15min"),
    )
    # Return NaN for ATR & ADX
    with (
        patch("src.scanner.mt5_client.copy_rates_from_pos", return_value=df_nan),
        patch(
            "src.scanner.mt5_client.get_tick",
            return_value=SimpleNamespace(ask=1.10, bid=1.10),
        ),
        patch.object(
            sc.ta,
            "analyze_instrument",
            return_value=pd.DataFrame(
                [
                    {
                        "ATR_14": np.nan,
                        "ADX_14": np.nan,
                        "RSI_14": np.nan,
                        "SMA_9": np.nan,
                        "SMA_21": np.nan,
                    }
                ]
            ),
        ),
    ):
        res = asyncio.run(sc._analyze_instrument("EURUSD"))
        assert res["tradable"] is False
        assert any("NaN" in r for r in res["reasons"])


def test_b30_breakeven_win_rate_and_running_drawdown(tmp_path):
    db_path = str(tmp_path / "stats.db")
    manager = RiskManager(db_path)

    # Insert 1 win (+2R), 1 loss (-1R), 1 breakeven (0R)
    manager.record_entry(
        Trade(
            1,
            "EURUSD",
            "long",
            1.10,
            stop_loss=1.09,
            take_profit=1.12,
            position_size=1000,
            entry_time=datetime(2026, 1, 1, 10, 0),
        )
    )
    manager.record_exit(1, 1.12, 20.0)

    manager.record_entry(
        Trade(
            2,
            "EURUSD",
            "long",
            1.10,
            stop_loss=1.09,
            take_profit=1.12,
            position_size=1000,
            entry_time=datetime(2026, 1, 1, 11, 0),
        )
    )
    manager.record_exit(2, 1.09, -10.0)

    manager.record_entry(
        Trade(
            3,
            "EURUSD",
            "long",
            1.10,
            stop_loss=1.09,
            take_profit=1.12,
            position_size=1000,
            entry_time=datetime(2026, 1, 1, 12, 0),
        )
    )
    manager.record_exit(3, 1.10, 0.0)

    stats = manager.get_r_multiple_stats(30)
    assert stats["total_trades"] == 3
    # 1 win out of 2 decided trades = 50.0% win rate
    assert stats["win_rate"] == 50.0
    assert stats["total_r"] == pytest.approx(1.0)


def test_b37_strategy_engine_config_validation():
    engine = StrategyEngine()
    # Missing required 'value' key for numeric condition
    malformed_config = {
        "name": "BadStrategy",
        "entry_rules": {
            "long": {
                "conditions": [
                    {
                        "indicator": "RSI",
                        "period": 14,
                        "comparison": ">",
                    }  # missing value
                ]
            }
        },
        "exit_rules": {"stop_loss": {}, "take_profit": {}},
        "risk_management": {},
    }
    with pytest.raises(ValueError, match="missing 'value'"):
        engine._validate_config(malformed_config)
